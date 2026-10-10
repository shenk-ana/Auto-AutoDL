# 可选：安装 Windows 计划任务，每 5 分钟回收一次过期租约/无主机器（第二层保险，不依赖 MCP 服务是否在运行）。
# 运行：   powershell -ExecutionPolicy Bypass -File install_reaper.ps1
# 只检查：  ... -File install_reaper.ps1 -DryRun
# 卸载：   schtasks /Delete /TN AutoDLReaper /F
param([switch]$DryRun)
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$cmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $cmd) { throw "找不到 python，请先安装 Python 并加入 PATH" }
$py = $cmd.Source
Write-Host "程序: $py  参数: autodl_cli.py reap  工作目录: $dir"
if ($DryRun) { Write-Host "[DryRun] 未注册任何任务。"; return }
$act = New-ScheduledTaskAction -Execute $py -Argument "autodl_cli.py reap" -WorkingDirectory $dir
$trg = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4)
Register-ScheduledTask -TaskName "AutoDLReaper" -Action $act -Trigger $trg -Settings $set -Description "AutoDL 过期租约回收" -Force | Out-Null
Write-Host "已安装 AutoDLReaper（每 5 分钟）。日志见 $dir\logs\autodl\"
