"""AutoDL 调度层：租约（防互相操作）、统一分配、保险关机、回收器、后台任务。

核心概念
- 租约 lease：acquire 时发放，持有 lease_id 才能对该机器执行命令/传文件/关机；别的窗口拿不到你的机器。
  lease_id 只返回给申请者，list 里只暴露 owner/到期时间。
- 三层保险保证用完关机：① release 立即关机 ② 回收器 reap（MCP 进程内每 2 分钟 + 可选 Windows 计划任务）
  回收过期租约和无主的自启机器 ③ 机器内守护脚本到点自己 shutdown（电脑关机/MCP 挂了也有效）。
"""
from __future__ import annotations

import secrets
import time

import autodl_core as c
import oplog
from autodl_core import AutoDLError, jload, jsave, load_config, state_lock
from oplog import human, ops

LEASES = c.STATE / "leases.json"
MANAGED = c.STATE / "managed.json"   # 由本工具开的机器：{uuid: 开机时间}，无租约时会被回收

GUARD_SCRIPT = r"""set -e
mkdir -p /root/.autodl_jobs
cat > /root/.autodl_guard.sh <<'EOS'
#!/bin/bash
echo $$ > /root/.autodl_guard.pid
trap 'rm -f /root/.autodl_guard.pid' EXIT
while true; do
  sleep 30
  now=$(date +%s); dl=$(cat /root/.autodl_deadline 2>/dev/null || echo 0); hd=$(cat /root/.autodl_hard 2>/dev/null || echo 0)
  [ "$dl" = "off" ] && exit 0
  if [ "$now" -gt "$dl" ]; then
    live=0
    for f in /root/.autodl_jobs/*.pid; do [ -e "$f" ] && tr '\0' ' ' < "/proc/$(cat "$f")/cmdline" 2>/dev/null | grep -q "$(basename "$f" .pid).sh" && live=1; done
    if [ "$live" = 0 ] || [ "$now" -gt "$hd" ]; then shutdown; exit 0; fi
  fi
done
EOS
chmod +x /root/.autodl_guard.sh
# 只认真正的守护进程：关机重开后旧 pid 文件里的数字可能已属于别的进程（后台任务同理，按命令行核对）
gp=$(cat /root/.autodl_guard.pid 2>/dev/null || true)
if ! { [ -n "$gp" ] && tr '\0' ' ' < "/proc/$gp/cmdline" 2>/dev/null | grep -q autodl_guard; }; then
  setsid nohup /root/.autodl_guard.sh >/dev/null 2>&1 < /dev/null &
fi
"""


# ---------------------------------------------------------------- 租约存取
def _load() -> dict:
    return jload(LEASES, {})


def _save(d: dict) -> None:
    jsave(LEASES, d)


def _ttl_seconds(cfg: dict, minutes: float | None) -> float:
    m = minutes if minutes else cfg["lease_ttl_minutes"]
    return max(60.0, min(m * 60, cfg["max_lease_hours"] * 3600))


def _deadline_cmds(rec: dict, cfg: dict) -> str:
    dl = int(rec["expires_at"] + cfg["guard_grace_minutes"] * 60)
    # 任一写入失败（磁盘满、/root 不可写）立即以 97 退出，不再执行后面的守护脚本或用户命令
    return (f"{{ echo {dl} > /root/.autodl_deadline && echo {int(rec['hard_deadline'])} > /root/.autodl_hard; }} "
            f"|| {{ echo '[autodl] 写入保险截止时间失败' >&2; exit 97; }}")


def _mark_managed(uuid: str, on: bool = True) -> None:
    with state_lock("managed"):
        m = jload(MANAGED, {})
        if on:
            m[uuid] = time.time()
        else:
            m.pop(uuid, None)
        jsave(MANAGED, m)


def _get_lease(lid: str, touch: bool = True, ttl_minutes: float | None = None, min_seconds: float = 0) -> dict:
    cfg = load_config()
    with state_lock():
        d = _load()
        rec = d.get(lid)
        if not rec:
            raise AutoDLError("租约不存在或已被回收（机器可能已被关机），请重新 autodl_acquire")
        if time.time() > rec["expires_at"] and rec["state"] == "active":
            raise AutoDLError("租约已过期，机器即将被回收，请重新 autodl_acquire")
        if touch:
            hold = max(_ttl_seconds(cfg, ttl_minutes or rec.get("ttl_min")), min_seconds)  # 长传输/长命令期间至少撑到它结束
            rec["expires_at"] = min(time.time() + hold, rec["hard_deadline"])
            d[lid] = rec
            _save(d)
        return dict(rec)


