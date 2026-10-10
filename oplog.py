"""操作日志（只追加，不记录 token / 密码），时间一律 [yyyy-mm-dd hh:mm:ss.fff]（本机时间）。

① logs/linux/YYYYMMDD_<机器>.log —— 对虚拟机的每条命令，TeraTerm 会话日志格式：
     [2026-10-07 12:34:56.789] [root@autodl-001 ~]$ nvidia-smi
     [2026-10-07 12:34:57.012] <输出行，含 stderr>
   多行命令续行以 "> " 开头；传文件用 "[local]$ scp ..." 记录。
② logs/autodl/YYYYMMDD.log —— AutoDL 平台操作：
     [2026-10-07 12:34:56.789] POWER_ON       OK   owner=my-project/train-01 lease=a1b2c3 instance=001机 mode=gpu
   格式：[时间] 动作(14 宽) 结果(OK/ERR/DENY, 4 宽) 键=值 ...；值含空格时用 JSON 引号。
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
# 尽力脱敏：命令或输出里常见的 token / 密码写法在写日志前替换成 ***（无法覆盖所有写法，敏感值请放环境文件）
_SECRET_RES = [
    re.compile(r"(?i)(--?[\w-]*(?:token|password|passwd|secret|api[-_]?key|access[-_]?key)[\w-]*(?:=|\s+)['\"]?)[^\s'\"]+"),
    re.compile(r"(?i)(\b[\w-]*(?:token|password|passwd|secret|api[-_]?key|access[-_]?key)[\w-]*\s*[=:]\s*['\"]?)[^\s'\"]+"),
    re.compile(r"(?i)(\bbearer\s+)\S+"),
    re.compile(r"(\b(?:hf_|sk-|ghp_|gho_|github_pat_|xox[bp]-))[A-Za-z0-9_-]{16,}"),
]


def redact(text: str) -> str:
    for r in _SECRET_RES:
        text = r.sub(r"\1***", text)
    return text

_lock = threading.Lock()
_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("oplog_ctx", default={})


def ts() -> str:
    n = datetime.now()
    return n.strftime("%Y-%m-%d %H:%M:%S") + f".{n.microsecond // 1000:03d}"


def human(t: float | None) -> str | None:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else None


def _append(path: Path, text: str) -> None:
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(text)


@contextlib.contextmanager
def context(**kv):
    """在 with 块内，所有 ops() 日志自动带上这些键（如 owner、lease）。"""
    tok = _ctx.set({**_ctx.get(), **{k: v for k, v in kv.items() if v}})
    try:
        yield
    finally:
        _ctx.reset(tok)


def _val(v) -> str:
    s = redact(str(v))
    if len(s) > 300:
        s = s[:300] + "…"
    return s if re.fullmatch(r"[\w./:@+\-]+", s) else json.dumps(s, ensure_ascii=False)


def ops(event: str, result: str = "OK", /, **kv) -> None:
    kv = {**_ctx.get(), **kv}
    body = " ".join(f"{k}={_val(v)}" for k, v in kv.items() if v not in (None, ""))
    line = f"[{ts()}] {event:<14} {result:<4} {body}".rstrip() + "\n"
    _append(LOG_DIR / "autodl" / f"{datetime.now():%Y%m%d}.log", line)


def machine_tag(alias: str | None, uuid: str = "") -> str:
    num = re.sub(r"\D", "", alias or "")
    return f"autodl-{num}" if num else (alias or uuid[:8] or "unknown")


class LinuxLog:
    """某台机器当天的 TeraTerm 风格会话日志。"""

    def __init__(self, alias: str | None, uuid: str = "", user: str = "root"):
        self.tag = machine_tag(alias, uuid)
        self.user = user
        self.path = LOG_DIR / "linux" / f"{datetime.now():%Y%m%d}_{self.tag}.log"

    def cmd(self, text: str, local: bool = False) -> None:
        prompt = "[local]$ " if local else f"[{self.user}@{self.tag} ~]$ "
        lines = redact(text).rstrip("\n").split("\n") or [""]
        out = "".join(f"[{ts()}] {prompt if i == 0 else '> '}{l}\n" for i, l in enumerate(lines))
        _append(self.path, out)

    def out(self, line: str) -> None:
        _append(self.path, f"[{ts()}] {redact(line).rstrip(chr(13) + chr(10))}\n")


def tail(kind: str, name: str = "", lines: int = 50) -> str:
    """读今天日志的最后几行。kind=autodl 或 linux（linux 需 name=机器编号，如 001）。"""
    day = f"{datetime.now():%Y%m%d}"
    if kind == "autodl":
        p = LOG_DIR / "autodl" / f"{day}.log"
    else:
        p = LOG_DIR / "linux" / f"{day}_{machine_tag(name)}.log"
    if not p.exists():
        return f"(没有 {p.name})"
    return "".join(p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-lines:])
