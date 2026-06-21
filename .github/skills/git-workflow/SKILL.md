---
name: git-workflow
description: >-
  **提交工作流** — 自动从 main 创建新分支、切换分支、生成 PR 内容。
  触发词：提交 / 创建分支 / 生成PR / PR内容 / 准备提交 / pre-commit / git workflow。
  USE FOR: 代码改完后需要规范化提交流程时。
  DO NOT USE FOR: 日常 git 操作（commit/push/pull 直接用终端）。
---

# Git 提交流程自动化

当用户说 **"提交"** / **"创建分支"** / **"准备PR"** / **"pre-commit"** 时，执行以下标准流程。

---

## 流程（严格按顺序）

### Step 1：检测变更

```bash
git branch --show-current          # 当前分支
git status --short                 # 未提交变更
git diff --stat main..HEAD         # 相对 main 的差异
git log --oneline main..HEAD       # 待合入的 commits
```

如果当前已经在 `main` 上且没有 commits，跳到 Step 3。
如果已有分支且已 commit，跳到 Step 5（只生成 PR）。

### Step 2：跑 CI 检查（🔴 必须全部通过才能继续，不可跳过）

> **这是整个流程最关键的步骤。CI 失败 = 阻断提交，没有任何例外（含文档改动）。**
> 你提交的代码在远程 CI 上也必须通过——本地先跑一遍，不要把失败留给下次 CI Run。

```bash
# 1. 格式 + Lint
uv run ruff format --check
uv run ruff check

# 2. 类型检查（按 CI 方式：每个包从自身目录运行，避免 "Duplicate module named src"）
cd doctor       && uv run mypy --strict src && cd ..
cd bug-factory  && uv run mypy --strict src && cd ..
cd benchmark    && uv run mypy --strict src && cd ..
uv run mypy --strict demo-app/backend/app

# 3. 单元测试（全量）
uv run pytest --tb=short
```

**执行方式**：用 `run_in_terminal` 工具**串行**执行上述命令（不要并行），依次等待每个命令完成。
每个命令执行后检查退出码：
- `exitCode != 0` → **🔴 阻断流程**，向用户报告失败详情（命令、退出码、关键错误行），**不继续后续步骤**。你必须先修复问题再重新触发"提交"。
- 全部通过 → 进入 Step 3。

> ⚠️ **只有 3 项（ruff + mypy + pytest）全部绿色，才算 CI 通过。缺一项都不行。**

### Step 3：自动推断分支名

分支命名规则：`{type}/{scope}-{简短描述}`

| type | 触发条件 |
|------|---------|
| `feat` | 新增文件为主 |
| `fix` | 修复类改动 |
| `refactor` | 重构（文件数少、改动集中） |
| `chore` | 配置/杂务 |
| `docs` | 仅文档变更 |

| scope | 从变更路径推断 |
|-------|---------------|
| `doctor` | `doctor/` 下文件 |
| `demo-app` | `demo-app/` 下文件 |
| `bug-factory` | `bug-factory/` 下文件 |
| `benchmark` | `benchmark/` 下文件 |
| `infra` | `infra/` 或 `.github/` 下 |

**示例**：
- 新增 `doctor/` 下 20+ 文件 → `feat/doctor-init`
- 修改 `demo-app/backend/app/api/tasks.py` → `fix/demo-app-task-api`
- 新增 `.github/skills/` → `chore/git-workflow-skill`

### Step 4：创建并切换分支

```bash
git checkout main
git pull origin main
git checkout -b {分支名}
```

如果分支已存在，用 `{分支名}-2` 或询问用户。

### Step 5：生成 PR 内容

按以下格式写入 `PR_CONTENT.md`（项目根目录）：

```markdown
## type(scope): 一句话描述

### 📝 变更摘要
1-2 句

### 📦 变更清单
- `path/file`：说明

### ✅ 验收
```bash
命令
```

### 📊 影响
| 模块 | 程度 | 说明 |
|------|------|------|

### 🔗 关联
- 任务：Dxx
```

### Step 6：输出摘要

报告给用户：
- 新分支名
- PR 文件位置
- 提示用户 `git add` + `git commit` + `git push`

---

## 约束

- **🔴 CI 优先原则**：任何代码改动，提交前必须通过完整 CI（ruff + mypy + pytest）。不允许"先提交再说，CI 挂了再修"——那会浪费 CI Runner 资源和所有人的时间。
- 所有 git 操作前先 `git status` 确认工作区状态
- 有未暂存变更时提醒用户先 `git stash` 或 `git add`
- 分支名全小写，用 `-` 分隔
- PR 文件直接覆盖写入，不加额外解释
- **mypy 必须从各包目录分别运行**（`cd doctor && uv run mypy --strict src`），不要从 repo root 一次性传所有路径，否则会触发 "Duplicate module named src" 错误。这是已知 monorepo 陷阱。
