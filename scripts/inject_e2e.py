#!/usr/bin/env python3
"""
inject_e2e.py — 端到端工作流调试：注入 Bug → 走前端触发 → 生成 buginfo 给 CopilotChat。

默认依赖已有的 uvicorn --reload 后端（如 VS Code Task）：修改文件后等待 reload
自动生效，触发完成后**保留注入的 bug**（方便 Doctor 直接诊断带 bug 的代码）。
用 --restore 可在触发后自动还原代码。如果后端未运行则自动启动。

与 eval_agent.py 的分工：
  - eval_agent.py：批量自动化评测后端 agent 诊断质量（调 Doctor API + Langfuse 打分）
  - inject_e2e.py：单 case 端到端工作流调试（注入 + 触发 + 生成 buginfo，人工接续诊断）

用法:
    uv run python scripts/inject_e2e.py BE-020
    uv run python scripts/inject_e2e.py BE-020 --restore        # 触发后自动还原代码
    uv run python scripts/inject_e2e.py BE-020 --no-reload       # 自己管理后端生命周期
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_WORKSPACE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_WORKSPACE / "bug-factory" / "src"))

from bug_factory.schema import TriggerResult, load_recipe
from bug_factory.trigger import TriggerRunner
from bug_factory.ai_rewriter import DiffPatchApplier

DEMO_BACKEND_DIR = _WORKSPACE / "demo-app" / "backend"
BACKEND_PORT = 8000

# 默认模式：依赖已有的 uvicorn --reload 后端（VS Code Task 启动）。
# 修改文件后等待 reload 自动生效，无需自己管理进程生命周期。
# --no-reload 可切回旧行为：kill → patch → start → trigger → kill → restore → restart。


def _health_ok(base_url: str) -> bool:
    """单次探测 /health，返回 True 如果 200。"""
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=2) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _kill_backend_on_port(port: int) -> None:
    """杀掉监听指定端口的进程（跨平台）。"""
    if sys.platform == "win32":
        subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"Get-NetTCPConnection -LocalPort {port} -State Listen "
                f"-ErrorAction SilentlyContinue | "
                f"Select-Object -ExpandProperty OwningProcess | "
                f"ForEach-Object {{ Stop-Process -Id $_ -Force "
                f"-ErrorAction SilentlyContinue }}",
            ],
            capture_output=True,
            timeout=15,
        )
    else:
        subprocess.run(
            ["bash", "-c", f"lsof -ti:{port} | xargs kill -9 2>/dev/null || true"],
            capture_output=True,
            timeout=15,
        )


def _start_backend(reload: bool = False) -> subprocess.Popen:
    """启动 uvicorn 作为后台进程。返回 Popen 对象。"""
    cmd = [
        "uv", "run", "uvicorn", "app.main:app",
        "--host", "127.0.0.1", "--port", str(BACKEND_PORT),
    ]
    if reload:
        cmd.append("--reload")

    # 在新进程组中启动，便于后续清理
    kwargs: dict = {
        "cwd": str(DEMO_BACKEND_DIR),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    return subprocess.Popen(cmd, **kwargs)


def _wait_for_backend(base_url: str, max_wait: int = 40) -> bool:
    """轮询 /health 等待 backend 就绪。"""
    for i in range(max_wait):
        if _health_ok(base_url):
            return True
        time.sleep(1)
    return False


def _find_recipe(recipe_id: str) -> Path:
    prefix = recipe_id.lower().replace("-", "_")
    gold = _WORKSPACE / "bug-factory" / "recipes" / "gold"
    candidates = sorted(p for p in gold.rglob(f"{prefix}*.yaml"))
    if not candidates:
        print(f"❌ 找不到配方: {recipe_id}")
        sys.exit(1)
    return candidates[0]


def _extract_trace_ids(result: TriggerResult) -> list[str]:
    ids: list[str] = []
    if hasattr(result, "trace_ids") and result.trace_ids:
        ids.extend(result.trace_ids)
    for err in (result.browser_errors or []):
        if err.trace_id and err.trace_id not in ids:
            ids.append(err.trace_id)
    return ids


def _restore(target: Path, original: str | None) -> None:
    if original is None:
        return
    print("\n🔄 恢复原始代码...")
    target.write_text(original, encoding="utf-8")
    print(f"   ✅ 已还原: {target.relative_to(_WORKSPACE)}")


def _wait_for_reload(base_url: str, wait_seconds: float = 5.0) -> None:
    """等待 uvicorn --reload 检测文件变更并完成重启。"""
    print(f"   ⏳ 等待 uvicorn reload（{wait_seconds:.0f}s）...")
    time.sleep(wait_seconds)
    if _health_ok(base_url):
        print("   ✅ Backend 已就绪")
    else:
        print("   ⚠️ Backend 尚未就绪，继续执行（可能需要手动检查）")


async def main(recipe_id: str, restore: bool = False, frontend_url: str | None = None, no_reload: bool = False):
    base_url = os.getenv("DEMO_APP_URL", "http://localhost:8000")

    yaml_path = _find_recipe(recipe_id)
    recipe = load_recipe(yaml_path)
    print(f"\n{'='*60}")
    print(f"  Bug: {recipe.id} — {recipe.title}")
    print(f"  分类: {recipe.category} | 严重度: {recipe.severity}")
    print(f"{'='*60}")

    target_file = _WORKSPACE / recipe.injection.target_file
    original_content: str | None = None
    backend_started = False

    try:
        if not recipe.injection.diff_patch:
            print("\n❌ 此配方没有 diff_patch，无法直接注入（需要 AI 改写 + OPENAI_API_KEY）")
            sys.exit(1)

        # ── Step 1: 确保 backend 在运行 + 注入 Bug ──────────────────
        if no_reload:
            # 旧行为：自己管理生命周期
            print("\n🔧 注入 Bug（no-reload 模式）...")
            print(f"   停止现有 demo backend (port {BACKEND_PORT})...")
            _kill_backend_on_port(BACKEND_PORT)
            time.sleep(2)

            original_content = target_file.read_text(encoding="utf-8")
            patched = DiffPatchApplier.apply(original_content, recipe.injection.diff_patch)
            target_file.write_text(patched, encoding="utf-8")
            print(f"   ✅ 已修改: {recipe.injection.target_file}")

            print(f"\n🚀 启动 demo backend（新代码）...")
            _start_backend(reload=False)
            backend_started = True
            if not _wait_for_backend(base_url):
                print(f"   ❌ Demo backend 未在 40s 内就绪: {base_url}")
                sys.exit(1)
            print("   ✅ Demo backend 已就绪（新代码已加载）")
        else:
            # 默认：依赖已有 uvicorn --reload，改文件等 reload 即可
            if not _health_ok(base_url):
                print(f"\n🚀 Demo backend 未运行，启动中（--reload 模式）...")
                _start_backend(reload=True)
                backend_started = True
                if not _wait_for_backend(base_url):
                    print(f"   ❌ Demo backend 未在 40s 内就绪: {base_url}")
                    sys.exit(1)
                print("   ✅ Demo backend 已启动（--reload 模式）")
            else:
                print(f"\n✅ Demo backend 已在运行（复用现有 --reload 进程）")

            print("\n🔧 注入 Bug（reload 模式）...")
            original_content = target_file.read_text(encoding="utf-8")
            patched = DiffPatchApplier.apply(original_content, recipe.injection.diff_patch)
            target_file.write_text(patched, encoding="utf-8")
            print(f"   ✅ 已修改: {recipe.injection.target_file}")

            _wait_for_reload(base_url)

        # ── Step 2: Trigger ────────────────────────────────────
        print("\n🚀 触发 Bug...")
        trigger_start = datetime.now(timezone.utc)
        runner = TriggerRunner(demo_app_base_url=base_url, frontend_url=frontend_url)
        trigger_result = await runner.run(recipe.trigger)

        if not trigger_result.success:
            print(f"   ❌ 触发失败: {trigger_result.error}")
            sys.exit(1)

        trace_ids = _extract_trace_ids(trigger_result)
        print(f"   ✅ 触发成功 ({len(trigger_result.steps)} 步)")
        if trace_ids:
            print(f"   🔗 Trace ID: {', '.join(trace_ids)}")

        # ── Prompt for CopilotChat ─────────────────────────────
        trigger_iso = trigger_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        trace_str = ", ".join(trace_ids) if trace_ids else "（无）"
        print(f"\n{'='*60}")
        print(f"📋 复制以下内容粘贴到 DiagDoctor CopilotChat:")
        print(f"{'='*60}")
        print(f"""
