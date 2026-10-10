# 把 AutoDL MCP 装成"登录后自动常驻"的本机服务（计划任务，崩溃自动重启，无窗口）。
# 运行：   powershell -ExecutionPolicy Bypass -File install_service.ps1
# 只检查：  ... -File install_service.ps1 -DryRun     （只打印将要注册的内容，不改系统）
# 卸载：   schtasks /Delete /TN AutoDLMcpService /F
# 手动前台： python mcp_server.py --http --port 8765
param([int]$Port = 8765, [switch]$DryRun)
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$cmd = Get-Command pythonw -ErrorAction SilentlyContinue
if (-not $cmd) { $cmd = Get-Command python -ErrorAction SilentlyContinue }   # 没有 pythonw 就用 python（会有控制台窗口）
if (-not $cmd) { throw "找不到 python / pythonw，请先安装 Python 并加入 PATH" }
$py = $cmd.Source
if (-not (Test-Path (Join-Path $dir "mcp_server.py"))) { throw "找不到 $dir\mcp_server.py" }
$args2 = "mcp_server.py --http --port $Port"
Write-Host "程序:     $py"
Write-Host "参数:     $args2"
Write-Host "工作目录: $dir"
if ($DryRun) { Write-Host "[DryRun] 未注册任何任务。"; return }
$act = New-ScheduledTaskAction -Execute $py -Argument $args2 -WorkingDirectory $dir
$trg = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "AutoDLMcpService" -Action $act -Trigger $trg -Settings $set -Description "AutoDL MCP 常驻服务（127.0.0.1:$Port）" -Force | Out-Null
Start-ScheduledTask -TaskName "AutoDLMcpService"
Write-Host "已安装并启动 AutoDLMcpService（登录后自动运行，只监听 127.0.0.1:$Port）。日志：$dir\logs\autodl\"
Write-Host "访问口令在 $dir\.secrets\service_token（首次启动自动生成），客户端配置见 README.md。"
