# AI 辅助编程最佳实践

> 创建日期：2026-06-19  
> 定位：**活文档**，持续积累 AI 辅助编程的实用技巧、踩坑记录、最佳实践。  
> 欢迎随时补充新技巧。

---

## 目录

1. [上下文管理](#1-上下文管理)
2. [提示词技巧](#2-提示词技巧)
3. [项目级配置](#3-项目级配置)
4. [代码生成策略](#4-代码生成策略)
5. [调试与排错](#5-调试与排错)
6. [评测与迭代](#6-评测与迭代)
7. [踩坑记录](#7-踩坑记录)

---

## 1. 上下文管理

### 1.1 分层上下文策略（核心技巧）

不要把全部设计文档塞进每次对话。按"热度"分三层：

| 层级 | 文件 | 加载方式 | 放什么 |
|------|------|---------|--------|
| 🟢 始终加载 | `copilot-instructions.md` | 每次对话自动注入 | 技术栈速查、命名规范、项目结构、当前阶段 |
| 🟡 按需加载 | `.github/instructions/*.instructions.md` | `applyTo` 文件匹配触发 | 后端/Frontend 特定约定、API 设计规范 |
| 🔴 手动触发 | `docs/*.md` | 对话中 attach 或 `@` 引用 | 完整架构文档、执行手册 |

### 1.2 `copilot-instructions.md` 编写原则

- **控制在 200 行以内**，超过则拆到 `*.instructions.md`
- 只放**全局适用**的信息，不放单个模块的细节
- 用表格、代码片段、速查表格式，减少叙述性文字
- 包含"当前开发阶段"进度条，方便 AI 理解上下文

### 1.3 `*.instructions.md` 的 `applyTo` 策略

```yaml
# ✅ 好的 applyTo：精确匹配
applyTo: "demo-app/backend/**"

# ❌ 避免：匹配所有文件（烧 token）
applyTo: "**"

# ✅ 好的 applyTo：多模式
applyTo: "demo-app/backend/**", "doctor/**", "bug-factory/**"
```

### 1.4 善用对话中的附件功能

- 需要 AI 理解完整架构时，直接 attach `docs/diagdoctor-from-scratch.md`
- 需要讨论具体代码时，先在编辑器中打开该文件（自动成为 context）
- 需要诊断 Bug 时，attach 日志文件 + 截图

---

## 2. 提示词技巧

### 2.1 给 AI 设定"角色 + 约束"

```markdown
# ❌ 模糊
帮我写一个 API

# ✅ 明确
在 `demo-app/backend/app/api/tasks.py` 中新增 `POST /api/tasks/{id}/comments` 端点。
要求：
- 使用 async SQLAlchemy session（Depends(get_db)）
- 校验用户是否有该项目的权限
- 返回 CommentResponse schema
- 添加 structlog 日志
```

### 2.2 给 AI 提供"验收标准"

参考 `docs/diagdoctor-execution-handbook.md` 的任务卡片格式：

```markdown
**验收标准：**
- [ ] `curl -X POST http://localhost:8000/api/tasks/{id}/comments` 返回 201
- [ ] 无权限用户调用返回 403
- [ ] `pytest tests/test_comments.py` 全部通过
```

### 2.3 善用"举例说明"

AI 对示例的遵循度远高于抽象描述：

```markdown
# ❌ 抽象
错误处理要做好

# ✅ 举例
错误处理参照 projects.py 的模式：
```python
logger.error("action_failed", error=str(e))
raise HTTPException(status_code=400, detail="具体错误描述")
```
```

### 2.4 "分步执行"优于"大包大揽"

```markdown
# ❌
重构整个 doctor 模块

# ✅
Step 1: 先创建 `doctor/src/graph/state.py`，定义 DoctorState
Step 2: 创建 `doctor/src/graph/main_graph.py`，实现主图
Step 3: 创建 `doctor/src/graph/nodes/triage_node.py`，实现 TriageAgent
```

---

## 3. 项目级配置

### 3.1 VS Code Copilot 定制文件体系

本项目的定制文件结构：

```
DiagDoctor/
├── copilot-instructions.md              # 全局约定（始终加载）
├── .github/
│   └── instructions/
│       ├── backend.instructions.md       # 后端约定（编辑 Python 文件时加载）
│       └── frontend.instructions.md      # 前端约定（编辑 TSX 文件时加载）
└── docs/
    ├── diagdoctor-from-scratch.md        # 完整架构（手动引用）
    ├── diagdoctor-execution-handbook.md  # 执行手册（手动引用）
    └── ai-assisted-dev-tips.md           # 本文档
```

### 3.2 可扩展的定制类型

根据 VS Code Copilot 支持，还可以创建：

| 类型 | 位置 | 用途 |
|------|------|------|
| Skill | `.github/skills/<name>/SKILL.md` | 多步骤工作流（如 `/deploy` 一键部署） |
| Prompt | `.github/prompts/<name>.prompt.md` | 参数化任务模板 |
| Agent | `.github/agents/<name>.agent.md` | 专用子 Agent（工具受限、上下文隔离） |
| Hooks | `.github/hooks/<name>.json` | 确定性生命周期操作（格式化、阻止危险操作） |

---

## 4. 代码生成策略

### 4.1 先骨架后填充

```
1. 让 AI 生成代码骨架（类/函数签名 + docstring）
2. 人工 review 结构是否正确
3. 让 AI 逐函数填充实现
4. 让 AI 生成测试
5. 跑测试验证
```

### 4.2 保持会话专注

- 一个对话做一件事（或紧密相关的一组事）
- 对话跑偏时开新会话
- 复杂任务拆成多个子任务，分别在不同会话中完成

### 4.3 代码审查由 AI 先做

```markdown
Review 以下代码，检查：
1. 是否有潜在 N+1 查询
2. 是否正确处理了权限
3. 错误处理是否完整
4. TypeScript 类型是否安全
```

---

## 5. 调试与排错

### 5.1 让 AI 帮你读日志

```
我附上后端日志，帮我分析为什么 POST /api/tasks 返回 500
```

### 5.2 Diff 驱动的 Bug 定位

```
对比 main 分支和当前分支的 diff，找出可能导致 xxx 问题的改动
```

### 5.3 让 AI 解释报错

```
这个 TypeScript 类型错误是什么意思？如何修复？
```

---

## 6. 评测与迭代

### 6.1 Prompt 迭代方法

参考 DiagDoctor 的 Harness 评测体系思路：

1. 准备一组固定输入
2. 跑 Agent，收集输出
3. 用 LLM Judge + 精确匹配双轨评估
4. 对比不同 Prompt 版本的得分
5. 选最优版本

### 6.2 对比式提问

```markdown
以下两个 Prompt 哪个更好？为什么？

Prompt A:
你是一个 Bug 诊断专家...

Prompt B:
你是一个拥有 10 年经验的全栈工程师...
```

---

## 7. 踩坑记录

### 7.1 Docker 网络问题

- **现象**：容器间无法通信，`curl` 超时
- **原因**：Docker Desktop 在 Windows 上的网络隔离 + 子网冲突
- **解决**：见 `docs/docker-network-fixes.md`
- **教训**：Windows 上 Docker 网络是高频坑点，`docker compose` 优先用 service name 通信

### 7.2 AI 生成代码的"幻觉"问题

- **现象**：AI 使用不存在的 API、参数名、库版本
- **预防**：
  - 明确指定库版本号
  - 要求 AI 先列出要使用的 API 再写代码
  - 生成后立即跑 `ruff check` + `mypy` 验证
- **经验**：Python 3.11+ 语法容易出错（如 `str | None` vs `Optional[str]`）

### 7.3 大文件编辑失败

- **现象**：`replace_string_in_file` 因为微小的空格/缩进差异匹配不上
- **解决**：
  - 直接用 `insert_edit_into_file` 插入代码块
  - 或者让 AI 分段编辑，每次只改小块代码
- **经验**：复杂改动优先让 AI 用 `insert_edit_into_file` 而非 `replace_string_in_file`

---

## 附录：推荐工具清单

| 工具 | 用途 | 适用场景 |
|------|------|---------|
| VS Code Copilot | 日常代码补全 + 对话 | 所有场景 |
| Copilot Edits | 多文件协同修改 | 跨文件重构 |
| Claude / GPT-4 | 架构设计讨论 | 重大决策前 |
| v0.dev | 前端组件快速生成 | 新页面/新组件 |
| ruff + mypy | 代码质量检查 | 每次 AI 生成后立即运行 |

---

> **新技巧？** 直接在对应章节下追加，或在末尾添加新章节。保持格式一致。