def _drop_lease(lid: str) -> None:
    with state_lock():
        d = _load()
        d.pop(lid, None)
        _save(d)


def _new_lease(owner: str, purpose: str, uuid: str, alias: str, ttl_min, cloned: bool, state="pending") -> tuple[str, dict]:
    cfg = load_config()
    now = time.time()
    lid = secrets.token_hex(6)
    boot = cfg["boot_timeout_seconds"] + (cfg["data_copy_timeout_seconds"] if cloned else 0) + 120
    rec = {"owner": owner, "purpose": purpose, "uuid": uuid, "alias": alias, "state": state,
           "created": now, "ttl_min": ttl_min or cfg["lease_ttl_minutes"], "cloned": cloned,
           "expires_at": now + boot + _ttl_seconds(cfg, ttl_min),
           "hard_deadline": now + cfg["max_lease_hours"] * 3600}
    d = _load()
    d[lid] = rec
    _save(d)
    return lid, rec


def _match(prefer: str | None, i: dict) -> bool:
    return (not prefer or prefer in (i["uuid"], i.get("name"), i.get("machine_alias"))
            or prefer in (i.get("machine_alias") or ""))


# ---------------------------------------------------------------- 统一分配
def acquire(owner: str, purpose: str = "", prefer: str | None = None, ttl_minutes: float | None = None,
            no_gpu: bool = False, log=lambda m: None) -> dict:
    """给 owner 分一台机器并发租约：空闲卡的关机机 → 开机；没有则每秒刷新最多等 wait_seconds；仍没有则克隆。"""
    if not owner:
        raise AutoDLError("owner 不能为空（写明谁在用，如 'my-project/train-01'）")
    cfg = load_config()
    deadline = time.time() + cfg["wait_seconds"]
    announced = False
    with oplog.context(owner=owner):
        ops("ACQUIRE_START", "OK", purpose=purpose, prefer=prefer, mode="non_gpu" if no_gpu else "gpu")
        while True:
            with state_lock():
                items = c.list_instances()
                leased = {l["uuid"] for l in _load().values()}
                pool = [i for i in items if i["uuid"] not in leased and _match(prefer, i)]
                pick, how = None, None
                if prefer or cfg["adopt_running"]:
                    for i in pool:
                        if i["status"] == "running" and (no_gpu or i["start_mode"] != "non_gpu"):
                            pick, how = i, "adopt"
                            break
                if not pick:
                    cand = sorted([i for i in pool if i["status"] == "shutdown" and (i["gpu_available"] or no_gpu)],
                                  key=lambda i: i["machine_alias"] or "")
                    if cand:
                        pick, how = cand[0], "power_on"
                if pick:
                    lid, rec = _new_lease(owner, purpose, pick["uuid"], pick["machine_alias"], ttl_minutes, False)
                    with oplog.context(lease=lid):
                        try:
                            if how == "power_on":
                                c.power_on(pick["uuid"], no_gpu)
                            _mark_managed(pick["uuid"])
                        except AutoDLError as e:
                            dd = _load()          # 已持有 state 锁，直接改，不能再调 _drop_lease
                            dd.pop(lid, None)
                            _save(dd)
                            ops("ACQUIRE", "ERR", error=str(e))
                            raise
                    break
                if time.time() >= deadline:
                    if not cfg["auto_clone"] or no_gpu:
                        ops("ACQUIRE", "ERR", error="等待超时，未克隆")
                        raise AutoDLError("等待超时，没有可用机器（auto_clone 关闭或无卡模式不克隆）")
                    cands = [i for i in items if _match(prefer, i)] or items
                    if not cands:
                        raise AutoDLError("账号下没有任何实例可克隆")
                    src = cands[0]
                    log("等待超时，克隆同配置机器（含数据盘）")
                    res = c.clone_instance(src["uuid"], cfg["clone_data_disk"])
                    pick, how = {"uuid": res["new_uuid"], "machine_alias": "(克隆中)"}, "clone"
                    lid, rec = _new_lease(owner, purpose, res["new_uuid"], "(克隆中)", ttl_minutes, True)
                    _mark_managed(res["new_uuid"])
                    c.set_machine(res["new_uuid"], cloned_from=src["uuid"], **c.login_overrides(src["uuid"]))
                    with oplog.context(lease=lid):
                        ops("LEASE_CREATE", "OK", instance=res["new_uuid"], via="clone")
                    break
            if not announced:
                log(f"没有空闲 GPU，每 {cfg['poll_seconds']}s 刷新，最多等 {cfg['wait_seconds']}s")
                announced = True
            time.sleep(cfg["poll_seconds"])
        with oplog.context(lease=lid):
            if how != "clone":
                ops("LEASE_CREATE", "OK", instance=f"{pick['machine_alias']}({pick['uuid']})", via=how,
                    ttl_min=rec["ttl_min"])
            log(f"已分配 {pick['machine_alias']}（{how}），租约 {lid}，等待机器 running")
            return _finish_boot(lid, rec, log)


