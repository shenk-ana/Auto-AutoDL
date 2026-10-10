"""AutoDL MCP 服务：统一查询 / 分配 / 执行命令 / 传文件 / 关机，带租约、日志、保险关机。

两种运行方式（工具完全相同）：
  python mcp_server.py --http [--port 8765]   常驻 HTTP 服务（推荐）：Claude Code / Claude Desktop / Codex 等共用同一个进程
  python mcp_server.py                         stdio：由客户端按需拉起，每个会话一个进程（备用）

AI 用法：autodl_acquire(owner=...) → 轮询 autodl_acquire_status 直到 ready 拿到 lease_id
        → autodl_exec/put/get（都要 lease_id）→ autodl_release。
"""
import argparse
import asyncio
import hmac
import secrets
import sys
import threading
import time

from mcp.server.mcpserver import MCPServer

import autodl_core as c
import autodl_mgr as m
import oplog

mcp = MCPServer("autodl")
TOKEN_FILE = c.ROOT / ".secrets" / "service_token"


async def _run(fn, *a, **k):
    """所有业务函数都是阻塞的（轮询、ssh）：放线程池跑，别卡住事件循环（常驻服务要同时服务多个客户端）。"""
    try:
        return await asyncio.to_thread(fn, *a, **k)
    except c.AutoDLError as e:
        return {"error": str(e), "auth_expired": isinstance(e, c.AuthError)}


# ---------------------------------------------------------------- 申请机器：票据 + 轮询
_jobs: dict[str, dict] = {}


def _job_view(ticket: str) -> dict:
    j = _jobs.get(ticket)
    if not j:
        return {"error": "票据不存在（服务可能重启过）。先 autodl_list 看看是否已有你的租约，没有就重新 autodl_acquire"}
    base = {"ticket": ticket, "elapsed_seconds": int(time.time() - j["t0"]), "progress": j["log"][-5:]}
    if j["state"] == "pending":
        return {**base, "state": "pending", "next": f"继续调用 autodl_acquire_status(ticket='{ticket}')，直到 state=ready"}
    if j["state"] == "error":
        return {**base, "state": "error", "error": j["error"]}
    return {**base, "state": "ready", **j["result"]}


@mcp.tool()
async def autodl_acquire(owner: str, purpose: str = "", prefer: str = "", ttl_minutes: int = 0,
                         no_gpu: bool = False, wait_seconds: int = 45) -> dict:
    """申请一台机器。owner 写明谁在用，如 'my-project/train-01'。
    立即开始分配，最多等 wait_seconds 秒：机器就绪则直接返回 state=ready 和 lease_id；
    否则返回 state=pending 和 ticket，请反复调用 autodl_acquire_status(ticket) 直到 ready（避免客户端工具超时）。
    流程：空闲卡的关机机→开机；没有则每秒刷新最多等 5 分钟；仍没有则克隆同配置机（含数据盘，等复制完才算就绪；每天最多克隆 10 次）。
    已被他人租用的机器不会分给你。开机约 1–2 分钟，克隆约 2–5 分钟，最坏等 5 分钟 + 克隆。
    租约规则：空闲超时默认 30 分钟（ttl_minutes 可改，1–720）；每次 exec/put/get/job_status/renew 都会自动顺延；
    长前台命令/传输按其 timeout 顺延；后台任务(background)运行中不会被回收；硬上限 12 小时，到点强制关机。
    机器内还有守护脚本：租约到期 +5 分钟自动 shutdown。用完务必 autodl_release。no_gpu=true 为无卡模式（只拷文件，便宜）。"""
    ticket = secrets.token_hex(5)
    job = {"state": "pending", "t0": time.time(), "log": [], "owner": owner}
    _jobs[ticket] = job

    def work():
        try:
            job["result"] = m.acquire(owner, purpose, prefer or None, ttl_minutes or None, no_gpu,
                                      log=lambda s: job["log"].append(f"{time.strftime('%H:%M:%S')} {s}"))
            job["state"] = "ready"
        except c.AutoDLError as e:
            job["error"], job["state"] = str(e), "error"
        except Exception as e:  # noqa: BLE001
            job["error"], job["state"] = repr(e), "error"
        for old in [t for t, j in _jobs.items() if j["state"] != "pending" and time.time() - j["t0"] > 86400]:
            _jobs.pop(old, None)

    t = threading.Thread(target=work, daemon=True)
    job["thread"] = t
    t.start()
    await asyncio.to_thread(t.join, max(1, min(wait_seconds, 55)))
    return _job_view(ticket)


