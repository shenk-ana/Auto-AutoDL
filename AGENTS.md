# Agent 规则

## 项目与结构
Auto-AutoDL：让 AI 通过 MCP 统一申请、使用、释放 AutoDL GPU 实例的本地常驻服务（租约、保险关机、操作日志）。
所有源码在仓库根目录，没有子包：

| 文件 | 内容 |
|---|---|
| `autodl_core.py` | 原语层：登录态、AutoDL 控制台内部接口、开关机、克隆、SSH/SCP、跨进程锁、机器登录信息登记 |
| `autodl_mgr.py` | 调度层：租约、统一分配、机内守护脚本、回收器、后台任务 |
| `mcp_server.py` | MCP 工具定义；`--http` 常驻服务（Bearer 口令）或 stdio |
| `autodl_cli.py` | 命令行入口，与 MCP 共用同一套逻辑和磁盘状态 |
| `oplog.py` | 只追加的操作日志（`logs/autodl/`、`logs/linux/`） |
| `install_service.ps1` / `install_reaper.ps1` | Windows 计划任务安装脚本（UTF-8 BOM + CRLF，兼容 Windows PowerShell 5.1，修改时保持该编码） |
| `config.json` | 用户配置，覆盖 `autodl_core.DEFAULT_CONFIG` |

文档：`README.md`（中文，主）、`README.en.md`（英文）。改功能或配置键时两份同步更新。

## 开发与验证
- 安装依赖：`pip install -r requirements.txt`（Python 3.10+）
- 尚无自动化测试。最低验证：`python -c "import mcp_server, autodl_cli"` 能导入；`python -m pyflakes .` 无新增问题（如已安装）。
- 涉及 AutoDL 接口、开关机、克隆的改动会产生真实费用，只能由用户在自己的账号上手动验证；不要声称“已在真实环境验证”除非确实做过。
- `install_*.ps1` 可用 `-DryRun` 检查而不改系统。

## 约束
- 不写入、不提交任何凭据或个人数据：`.secrets/`、`state/`、`logs/`、`machines.json`、`ssh_config.autodl` 均已在 `.gitignore`，保持如此。
- 返回给 AI 的实例字段走 `PUBLIC_FIELDS` 白名单，不得加入 `root_password`、`jupyter_token` 等；日志不得记录 token / 密码。
- HTTP 服务默认只绑定 `127.0.0.1`，不要改默认值。
- 已有 Agent 规则先读后增量修改，不覆盖用户规则。

## 代码审查
AI 代码审查的标题、findings、总结和建议统一中文，代码、路径、协议名可原样。
合并前检查行内评论及 review commit，逐项复现，记录修正和误报依据。
不主动召唤审查 bot，发召唤评论前先询问用户。