请帮我诊断这个 Bug：

【错误现象】
{recipe.title}

【触发时间（UTC）】
{trigger_iso}

【Trace ID】
{trace_str}

【补充信息】
- Bug 分类: {recipe.category}
- 注入文件: {recipe.injection.target_file}
""")
        print(f"{'='*60}")

    finally:
        # ── 清理：可选还原 + 进程管理 ──────────────────────────
        if no_reload and backend_started:
            print("\n🛑 停止测试 backend...")
            _kill_backend_on_port(BACKEND_PORT)
            time.sleep(2)

        if restore and original_content is not None:
            _restore(target_file, original_content)
            if not no_reload:
                # reload 模式下，还原代码后也要等 uvicorn 重新加载
                _wait_for_reload(base_url, wait_seconds=3.0)

        if no_reload:
            # no-reload 模式：重启 backend（--reload，方便后续开发）
            print(f"\n🔄 重启 demo backend（--reload 模式）...")
            _start_backend(reload=True)
            time.sleep(3)
            if _health_ok(base_url):
                print("   ✅ Demo backend 已重启（开发模式）")
            else:
                print("   ⚠️ Demo backend 重启中，请稍后手动检查")
        else:
            if restore:
                print("\n✅ 代码已还原，backend 已通过 reload 恢复")
            else:
                print(f"\n💡 Bug 代码已保留在 {target_file.relative_to(_WORKSPACE)}，可直接供 Doctor 诊断")
                print("   诊断完成后运行 git checkout 或 --restore 还原")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="E2E workflow: inject bug, trigger, generate buginfo for CopilotChat")
    parser.add_argument("recipe_id", help="Recipe ID, e.g. FE-020")
    parser.add_argument("--restore", action="store_true", help="触发后自动还原代码（默认保留注入的 bug）")
    parser.add_argument("--no-reload", action="store_true", help="自己管理后端生命周期（kill→patch→start→trigger→kill→restore）")
    parser.add_argument("--frontend", help="Demo frontend URL (for Playwright triggers)")
    args = parser.parse_args()
    asyncio.run(main(args.recipe_id, args.restore, args.frontend, args.no_reload))
