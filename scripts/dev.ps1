# DiagDoctor 一键启动前后端开发服务器
# 用法: .\scripts\dev.ps1
# 前提: PostgreSQL 已运行，taskflow 数据库已创建

param(
    [switch]$BackendOnly,
    [switch]$FrontendOnly,
    [switch]$Init  # 首次运行时加此参数：安装依赖 + 数据库迁移
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  DiagDoctor 开发服务器启动" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ---- 首次初始化 ----
if ($Init) {
    Write-Host "[1/2] 安装后端依赖..." -ForegroundColor Yellow
    Push-Location "$root\demo-app\backend"
    uv sync
    Write-Host "[2/2] 运行数据库迁移..." -ForegroundColor Yellow
    uv run alembic upgrade head
    Pop-Location
    Write-Host "初始化完成！" -ForegroundColor Green
    Write-Host ""
}

# ---- 启动后端 ----
if (-not $FrontendOnly) {
    Write-Host "启动后端: http://localhost:8000" -ForegroundColor Green
    Write-Host "  API 文档: http://localhost:8000/docs" -ForegroundColor Gray
    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-Command",
        "Set-Location '$root\demo-app\backend'; Write-Host '=== TaskFlow Backend ===' -ForegroundColor Cyan; uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"
    )
}

# ---- 启动前端 ----
if (-not $BackendOnly) {
    Write-Host "启动前端: http://localhost:5173" -ForegroundColor Green
    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-Command",
        "Set-Location '$root\demo-app\frontend'; Write-Host '=== TaskFlow Frontend ===' -ForegroundColor Cyan; pnpm dev"
    )
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
if (-not $FrontendOnly) { Write-Host "  后端:  http://localhost:8000/docs" -ForegroundColor White }
if (-not $BackendOnly) { Write-Host "  前端:  http://localhost:5173" -ForegroundColor White }
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "按任意键关闭此窗口..." -ForegroundColor Gray
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