def _finish_boot(lid: str, rec: dict, log=lambda m: None) -> dict:
    """等机器 running，立即装保险；克隆则再等数据盘复制完；把租约转为 active 并刷新保险截止时间。失败则关机并撤销租约。"""
    cfg = load_config()
    try:
        inst = c.wait_status(rec["uuid"], "running", cfg["boot_timeout_seconds"])
        log(f"{inst['machine_alias']} 已 running，安装保险关机" + ("，等待数据盘复制完成" if rec["cloned"] else ""))
        _install_guard(rec["uuid"], rec, cfg)  # 先装守护（截止时间已含复制时长），复制期间本地服务停了也会到点关机
        if rec["cloned"]:
            c.wait_data_copy(rec["uuid"], cfg["data_copy_timeout_seconds"], log=log)
        with state_lock():
            d = _load()
            r = d[lid]
            r.update(state="active", alias=inst["machine_alias"],
                     expires_at=min(time.time() + _ttl_seconds(cfg, r["ttl_min"]), r["hard_deadline"]))
            d[lid] = r
            _save(d)
        _install_guard(rec["uuid"], r, cfg)  # 刷新为 active 后的截止时间
        ops("LEASE_ACTIVE", "OK", instance=f"{inst['machine_alias']}({rec['uuid']})", expires=human(r["expires_at"]))
        return _lease_view(lid, r, inst)
    except Exception as e:
        ops("ACQUIRE", "ERR", error=str(e))
        try:
            _release(lid, power_off=True, reason="启动失败自动回收")
        except Exception:
            pass
        raise


def _lease_view(lid: str, rec: dict, inst: dict) -> dict:
    lg = c.machine_login(inst)
    return {"lease_id": lid, "owner": rec["owner"], "instance": rec["uuid"], "alias": inst["machine_alias"],
            "cloned": rec["cloned"], "region": inst["region_sign"], "H3_SSH": inst.get("ssh"),
            "login": {k: lg[k] for k in ("host", "port", "user", "key", "ssh_command")},
            "expires_at": human(rec["expires_at"]), "hard_deadline": human(rec["hard_deadline"]),
            "note": "用完务必 autodl_release；每次操作自动顺延租约，长任务请用 background=true 并定期 autodl_renew"}


def _install_guard(uuid: str, rec: dict, cfg: dict, attempts: int = 6) -> None:
    """装机内保险关机。刚 running 时 sshd 可能还没就绪，重试几次；始终失败则抛错，由调用方关机回收。"""
    script = _deadline_cmds(rec, cfg) + "\n" + GUARD_SCRIPT
    for n in range(1, attempts + 1):
        r = c.run_ssh(uuid, script, timeout=60,
                      shown=f"# [autodl] 安装保险关机守护 deadline={human(rec['expires_at'] + cfg['guard_grace_minutes'] * 60)}")
        ops("GUARD_INSTALL", "OK" if r["exit"] == 0 else "ERR", instance=uuid, exit=r["exit"], attempt=n)
        if r["exit"] == 0:
            return
        if n < attempts:
            time.sleep(10)
    raise AutoDLError(f"保险关机守护安装失败（ssh exit={r['exit']}）：{r['output'][-300:]}")


