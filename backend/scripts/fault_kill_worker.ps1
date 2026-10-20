# 故障注入：worker 执行到一半被杀，任务能否恢复并最终完成
#
#   powershell -File scripts/fault_kill_worker.ps1
#
# 前置：API 以 EXECUTION_MODE=queue 启动（脚本会先检查）
# 断言：
#   1) 杀 worker 后任务不会丢，也不会卡在 running；
#   2) 租约过期后作业被回收，重新领取；
#   3) 任务最终 succeeded，attempt_count >= 2，且事件流里有 task.resumed。

param(
    [string]$ApiBase = "http://127.0.0.1:8000",
    [string]$Python = ".\venv\Scripts\python.exe",
    [int]$LeaseSeconds = 12,
    [string]$City = "西安",
    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$script:failures = @()

function Write-Step($msg) { Write-Host "`n[$(Get-Date -Format 'HH:mm:ss')] $msg" -ForegroundColor Cyan }
function Assert($condition, $message) {
    if ($condition) {
        Write-Host "  PASS  $message" -ForegroundColor Green
    } else {
        Write-Host "  FAIL  $message" -ForegroundColor Red
        $script:failures += $message
    }
}

function Get-Task($taskId) {
    $json = & curl.exe -s -m 15 "$ApiBase/api/trip/tasks/$taskId"
    return $json | ConvertFrom-Json
}

function Stop-Workers {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*app.worker*" } |
        ForEach-Object {
            Write-Host "  停止 worker pid=$($_.ProcessId)"
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

function Start-Worker($lease) {
    $env:WORKER_LEASE_SECONDS = "$lease"
    $worker = Start-Process -FilePath $Python -ArgumentList "-u", "-m", "app.worker" `
        -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput "worker-fault.log" -RedirectStandardError "worker-fault.err.log"
    Write-Host "  启动 worker pid=$($worker.Id) lease=${lease}s"
    return $worker
}

Write-Step "0. 检查 API 是否处于 queue 模式"
$healthJson = & curl.exe -s -m 15 "$ApiBase/api/trip/health"
$health = $healthJson | ConvertFrom-Json
if ($health.execution_mode -ne "queue") {
    Write-Host "  API 当前 execution_mode=$($health.execution_mode)，请用 EXECUTION_MODE=queue 重启后端" -ForegroundColor Yellow
    exit 2
}
Write-Host "  execution_mode=queue queue_backend=$($health.queue_backend)"

Write-Step "1. 清掉残留 worker，提交一个全新城市的任务"
Stop-Workers
Start-Sleep -Seconds 1

$body = @{
    city            = $City
    start_date      = "2026-10-25"
    end_date        = "2026-10-26"
    travel_days     = 2
    transportation  = "公共交通"
    accommodation   = "经济型酒店"
    preferences     = @("历史文化")
    free_text_input = "故障注入测试"
} | ConvertTo-Json -Compress

$tmp = Join-Path $env:TEMP "fault_req.json"
[System.IO.File]::WriteAllText($tmp, $body, (New-Object System.Text.UTF8Encoding($false)))
$sub = (& curl.exe -s -m 20 -X POST "$ApiBase/api/trip/plan" -H "Content-Type: application/json" --data-binary "@$tmp") | ConvertFrom-Json
$taskId = $sub.task_id
Assert ($taskId -like "task_*") "任务已提交（$taskId）"

Write-Step "2. 启动 worker，等它真正开始跑（status=running 且已有步骤）"
$worker = Start-Worker $LeaseSeconds
$started = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 1
    $t = Get-Task $taskId
    if ($t.status -eq "running" -and $t.steps.Count -ge 1) { $started = $true; break }
    if ($t.status -in @("succeeded", "failed", "canceled")) { break }
}
Assert $started "worker 已认领任务并产出至少 1 个步骤"
$stepsBeforeKill = (Get-Task $taskId).steps.Count

Write-Step "3. 强杀 worker（模拟进程崩溃）"
Stop-Workers
Start-Sleep -Seconds 2
$afterKill = Get-Task $taskId
Write-Host "  杀完之后 status=$($afterKill.status) steps=$($afterKill.steps.Count)"
Assert ($afterKill.status -eq "running") "任务没被 ack，仍处于 running（不会被静默丢弃）"

Write-Step "4. 租约过期语义检查"
Write-Host "  等待 ${LeaseSeconds}s 让租约过期（回收动作由 worker 触发，没 worker 时不会自己动，这是有意的）"
Start-Sleep -Seconds ($LeaseSeconds + 6)
$queueStats = (& curl.exe -s -m 10 "$ApiBase/api/metrics/json") | ConvertFrom-Json
Write-Host "  队列: ready=$($queueStats.queue.ready) leased=$($queueStats.queue.leased) done=$($queueStats.queue.done) dead=$($queueStats.queue.dead)"
Assert ($queueStats.queue.leased -eq 1) "租约期内作业被锁住，其他 worker 抢不到（leased=1）"
Assert ($queueStats.queue.dead -eq 0) "作业没有因为一次崩溃就进死信（dead=0）"

Write-Step "5. 重启 worker，等任务恢复完成"
$worker2 = Start-Worker 120
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$final = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $t = Get-Task $taskId
    if ($t.status -in @("succeeded", "failed", "canceled")) { $final = $t; break }
}

if ($null -eq $final) {
    Write-Host "  超时未完成" -ForegroundColor Red
    $script:failures += "任务在 $TimeoutSeconds 秒内没有结束"
} else {
    Write-Host "  最终 status=$($final.status) attempts=$($final.attempt_count) steps=$($final.steps.Count) elapsed=$($final.elapsed_ms)ms"
    Assert ($final.status -eq "succeeded") "任务最终 succeeded（不是 failed / 卡住）"
    Assert ($final.attempt_count -ge 2) "attempt_count=$($final.attempt_count) ≥ 2（说明是恢复执行）"
    Assert ($final.plan.days.Count -ge 1) "结果里真的有行程（days=$($final.plan.days.Count)）"
    Assert ($final.steps.Count -gt $stepsBeforeKill) "步骤数从 $stepsBeforeKill 增长到 $($final.steps.Count)"

    $events = & curl.exe -s -m 10 "$ApiBase/api/trip/tasks/$taskId/events"
    $resumed = ($events | Select-String -Pattern "task.resumed" | Measure-Object).Count
    Assert ($resumed -ge 1) "事件流里出现 task.resumed（可追溯的恢复记录）"
}

Write-Step "6. 收尾：停掉测试起的所有 worker"
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*app.worker*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

if ($script:failures.Count -eq 0) {
    Write-Host "`n===== 故障注入通过：worker 被杀后任务恢复并完成 =====" -ForegroundColor Green
    exit 0
} else {
    Write-Host "`n===== 失败 $($script:failures.Count) 项 =====" -ForegroundColor Red
    $script:failures | ForEach-Object { Write-Host "  - $_" }
    exit 1
}
