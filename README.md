# Auto-AutoDL

[English](README.en.md) | 中文

让 AI 统一申请、使用、释放 [AutoDL](https://www.autodl.com) GPU 机器的 MCP 服务：多个窗口 / 多个客户端（Claude Code、Claude Desktop、Codex 等）共用**同一个常驻服务**，互不干扰、用完及时关机、全程留日志。走控制台内部接口，不点网页。

> **免责声明**
> - 本项目是个人工具，**与 AutoDL 官方无关**，也未获其认可。
> - 它调用的是 AutoDL 控制台前端使用的**非公开内部接口**，随时可能因改版失效；使用前请自行确认不违反 AutoDL 的服务条款。
> - 开机、克隆都会**产生真实费用**。代码有多层保险关机，但不保证没有 bug，请定期在控制台核对机器状态和余额。按 MIT 协议“按原样”提供，作者不对任何费用或数据损失负责。

## 功能
- **统一分配**：有空闲卡的关机机器直接开机；没有就每秒刷新等待；等满仍没有则自动克隆同配置机器（含数据盘）。
- **租约**：持有 `lease_id` 才能对机器执行命令、传文件、关机，多个 AI 窗口不会互相抢机器、误关机器。
- **三层保险关机**：主动 `release` → 服务内回收器 → 机器内守护脚本到点自己 `shutdown`（本地电脑关机也有效）。
- **日志**：平台操作和每台机器的命令会话都按行追加记录，不记录 token 和密码。

## 运行环境
- Python 3.10+，`pip install -r requirements.txt`
- 本地已安装 OpenSSH 客户端（`ssh` / `scp`），并且 AutoDL 实例里已配置好你的公钥
- **Windows 为主要使用平台**：剪贴板登录、`install_*.ps1` 计划任务只适用于 Windows。Linux / macOS 下核心功能可用，登录改用 `login -` 从标准输入读取，常驻服务请自行用 systemd / launchd 等托管。

## 工具
| 工具 | 作用 |
|---|---|
| `autodl_acquire(owner, purpose, prefer, ttl_minutes, no_gpu, wait_seconds)` | 申请机器。立即开始分配，最多等 `wait_seconds`；就绪返回 `lease_id`，否则返回 `ticket` |
| `autodl_acquire_status(ticket)` | 轮询申请进度，`state=ready` 时拿到 `lease_id` 和登录信息（避免客户端工具超时） |
| `autodl_exec(lease_id, command, timeout, background)` | 在租约机器上执行命令（多行可）；长任务用 `background=true` |
| `autodl_job_status(lease_id, job_id)` | 后台任务状态与日志末尾 |
| `autodl_put` / `autodl_get` | 上传 / 下载文件 |
| `autodl_renew(lease_id)` | 续租 |
| `autodl_release(lease_id, keep_running)` | **用完调用**：关机并撤销租约 |
| `autodl_power_off(instance, lease_id, force)` | 关机；他人租用的机器需对应 `lease_id` |
| `autodl_clone(owner, instance, copy_data_disk, dry_run)` | 手动克隆，默认只预览 |
| `autodl_list` / `autodl_machines` / `autodl_quota` / `autodl_balance` / `autodl_logs` | 只读查询 |

## 架构
- **一个常驻 HTTP 服务**（`python mcp_server.py --http`，只监听 `127.0.0.1:8765`，Bearer 口令保护）。所有客户端连同一个进程，回收器只有一个且一直在跑。也保留 stdio 模式（`python mcp_server.py`，每个会话一个进程）作备用，工具和规则完全一样。
- 服务是无状态 HTTP + JSON 响应：重启服务后客户端无需重新握手。所有状态在磁盘（`state/`），重启不丢租约。
- 工具内部都在线程池执行，一个窗口的长命令不会卡住其他窗口。
- **租约（防互相操作）**：分配、克隆都在跨进程锁里完成，同时申请不会拿到同一台，也不会重复克隆。`list` 只暴露 owner 和到期时间，不暴露 `lease_id`。
- **租约时长**：空闲超时默认 30 分钟，每次操作自动顺延；长传输/长前台命令按其超时顺延；后台任务在跑时不回收；硬上限 12 小时。
- **三层保险保证关机**：① `release` 立即关；② 回收器（服务内每 2 分钟 + 可选计划任务 `install_reaper.ps1`）关掉过期租约和无租约的自启机器；③ 机器内守护脚本到点自己 `shutdown`（租约到期 +5 分钟）。
- **机器登录信息统一登记**：`machines.json` 记录每台机器当前的主机、端口、用户、密钥路径、克隆来源，每次查询自动刷新；同时生成 `ssh_config.autodl`，在 `~/.ssh/config` 里 `Include` 它即可用 `ssh autodl-001` 这类别名。**不保存密码**；root 密码、jupyter token 不会返回给 AI。
- **克隆配额**：默认每天最多 10 次（北京时间自然日），计数在 `state/clone_log.json`，超出直接拒绝。
- **日志（只追加，每行 年-月-日 时:分:秒.毫秒）**
  - `logs/linux/YYYYMMDD_autodl-<编号>.log`：每台机器的命令会话，TeraTerm 格式：命令行带提示符 `[root@autodl-001 ~]$ …`，多行续行 `> `，输出含 stderr，传文件记为 `[local]$ scp …`。
  - `logs/autodl/YYYYMMDD.log`：平台操作，`[时间] 动作 结果(OK/ERR/DENY) 键=值…`，含 owner、lease、instance。动作有 `ACQUIRE_START / POWER_ON / POWER_OFF / CLONE / LEASE_* / EXEC / JOB_START / PUT / GET / GUARD_INSTALL / DATA_COPY / REAP / FORCE_OFF / SERVICE / LOGIN`。

## 安装（一次）
1. `pip install -r requirements.txt`
2. 准备 SSH 密钥：默认使用 `~/.ssh/id_ed25519`，并把对应公钥加到 AutoDL 实例里；用别的密钥请在 `config.json` 里设置 `ssh_key`。
3. 登录 AutoDL（token 只存本机 `.secrets/auth.json`，已被 `.gitignore` 排除）：
   - 已登录的控制台页面按 F12 → Console 执行：
     `copy(JSON.stringify({token:localStorage.token,app_version:localStorage.app_version}))`
   - Windows：紧接着运行 `python autodl_cli.py login`（读剪贴板），看到“验证通过”即成功。
   - Linux / macOS：运行 `python autodl_cli.py login -`，粘贴后按 Ctrl-D。
   - token 过期后重复这一步。
4. 启动常驻服务（二选一）：
   - Windows 开机自启（推荐）：`powershell -ExecutionPolicy Bypass -File install_service.ps1`（计划任务，崩溃自动重启；卸载：`schtasks /Delete /TN AutoDLMcpService /F`；先加 `-DryRun` 可只检查不注册）
   - 手动前台：`python mcp_server.py --http --port 8765`
5. 取访问口令：首次启动自动生成在 `.secrets/service_token`，用下面客户端配置时填进去。**别贴到聊天里，也别提交到 git。**
6. 可选（Windows）：`powershell -ExecutionPolicy Bypass -File install_reaper.ps1`，服务没开时也能每 5 分钟回收一次。

## 各客户端怎么连（地址 `http://127.0.0.1:8765/mcp`，头 `Authorization: Bearer <口令>`）
**Claude Code**
```bash
claude mcp add --scope user --transport http autodl http://127.0.0.1:8765/mcp --header "Authorization: Bearer <口令>"
```
**Codex**（`~/.codex/config.toml`；键名请按你的 Codex 版本核对，未经验证）
```toml
[mcp_servers.autodl]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "AUTODL_MCP_TOKEN"   # 先把口令设为用户环境变量 AUTODL_MCP_TOKEN
tool_timeout_sec = 3600
```
**Claude Desktop**（它的配置文件只支持本地命令，需 `mcp-remote` 做 HTTP→stdio 转接；未经验证）
```json
"autodl": {
  "command": "npx",
  "args": ["-y", "mcp-remote", "http://127.0.0.1:8765/mcp", "--header", "Authorization:${AUTODL_AUTH}"],
  "env": {"AUTODL_AUTH": "Bearer <口令>"}
}
```
客户端的工具超时各不相同（常见 60 秒）：申请机器用 `acquire` + `acquire_status` 轮询、长命令用 `background=true`，已经规避；仍建议把客户端的工具超时调大。

## 命令行
`python autodl_cli.py` 不带参数查看全部命令（login / list / machines / quota / balance / acquire / exec / put / get / renew / release / off / clone / reap / logs）。命令行直连磁盘状态，和服务共用同一份租约，可同时使用。

## 配置（config.json）
未写的键使用代码里的默认值，改后重启服务生效。

| 键 | 默认 | 说明 |
|---|---|---|
| `wait_seconds` | 300 | 没有空闲卡时最多等多久，之后改为克隆 |
| `poll_seconds` | 1 | 等待时的刷新间隔 |
| `auto_clone` | true | 等满后是否自动克隆 |
| `clone_data_disk` | true | 克隆时是否复制数据盘 |
| `max_clones_per_day` | 10 | 每天最多克隆次数（北京时间自然日） |
| `lease_ttl_minutes` | 30 | 租约空闲超时，每次操作自动顺延 |
| `max_lease_hours` | 12 | 租约硬上限，到点强制关机 |
| `guard_grace_minutes` | 5 | 机内守护比租约到期晚多久关机 |
| `reap_grace_minutes` | 10 | 无租约的自启机器多久后被回收 |
| `adopt_running` | false | 是否允许接管你自己开着的无租约机器 |
| `ssh_key` | `~/.ssh/id_ed25519` | 登录实例用的私钥路径 |
| `region_groups` | `[["west-B","west-C","west-D","west-E"]]` | 可互相克隆的地区分组（同一地区的 region_sign）；你的实例在别的地区时按需添加 |

## 注意
- 克隆只能在同一地区内（见 `region_groups`），部分宿主机不支持克隆，代码会自动换下一台。
- 接口来自控制台前端，AutoDL 改版可能失效；失效时报错，需更新 `autodl_core.py`。欢迎提 issue / PR。
- 你在网页上自己开的机器不受租约保护，也不会被回收器关掉（除非用 `autodl_acquire(prefer=…)` 接管）。
- 服务只绑定 127.0.0.1；**不要改成 0.0.0.0 暴露到网络**——拿到口令的人可以在你的机器上执行任意命令并产生费用。
- 连接实例时关闭了 SSH 主机指纹校验（AutoDL 克隆/换机后主机和端口会变），请只在可信网络中使用。
- 以下文件包含个人信息或凭据，已被 `.gitignore` 排除，**不要提交**：`.secrets/`、`state/`、`logs/`、`machines.json`、`ssh_config.autodl`。

## 许可证
[MIT](LICENSE)