# ---------------------------------------------------------------- 租约内操作（都需要 lease_id）
def renew(lease_id: str, ttl_minutes: float | None = None) -> dict:
    cfg = load_config()
    rec = _get_lease(lease_id, True, ttl_minutes)
    with oplog.context(owner=rec["owner"], lease=lease_id):
        r = c.run_ssh(rec["uuid"], _deadline_cmds(rec, cfg) + "\n" + GUARD_SCRIPT, timeout=60,
                      shown=f"# [autodl] 续租，保险关机顺延至 {human(rec['expires_at'] + cfg['guard_grace_minutes'] * 60)}")
        _check_remote(r, "LEASE_RENEW", rec["uuid"])
        ops("LEASE_RENEW", "OK", instance=rec["uuid"], expires=human(rec["expires_at"]))
    return {"lease_id": lease_id, "expires_at": human(rec["expires_at"]), "hard_deadline": human(rec["hard_deadline"])}


def exec_cmd(lease_id: str, command: str, timeout: int = 600, background: bool = False) -> dict:
    """在租约机器上执行命令（经 bash -s）。background=true 用于长任务：立即返回 job_id，用 job_status 查看。"""
    cfg = load_config()
    rec = _get_lease(lease_id, min_seconds=0 if background else timeout + 60)
    with oplog.context(owner=rec["owner"], lease=lease_id):
        refresh = _deadline_cmds(rec, cfg) + "\n"
        if not background:
            r = c.run_ssh(rec["uuid"], refresh + command, timeout=timeout, shown=command)
            ops("EXEC", "OK" if r["exit"] == 0 else "ERR", instance=f"{rec['alias']}({rec['uuid']})",
                exit=r["exit"], cmd=command.replace("\n", " ⏎ "))
            return r
        jid = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        eof = f"EOC_{secrets.token_hex(4)}"
        d = "/root/.autodl_jobs"
        script = (f"{refresh}mkdir -p {d}\ncat > {d}/{jid}.sh <<'{eof}'\n"
                  f"echo $$ > {d}/{jid}.pid\n{command}\necho $? > {d}/{jid}.exit\n{eof}\n"
                  f"setsid nohup bash {d}/{jid}.sh > {d}/{jid}.log 2>&1 < /dev/null &\necho started {jid}\n")
        r = c.run_ssh(rec["uuid"], script, timeout=60, shown=f"# [autodl] 后台任务 {jid}\n{command}")
        ops("JOB_START", "OK" if r["exit"] == 0 else "ERR", instance=f"{rec['alias']}({rec['uuid']})", job=jid,
            cmd=command.replace("\n", " ⏎ "))
        return {"job_id": jid, "exit": r["exit"], "note": "用 autodl_job_status 查看进度；任务运行中机器不会被保险关机（到硬上限为止）"}


def job_status(lease_id: str, job_id: str, tail: int = 40) -> dict:
    cfg = load_config()
    rec = _get_lease(lease_id)
    d = "/root/.autodl_jobs"
    jid = "".join(ch for ch in job_id if ch.isalnum() or ch == "-")
    script = (_deadline_cmds(rec, cfg) + "\n"
              f"cd {d} 2>/dev/null || {{ echo NOJOB; exit 0; }}\n"
              f"if [ -e {jid}.exit ]; then echo \"STATE=done exit=$(cat {jid}.exit)\"; "
              f"elif [ -e {jid}.pid ] && tr '\\0' ' ' < \"/proc/$(cat {jid}.pid)/cmdline\" 2>/dev/null | grep -q {jid}.sh; "
              f"then echo STATE=running; "
              f"else echo STATE=unknown; fi\ntail -n {int(tail)} {jid}.log 2>/dev/null\n")
    with oplog.context(owner=rec["owner"], lease=lease_id):
        r = c.run_ssh(rec["uuid"], script, timeout=60, shown=f"# [autodl] 查看后台任务 {jid}")
        ops("JOB_STATUS", "OK", instance=rec["uuid"], job=jid)
    return r


