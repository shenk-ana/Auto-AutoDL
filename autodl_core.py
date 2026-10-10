"""AutoDL 控制台客户端（原语层）：登录态、查询、开关机、克隆、SSH/SCP、状态锁、克隆配额、机器登录信息登记。

走的是控制台网页自己用的内部接口（www.autodl.com/api/v1/...），
登录态用控制台的 token（localStorage.token），保存在 .secrets/auth.json，从不打印。
接口来自前端源码，AutoDL 改版可能失效；失效时报 AutoDLError，由调用方降级到浏览器。
租约 / 分配 / 保险关机在 autodl_mgr.py。
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from oplog import LinuxLog, ops

ROOT = Path(__file__).resolve().parent
SECRETS = ROOT / ".secrets" / "auth.json"
CONFIG = ROOT / "config.json"
STATE = ROOT / "state"
MACHINES = ROOT / "machines.json"        # 每台机器的登录信息（主机/端口/用户/密钥路径），统一登记
SSH_CONFIG = ROOT / "ssh_config.autodl"  # 自动生成，可在 ~/.ssh/config 里 Include
BASE = "https://www.autodl.com"
BJ = timezone(timedelta(hours=8))

DEFAULT_CONFIG = {
    "wait_seconds": 300,          # 等不到卡多久后改为克隆
    "poll_seconds": 1,            # 轮询间隔
    "auto_clone": True,           # 等满后是否自动克隆
    "clone_data_disk": True,      # 克隆时带数据盘
    "max_clones_per_day": 10,     # 每天最多克隆几次（北京时间自然日）
    "boot_timeout_seconds": 600,  # 开机/克隆后等待 running 的上限
    "data_copy_timeout_seconds": 1800,
    "lease_ttl_minutes": 30,      # 空闲多久后回收；每次操作自动顺延
    "max_lease_hours": 12,        # 租约硬上限，到点强制关机
    "guard_grace_minutes": 5,     # 机内保险关机：比租约到期晚多久
    "reap_grace_minutes": 10,     # 无租约的自启机器多久后被回收
    "adopt_running": False,       # 是否允许抢占用户自己开着的无租约机器
    "ssh_key": str(Path.home() / ".ssh" / "id_ed25519"),
    # 克隆只能在同一"地区"内：每组是控制台里同一地区的 region_sign，例如西北B区 = west-B/C/D/E
    "region_groups": [["west-B", "west-C", "west-D", "west-E"]],
}
# 返回给 AI 的字段白名单：绝不包含 root_password / jupyter_token
PUBLIC_FIELDS = [
    "uuid", "name", "machine_alias", "machine_id", "region_sign", "region_name",
    "status", "sub_status", "start_mode", "req_gpu_amount", "gpu_idle_num",
    "snapshot_gpu_alias_name", "charge_type", "payg_price", "proxy_host",
    "ssh_port", "driver_version", "highest_cuda_version", "started_at", "stopped_at",
]


class AutoDLError(RuntimeError):
    pass


class AuthError(AutoDLError):
    pass


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    return cfg


# ---------------------------------------------------------------- 状态文件 / 跨进程锁
def jload(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def jsave(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def state_lock(name: str = "state", timeout: float = 90, stale: float = 180):
    """跨进程（多个窗口）互斥：O_EXCL 锁文件，持有超过 stale 秒视为遗留锁。"""
    STATE.mkdir(exist_ok=True)
    path = STATE / f"{name}.lock"
    start = time.time()
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.time() - start > timeout:
                raise AutoDLError(f"等待锁 {name} 超时（另一个窗口正在分配/操作）")
            time.sleep(0.2)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------- 登录态
def save_login(raw: str) -> None:
    """raw 可以是裸 token，或 {"token":..., "app_version":...} 的 JSON。"""
    raw = raw.strip()
    try:
        d = json.loads(raw)
        token, ver = d["token"], d.get("app_version", "")
    except (json.JSONDecodeError, KeyError, TypeError):
        token, ver = raw.strip('"'), ""
    if not ver:
        try:
            ver = requests.get(f"{BASE}/version.json", timeout=10).json().get("version", "")
        except Exception:
            ver = ""
    if len(token) < 20 or re.search(r"\s", token):
        raise AutoDLError("剪贴板里不是 token（含空格或太短）：请先在控制台 Console 执行 copy(...)，再运行 login")
    prev = SECRETS.read_text(encoding="utf-8") if SECRETS.exists() else None
    SECRETS.parent.mkdir(exist_ok=True)
    SECRETS.write_text(json.dumps({"token": token, "app_version": ver}), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(SECRETS, 0o600)  # Linux/macOS 下只允许本人读；Windows 上无效果
    try:
        _call("GET", "/api/v1/user/detail")
    except AutoDLError:
        if prev is None:
            SECRETS.unlink(missing_ok=True)
        else:
            SECRETS.write_text(prev, encoding="utf-8")
        ops("LOGIN", "ERR", reason="token 无效")
        raise AuthError("服务器不认这个 token，请确认控制台仍处于登录状态后重新复制")
    ops("LOGIN", "OK")


def _auth() -> dict:
    if not SECRETS.exists():
        raise AuthError("未登录：请在已登录的控制台页面按 F12 → Console 执行 "
                        "copy(JSON.stringify({token:localStorage.token,app_version:localStorage.app_version})) "
                        "然后运行 python autodl_cli.py login")
    return json.loads(SECRETS.read_text(encoding="utf-8"))


def _call(method: str, path: str, body: dict | None = None, params: dict | None = None) -> Any:
    a = _auth()
    headers = {"Authorization": a["token"], "AppVersion": a.get("app_version", ""),
               "Content-Type": "application/json"}
    r = requests.request(method, BASE + path, headers=headers, params=params,
                         data=json.dumps(body) if body is not None else None, timeout=20)
    if r.status_code in (401, 403):
        raise AuthError("token 失效，请重新 login")
    try:
        j = r.json()
    except ValueError:
        raise AutoDLError(f"{path} 返回非 JSON：HTTP {r.status_code}")
    code = j.get("code")
    if code != "Success":
        msg = j.get("msg", "")
        if code in ("Unauthorized", "TokenExpired", "NeedLogin", "AuthorizeFailed") or "登录" in msg:
            raise AuthError(f"token 失效：{msg}")
        raise AutoDLError(f"{path} 失败 code={code} msg={msg}")
    return j.get("data")


# ---------------------------------------------------------------- 机器登录信息统一登记
def _host_port(ssh: str | None) -> tuple[str | None, int | None]:
    m = re.search(r"-p\s+(\d+)\s+\S+@(\S+)", ssh or "")
    return (m.group(2), int(m.group(1))) if m else (None, None)


def sync_machines(items: list[dict]) -> None:
    """把最新的主机/端口写进 machines.json（克隆/换机后登录串会变）。不存密码，只存密钥路径。"""
    cfg = load_config()
    db = jload(MACHINES, {})
    new = json.loads(json.dumps(db))
    new["default"] = {"user": (db.get("default") or {}).get("user") or "root", "key": cfg["ssh_key"]}
    ins = new.setdefault("instances", {})
    for i in items:
        host, port = _host_port(i.get("ssh"))
        rec = ins.setdefault(i["uuid"], {"first_seen": time.strftime("%Y-%m-%d %H:%M:%S")})
        rec.update(alias=i.get("machine_alias"), region=i.get("region_sign"), host=host, port=port)
    if new == db:
        return
    with state_lock("machines"):
        # 合并写入：别的进程可能刚改过密钥等字段，以磁盘最新为底，只覆盖主机/端口
        cur = jload(MACHINES, {})
        cur["default"] = {**new["default"], **{k: v for k, v in (cur.get("default") or {}).items() if k == "user"}}
        cins = cur.setdefault("instances", {})
        for u, r in new["instances"].items():
            cins.setdefault(u, {}).update({k: r[k] for k in ("alias", "region", "host", "port", "first_seen") if k in r})
        jsave(MACHINES, cur)
        lines = ["# 自动生成：在 ~/.ssh/config 加一行 Include <本文件绝对路径> 即可用 ssh autodl-001 之类的别名"]
        for u, r in cins.items():
            if r.get("host") and r.get("port"):
                num = re.sub(r"\D", "", r.get("alias") or "") or u[:6]
                lines += [f"Host autodl-{num}", f"  HostName {r['host']}", f"  Port {r['port']}",
                          f"  User {r.get('user') or cur['default']['user']}",
                          f"  IdentityFile {r.get('key') or cur['default']['key']}",
                          "  StrictHostKeyChecking no", "  UserKnownHostsFile /dev/null", ""]
        SSH_CONFIG.write_text("\n".join(lines), encoding="utf-8")


def machine_login(inst: dict) -> dict:
    db = jload(MACHINES, {})
    rec = db.get("instances", {}).get(inst["uuid"], {})
    host, port = _host_port(inst.get("ssh"))
    user = rec.get("user") or (db.get("default") or {}).get("user") or "root"
    key = rec.get("key") or load_config()["ssh_key"]  # 默认密钥始终以 config.json 为准，逐台覆盖值优先
    return {"host": host, "port": port, "user": user, "key": key,
            "ssh_command": f"ssh -i {key} -p {port} {user}@{host}" if host else None}


def login_overrides(uuid: str) -> dict:
    """某台机器在 machines.json 里显式写的 user/key（克隆时沿用给新机器；没写的继续跟随默认值）。"""
    rec = jload(MACHINES, {}).get("instances", {}).get(uuid, {})
    return {k: rec[k] for k in ("user", "key") if rec.get(k)}


def set_machine(uuid: str, **fields) -> None:
    with state_lock("machines"):
        db = jload(MACHINES, {"default": {"user": "root", "key": load_config()["ssh_key"]}, "instances": {}})
        db.setdefault("instances", {}).setdefault(uuid, {}).update({k: v for k, v in fields.items() if v})
        jsave(MACHINES, db)


# ---------------------------------------------------------------- 克隆配额
def clone_quota() -> dict:
    today = datetime.now(BJ).strftime("%Y-%m-%d")
    log = jload(STATE / "clone_log.json", [])
    used = sum(1 for e in log if e.get("date") == today)
    limit = load_config()["max_clones_per_day"]
    return {"date": today, "used": used, "limit": limit, "remaining": max(0, limit - used)}


# ---------------------------------------------------------------- 查询
def _raw_instances() -> list[dict]:
    d = _call("POST", "/api/v1/instance", {
        "page_index": 1, "page_size": 100, "status": [], "charge_type": [],
        "sub_name": "", "unbind_sub_user": False})
    return d.get("list", [])


def _public(i: dict) -> dict:
    out = {k: i.get(k) for k in PUBLIC_FIELDS}
    m = re.search(r"-p\s+(\d+)\s+root@(\S+)", i.get("ssh_command") or "")
    if m:
        out["ssh"] = f"-p {m.group(1)} root@{m.group(2)}"
    out["gpu_available"] = (i.get("status") == "shutdown"
                            and (i.get("req_gpu_amount") or 1) <= (i.get("gpu_idle_num") or 0))
    return out


def list_instances() -> list[dict]:
    items = [_public(i) for i in _raw_instances()]
    sync_machines(items)
    return items


def balance() -> Any:
    return _call("GET", "/api/v1/wallet/balance", params={"charge_type": "payg"})


def find(items: list[dict], ref: str) -> dict:
    ref = (ref or "").strip()
    for i in items:
        if ref in (i["uuid"], i.get("name"), i.get("machine_alias")):
            return i
    hits = [i for i in items if ref and (ref in (i.get("machine_alias") or "") or ref in i["uuid"])]
    if len(hits) == 1:
        return hits[0]
    raise AutoDLError(f"找不到唯一匹配的实例：{ref!r}")


# ---------------------------------------------------------------- 开关机
def _instance_op(op: str, body: dict) -> None:
    """普通实例和 Pro 实例用不同路径：先试普通，查无记录再试 Pro。"""
    try:
        _call("POST", f"/api/v1/instance/{op}", body)
    except AuthError:
        raise
    except AutoDLError as e:
        if "RecordNotFound" not in str(e):
            raise
        _call("POST", f"/api/v1/instance/pro/{op}", body)


def power_on(ref: str, no_gpu: bool = False) -> dict:
    inst = find(list_instances(), ref)
    tag = {"instance": f"{inst['machine_alias']}({inst['uuid']})", "mode": "non_gpu" if no_gpu else "gpu"}
    try:
        if inst["status"] != "shutdown":
            raise AutoDLError(f"{inst['machine_alias']} 当前状态 {inst['status']}，不是 shutdown")
        if not no_gpu and not inst["gpu_available"]:
            raise AutoDLError(f"{inst['machine_alias']} 现在没有空闲 GPU（gpu_idle_num=0）")
        _instance_op("power_on", {"instance_uuid": inst["uuid"], "payload": "non_gpu" if no_gpu else ""})
    except AutoDLError as e:
        ops("POWER_ON", "ERR", error=str(e), **tag)
        raise
    ops("POWER_ON", "OK", **tag)
    return {"uuid": inst["uuid"], "alias": inst["machine_alias"], "requested": tag["mode"]}


def power_off(ref: str) -> dict:
    inst = find(list_instances(), ref)
    tag = {"instance": f"{inst['machine_alias']}({inst['uuid']})"}
    if inst["status"] == "shutdown":
        ops("POWER_OFF", "OK", note="已经是关机状态", **tag)
        return {"uuid": inst["uuid"], "alias": inst["machine_alias"], "note": "已经是关机状态"}
    try:
        _instance_op("power_off", {"instance_uuid": inst["uuid"]})
    except AutoDLError as e:
        ops("POWER_OFF", "ERR", error=str(e), **tag)
        raise
    ops("POWER_OFF", "OK", **tag)
    return {"uuid": inst["uuid"], "alias": inst["machine_alias"], "requested": "power_off"}


def wait_status(uuid: str, want: str, timeout: int, poll: float = 2) -> dict:
    end = time.time() + timeout
    last = None
    while time.time() < end:
        for i in list_instances():
            if i["uuid"] == uuid:
                last = i
                if i["status"] == want and (want != "running" or i.get("ssh")):
                    return i
        time.sleep(poll)
    raise AutoDLError(f"等待 {uuid} → {want} 超时，最后状态：{last and last['status']}")


# ---------------------------------------------------------------- 克隆
def _machines(region_signs: list[str], gpu_name: str) -> list[dict]:
    d = _call("POST", "/api/v1/user/machine/list", {
        "charge_type": "payg", "region_sign": "", "gpu_type_name": [gpu_name] if gpu_name else [],
        "machine_tag_name": [], "gpu_idle_num": 1, "mount_net_disk": False,
        "instance_disk_size_order": "", "date_range": "", "date_from": "", "date_to": "",
        "page_index": 1, "page_size": 50, "pay_price_order": "", "gpu_idle_type": "",
        "default_order": True, "region_sign_list": region_signs, "cpu_arch": [], "chip_corp": []})
    return d.get("list", [])


def _region_group(sign: str) -> list[str]:
    """同地区的 region_sign 列表（config.json 的 region_groups）；未登记的地区只在自身内找。"""
    for g in load_config()["region_groups"]:
        if sign in g:
            return g
    return [sign]


def clone_instance(ref: str, copy_data_disk: bool = True, dry_run: bool = False) -> dict:
    """克隆一台同配置机器（含数据盘），按量计费，克隆后自动开机。受每日配额限制。"""
    raw = {i["uuid"]: i for i in _raw_instances()}
    src = find([_public(i) for i in raw.values()], ref)
    s = raw[src["uuid"]]
    snap = _call("GET", "/api/v1/instance/snapshot", params={"instance_uuid": src["uuid"]})
    img = snap.get("image") or {}
    gpu = snap.get("snapshot_gpu_alias_name") or s.get("snapshot_gpu_alias_name") or ""
    cands = [m for m in _machines(_region_group(s["region_sign"]), gpu)
             if m.get("gpu_idle_num", 0) >= (s.get("req_gpu_amount") or 1)]
    if not cands:
        ops("CLONE", "ERR", source=src["machine_alias"], error=f"同地区没有空闲的 {gpu}")
        raise AutoDLError(f"同地区没有空闲的 {gpu}，无法克隆")
    # 优先同一台宿主机（镜像/数据盘就近），否则取空闲卡最多的
    cands.sort(key=lambda m: (m["machine_id"] != s["machine_id"], -m.get("gpu_idle_num", 0)))
    gpu_num = s.get("req_gpu_amount") or 1
    expand = int(snap.get("expand_data_disk_size") or 0)

    def build(tgt: dict) -> dict:
        return {
            "instance_uuid": src["uuid"],
            "instance_info": {
                "machine_id": tgt["machine_id"], "charge_type": "payg", "req_gpu_amount": gpu_num,
                "base_image_uuid": img.get("base_image_uuid", ""),
                "instance_name": "",
                "private_image_uuid": img.get("private_image_uuid", ""),
                "reproduction_uuid": img.get("reproduction_uuid", ""),
                "cg_application_uuid": snap.get("cg_application_uuid", "") or "",
                "cg_application_info": snap.get("cg_application_info") or {},
                "expand_data_disk": expand,
                "copy_data_disk_after_clone": bool(copy_data_disk),
                "keep_src_user_service_address_after_clone": False,
                "reproduction_id": int(img.get("reproduction_id") or 0),
                "ft_sparse": False,
            },
            "price_info": {"coupon_id_list": [], "machine_id": tgt["machine_id"], "charge_type": "payg",
                           "duration": 1, "num": gpu_num, "expand_data_disk": expand},
        }

    summary = {"source": src["machine_alias"], "gpu": gpu, "copy_data_disk": copy_data_disk}
    if dry_run:
        ops("CLONE_DRY", "OK", source=src["machine_alias"], target=cands[0].get("machine_alias"))
        return {"dry_run": True, **summary, "target_machine": cands[0].get("machine_alias"),
                "candidates": len(cands), "quota": clone_quota(), "payload": build(cands[0])}
    errors = []
    with state_lock("clone"):
        q = clone_quota()
        if q["remaining"] <= 0:
            ops("CLONE", "DENY", source=src["machine_alias"], reason=f"今日克隆已达上限 {q['limit']}")
            raise AutoDLError(f"今日克隆已达上限 {q['limit']} 次（北京时间 {q['date']}），明天再试")
        for tgt in cands[:10]:
            try:
                new_uuid = _call("POST", "/api/v1/order/instance/clone/payg", build(tgt))
            except AuthError:
                raise
            except AutoDLError as e:
                errors.append(f"{tgt.get('machine_alias')}: {e}")
                ops("CLONE_TRY", "ERR", source=src["machine_alias"], target=tgt.get("machine_alias"), error=str(e))
                if "FacilityNotSupported" not in str(e) and "NoGpu" not in str(e) and "Stock" not in str(e):
                    raise
                continue
            log = jload(STATE / "clone_log.json", [])
            log.append({"date": q["date"], "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "source": src["uuid"], "new_uuid": new_uuid})
            jsave(STATE / "clone_log.json", log)
            ops("CLONE", "OK", source=f"{src['machine_alias']}({src['uuid']})", new_instance=new_uuid,
                target=tgt.get("machine_alias"), data_disk=copy_data_disk, quota_left=q["remaining"] - 1)
            return {"new_uuid": new_uuid, "source_uuid": src["uuid"], "target_machine": tgt.get("machine_alias"),
                    "quota_remaining": q["remaining"] - 1, **summary}
    ops("CLONE", "ERR", source=src["machine_alias"], error=" | ".join(errors))
    raise AutoDLError("所有候选机器都克隆失败：" + " | ".join(errors))


# ---------------------------------------------------------------- SSH / SCP（TeraTerm 格式日志）
def _bin(name: str) -> str:
    """优先 Windows 自带 OpenSSH：Git 自带的 MSYS ssh 会把 UserKnownHostsFile=NUL 当成真文件名，并可能读不到用户 ssh 配置。"""
    win = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / f"{name}.exe"
    return str(win) if win.exists() else (shutil.which(name) or name)


_SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]


def _running(ref: str) -> dict:
    inst = find(list_instances(), ref)
    if inst["status"] != "running" or not inst.get("ssh"):
        raise AutoDLError(f"{inst['machine_alias']} 未运行（状态 {inst['status']}）")
    return inst


def _stream(args: list[str], lg: LinuxLog | None, shown: str, local: bool, input_text: str | None,
            timeout: int) -> dict:
    """运行子进程，stdout+stderr 合并按行实时写日志；超时则杀掉。"""
    if lg:
        lg.cmd(shown, local=local)
    p = subprocess.Popen(args, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace")
    flag = {"to": False}

    def kill():
        flag["to"] = True
        p.kill()

    timer = threading.Timer(timeout, kill)
    timer.start()
    if input_text is not None:
        try:
            p.stdin.buffer.write(input_text.encode("utf-8"))  # 写字节：文本模式在 Windows 会把换行变成回车+换行
            p.stdin.close()
        except OSError:
            pass
    buf: deque[str] = deque()  # 只留够返回的尾部 8000 字符：高频输出的命令不会把常驻服务的内存撑爆
    size = 0
    for line in p.stdout:
        buf.append(line)
        size += len(line)
        while size - len(buf[0]) >= 8000:
            size -= len(buf.popleft())
        if lg:
            lg.out(line)
    rc = p.wait()
    timer.cancel()
    if flag["to"] and lg:
        lg.out(f"[autodl] 命令超过 {timeout}s，已被终止")
    return {"exit": rc, "output": "".join(buf)[-8000:], "timed_out": flag["to"]}


def run_ssh(ref: str, command: str, timeout: int = 600, log: bool = True, shown: str | None = None) -> dict:
    """在机器上执行脚本（经 bash -s 的标准输入传入，支持多行）。命令与输出按 TeraTerm 格式记入 logs/linux/。
    shown：写进日志的命令文本（默认 = command；内部加的保险刷新行不显示）。"""
    inst = _running(ref)
    lg = machine_login(inst)
    args = [_bin("ssh"), "-i", lg["key"], "-p", str(lg["port"]), *_SSH_OPTS, f"{lg['user']}@{lg['host']}", "bash -s"]
    ll = LinuxLog(inst["machine_alias"], inst["uuid"], lg["user"]) if log else None
    return _stream(args, ll, shown if shown is not None else command, False, command + "\n", timeout)


def run_scp(ref: str, local: str, remote: str, upload: bool, timeout: int = 3600) -> dict:
    inst = _running(ref)
    lg = machine_login(inst)
    rem = f"{lg['user']}@{lg['host']}:{remote}"
    args = [_bin("scp"), "-S", _bin("ssh"), "-r", "-i", lg["key"], "-P", str(lg["port"]), *_SSH_OPTS]  # -S：scp 默认会去 PATH 找 ssh，可能找到 Git 自带那个
    args += [local, rem] if upload else [rem, local]
    tag = LinuxLog(inst["machine_alias"], inst["uuid"], lg["user"])
    shown = f"scp {local} {tag.tag}:{remote}" if upload else f"scp {tag.tag}:{remote} {local}"
    return _stream(args, tag, shown, True, None, timeout)


def wait_data_copy(ref: str, timeout: int = 1800, poll: int = 10, log=lambda m: None) -> dict:
    """克隆后数据盘在后台复制：等 /root/autodl-tmp 下的 .xxx.safetensors.XXXX 临时文件消失且大小稳定。"""
    end = time.time() + timeout
    cmd = "find /root/autodl-tmp -name '.*.safetensors.*' 2>/dev/null | wc -l; du -sm /root/autodl-tmp | cut -f1"
    last = None
    while time.time() < end:
        try:
            out = re.findall(r"\d+", run_ssh(ref, cmd, timeout=60, log=False)["output"])
            tmp, mb = int(out[-2]), int(out[-1])
            if tmp == 0 and last == mb:       # 没有临时文件且大小连续两次不变
                ops("DATA_COPY", "OK", instance=ref, data_mb=mb)
                return {"copy_done": True, "data_mb": mb}
            if mb != last:
                log(f"数据盘复制中：{mb} MB，临时文件 {tmp} 个")
            last = mb
        except (AutoDLError, subprocess.TimeoutExpired, ValueError, IndexError):
            pass
        time.sleep(poll)
    ops("DATA_COPY", "ERR", instance=ref, error="等待超时")
    raise AutoDLError("等待数据盘复制超时")
