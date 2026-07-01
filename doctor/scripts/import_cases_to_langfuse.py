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
                    "fix_suggestion": expected.get("fix_suggestion", ""),
                    "fix_keywords": expected.get("fix_keywords", []),
                },
                metadata={
                    "bug_id": bug_id,
                    "recipe_id": bug_id,
                    "difficulty": difficulty,
                    "severity": recipe.get("severity", "medium"),
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