def _check_remote(r: dict, event: str, uuid: str) -> None:
    """远端改保险截止时间必须成功：ssh 失败（如 exit 255）时报错，别让调用方以为已顺延/已解除。"""
    if r["exit"] != 0:
        ops(event, "ERR", instance=uuid, exit=r["exit"])
        raise AutoDLError(f"机内保险关机未能更新（ssh exit={r['exit']}），请稍后重试：{r['output'][-300:]}")


def _touch_remote(rec: dict, lease_id: str, why: str) -> None:
    cfg = load_config()
    r = c.run_ssh(rec["uuid"], _deadline_cmds(rec, cfg), timeout=60, shown=f"# [autodl] {why}，保险关机顺延至 "
                  f"{human(rec['expires_at'] + cfg['guard_grace_minutes'] * 60)}")
    _check_remote(r, "LEASE_TOUCH", rec["uuid"])


def put_file(lease_id: str, local: str, remote: str) -> dict:
    rec = _get_lease(lease_id, min_seconds=3600 + 60)
    with oplog.context(owner=rec["owner"], lease=lease_id):
        _touch_remote(rec, lease_id, "开始上传")
        r = c.run_scp(rec["uuid"], local, remote, upload=True)
        ops("PUT", "OK" if r["exit"] == 0 else "ERR", instance=rec["uuid"], local=local, remote=remote)
    return r


def get_file(lease_id: str, remote: str, local: str) -> dict:
    rec = _get_lease(lease_id, min_seconds=3600 + 60)
    with oplog.context(owner=rec["owner"], lease=lease_id):
        _touch_remote(rec, lease_id, "开始下载")
        r = c.run_scp(rec["uuid"], local, remote, upload=False)
        ops("GET", "OK" if r["exit"] == 0 else "ERR", instance=rec["uuid"], remote=remote, local=local)
    return r


# ---------------------------------------------------------------- 释放 / 关机
def _release(lid: str, power_off: bool, reason: str = "") -> dict:
    with state_lock():
        rec = _load().get(lid)
    if not rec:
        raise AutoDLError("租约不存在或已被回收")
    with oplog.context(owner=rec["owner"], lease=lid):
        out = {"lease_id": lid, "instance": rec["uuid"], "alias": rec["alias"]}
        if power_off:
            try:
                out.update(c.power_off(rec["uuid"]))
                c.wait_status(rec["uuid"], "shutdown", 180)
                out["status"] = "shutdown"
            finally:
                _mark_managed(rec["uuid"], False)
        else:
            # 交还给用户自己用：关掉保险。失败则抛错并保留租约，否则机内守护仍会按旧时间关机
            r = c.run_ssh(rec["uuid"], "echo off > /root/.autodl_deadline", timeout=30,
                          shown="# [autodl] 解除保险关机（用户保留机器）")
            _check_remote(r, "LEASE_RELEASE", rec["uuid"])
            _mark_managed(rec["uuid"], False)
            out["status"] = "running（保险关机已解除，请自行关机）"
        _drop_lease(lid)
        ops("LEASE_RELEASE", "OK", instance=rec["uuid"], power_off=power_off, reason=reason,
            held=f"{int(time.time() - rec['created'])}s")
        return out


def release(lease_id: str, keep_running: bool = False) -> dict:
    """用完调用：默认关机并撤销租约。"""
    return _release(lease_id, power_off=not keep_running, reason="用户释放")


def power_off_guarded(instance: str, lease_id: str = "", force: bool = False) -> dict:
    """无租约的机器可直接关；有租约的需提供对应 lease_id，或 force=true（会记录在日志里）。"""
    inst = c.find(c.list_instances(), instance)
    with state_lock():
        owner = next(((lid, r) for lid, r in _load().items() if r["uuid"] == inst["uuid"]), None)
        if not owner:  # 无租约：在锁内直接关，避免查完到关机之间被别的窗口 acquire 接管
            r = c.power_off(inst["uuid"])
            _mark_managed(inst["uuid"], False)
            return r
    if lease_id != owner[0]:
        if not force:
            ops("POWER_OFF", "DENY", instance=inst["machine_alias"], holder=owner[1]["owner"],
                reason="机器被他人租用，缺少 lease_id")
            raise AutoDLError(f"{inst['machine_alias']} 正被 {owner[1]['owner']} 租用，到期 "
                              f"{human(owner[1]['expires_at'])}。传入对应 lease_id，或在确认无误后 force=true")
        ops("FORCE_OFF", "OK", instance=inst["machine_alias"], holder=owner[1]["owner"])
    return _release(owner[0], power_off=True, reason="强制关机" if force else "关机")


