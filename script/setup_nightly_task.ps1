# ============================================================
# Windows 任务计划程序 配置脚本
# 创建每晚23:00自动执行 server_nightly.sh 的计划任务
#
# 用法（PowerShell 管理员权限）:
#   powershell -ExecutionPolicy Bypass -File script/setup_nightly_task.ps1
#   powershell -ExecutionPolicy Bypass -File script/setup_nightly_task.ps1 -Hour 23 -Minute 0
#
# 查看任务状态:
#   schtasks /query /tn "b_quant_nightly" /v
#
# 手动触发:
#   schtasks /run /tn "b_quant_nightly"
#
# 删除任务:
#   schtasks /delete /tn "b_quant_nightly" /f
# ============================================================

param(
    [int]$Hour = 23,
    [int]$Minute = 0,
    [string]$TaskName = "b_quant_nightly",
    [string]$Weekdays = "MON-FRI"   # 周一至周五（交易日）
)

# ======== 自动检测项目根目录 ========
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjRoot = Split-Path -Parent $ScriptDir

Write-Host "============================================"
Write-Host "  b_quant 夜间任务计划配置"
Write-Host "============================================"
Write-Host "  项目目录: $ProjRoot"
Write-Host "  执行时间: 每天 ${Hour}:$(($Minute).ToString('00'))"
Write-Host "  交易日:   $Weekdays"
Write-Host "  任务名称: $TaskName"
Write-Host "============================================"

# ======== 检查 Git Bash ========
$GitBash = "C:\Program Files\Git\bin\bash.exe"
if (-not (Test-Path $GitBash)) {
    $GitBash = "C:\Program Files (x86)\Git\bin\bash.exe"
}
if (-not (Test-Path $GitBash)) {
    # 尝试从环境变量找
    $GitBash = (Get-Command bash.exe -ErrorAction SilentlyContinue).Source
}
if (-not $GitBash) {
    Write-Host "[ERROR] 找不到 Git Bash (bash.exe)，请先安装 Git for Windows"
    Write-Host "  下载: https://git-scm.com/download/win"
    exit 1
}
Write-Host "  Git Bash:  $GitBash"

# ======== 脚本路径 ========
$NightlyScript = Join-Path $ProjRoot "script\server_nightly.sh"
$BashScript = $NightlyScript -replace '\\', '/'   # Git Bash 用正斜杠

# ======== 删除旧任务 ========
$existing = schtasks /query /tn $TaskName 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "  删除已有任务: $TaskName"
    schtasks /delete /tn $TaskName /f 2>$null | Out-Null
}

# ======== 创建计划任务 ========
# schtasks 命令: 每周一到周五 23:00 执行
# /IT 允许交互（可以看到窗口）
$action = "`"$GitBash`" -l -c `"cd '$BashScript/..' && bash '$BashScript'`""

$createArgs = @(
    "/create",
    "/tn", $TaskName,
    "/tr", $action,
    "/sc", "WEEKLY",
    "/d", $Weekdays,
    "/st", "$(($Hour).ToString('00')):$((($Minute)).ToString('00'))",
    "/f",                          # 强制覆盖
    "/rl", "LIMITED"               # 普通用户权限即可
)

Write-Host ""
Write-Host "  创建计划任务..."
$result = schtasks @createArgs 2>&1
Write-Host "  $result"

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "============================================"
    Write-Host "  [OK] 任务 '$TaskName' 已创建"
    Write-Host "  时间: 每 $Weekdays ${Hour}:$(($Minute).ToString('00'))"
    Write-Host ""
    Write-Host "  管理命令:"
    Write-Host "    查看: schtasks /query /tn '$TaskName' /v"
    Write-Host "    手动运行: schtasks /run /tn '$TaskName'"
    Write-Host "    删除: schtasks /delete /tn '$TaskName' /f"
    Write-Host "============================================"
} else {
    Write-Host ""
    Write-Host "[ERROR] 任务创建失败，请以管理员身份运行此脚本"
    exit 1
}
