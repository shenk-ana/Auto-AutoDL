"""命令行入口（与 MCP 同一套逻辑）。
  python autodl_cli.py login                      从剪贴板保存登录态（Windows）
  python autodl_cli.py login -                    从标准输入读取（Linux/macOS，或 Windows 下粘贴）
  python autodl_cli.py list | machines | quota | balance | logs [autodl|linux 机器编号]
  python autodl_cli.py acquire <owner> [--prefer 机] [--no-gpu] [--ttl 分钟]
  python autodl_cli.py exec <lease_id> <命令> | put <lease> <本地> <远程> | get <lease> <远程> <本地>
  python autodl_cli.py renew <lease_id> | release <lease_id> [--keep]
  python autodl_cli.py off <机> [--lease ID] [--force]
  python autodl_cli.py clone <owner> <机> [--yes]  不带 --yes 只预览
  python autodl_cli.py reap                       回收过期租约/无主机器（计划任务每 5 分钟调用）
"""
import json
import subprocess
import sys

import autodl_core as c
import autodl_mgr as m
import oplog

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def _clipboard() -> str:
    return subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                          capture_output=True, text=True, encoding="utf-8").stdout


def _opt(rest: list[str], name: str, default=None):
    return rest[rest.index(name) + 1] if name in rest and rest.index(name) + 1 < len(rest) else default


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, a = argv[0], argv[1:]
    log = lambda s: print(s, file=sys.stderr)  # noqa: E731
    try:
        if cmd == "login":
            c.save_login(sys.stdin.read() if a[:1] == ["-"] or sys.platform != "win32" else _clipboard())
            print(f"验证通过，共 {len(c.list_instances())} 台实例")
            return 0
        out = {
            "list": lambda: m.list_annotated(),
            "machines": lambda: m.machines_view(),
            "quota": lambda: c.clone_quota(),
            "balance": lambda: c.balance(),
            "logs": lambda: oplog.tail(a[0] if a else "autodl", a[1] if len(a) > 1 else "", 60),
            "acquire": lambda: m.acquire(a[0], _opt(a, "--purpose", ""), _opt(a, "--prefer"),
                                         float(_opt(a, "--ttl", 0)) or None, "--no-gpu" in a, log),
            "exec": lambda: m.exec_cmd(a[0], " ".join(a[1:])),
            "put": lambda: m.put_file(a[0], a[1], a[2]),
            "get": lambda: m.get_file(a[0], a[1], a[2]),
            "renew": lambda: m.renew(a[0]),
            "release": lambda: m.release(a[0], "--keep" in a),
            "off": lambda: m.power_off_guarded(a[0], _opt(a, "--lease", ""), "--force" in a),
            "clone": lambda: m.manual_clone(a[0], a[1], True, "--yes" not in a, log),
            "reap": lambda: m.reap(),
        }[cmd]()
    except (c.AutoDLError, KeyError, IndexError) as e:
        print(f"错误：{e!r}", file=sys.stderr)
        return 2
    print(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