@mcp.tool()
async def autodl_acquire_status(ticket: str, wait_seconds: int = 45) -> dict:
    """查询 autodl_acquire 的进度。最多再等 wait_seconds 秒；state=ready 时返回 lease_id 和登录信息。"""
    j = _jobs.get(ticket)
    if j and j["state"] == "pending":
        await asyncio.to_thread(j["thread"].join, max(1, min(wait_seconds, 55)))
    return _job_view(ticket)


# ---------------------------------------------------------------- 只读
@mcp.tool()
async def autodl_list() -> list | dict:
    """列出所有实例（不含密码）。gpu_available=true 表示关机且有空闲卡；lease 字段显示谁在租用及到期时间。"""
    return await _run(m.list_annotated)


@mcp.tool()
async def autodl_machines() -> list | dict:
    """每台机器的登录信息统一视图：主机、端口（克隆后会变，这里始终最新）、用户、密钥路径、完整 ssh 命令、克隆来源、租用人。"""
    return await _run(m.machines_view)


@mcp.tool()
async def autodl_balance() -> dict:
    """查询账户按量计费余额。"""
    return await _run(c.balance)


@mcp.tool()
async def autodl_quota() -> dict:
    """今日克隆配额（每天最多 10 次，北京时间自然日）。"""
    return await _run(c.clone_quota)


@mcp.tool()
async def autodl_logs(kind: str = "autodl", machine: str = "", lines: int = 50) -> str:
    """查看今天的操作日志末尾。kind='autodl'（平台操作）或 'linux'（某机器的命令会话，machine 填编号如 001）。"""
    return await asyncio.to_thread(oplog.tail, kind, machine, lines)


# ---------------------------------------------------------------- 租约内操作
@mcp.tool()
async def autodl_exec(lease_id: str, command: str, timeout: int = 600, background: bool = False) -> dict:
    """在租约机器上执行命令（多行脚本也行，经 bash -s）；命令与输出按 TeraTerm 格式记入日志；每次调用顺延租约。
    前台命令按 timeout（秒，默认 600）等待，超时会被终止；租约至少撑到 timeout 结束。注意客户端自身的工具超时（常见 60 秒）：
    预计超过 30 秒的任务请 background=true：立即返回 job_id，用 autodl_job_status 查看；运行中机器不会被回收/保险关机（到 12 小时硬上限）。"""
    return await _run(m.exec_cmd, lease_id, command, timeout, background)


@mcp.tool()
async def autodl_job_status(lease_id: str, job_id: str, tail: int = 40) -> dict:
    """查看后台任务状态（running/done exit=N）和日志末尾。"""
    return await _run(m.job_status, lease_id, job_id, tail)


@mcp.tool()
async def autodl_put(lease_id: str, local: str, remote: str) -> dict:
    """上传本地文件/目录到租约机器；租约至少顺延 1 小时（长传输不会被半路关机）。"""
    return await _run(m.put_file, lease_id, local, remote)


@mcp.tool()
async def autodl_get(lease_id: str, remote: str, local: str) -> dict:
    """从租约机器下载文件/目录；租约至少顺延 1 小时（长传输不会被半路关机）。"""
    return await _run(m.get_file, lease_id, remote, local)