# ---------------------------------------------------------------- 回收器
def _jobs_alive(uuid: str) -> bool | None:
    """机内是否还有后台任务在跑；ssh 不通时返回 None（未知），别当成“没有任务”去关机。"""
    try:
        r = c.run_ssh(uuid, 'for f in /root/.autodl_jobs/*.pid; do [ -e "$f" ] && tr "\\0" " " < "/proc/$(cat "$f")/cmdline" '
                      '2>/dev/null | grep -q "$(basename "$f" .pid).sh" && echo ALIVE; done; true', timeout=30, log=False)
    except AutoDLError:
        return None
    return "ALIVE" in r["output"] if r["exit"] == 0 else None


def reap() -> list[str]:
    """回收：① 过期租约 → 关机 ② 租约对应机器已关机 → 清理 ③ 本工具开的、无租约且超过宽限的机器 → 关机。"""
    cfg = load_config()
    acts: list[str] = []
    now = time.time()
    with state_lock():
        by = {i["uuid"]: i for i in c.list_instances()}
        d = _load()
        for lid, r in list(d.items()):
            inst = by.get(r["uuid"])
            with oplog.context(owner=r["owner"], lease=lid):
                if inst is None:
                    d.pop(lid)
                    ops("REAP", "OK", action="drop_lease", reason="实例已不存在", instance=r["uuid"])
                    acts.append(f"{lid}: 实例不存在，撤销租约")
                    continue
                expired = now > r["expires_at"]
                if expired and inst["status"] == "running" and r["state"] == "active" and now < r["hard_deadline"]:
                    alive = _jobs_alive(r["uuid"])
                    if alive is None:  # 探测失败：状态未知，保留租约下一轮再看（硬上限后不再探测，直接关）
                        ops("REAP", "ERR", action="probe", reason="ssh 探测后台任务失败", instance=r["uuid"])
                        acts.append(f"{lid}: 探测后台任务失败，下一轮重试")
                        continue
                    if alive:  # 本地和机内守护一起顺延，远端写不进去就不顺延
                        ext = {**r, "expires_at": min(now + 600, r["hard_deadline"])}
                        try:
                            rr = c.run_ssh(r["uuid"], _deadline_cmds(ext, cfg), timeout=60,
                                           shown=f"# [autodl] 后台任务仍在运行，保险关机顺延至 "
                                                 f"{human(ext['expires_at'] + cfg['guard_grace_minutes'] * 60)}")
                        except AutoDLError as e:
                            rr = {"exit": -1, "output": str(e)}
                        if rr["exit"] != 0:
                            ops("REAP", "ERR", action="extend", instance=r["uuid"], exit=rr["exit"])
                            acts.append(f"{lid}: 后台任务仍在跑，但机内保险未能顺延，下一轮重试")
                            continue
                        d[lid] = r = ext
                        ops("REAP", "OK", action="extend", reason="后台任务仍在运行", instance=r["uuid"],
                            expires=human(r["expires_at"]))
                        acts.append(f"{lid}: 后台任务仍在跑，顺延 10 分钟")
                        continue
                if expired:
                    if inst["status"] in ("starting", "creating", "created_cloning", "created"):
                        # 还在开机/克隆，现在关不了：保留租约，等它 running 后下一轮再关
                        ops("REAP", "OK", action="wait_boot", reason=f"实例 {inst['status']}，开机后再关",
                            instance=r["uuid"])
                        acts.append(f"{lid}: 租约过期但实例 {inst['status']}，下一轮再关")
                        continue
                    if inst["status"] == "running":
                        try:
                            c.power_off(r["uuid"])
                        except AutoDLError as e:
                            ops("REAP", "ERR", action="power_off", instance=r["uuid"], error=str(e))
                            acts.append(f"{lid}: 关机失败，保留租约下一轮重试")
                            continue
                    _mark_managed(r["uuid"], False)
                    d.pop(lid)
                    ops("REAP", "OK", action="expire_off", instance=f"{inst['machine_alias']}({r['uuid']})",
                        expired_at=human(r["expires_at"]))
                    acts.append(f"{lid}({inst['machine_alias']}): 租约过期，已关机并撤销")
                elif inst["status"] == "shutdown" and r["state"] == "active":
                    d.pop(lid)
                    _mark_managed(r["uuid"], False)
                    ops("REAP", "OK", action="drop_lease", reason="机器已被关机", instance=r["uuid"])
                    acts.append(f"{lid}: 机器已关机，撤销租约")
        _save(d)
        leased = {r["uuid"] for r in d.values()}
        with state_lock("managed"):
            m = jload(MANAGED, {})
        for u, t in list(m.items()):
            inst = by.get(u)
            if inst is None or inst["status"] == "shutdown":
                _mark_managed(u, False)
            elif u not in leased and now - t > cfg["reap_grace_minutes"] * 60:
                with oplog.context():
                    try:
                        c.power_off(u)
                    except AutoDLError as e:  # 保留 managed 标记，下一轮重试
                        ops("REAP", "ERR", action="orphan_off", instance=u, error=str(e))
                        acts.append(f"{inst['machine_alias']}: 无租约的自启机器关机失败，下一轮重试")
                        continue
                    ops("REAP", "OK", action="orphan_off", instance=f"{inst['machine_alias']}({u})")
                    acts.append(f"{inst['machine_alias']}: 无租约的自启机器，已关机")
                _mark_managed(u, False)
    return acts


