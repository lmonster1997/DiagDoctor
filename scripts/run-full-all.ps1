<#
.SYNOPSIS
    DiagDoctor Bug Factory — 一键批量跑通所有配方的 full 流程
.DESCRIPTION
    对所有 bug-factory/recipes/ 下的配方依次执行:
        1. inject   (AI 改写代码注入 bug)
        2. trigger  (对运行中的 demo-app 触发 bug)
        3. collect  (从 Loki/Tempo 收集日志和 trace)
        4. generate (生成 benchmark/cases/{id}.yaml)

    前置条件:
        - demo-app 后端已启动 (http://localhost:8000)
        - Loki + Tempo 已启动 (Docker Compose)
        - OPENAI_API_KEY 环境变量已设置

.PARAMETER RecipeIds
    指定要跑的配方 ID 列表，逗号分隔。不传则跑全部。

.PARAMETER SkipInject
    跳过注入步骤 (bug 已经在分支上)。

.PARAMETER SkipTrigger
    跳过触发步骤 (bug 已经触发过)。

.PARAMETER BaseUrl
    demo-app 后端地址，默认 http://localhost:8000。

.EXAMPLE
    .\scripts\run-full-all.ps1
    跑全部 15 个配方的完整流程。

.EXAMPLE
    .\scripts\run-full-all.ps1 -RecipeIds "BE-001,FE-001,PERF-001"
    只跑指定的 3 个。

.EXAMPLE
    .\scripts\run-full-all.ps1 -SkipInject
    只 trigger + collect + generate（bug 已注入）。
#>

param(
    [string]$RecipeIds = "",
    [switch]$SkipInject,
    [switch]$SkipTrigger,
    [string]$BaseUrl = "http://localhost:8000"
)

$ErrorActionPreference = "Continue"
$root = $PSScriptRoot | Split-Path -Parent

# ── 激活虚拟环境 ────────────────────────────────────────────────
$venvActivate = Join-Path $root ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    Write-Host "[env] 激活虚拟环境: $venvActivate" -ForegroundColor Gray
    . $venvActivate
} else {
    Write-Host "[warn] 未找到 .venv，使用系统 Python" -ForegroundColor Yellow
}

# ── 收集配方列表 ────────────────────────────────────────────────
$recipesDir = Join-Path $root "bug-factory\recipes"

if ($RecipeIds) {
    $ids = $RecipeIds -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ }
} else {
    # 自动发现所有 .yaml 配方文件，按文件名排序
    $ids = Get-ChildItem -Path $recipesDir -Filter "*.yaml" -Exclude ".gitkeep" |
        Sort-Object Name |
        ForEach-Object { $_.BaseName } |
        ForEach-Object {
            # 从文件名提取 ID: be_001_n_plus_1 → BE-001
            if ($_ -match "^([a-z]+)_(\d{3})_") {
                "$($matches[1].ToUpper())-$($matches[2])"
            }
        }
}

if (-not $ids -or $ids.Count -eq 0) {
    Write-Host "[error] 没有找到任何配方文件！" -ForegroundColor Red
    exit 1
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  DiagDoctor Bug Factory — 批量全流程" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "配方数量 : $($ids.Count)" -ForegroundColor White
Write-Host "后端地址 : $BaseUrl" -ForegroundColor White
Write-Host "跳过注入 : $SkipInject" -ForegroundColor $(if ($SkipInject) { "Yellow" } else { "Gray" })
Write-Host "跳过触发 : $SkipTrigger" -ForegroundColor $(if ($SkipTrigger) { "Yellow" } else { "Gray" })
Write-Host ""

# ── 检查前置条件 ────────────────────────────────────────────────
Write-Host "── 检查前置条件 ──" -ForegroundColor Cyan

# 检查 demo-app 后端是否可达
try {
    $health = Invoke-RestMethod -Uri "$BaseUrl/health" -Method Get -TimeoutSec 5 -ErrorAction Stop
    Write-Host "  ✓ demo-app 后端健康: $($health.status)" -ForegroundColor Green
} catch {
    Write-Host "  ✗ demo-app 后端不可达 ($BaseUrl)，请先启动！" -ForegroundColor Red
    Write-Host "    提示: docker compose up -d 或 .\scripts\dev.ps1" -ForegroundColor Yellow
    exit 1
}

# 检查 LLM API Key
if (-not $env:OPENAI_API_KEY) {
    Write-Host "  ⚠ OPENAI_API_KEY 未设置，AI 改写和 user_report 生成可能失败" -ForegroundColor Yellow
}

# ── 先校验所有配方 ──────────────────────────────────────────────
Write-Host ""
Write-Host "── 校验配方 ──" -ForegroundColor Cyan
$validateResult = uv run python -m bug_factory.cli validate 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host $validateResult -ForegroundColor Red
    Write-Host "  ✗ 配方校验失败，请修复后再跑" -ForegroundColor Red
    exit 1
}
Write-Host "  ✓ 所有配方校验通过" -ForegroundColor Green

