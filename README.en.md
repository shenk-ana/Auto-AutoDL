# Auto-AutoDL

English | [中文](README.md)

An MCP service that lets AI agents acquire, use and release [AutoDL](https://www.autodl.com) GPU instances through one shared, long-running local server. Multiple windows and clients (Claude Code, Claude Desktop, Codex, …) share the same process, never step on each other's machines, shut machines down when done, and log everything. It talks to the console's internal API instead of clicking through the web UI.

> **Disclaimer**
> - This is a personal project. It is **not affiliated with or endorsed by AutoDL**.
> - It uses the **undocumented internal API** of the AutoDL web console, which may break at any time. Make sure your use complies with AutoDL's terms of service.
> - Powering on and cloning instances **costs real money**. There are several layers of automatic shutdown, but bugs are possible — check your instances and balance in the console regularly. Provided "as is" under the MIT license; the authors are not liable for any charges or data loss.

## Features
- **Unified allocation**: power on a stopped instance with a free GPU; otherwise poll every second; if still none after the wait, clone an identical instance (including the data disk).
- **Leases**: only the holder of a `lease_id` can run commands, transfer files or power off that machine, so concurrent agents can't grab or kill each other's machines.
- **Three layers of auto-shutdown**: explicit `release` → in-service reaper → an on-instance guard script that runs `shutdown` at the deadline (works even if your PC is off).
- **Logs**: append-only logs of platform operations and per-machine shell sessions; tokens and passwords are never logged.

## Requirements
- Python 3.10+, `pip install -r requirements.txt`
- A local OpenSSH client (`ssh` / `scp`), with your public key installed on the AutoDL instances
- **Windows is the primary platform**: clipboard login and the `install_*.ps1` scheduled tasks are Windows-only. On Linux / macOS the core works; log in with `login -` (reads stdin) and run the service under systemd / launchd yourself.

## Tools
| Tool | Purpose |
|---|---|
| `autodl_acquire(owner, purpose, prefer, ttl_minutes, no_gpu, wait_seconds)` | Request a machine. Returns `lease_id` when ready within `wait_seconds`, otherwise a `ticket` |
| `autodl_acquire_status(ticket)` | Poll an acquisition; `state=ready` returns `lease_id` and login info |
| `autodl_exec(lease_id, command, timeout, background)` | Run a (multi-line) command; use `background=true` for long jobs |
| `autodl_job_status(lease_id, job_id)` | Background job state and log tail |
| `autodl_put` / `autodl_get` | Upload / download files |
| `autodl_renew(lease_id)` | Extend the lease |
| `autodl_release(lease_id, keep_running)` | **Call when done**: power off and revoke the lease |
| `autodl_power_off(instance, lease_id, force)` | Power off; a machine leased by someone else needs its `lease_id` |
| `autodl_clone(owner, instance, copy_data_disk, dry_run)` | Manual clone, dry run by default |
| `autodl_list` / `autodl_machines` / `autodl_quota` / `autodl_balance` / `autodl_logs` | Read-only queries |

## Setup
1. `pip install -r requirements.txt`
2. SSH key: `~/.ssh/id_ed25519` by default, with its public key added to your AutoDL instances; set `ssh_key` in `config.json` to use another key.
3. Log in (the token is stored only in `.secrets/auth.json`, which is git-ignored):
   - In a logged-in console tab, open DevTools (F12) → Console and run:
     `copy(JSON.stringify({token:localStorage.token,app_version:localStorage.app_version}))`
   - Windows: run `python autodl_cli.py login` (reads the clipboard).
   - Linux / macOS: run `python autodl_cli.py login -`, paste, then press Ctrl-D.
4. Start the service:
   - Windows autostart: `powershell -ExecutionPolicy Bypass -File install_service.ps1` (add `-DryRun` to preview; uninstall with `schtasks /Delete /TN AutoDLMcpService /F`)
   - Foreground: `python mcp_server.py --http --port 8765`
5. The access token is generated on first start at `.secrets/service_token`. **Never paste it into a chat or commit it.**
6. Optional (Windows): `powershell -ExecutionPolicy Bypass -File install_reaper.ps1` runs the reaper every 5 minutes even when the service is down.

## Connecting clients (`http://127.0.0.1:8765/mcp`, header `Authorization: Bearer <token>`)
**Claude Code**
```bash
claude mcp add --scope user --transport http autodl http://127.0.0.1:8765/mcp --header "Authorization: Bearer <token>"
```
For Codex and Claude Desktop examples see the [Chinese README](README.md).

## CLI
Run `python autodl_cli.py` with no arguments to list commands. The CLI shares the same on-disk lease state as the service.

## Configuration
See the table in the [Chinese README](README.md). Notable keys: `ssh_key`, `max_clones_per_day`, `lease_ttl_minutes`, `max_lease_hours`, and `region_groups` (which region signs count as one region for cloning — add yours if your instances are outside the default group).

## Security notes
- The service binds to `127.0.0.1` only. **Do not expose it on `0.0.0.0`** — anyone with the token can run arbitrary commands on your instances and spend your balance.
- SSH host-key checking is disabled because AutoDL hosts/ports change after cloning; use on trusted networks.
- `.secrets/`, `state/`, `logs/`, `machines.json` and `ssh_config.autodl` contain credentials or personal data and are git-ignored — don't commit them.

## License
[MIT](LICENSE)
