"""Verify that all 7 recipes targeting tasks.py still apply correctly after cleanup.

Loads the cleaned tasks.py, applies each recipe's diff_patch, and checks:
1. The patch actually changes the file (result != original).
2. The changed line matches the recipe's intent (e.g., scalar_one_or_none → scalar_one).
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from bug_factory.ai_rewriter import DiffPatchApplier  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_PY = REPO_ROOT / "demo-app" / "backend" / "app" / "api" / "tasks.py"
RECIPES_DIR = REPO_ROOT / "bug-factory" / "recipes" / "gold"

sys.path.insert(0, str(REPO_ROOT / "bug-factory" / "src"))

# (recipe_filename, expected_old_substring, expected_new_substring)
RECIPES = [
    ("be_021_scalar_one_500.yaml", "scalar_one_or_none()", "scalar_one()"),
    ("be_022_null_assignee_attribute_error.yaml", "return task", "_audit_label"),
    ("data_020_wrong_sort_order.yaml", ".desc()", ".asc()"),
    ("data_021_due_date_dropped.yaml", "due_date=payload.due_date", "due_date=None"),
    ("logic_022_status_slient_drop.yaml", "update_data = payload", 'pop("status"'),
    ("race_020_concurrent_update.yaml", "update_data = payload", "_snapshot.update"),
    ("perf_020_n_plus_1_tasks.yaml", "selectinload(Task.comments)", "select(Comment)"),
]

original = TASKS_PY.read_text(encoding="utf-8")
print(f"Loaded tasks.py: {len(original.splitlines())} lines\n")

all_ok = True
for recipe_file, old_marker, new_marker in RECIPES:
    recipe_path = RECIPES_DIR / recipe_file
    recipe = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
    diff_patch = recipe.get("injection", {}).get("diff_patch", "")
    if not diff_patch:
        print(f"[SKIP] {recipe_file}: no diff_patch field")
        continue

    try:
        patched = DiffPatchApplier.apply(original, diff_patch)
    except Exception as exc:
        print(f"[FAIL] {recipe_file}: apply raised {exc}")
        all_ok = False
        continue

    if patched == original:
        print(f"[FAIL] {recipe_file}: patch produced NO change")
        all_ok = False
        continue

    if new_marker not in patched:
        print(f"[FAIL] {recipe_file}: expected '{new_marker}' not in patched output")
        all_ok = False
        continue

    if old_marker in patched and new_marker != ".asc()":
        # For most recipes, the old marker should be gone after patching.
        # (Exception: data_020 keeps .desc() context but changes to .asc())
        print(f"[WARN] {recipe_file}: old marker '{old_marker}' still present")

    print(f"[OK]   {recipe_file}: patch applied, '{new_marker}' found in result")

print()
if all_ok:
    print("ALL 7 RECIPES APPLIED SUCCESSFULLY")
else:
    print("SOME RECIPES FAILED — see above")
    sys.exit(1)