# ── 构建 full 命令参数 ──────────────────────────────────────────
$extraArgs = @()
if ($SkipInject) { $extraArgs += "--skip-inject" }
if ($SkipTrigger) { $extraArgs += "--skip-trigger" }
if ($BaseUrl -ne "http://localhost:8000") { $extraArgs += "--base-url"; $extraArgs += $BaseUrl }

# ── 逐个跑 ──────────────────────────────────────────────────────
$total = $ids.Count
$passed = 0
$failed = 0
$failedList = @()
$startTime = Get-Date

Write-Host ""
Write-Host "── 开始批量执行 ($total 个配方) ──" -ForegroundColor Cyan
Write-Host ""

for ($i = 0; $i -lt $total; $i++) {
    $id = $ids[$i]
    $num = $i + 1
    $header = "[$num/$total] $id"
    
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor DarkGray
    Write-Host "$header 开始..." -ForegroundColor Yellow
    
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    
    # 执行 full 命令
    $cmdArgs = @("-m", "bug_factory.cli", "full", $id) + $extraArgs
    $output = uv run python @cmdArgs 2>&1
    $exitCode = $LASTEXITCODE
    
    $sw.Stop()
    $elapsed = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    
    if ($exitCode -eq 0) {
        Write-Host "$header ✓ 成功 ($($elapsed)s)" -ForegroundColor Green
        $passed++
    } else {
        Write-Host "$header ✗ 失败 ($($elapsed)s)" -ForegroundColor Red
        # 打印最后 20 行输出帮助排错
        $lastLines = $output | Select-Object -Last 20
        foreach ($line in $lastLines) {
            Write-Host "  $line" -ForegroundColor DarkRed
        }
        $failed++
        $failedList += $id
    }
    Write-Host ""
}

# ── 汇总 ────────────────────────────────────────────────────────
$totalElapsed = [math]::Round(((Get-Date) - $startTime).TotalMinutes, 1)

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  批量全流程完成！" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "总计     : $total 个" -ForegroundColor White
Write-Host "成功     : $passed 个" -ForegroundColor Green
Write-Host "失败     : $failed 个" -ForegroundColor $(if ($failed -gt 0) { "Red" } else { "White" })
Write-Host "总耗时   : $totalElapsed 分钟" -ForegroundColor White
Write-Host ""

if ($failedList.Count -gt 0) {
    Write-Host "失败列表:" -ForegroundColor Red
    foreach ($fid in $failedList) {
        Write-Host "  ✗ $fid" -ForegroundColor Red
    }
    Write-Host ""
    Write-Host "重跑失败配方:" -ForegroundColor Yellow
    $retryIds = $failedList -join ","
    Write-Host "  .\scripts\run-full-all.ps1 -RecipeIds '$retryIds'" -ForegroundColor Gray
}

Write-Host "生成案例位置: benchmark\cases\" -ForegroundColor Gray
Write-Host "证据数据位置: output\{id}\evidence\" -ForegroundColor Gray
Write-Host ""

# 检查 benchmark/cases 下的生成结果
$casesDir = Join-Path $root "benchmark\cases"
if (Test-Path $casesDir) {
    $caseFiles = Get-ChildItem -Path $casesDir -Filter "*.yaml" | Measure-Object
    Write-Host "benchmark/cases/ 现有 $($caseFiles.Count) 个案例文件" -ForegroundColor Cyan
}

exit $(if ($failed -gt 0) { 1 } else { 0 })
