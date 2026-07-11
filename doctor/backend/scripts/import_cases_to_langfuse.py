"""将 bug-factory/recipes/gold/*.yaml 直接导入 Langfuse Dataset。

配方是唯一权威源 —— 包含 title（即 user_report）+ expected_diagnosis（标准答案）。
不需要经过 output/*/case.yaml 中间产物，一跳直达。

用法：
    cd doctor && uv run python scripts/import_cases_to_langfuse.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from langfuse import Langfuse

# 添加项目根目录到 Python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import settings  # noqa: E402

# ── 常量 ──────────────────────────────────────────────────────────
RECIPES_DIR = PROJECT_ROOT.parent / "bug-factory" / "recipes" / "gold"
DATASET_NAME = "diagdoctor-benchmark"

# smoke 子集：4 个代表性 case，覆盖主要类别 + 1 个 smokeless 类。
# 与 run_baseline_experiment.py 的 SMOKE_CASES 保持一致。
# 用途：大改后第一次 sanity check（~5min），只回答「有没有彻底崩」。
SMOKE_CASES: set[str] = {"BE-020", "FE-020", "PERF-020", "LOGIC-020"}

# train 子集：8 个 case，覆盖 6/6 类别 + 完整难度梯度（L2/L3/L4，无 L1）。
# 用途：每个 harness 机制实现完跑一次（~40min），验证机制在各类别上不退步 / 有效。
# 选 case 的标准：
#   - 覆盖全部 6 个类别（backend_error / frontend_crash / performance / logic / data / config）
#   - 排除 L1（agent 不需要 harness 帮助就能解，测不出机制效果）
#   - 含 red-herring（测 harness 不会被误导信号带偏）
#   - 含 silent-drop / data-loss（测「无 error log 时 harness 怎么帮 agent」）
#   - 含 smokeless（测「observability 拿不到证据时」的推理上限）
#   - 含 pair 关系（同模式不同层 / 同模式不同接口，验证机制是「学到模式」而非「记住 case」）
TRAIN_CASES: set[str] = {
    "BE-022",      # L3 backend_error,  red-herring + null-deref
    "FE-021",      # L2 frontend_crash, red-herring + null-deref (与 BE-022 配对，同模式不同层)
    "PERF-021",    # L2 performance,    N+1 projects (与 PERF-020 配对，验证同模式泛化)
    "LOGIC-021",   # L3 logic,          red-herring + data-leak
    "LOGIC-022",   # L2 logic,          silent-drop (无 error log)
    "DATA-021",    # L2 data,           data-loss (无 error log)
    "CONFIG-020",  # L3 config,         misleading-default (根因不在业务代码层)
    "RACE-020",    # L4 logic,          smokeless (无 observability 证据，需推理)
}

# 其余 3 个 (BE-021 / DATA-020 / CASCADE-020) 标记为 blind，
# 留作 Iteration 0 baseline / 决策点 ablation / 面试叙事用，日常 iteration 不跑。
# 三层节奏：smoke (4, ~5min) → train (8, ~40min) → all (15, ~75min)

# ── 主逻辑 ──────────────────────────────────────────────────────────


def main() -> None:
    langfuse = Langfuse(
        secret_key=settings.langfuse_secret_key,
        public_key=settings.langfuse_public_key,
        host=settings.langfuse_host,
    )

    # 创建 Dataset（幂等：如已存在则跳过）
    langfuse.create_dataset(name=DATASET_NAME)
    print(f"Dataset: {DATASET_NAME}")

    recipe_files = sorted(RECIPES_DIR.glob("*.yaml"))
    print(f"找到 {len(recipe_files)} 个配方文件\n")

    imported = 0
    skipped = 0

    for recipe_file in recipe_files:
        recipe = yaml.safe_load(recipe_file.read_text(encoding="utf-8"))
        bug_id = recipe["id"]
        expected = recipe["expected_diagnosis"]

        # title 就是 user_report（中文、用户口吻、描述现象而非根因）
        user_report = recipe["title"]

        # categories: 用 recipe 中的 categories 列表，fallback 到单个 category
        categories = recipe.get("categories", [recipe["category"]])

        # difficulty 从 tags 中提取（如 "difficulty:L1"）
        tags = recipe.get("tags", [])
        difficulty = "L2"
        for tag in tags:
            if tag.startswith("difficulty:"):
                difficulty = tag.split(":", 1)[1]
                break

        try:
            # Use id=bug_id for upsert: same id → update, new id → create.
            # This prevents duplicates when re-importing.
            langfuse.create_dataset_item(
                id=bug_id,
                dataset_name=DATASET_NAME,
                input={
                    "user_report": user_report,
                },
                expected_output={
                    "primary_category": recipe[
                        "category"
                    ],  # single primary category for binary match
                    "category": categories,
                    "root_cause": expected.get("root_cause", ""),
                    "affected_file": expected.get("affected_file", ""),
                    "affected_line": expected.get("affected_line"),
                    "fix_suggestion": expected.get("fix_suggestion", ""),
                    "fix_keywords": expected.get("fix_keywords", []),
                },
                metadata={
                    "bug_id": bug_id,
                    "recipe_id": bug_id,
                    "difficulty": difficulty,
                    "severity": recipe.get("severity", "medium"),
                    "split": (
                        "smoke"
                        if bug_id in SMOKE_CASES
                        else "train"
                        if bug_id in TRAIN_CASES
                        else "blind"
                    ),
                },
            )
            print(f"  ✓ {bug_id}: {user_report[:50]}...")
            imported += 1
        except Exception as exc:
            err_msg = str(exc)[:200]
            if "already exists" in err_msg.lower() or "duplicate" in err_msg.lower():
                print(f"  ⏭ {bug_id}: 已存在，跳过")
                skipped += 1
            else:
                print(f"  ✗ {bug_id}: {err_msg}")
                skipped += 1

    print(f"\n{'=' * 50}")
    print(f"导入完成: {imported} 新增, {skipped} 跳过")
    print(f"查看: Langfuse Dashboard → Datasets → {DATASET_NAME}")


if __name__ == "__main__":
    main()
