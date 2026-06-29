# Run all 15 gold standard recipes serially
$ErrorActionPreference = "Continue"
$root = "d:\Work\LearnAI\DiagDoctor"

$recipes = @(
    "BE-020",
    "BE-021", 
    "BE-022",
    "CASCADE-020",
    "CONFIG-020",
    "DATA-020",
    "DATA-021",
    "FE-020",
    "FE-021",
    "LOGIC-020",
    "LOGIC-021",
    "LOGIC-022",
    "PERF-020",
    "PERF-021",
    "RACE-020"
)

$total = $recipes.Count
$passed = 0
$failed = 0
$startTime = Get-Date

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  金标准菜谱串行执行 ($total 个)" -ForegroundColor Cyan
Write-Host "  开始时间: $startTime" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

for ($i = 0; $i -lt $total; $i++) {
    $recipe = $recipes[$i]
    $num = $i + 1
    $recipeStart = Get-Date
    
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Yellow
    Write-Host "  [$num/$total] 执行: $recipe" -ForegroundColor Yellow
    Write-Host "  开始: $recipeStart" -ForegroundColor Yellow
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Yellow
    
    Push-Location $root\bug-factory
    try {
        uv run python -m bug_factory.cli full $recipe --clear-loki
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  [✓] $recipe 完成" -ForegroundColor Green
            $passed++
        } else {
            Write-Host "  [✗] $recipe 失败 (exit code: $LASTEXITCODE)" -ForegroundColor Red
            $failed++
        }
    } catch {
        Write-Host "  [✗] $recipe 异常: $_" -ForegroundColor Red
        $failed++
    }
    Pop-Location
    
    $recipeEnd = Get-Date
    $elapsed = ($recipeEnd - $recipeStart).TotalSeconds
    Write-Host "  耗时: $([math]::Round($elapsed, 1))s" -ForegroundColor Gray
    Write-Host ""
}

$endTime = Get-Date
$totalElapsed = ($endTime - $startTime).TotalMinutes

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  执行完毕" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  通过: $passed / $total" -ForegroundColor Green
if ($failed -gt 0) {
    Write-Host "  失败: $failed / $total" -ForegroundColor Red
}
Write-Host "  总耗时: $([math]::Round($totalElapsed, 1)) 分钟" -ForegroundColor Cyan
Write-Host "  结束时间: $endTime" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