# ---------------------------------------------------------------- 只读视图
def list_annotated() -> list[dict]:
    leases = {r["uuid"]: r for r in _load().values()}
    out = []
    for i in c.list_instances():
        r = leases.get(i["uuid"])
        i = dict(i)
        i["lease"] = ({"owner": r["owner"], "purpose": r.get("purpose", ""), "state": r["state"],
                       "expires_at": human(r["expires_at"]),
                       "remaining_min": max(0, int((r["expires_at"] - time.time()) / 60))} if r else None)
        out.append(i)
    return out


def machines_view() -> list[dict]:
    """每台机器的登录信息（主机/端口/用户/密钥路径/完整 ssh 命令），含租约持有人。不含密码。"""
    leases = {r["uuid"]: r for r in _load().values()}
    db = jload(c.MACHINES, {}).get("instances", {})
    out = []
    for i in c.list_instances():
        lg = c.machine_login(i)
        r = leases.get(i["uuid"])
        out.append({"alias": i["machine_alias"], "uuid": i["uuid"], "region": i["region_sign"], "status": i["status"],
                    "host": lg["host"], "port": lg["port"], "user": lg["user"], "key": lg["key"],
                    "ssh_command": lg["ssh_command"], "cloned_from": db.get(i["uuid"], {}).get("cloned_from"),
                    "first_seen": db.get(i["uuid"], {}).get("first_seen"),
                    "leased_by": r["owner"] if r else None})
    return out


def manual_clone(owner: str, instance: str, copy_data_disk: bool = True, dry_run: bool = True, log=lambda m: None) -> dict:
    """手动克隆。dry_run=True 只预览；否则下单并给 owner 发租约（克隆后自动开机、等数据盘复制完）。"""
    if dry_run:
        return c.clone_instance(instance, copy_data_disk, dry_run=True)
    if not owner:
        raise AutoDLError("owner 不能为空")
    with oplog.context(owner=owner):
        with state_lock():
            res = c.clone_instance(instance, copy_data_disk)
            lid, rec = _new_lease(owner, "手动克隆", res["new_uuid"], "(克隆中)", None, True)
            _mark_managed(res["new_uuid"])
            src = c.find(c.list_instances(), instance)
            c.set_machine(res["new_uuid"], cloned_from=src["uuid"], **c.login_overrides(src["uuid"]))
        with oplog.context(lease=lid):
            ops("LEASE_CREATE", "OK", instance=res["new_uuid"], via="manual_clone")
            return _finish_boot(lid, rec, log)