@mcp.tool()
async def autodl_renew(lease_id: str, ttl_minutes: int = 0) -> dict:
    """续租：把到期时间重置为 现在+ttl_minutes（不传则用申请时的时长，默认 30 分钟，最长不超过 12 小时硬上限），同时顺延机内保险关机。
    长时间不操作但还要用时调用；平时 exec/put/get 会自动顺延，不需要手动续。"""
    return await _run(m.renew, lease_id, ttl_minutes or None)


@mcp.tool()
async def autodl_release(lease_id: str, keep_running: bool = False) -> dict:
    """用完必须调用：默认关机并撤销租约（停止计费）。keep_running=true 表示交还给用户自己用（解除保险关机，之后需自行关机）。
    忘了调用也不会一直计费：租约空闲 30 分钟后由回收器关机，机内守护 +5 分钟再兜底。"""
    return await _run(m.release, lease_id, keep_running)


@mcp.tool()
async def autodl_power_off(instance: str, lease_id: str = "", force: bool = False) -> dict:
    """关机。无租约的机器可直接关；被他人租用的机器必须给出对应 lease_id，
    force=true 仅在用户明确要求或确认无人使用时用（会记入日志 FORCE_OFF）。关租约机器时会同时撤销租约。"""
    return await _run(m.power_off_guarded, instance, lease_id, force)


@mcp.tool()
async def autodl_clone(owner: str, instance: str, copy_data_disk: bool = True, dry_run: bool = True) -> dict:
    """手动克隆。默认 dry_run=True 只预览；下单需 dry_run=false，计入每日 10 次配额，并给 owner 发租约
    （真下单会等克隆机开机并复制完数据盘才返回，可能数分钟，注意客户端超时）。"""
    return await _run(m.manual_clone, owner, instance, copy_data_disk, dry_run)


# ---------------------------------------------------------------- 回收器 / 启动
def _reaper_loop() -> None:
    """服务存活期间，每 2 分钟回收一次过期租约 / 无主机器。"""
    while True:
        time.sleep(120)
        try:
            m.reap()
        except Exception as e:  # noqa: BLE001  回收器不能因为任何错误退出
            oplog.ops("REAP", "ERR", error=repr(e))


def service_token() -> str:
    """HTTP 服务的访问口令（首次自动生成，只存本机 .secrets/service_token；客户端配置里要带上）。"""
    if not TOKEN_FILE.exists():
        TOKEN_FILE.parent.mkdir(exist_ok=True)
        TOKEN_FILE.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


class _BearerAuth:
    """纯 ASGI 中间件：没有正确的 Authorization: Bearer <token> 一律 401（防本机其他程序/网页乱调）。"""

    def __init__(self, app, token: str):
        self.app, self.expect = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expect):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


def serve_http(host: str, port: int) -> None:
    import uvicorn
    # 无状态 + JSON 响应：服务重启后客户端无需重新握手，也不依赖长连接
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, host=host)
    oplog.ops("SERVICE", "OK", event="start", host=host, port=port)
    uvicorn.run(_BearerAuth(app, service_token()), host=host, port=port, log_level="warning")


def _ensure_streams() -> None:
    """pythonw（计划任务/无窗口运行）下 stdout/stderr 是 None，uvicorn 配置日志会直接崩溃：改写到 logs/service.log。"""
    if sys.stdout is None or sys.stderr is None:
        (c.ROOT / "logs").mkdir(exist_ok=True)
        f = open(c.ROOT / "logs" / "service.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f


if __name__ == "__main__":
    _ensure_streams()
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true", help="常驻 HTTP 服务（推荐）")
    ap.add_argument("--host", default="127.0.0.1", help="默认只监听本机")
    ap.add_argument("--port", type=int, default=8765)
    ns = ap.parse_args()
    threading.Thread(target=_reaper_loop, daemon=True).start()
    if ns.http:
        serve_http(ns.host, ns.port)
    else:
        mcp.run()
