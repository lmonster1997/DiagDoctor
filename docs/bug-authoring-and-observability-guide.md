# Bug 配方创作指南 & 可观测性改进手册

> **文档目的**：本文档有两个目标读者。
> 1. **Bug Recipe 作者（含能力较弱的 LLM）**：照着第一部分的规则、模板和自检清单，就能产出"可被诊断"的高质量 bug 配方。
> 2. **demo-app / bug-factory 维护者**：第二部分给出当前实现"能否生成适合诊断的日志"的体检结论与改进方案。
>
> **范围**：本文档**不涉及 Doctor（诊断 Agent）侧**的实现。只关注"被诊断系统（demo-app）"与"bug 生成系统（bug-factory）"。
>
> **核心论断（先说结论）**：当前 demo-app **几乎产生不了任何对诊断有用的日志或 SQL trace**，导致大部分 bug 配方即使注入成功，证据里也"什么都看不到"——这是当前 bug 质量低的**头号根因**，比 recipe 写法本身更致命。详见第二部分。
>
> **第三个警告（价值证明）**：即使信号齐全，如果 bug 都是“教科书级反模式且症状=根因”，会被质疑“bug 太简单、Doctor 价值不大”。这个质疑在当前状态下是成立的。解法是第 8.5 节的“难度梯度 + 裸 LLM 基线对照 + 自主取证”。

---

## 目录

- [第一部分：高质量 Bug Recipe 创作指南](#第一部分高质量-bug-recipe-创作指南)
  - [1. 什么叫"高质量可诊断 Bug"](#1-什么叫高质量可诊断-bug)
  - [2. 七条黄金规则（弱模型必须逐条遵守）](#2-七条黄金规则弱模型必须逐条遵守)
  - [3. Recipe 字段逐项规范](#3-recipe-字段逐项规范)
  - [4. 注入策略：优先 diff_patch，慎用 ai_instruction](#4-注入策略优先-diff_patch慎用-ai_instruction)
  - [5. 每类 Bug 的"可观测信号"要求](#5-每类-bug-的可观测信号要求)
  - [6. 触发序列设计规范](#6-触发序列设计规范)
  - [7. 正反例对比](#7-正反例对比)
  - [8. 提交前自检清单](#8-提交前自检清单)
  - [8.5 让 Doctor “有价值”：难度梯度 + 裸 LLM 基线 + 自主取证](#85-让-doctor-有价值难度梯度--裸-llm-基线--自主取证)
- [第二部分：demo-app / bug-factory 可观测性体检与改进](#第二部分demo-app--bug-factory-可观测性体检与改进)
  - [9. 体检结论：当前为什么生成不出可诊断日志](#9-体检结论当前为什么生成不出可诊断日志)
  - [10. 改进方案（按优先级）](#10-改进方案按优先级)
  - [11. 改进后日志/Trace 应长什么样](#11-改进后日志trace-应长什么样)
  - [12. 注入验证闭环](#12-注入验证闭环)
  - [13. 各类 Bug 场景的生成方案 + 所需日志/调用链](#13-各类-bug-场景的生成方案--所需日志调用链)

---

# 第一部分：高质量 Bug Recipe 创作指南

## 1. 什么叫"高质量可诊断 Bug"

一个 bug 配方只有**同时满足下面 5 个标准**才算合格。任何一条不满足，诊断系统就无从下手，这个 bug 就是"低质量"的。

| # | 标准 | 通俗解释 | 反例 |
|---|------|----------|------|
| **S1 注入确定性** | 注入后代码改动**完全可预期**，每次运行结果一致 | 同一个 recipe 跑 10 次，注入的代码必须 100% 相同 | 用 `ai_instruction` 让 LLM "自由发挥"改代码 |
| **S2 可触发性** | 存在一条**确定的请求序列**能稳定触发 bug | 触发步骤跑完，bug 现象 100% 出现 | 依赖随机/并发时序，10 次只复现 3 次 |
| **S3 可观测性** | 触发后**必然在日志或 trace 里留下可定位的信号** | 日志里有异常堆栈、慢查询、错误状态码等 | bug 触发了但日志里"风平浪静" |
| **S4 信号-期望对齐** | `expected_observation` 描述的信号**真的会出现** | recipe 说"日志出现 50 次 SELECT"，就真的有 | 期望信号靠脑补，与系统实际输出不符 |
| **S5 根因唯一可判** | 根因明确、单一，能写出确定的修复 | "缺少 owner 校验" | 模糊的"性能不太好" |

> **关键洞察**：S3 和 S4 是当前项目**最容易翻车**的地方。因为 demo-app 当前日志/trace 几乎是空的（见第二部分），导致再好的 recipe 也满足不了 S3/S4。**写 recipe 前，必须先确认目标代码路径真的会产生可观测信号。**

---

## 2. 七条黄金规则（弱模型必须逐条遵守）

如果你是一个能力有限的模型，**不要自由发挥**，严格执行下面 7 条即可：

1. **能用 `diff_patch` 就绝不用 `ai_instruction`**。确定性改动一律写成精确的 unified diff。
2. **绝不制造"语义对抗型"bug**（例如"假装写权限检查但实际不生效"）。这类 bug 会被对齐良好的模型自作主张"修正"。改成**直接删除/替换真实代码**。
3. **注入点必须在一条会被日志或 trace 覆盖的代码路径上**。如果那段代码不产生任何可观测信号，先去给它加埋点（见第二部分），否则 bug 不可诊断。
4. **`expected_observation` 里写的每一个 `log_pattern` / `trace_attribute`，都必须是你亲眼在真实运行中见过的**，不能凭空想象。
5. **触发序列必须自包含且确定**：自己登录、自己造数据、自己发请求，不依赖外部预置状态。
6. **`fix_keywords` / `must_mention_keywords` 要少而精**，只放根因和修复的**核心术语**，不要堆砌近义词。
7. **每个 recipe 改且仅改一个文件、注入且仅注入一个 bug**。不要在一个 recipe 里混入多个问题。

---

## 3. Recipe 字段逐项规范

以下是每个字段的填写要求。`字段 = 用途 + 硬性要求`。

```yaml
id: BE-001                    # 必填。格式 ^[A-Z]+-\d{3}$。前缀仅作编号分组（BE/FE/LOGIC/PERF/DATA/CONFIG）
title: "一句话描述现象"        # 必填。从"用户视角"描述现象，不是从代码视角
category: performance         # 必填枚举。这是分类的"真值"，与 id 前缀无关！见下方说明
severity: high                # 必填枚举：low/medium/high/critical
tags: ["n+1", "orm"]          # 选填。自由标签，便于检索

expected_diagnosis:           # 诊断的"标准答案"，用于评测
  root_cause: "..."           # 必填。根因，一句话讲清"哪里因为什么出了什么问题"
  affected_file: "相对路径"    # 必填。被改的文件（与 injection.target_file 一致）
  affected_line: null         # 选填。能给具体行号最好，给不了写 null
  fix_suggestion: "..."       # 必填。可执行的修复方案，不要笼统
  fix_keywords: [...]         # 必填。3-6 个核心术语，评测时检查诊断是否命中

injection:                    # 如何把 bug 注入代码
  strategy: code_replace      # code_replace / code_insert
  target_file: "相对路径"      # 必填。从仓库根算起的相对路径
  ai_instruction: null        # 见第 4 节：优先留 null，改用 diff_patch
  diff_patch: |               # 强烈推荐：精确的 unified diff
    --- a/...
    +++ b/...
    @@ ...

trigger:                      # 如何触发 bug
  type: api_call
  steps: [...]                # 见第 6 节
  expected_observation:       # 触发后"应该看到什么"——必须与真实信号对齐（S4）
    log_patterns: [...]       # 见 S4：只写真实会出现的
    trace_attributes: {...}
    api_response: {...}

evaluation:                   # 评测标准
  must_mention_keywords: [...]   # 诊断"必须"提到的词（少而精）
  should_mention_keywords: [...] # "最好"提到的词
  llm_judge_criteria: "..."      # 给 LLM 评委的判定标准，写清"什么样算诊断正确"
  min_confidence: 0.6            # 诊断置信度下限
```

### ⚠️ `category` vs `id 前缀` 的陷阱

`id: BE-001` 的前缀 `BE`（backend）和 `category: performance` **是两回事**。

- **`id` 前缀**只是编号分组，方便人看。
- **`category` 字段**才是分类的**唯一真值**，评测和路由都以它为准。

**规则**：写 recipe 时，`category` 要填 bug 的**真实性质**，不要被 id 前缀带偏。例如一个"后端的 N+1 性能问题"，`id` 可以叫 `BE-001`，但 `category` 必须是 `performance`。

合法 `category` 枚举（共 6 类）：
`frontend_crash` / `backend_error` / `performance` / `logic` / `data` / `config`

---

## 4. 注入策略：优先 diff_patch，慎用 ai_instruction

这是**提升 bug 质量最有效的一招**。当前所有示例 recipe 都用 `ai_instruction: ... / diff_patch: null`，这是低质量的主要来源之一。

### 为什么 ai_instruction 不可靠

`ai_instruction` 让 LLM 在注入时"按描述改写整个文件"，会带来三类不确定：

1. **指令执行不完整**：忘记删某行、加了多余注释、改了不该改的地方。
2. **语法破坏**：文件较大时输出被 token 上限截断，产生语法错误。
3. **对齐反噬**：越是"故意写错"的指令（如 LOGIC-001 让模型"假装做权限检查"），越规范的模型越倾向于"我来帮你写正确"，结果 bug 根本没被注入。

### 正确做法：写 diff_patch

对**任何可以静态描述的改动**，直接写 unified diff，绕过 LLM：

```yaml
injection:
  strategy: code_replace
  target_file: "demo-app/backend/app/api/tasks.py"
  ai_instruction: null
  diff_patch: |
    --- a/demo-app/backend/app/api/tasks.py
    +++ b/demo-app/backend/app/api/tasks.py
    @@ -28,11 +28,15 @@ async def list_tasks(
         result = await db.execute(
             select(Task)
             .where(Task.project_id == project_id)
    -        .options(selectinload(Task.comments))
             .order_by(Task.created_at.desc())
         )
    -    return list(result.scalars().all())
    +    tasks = list(result.scalars().all())
    +    # BUG(BE-001): N+1 — query comments one-by-one instead of eager loading
    +    for task in tasks:
    +        cres = await db.execute(select(Comment).where(Comment.task_id == task.id))
    +        _ = cres.scalars().all()
    +    return tasks
```

### 什么时候才允许用 ai_instruction

只有当改动**无法用静态 diff 表达**（例如需要根据运行环境动态生成代码）时才用。即使用了，也必须：
- 指令**只描述"删什么/加什么"，给出确切代码块**，不留发挥空间。
- **绝不**包含"假装"、"看起来像"、"故意"之类的语义对抗描述。
- 注入后**必须跑语法门禁**（`ruff check` / `tsc --noEmit`）。

---

## 5. 每类 Bug 的"可观测信号"要求

**这是 S3/S4 的落地表**。写 recipe 前对照本表：你注入的 bug，触发后**必须**在下列某处留下信号，否则不可诊断。

> ⚠️ 前提：下表假设 demo-app 已按第二部分完成可观测性改造。**改造前，下表大部分信号都不存在**——这就是当前 bug 质量低的根因。

| category | 必须产生的可观测信号 | 信号载体 | 当前能否产生 |
|----------|---------------------|----------|--------------|
| `backend_error` | 异常类型 + 堆栈 + 5xx 状态码 + trace_id | 错误日志 + trace span status=ERROR | ❌ 否（无异常日志） |
| `performance` | 慢请求耗时 / N+1 的重复 SQL span | trace span duration + SQL span 计数 | ❌ 否（SQL 未 instrument） |
| `logic` | 错误的返回数据 / 缺失的鉴权日志 | 业务日志 + 响应体 | ❌ 否（业务零日志） |
| `data` | 编码/精度/时区异常的字段值或异常 | 业务日志 + 异常日志 | ❌ 否 |
| `config` | 启动告警 / 连接失败 / CORS 拒绝日志 | 启动日志 + 错误日志 | ⚠️ 部分 |
| `frontend_crash` | JS 错误 + 组件栈 + 用户操作上下文 | 前端错误上报日志 | ❌ 否（前端无上报） |

**给 recipe 作者的硬规则**：
- 如果你要写一个 `performance` 类 N+1 bug，**先确认 SQLAlchemy 已被 instrument**（trace 里能看到每条 SQL span），否则诊断系统看不到"50 次查询"，bug 无效。
- 如果你要写 `backend_error` bug，**先确认有全局异常处理器把堆栈写进结构化日志**，否则只有一个干巴巴的 500，无法定位。
- `expected_observation.log_patterns` 里的每条正则，**必须**能在真实运行的日志里 `grep` 到。写完 recipe 后务必真实跑一遍验证（见第 12 节）。

---

## 6. 触发序列设计规范

`trigger.steps` 支持 5 种 action：`login` / `api_call` / `create_data` / `ui_click` / `wait`。

### 规范

1. **自包含**：序列第一步通常是 `login`，自己拿 token；需要数据就用 `create_data` 自己造，不假设数据库里已有数据。
2. **确定可复现**：避免依赖并发时序的 bug（如 race condition）除非你能用 `repeat` + 明确的并发模型稳定复现。
3. **用模板变量串联**：`create_data` 产生的 id 用 `{project_id}` / `{task_id}` 在后续步骤引用。多个同类资源用 `{project_id:1}` 指定索引。
4. **规模要够触发信号**：性能类 bug 要造足量数据（如 N+1 至少 50 条），否则 trace 上耗时差异不明显。
5. **末尾留日志刷新时间**：触发完依赖 `log_flush_seconds`（默认 3 秒）等 Loki/Tempo 摄取完成，别过早收集证据。

### 模板

```yaml
trigger:
  type: api_call
  steps:
    - action: login
      params: { email: "admin@example.com", password: "Admin123!" }
    - action: create_data
      params:
        entity: "project"
        data: { name: "Perf Test", description: "..." }
    - action: create_data
      params:
        entity: "task"
        data: { title: "Bulk Task", project_index: 0 }
        repeat: 50                       # 造 50 条，触发 N+1
    - action: api_call
      params:
        method: "GET"
        path: "/api/projects/{project_id}/tasks"
  expected_observation:
    trace_attributes:
      "GET /api/projects/{id}/tasks":
        duration_ms: ">2000"             # 必须是真实会达到的阈值
    log_patterns:
      - pattern: "SELECT .* FROM comments WHERE comments.task_id"
        min_occurrences: 50              # 必须 instrument SQL 后才成立
```

---

## 7. 正反例对比

### 例 A：N+1 性能 Bug（BE-001）

| | ❌ 低质量写法 | ✅ 高质量写法 |
|--|--------------|--------------|
| 注入 | `ai_instruction` 让模型"改成逐个查询" | `diff_patch` 精确删除 `selectinload` 并插入循环查询 |
| 信号 | 期望"日志里 50 次 SELECT"，但 SQL 没 instrument，实际为空 | 先 instrument SQLAlchemy，trace 里真出现 50 个 SQL span |
| 验证 | 从没真实跑过，evidence 是空壳 | 注入后真实触发，确认 trace 有 50 span、耗时 >2s |

### 例 B：越权 Bug（LOGIC-001）—— 语义对抗的反面教材

❌ **当前写法（低质量）**：
```yaml
ai_instruction: |
  添加注释假装做权限检查，但实际用 pass 绕过，让代码看起来像"有意为之"...
```
问题：(1) 依赖 LLM 配合"演戏"，对齐良好的模型会拒绝或写成真检查；(2) 注入结果不确定。**实测后果**：该 bug 的 `pass` 已经被注入到了主分支的 `demo-app/backend/app/api/tasks.py` 里且与注释自相矛盾，污染了 baseline。

✅ **应改为（高质量）**：用 diff_patch **直接删除真实存在的权限校验代码**：
```yaml
injection:
  strategy: code_replace
  target_file: "demo-app/backend/app/api/tasks.py"
  ai_instruction: null
  diff_patch: |
    --- a/demo-app/backend/app/api/tasks.py
    +++ b/demo-app/backend/app/api/tasks.py
    @@ ...
    -    project = await db.get(Project, project_id)
    -    if project.owner_id != current_user.id:
    -        raise HTTPException(status_code=403, detail="Forbidden")
         result = await db.execute(select(Task).where(...))
```
> 前提：baseline 代码里**本来就有**这段校验。即"先有健康实现，再删掉它"，而不是"凭空假装"。

---

## 8. 提交前自检清单

每个 recipe 提交前，逐条打勾：

- [ ] **S1** 注入用的是 `diff_patch`（或 ai_instruction 但已跑过语法门禁）
- [ ] **S1** 没有任何"假装/看起来像/故意"的语义对抗描述
- [ ] **S2** 触发序列自包含，本地真实跑通且 100% 复现
- [ ] **S3** 触发后**真实**在日志/trace 里看到了信号（不是脑补）
- [ ] **S4** `expected_observation` 的每条 pattern 都能在真实日志里 grep 到
- [ ] **S5** `root_cause` / `fix_suggestion` 明确、单一、可执行
- [ ] `category` 填的是 bug 真实性质（不是 id 前缀）
- [ ] 只改一个文件、只注入一个 bug
- [ ] `fix_keywords` / `must_mention_keywords` 少而精（≤6 个）
- [ ] 注入后跑了 `ruff check` / `tsc --noEmit`，无语法错误
- [ ] 注入产生的改动能用 `git diff` 干净回滚，不污染其他文件

---

## 8.5 让 Doctor "有价值"：难度梯度 + 裸 LLM 基线 + 自主取证

> **为什么单独立一节**：前面 8 节解决的是「bug 能不能被诊断」。本节解决一个更尖锐的问题——**「这些 bug 配得上一整套诊断系统吗」**。
>
> 如果一批 bug 全是「N+1 / null 访问 / 缺索引」这类**症状即根因、单文件、教科书级反模式**，评审会直接质疑：
> > 「把日志贴给 ChatGPT，一句 prompt 它也能答对，你这套 LangGraph + RAG + 多 Agent 的边际价值在哪？」
>
> **这个质疑在当前 bug 质量下是成立的。** 堵住它不能靠「把 bug 改难」一句话，要靠下面三件可落地的事。

### 8.5.1 核心检验标准：「裸 LLM 基线」

任何一个 bug 配方，写完先问自己一句：

> **把 evidence（日志+trace）直接贴给一个强 LLM，一句「这是什么 bug、怎么修」，它能不能答对？**

- 如果**能**：这个 bug 只够当「冒烟测试」（证明流水线通），**不能用来证明 Doctor 的价值**。
- 如果**不能**（需要多步取证、关联多个信号、跨越症状到根因）：这才是能体现 Doctor 价值的 bug。

**落地要求**：评测报告里**永远放两列对照**——`单次 LLM 基线准确率` vs `完整 Doctor 准确率`。
- 在简单 bug 上两者接近 → 诚实承认，这类本就简单。
- 在难 bug 上 Doctor 显著高于基线（如 +30%）→ **这就是价值的量化证明**。

> 没有这个对照，再高的准确率都会被质疑「是题简单，还是系统强」。这是最便宜、最有杀伤力的一招。

### 8.5.2 建立「难度梯度」，不要只有 L1

bug 集必须覆盖难度分层，并在 recipe 里用 `tags` 标注难度（如 `difficulty:L2`）：

| 难度 | 特征 | 为什么需要 Doctor | 例子 |
|------|------|------------------|------|
| **L1 教科书** | 症状=根因，单文件，单信号 | 基本不需要（裸 LLM 可解）。仅作 sanity check | 现有 BE-001 / FE-001 |
| **L2 跨层** | 症状在 A 层，根因在 B 层 | 需要跨日志/trace/代码关联 | 前端白屏，根因是某迁移漏了字段导致 API 返回 null |
| **L3 误导信号** | 有红鲱鱼，错误日志指向错误位置 | 需要排除干扰、辨别因果 | X 端点报 500，真因是上游 Y 的缓存污染 |
| **L4 多信号必需** | 单看日志或单看 trace 都判不出 | 必须关联多源证据才能定位 | 间歇性慢，只有 trace 上并发 span 重叠才看得出竞态 |

**配比建议**：L1 占比不超过 30%（够证明流水线即可），L2/L3/L4 合计 ≥ 70%。Doctor 的价值**只在 L2 以上才开始显现**。

### 8.5.3 设计「难 bug」的三个手法

弱模型按下面三个手法之一，就能把一个 L1 bug 升级成 L2-L4：

1. **拉开症状与根因的距离（→ L2）**
   - 症状出现的层 ≠ 根因所在的层。例如：前端崩溃，但根因是后端某字段类型变了 / 某配置错了 / 某迁移没跑。
   - recipe 要点：`affected_file` 故意距离「现象爆发点」一跳以上，逼 Doctor 沿调用链回溯。

2. **植入误导信号（→ L3）**
   - 在触发序列里制造一个**显眼但无关**的错误日志（红鲱鱼），真正的根因信号更隐蔽。
   - recipe 要点：`expected_observation` 里同时标注「干扰信号」和「真实根因信号」，评测时检查 Doctor 有没有被带偏。

3. **让单一信号不足以定论（→ L4）**
   - 设计成「只看日志会得出错误结论，必须叠加 trace / 数据库状态才能确认」。
   - recipe 要点：竞态、缓存穿透、时区错误这类**需要时序或多请求关联**的 bug 天然属于 L4。

### 8.5.4 改为「自主取证」，杜绝喂答案

当前最大的隐性贬值：**证据是喂给 Doctor 的**。recipe 把 `logs`/`traces` 都装进 evidence，`affected_file`/`fix_keywords` 都写好，Doctor 实际在做「阅读理解」而非「自主侦查」。

**整改要求**：
- 给 Doctor 的输入**只保留 `user_report` + 时间窗**，**不要**把 `logs` / `traces` 预先塞进 evidence。
- 强制 Doctor 自己决定去 Loki / Tempo 查什么、查哪个时间窗、如何关联。
- 这样即便是 L1 的 N+1，也升级成「你能不能自己找到那 50 条 SQL」的考题，而不是「这段日志说明什么」的阅读题。

> `expected_diagnosis` 仍可保留作为**评测标准答案**（给 LLM judge 用），但**绝不能出现在 Doctor 的输入里**——否则就是数据泄漏。

### 8.5.5 价值证明自检清单

每批 bug 交付前，额外打勾：

- [ ] 跑过「裸 LLM 基线」对照，报告里有两列数据
- [ ] L1 占比 ≤ 30%，L2 以上 ≥ 70%
- [ ] 至少有 1-2 个 L3（误导信号）和 1-2 个 L4（多信号必需）case
- [ ] Doctor 的输入只有 `user_report` + 时间窗，没有预置 `logs`/`traces`
- [ ] `expected_diagnosis` 只用于评测，未泄漏进输入
- [ ] 能用一句话回答「为什么这个 bug 裸 LLM 答不对、需要 Doctor」

> **一句话总结本节**：堵住「bug 太简单」质疑的关键，不是把 bug 改难，而是 **(a) 用裸 LLM 基线量化边际价值、(b) 建难度梯度逼出多步推理、(c) 改自主取证杜绝喂答案**。其中 (a) 最便宜、最有说服力。

---

# 第二部分：demo-app / bug-factory 可观测性体检与改进

## 9. 体检结论：当前为什么生成不出可诊断日志

> **一句话**：demo-app 的**日志投递通道是通的**（通过 docker-entrypoint.sh 的管道捕获 stdout 转发到 Loki），但**应用几乎不往 stdout 写任何有用的东西**（业务零日志、SQL echo 关闭、无异常堆栈），所以管道里流过的只有 uvicorn 默认的 `GET /health 200 OK` 访问日志。**问题不是“日志出不去”，而是“根本没有有用的日志被产生”。**

### 9.0 真实日志机制（重要：与早期设计不同）

`app/observability.py` 里的 `setup_loki_logging()`（应用内 `_LokiHandler`）**当初因故被放弃了**。现在的真实机制是 [demo-app/backend/docker-entrypoint.sh](demo-app/backend/docker-entrypoint.sh#L9) 里的**管道捕获**：

```sh
exec uvicorn app.main:app ... 2>&1 | python -c "逐行读 stdin → POST 到 Loki"
```

这意味着：
- ✅ **任何写到 stdout/stderr 的行都会被自动转发到 Loki**。通道是通的，不需要改 Loki 机制。
- ❌ 但 uvicorn 默认只往 stdout 写**访问日志**（`GET /health 200 OK`）。应用代码、SQL、异常堆栈都没往 stdout 写 → 所以 Loki 里全是无用的 health 日志。

> **关键推论**：修复成本比想象低得多。**只要让应用往 stdout 写有用的东西（开 SQL echo、加 logger、加异常处理），管道会自动把它们送进 Loki。** 不需要重新启用 `setup_loki_logging()`（两套机制会重复推送）。

### 9.1 缺陷清单（按严重度）

| # | 缺陷 | 证据位置 | 后果 |
|---|------|----------|------|
| **C1** | **业务代码零日志**：`app/api/*`、`app/services/*` 没有任何 `logger` 调用 | 全仓 grep `logger`/`logging` 在业务层 0 命中 | logic/data/backend_error 类 bug 触发后无任何业务日志 |
| **C2 ⚠修正** | **SQL echo 关闭**：`db.echo = settings.debug` 且 `DEBUG=false` | `app/database.py` + compose `DEBUG: "false"` | stdout 没有任何 SQL → 管道转发不到 → N+1 的 50 次 SELECT 在 Loki 中不可见（**这是 BE-001 失败的直接原因**） |
| **C3** | **`instrument_sqlalchemy()` 从未被调用** | `app/main.py` 未调用 | **SQL 不产生 trace span**，N+1 类性能 bug 在 trace 里也不可见（与 C2 双重缺失，日志和 trace 都看不到 SQL） |
| **C4** | **无请求访问日志中间件**（只有 uvicorn 默认 access log，不含耗时/trace_id） | `app/main.py` 无 middleware | 无结构化的 method/path/status/latency 记录，无法关联 trace |
| **C5** | **无全局异常处理器** | `app/main.py` 无 exception handler | 后端 5xx 异常只有 uvicorn 一行报错，无结构化堆栈 |
| **C6** | **日志无 trace_id、非结构化**：管道只打 `service_name` label，日志体是纯文本 | `docker-entrypoint.sh` 管道脚本 | 日志与 trace **无法关联**，无法从慢 trace 跳到对应日志 |
| **C7** | **前端无错误上报** | 证据收集列了 `demo-frontend` 但前端无上报通道 | `frontend_crash` 类 bug 无前端日志可查 |

### 9.2 与 recipe 期望的冲突

以 BE-001 为例，它的 `expected_observation` 要求：
```yaml
log_patterns:
  - pattern: "SELECT.*FROM comments.*WHERE comments.task_id"
    min_occurrences: 50
trace_attributes:
  "GET /api/projects/{id}/tasks":
    duration_ms: ">2000"
```
- 第一条要求"日志里出现 50 次 SELECT" → 管道能转发 stdout，但因 SQL echo 关闭（C2），stdout 里根本没 SQL → **当前无法满足，但只要开 echo 就能满足**。
- 第二条要求"trace 显示耗时 >2s" → FastAPI 已 instrument，HTTP span 的总耗时**能看到**，但因 SQL 未 instrument（C3），**看不到 50 个 SQL 子 span**，无法从 trace 证明根因是 N+1。

**结论**：recipe 的期望信号与系统实际能力错位（违反 S4）。修复点不在 Loki/管道，而在“让应用把有用信息写到 stdout + 给 SQL 加 trace span”。

---

## 10. 改进方案（按优先级）

### P0 — 让信号"能产生"（最高优先级，没有它一切免谈）

> **前提认知**：日志投递链路（docker-entrypoint.sh 管道）已经是通的。下面所有改进的本质是**让应用把有用信息写到 stdout / 加上 SQL trace span**，管道会自动转发到 Loki。**不要**再去启用 `setup_loki_logging()`（会与管道双重推送，重复日志）。

**P0-1：打开 SQL echo（改 1 处，收益最大）**

这是让 BE-001 等性能/查询类 bug 立刻可诊断的最快一招。SQLAlchemy 的 `echo=True` 会把每条 SQL 打到 stdout → 管道自动转发 → N+1 的 50 条 `SELECT comments` 直接出现在 Loki。

把 `app/database.py` 的 engine 创建改为始终开启 echo（或新增独立开关，避免和 DEBUG 耦合）：

```python
# app/database.py
import os

engine = create_async_engine(
    settings.database_url,
    echo=True,  # 始终打印 SQL 到 stdout，供 Loki 采集（demo 环境专用）
    pool_size=10,
    max_overflow=20,
)
```
> 注意：echo 输出多行（SQL + 参数），管道按行转发即可被 `grep "SELECT .* FROM comments"` 命中。

**P0-2：给 SQL 加 trace span（让 N+1 在 trace 里可见）**

在 `app/main.py` 调用已实现的 `instrument_sqlalchemy()`，这样每条 SQL 成为 HTTP span 的子 span，trace 上能直接数出"50 个 SELECT comments"：

```python
from app.observability import (
    init_observability,
    instrument_fastapi,
    instrument_sqlalchemy,   # 新增
)

init_observability()
instrument_sqlalchemy()      # 新增：每条 SQL 产生 trace span
# ... app = FastAPI(...) ...
instrument_fastapi(app)
```

**P0-3：加全局异常处理器（结构化记录堆栈 + trace_id）**

让 `backend_error` 类 bug 触发后，stdout（→Loki）里有完整异常类型、消息、堆栈和 trace_id：

```python
import logging, traceback, sys
from opentelemetry import trace
from fastapi import Request
from fastapi.responses import JSONResponse

# 让 logger 走 stdout（管道会转发）
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
logger = logging.getLogger("app")

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    span = trace.get_current_span()
    trace_id = format(span.get_span_context().trace_id, "032x")
    logger.error(
        '{"event":"unhandled_exception","path":"%s","method":"%s",'
        '"exc_type":"%s","exc_msg":"%s","trace_id":"%s","stack":%r}',
        request.url.path, request.method, type(exc).__name__,
        str(exc), trace_id, traceback.format_exc(),
    )
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
```

**P0-4：加请求访问日志中间件（结构化 + trace_id）**

替代 uvicorn 干巴巴的 access log，输出含耗时和 trace_id 的结构化行：

```python
import time
@app.middleware("http")
async def access_log(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    span = trace.get_current_span()
    trace_id = format(span.get_span_context().trace_id, "032x")
    logger.info(
        '{"event":"http_request","method":"%s","path":"%s",'
        '"status":%d,"latency_ms":%.1f,"trace_id":"%s"}',
        request.method, request.url.path, response.status_code, elapsed_ms, trace_id,
    )
    return response
```

### P1 — 让信号"可关联、可结构化"

**P1-1：日志加 `trace_id` label，日志体改 JSON 结构化**

当前管道（`docker-entrypoint.sh`）只给日志打 `service_name` label、日志体是纯文本。建议改造管道脚本或改用应用内结构化日志：让每行日志是 JSON（含 `event`/`path`/`status`/`trace_id`），并把 `trace_id` 提取为 Loki label。这样 Doctor 才能"从慢 trace 跳到对应日志"。推荐用 `structlog`（项目全局约定本就要求）输出 JSON，再由管道转发。

**P1-2：业务关键路径埋点**

在鉴权、关键查询、状态变更处加结构化日志，例如 `list_tasks` 里记录 `project_id` / `task_count` / `current_user`，让 `logic` 类 bug（如越权）能从日志看出"谁访问了谁的资源"。

**P1-3：慢查询专项日志（可选但推荐）**

用 SQLAlchemy `before_cursor_execute` / `after_cursor_execute` event 记录每条 SQL 的耗时，超过阈值打 WARNING。这让 N+1 / 缺索引类 bug 即使只看日志也能定位。

### P2 — 前端与边界

**P2-1：前端全局错误上报**

前端加 `window.onerror` / `ErrorBoundary`，把 JS 错误（含组件栈、用户操作、当前路由）上报到一个后端 `/api/client-logs` 端点，再转发 Loki，打 `service_name="demo-frontend"` label。这样 `frontend_crash` 类 bug 才有证据。

**P2-2：清理被污染的 baseline**

`demo-app/backend/app/api/tasks.py` 当前残留了 LOGIC-001 注入的 `pass` 绕过代码（且注释自相矛盾，声称是 healthy baseline）。需要**先把 baseline 恢复成真正健康的实现**（含 owner 校验），bug 才能"从健康态注入"。后续 `GitManager` 注入应使用临时 worktree 隔离，避免污染主工作区。

---

## 11. 改进后日志/Trace 应长什么样

完成 P0/P1 后，触发 BE-001（N+1）应在证据里看到：

**Loki 日志（结构化 JSON，带 trace_id）：**
```json
{"event":"http_request","method":"GET","path":"/api/projects/.../tasks","status":200,"latency_ms":2384.5,"trace_id":"a1b2c3..."}
{"event":"slow_query","sql":"SELECT * FROM comments WHERE comments.task_id = $1","duration_ms":31.2,"trace_id":"a1b2c3..."}
... (重复 50 次 slow_query / SELECT comments) ...
```

**Tempo Trace（HTTP span 下挂 50 个 SQL 子 span）：**
```
GET /api/projects/{id}/tasks            [2384 ms]
├── SELECT tasks WHERE project_id=...   [   8 ms]
├── SELECT comments WHERE task_id=#1    [  30 ms]
├── SELECT comments WHERE task_id=#2    [  29 ms]
│   ... × 50 ...
└── SELECT comments WHERE task_id=#50   [  31 ms]
```
> 有了这两样，诊断系统就能明确判定"N+1 查询，建议 selectinload"，BE-001 真正可诊断。

对 `backend_error` 类，应看到：
```json
{"event":"unhandled_exception","path":"...","exc_type":"ValueError","exc_msg":"...","trace_id":"...","stack":"Traceback ..."}
```

---

## 12. 注入验证闭环

光改 demo-app 还不够，bug-factory 必须**验证 bug 真的注入并触发了**。建议在 `EvidenceCollector` 之后加一个 **`evidence_validator`** 步骤：

1. 注入后跑语法门禁（`ruff check` / `tsc --noEmit`），失败则 reject 重注入。
2. 触发并收集证据后，用 recipe 的 `expected_observation.log_patterns` / `trace_attributes` **自动校验**采集到的证据是否匹配：
   - 每条 `log_pattern` 在收集到的日志里 `grep`，命中数 ≥ `min_occurrences`。
   - `trace_attributes` 的耗时阈值在收集到的 trace 里成立。
3. 任一不匹配 → 把该 case 标记为 `invalid`，不进入评测集。

这样能从机制上保证：**进入 benchmark 的每一个 case，都是"已验证真实可触发、真实有信号"的高质量 bug。**

---

## 13. 各类 Bug 场景的生成方案 + 所需日志/调用链

> 本节是全文最实用的部分。对 **6 个 category** 逐一给出：典型场景、推荐生成方案（注入方式）、**触发后必须出现的日志/Trace 信号**、以及 demo-app 需要补的**埋点**。
>
> 弱模型创作时：**先查本节确认"这类 bug 需要什么信号 + demo-app 是否已具备"**，再写 recipe。信号不具备就先补埋点（对应第 10 节 P0/P1），否则 bug 不可诊断。

每个场景统一用这个结构描述：

```
场景 → 注入方案（diff_patch 思路）→ 触发要点 → 必需日志信号 → 必需 Trace 信号 → demo-app 需补的埋点
```

---

### 13.1 `performance` — 性能问题

**典型场景**：N+1 查询（BE-001）、缺索引慢查询（PERF-001）、缓存穿透（PERF-002）、轮询风暴（PERF-003）。

| 场景 | 注入方案 | 触发要点 |
|------|----------|----------|
| **N+1 查询** | diff_patch：删 `selectinload`，改成循环逐条查 | 造 ≥50 条关联数据后请求列表 |
| **缺索引** | diff_patch：删 model 上 `index=True`，并生成对应 alembic 迁移 | 造 ≥200 条数据后按该列过滤查询 |
| **缓存穿透** | diff_patch：去掉"查不到也写空值缓存"的逻辑 | 高频请求不存在的 id |
| **轮询风暴** | diff_patch（前端）：把合理轮询间隔改成极短（如 100ms） | 打开页面停留，观察请求频率 |

**必需日志信号**（→ 依赖 P0-1 SQL echo / P1-3 慢查询日志）：
```json
{"event":"slow_query","sql":"SELECT ... FROM comments WHERE task_id=$1","duration_ms":31,"trace_id":"..."}   // 重复 N 次
{"event":"http_request","path":"/api/.../tasks","latency_ms":2384,"status":200,"trace_id":"..."}            // 总耗时偏高
```

**必需 Trace 信号**（→ 依赖 P0-2 instrument_sqlalchemy）：
- HTTP span 下挂 **大量重复的 SQL 子 span**（N+1：50 个相同形状的 SELECT；缺索引：单条 SQL span 耗时显著偏高）。
- span 上有 `db.statement` 属性，便于识别是哪条查询慢。

**demo-app 需补的埋点**：P0-1（SQL echo）+ P0-2（SQL trace span）+ P1-3（慢查询专项日志）。**这是 performance 类的硬前提**。

---

### 13.2 `backend_error` — 后端异常

**典型场景**：校验绕过引发的脏数据异常（BE-002）、JWT 过期处理不当（BE-003）、未捕获异常导致 500。

| 场景 | 注入方案 | 触发要点 |
|------|----------|----------|
| **校验绕过** | diff_patch：删 Pydantic 校验或 `if not valid: raise` 分支 | 发送非法 payload（超长、负数、缺字段） |
| **JWT 过期处理错误** | diff_patch：把 token 过期校验注释掉，或吞掉过期异常 | 用过期/伪造 token 请求受保护端点 |
| **未捕获异常** | diff_patch：在关键路径引入会抛错的操作（如对 None 取属性） | 触发该路径 |

**必需日志信号**（→ 依赖 P0-3 全局异常处理器）：
```json
{"event":"unhandled_exception","path":"/api/tasks","method":"POST","exc_type":"ValidationError",
 "exc_msg":"...","trace_id":"...","stack":"Traceback (most recent call last): ..."}
{"event":"http_request","path":"/api/tasks","status":500,"trace_id":"..."}
```

**必需 Trace 信号**：
- HTTP span 的 `status = ERROR`，span 上记录 `exception.type` / `exception.message` / `exception.stacktrace`（OTel 异常事件）。

**demo-app 需补的埋点**：P0-3（异常处理器，把 `exc_type`/`stack`/`trace_id` 写进结构化日志）+ P0-4（访问日志带 status）。没有异常处理器，500 只会是 uvicorn 一行干巴巴的报错，无法定位文件/行号。

---

### 13.3 `logic` — 业务逻辑错误

**典型场景**：越权访问（LOGIC-001）、并发竞态（LOGIC-002）、时区计算错误（LOGIC-003）。

| 场景 | 注入方案 | 触发要点 |
|------|----------|----------|
| **越权** | diff_patch：**删除真实存在的 owner 校验代码**（不是"假装"） | A 用户造数据，B 用户访问 A 的资源 |
| **并发竞态** | diff_patch：去掉锁/原子操作/`SELECT FOR UPDATE` | `api_call` + `repeat` 并发改同一资源 |
| **时区错误** | diff_patch：把 `datetime.now(tz=utc)` 改成 naive `datetime.now()` | 创建带时间的资源后校验返回时间 |

**必需日志信号**（→ 依赖 P1-2 业务关键路径埋点）：
```json
// 越权：能看出"谁访问了谁的资源"
{"event":"access_resource","user_id":"alice","resource_owner_id":"admin","project_id":"...","allowed":true,"trace_id":"..."}
// 竞态：能看出同一资源被并发改写
{"event":"task_update","task_id":"...","old_status":"todo","new_status":"done","actor":"...","trace_id":"..."}
```

**必需 Trace 信号**：
- 越权：trace 显示请求成功（200）但**缺少**鉴权检查 span / 鉴权 span 未拒绝。
- 竞态：多个并发请求的 span 时间重叠，作用于同一 `resource_id`。

**demo-app 需补的埋点**：**P1-2 是关键** —— 在鉴权点、状态变更点记录 `user_id`/`resource_owner_id`/`old→new` 等业务字段。logic 类 bug **靠通用日志看不出来**，必须有业务语义日志。响应体（`api_response`）也是重要证据，触发器应保存响应内容。

---

### 13.4 `data` — 数据问题

**典型场景**：编码错误（DATA-001）、浮点精度（DATA-002）、时区/格式（与 logic 时区类似但侧重数据值本身）。

| 场景 | 注入方案 | 触发要点 |
|------|----------|----------|
| **编码错误** | diff_patch：对响应/存储用错误编码（如强制 latin-1） | 提交含中文/emoji 的内容后读回 |
| **浮点精度** | diff_patch：金额/统计用 `float` 累加替代 `Decimal` | 多次小额累加后校验总和 |

**必需日志信号**（→ 依赖 P1-2 业务埋点，记录关键字段值）：
```json
{"event":"data_persist","field":"title","raw":"任务✓","stored":"ä»»å¡","trace_id":"..."}   // 编码异常可见
{"event":"sum_compute","inputs":[0.1,0.2],"result":0.30000000000000004,"trace_id":"..."}    // 精度异常可见
```

**必需 Trace 信号**：
- 一般无专门 trace 信号；若编码错误引发异常，则退化为 backend_error 的 ERROR span。

**demo-app 需补的埋点**：**P1-2，且要记录"关键字段的实际值"**（写入前/读出后）。data 类 bug 的本质是"值不对"，日志必须包含**具体数据值**才能诊断，仅记录"发生了一次写入"无用。注意脱敏：只记录诊断必需字段。

---

### 13.5 `config` — 配置/环境问题

**典型场景**：CORS 配置错误（CONFIG-001）、环境变量缺失/错配、连接串错误。

| 场景 | 注入方案 | 触发要点 |
|------|----------|----------|
| **CORS 错配** | diff_patch：把 `allow_origins` 改成错误域名或空 | 从前端域发跨域请求 |
| **环境变量错配** | diff_patch / 改 compose env：把某连接串指向错误地址 | 启动应用或触发依赖该配置的功能 |

**必需日志信号**：
```json
// CORS：浏览器侧被拦，后端可记录 Origin 不匹配
{"event":"cors_rejected","origin":"http://evil.com","allowed_origins":["http://localhost:3000"],"trace_id":"..."}
// 连接错配：启动期或首次调用时连接失败
{"event":"dependency_error","target":"postgres://wrong-host:5432","error":"Connection refused"}
```

**必需 Trace 信号**：
- 依赖连接失败时，外呼 span（DB/HTTP client）`status = ERROR`。
- CORS 属于浏览器行为，后端 trace 通常只看到 OPTIONS 预检请求。

**demo-app 需补的埋点**：
- CORS：加一个记录被拒 Origin 的中间件/日志（默认 Starlette CORS 不打日志）。
- 配置类问题很多发生在**启动期**，需保证启动日志（含配置摘要、依赖连通性检查）也写到 stdout → Loki。
- **特别注意时间窗口**：config bug 常在启动时暴露，证据收集的时间窗要覆盖应用启动时刻，否则采集不到关键启动日志。

---

### 13.6 `frontend_crash` — 前端崩溃

**典型场景**：访问 null 属性导致白屏（FE-001）、未捕获 Promise（FE-002）、状态错乱（FE-003）。

| 场景 | 注入方案 | 触发要点 |
|------|----------|----------|
| **null 属性访问** | diff_patch：删 `?.` 可选链或默认值兜底 | 构造缺字段的数据（如无 assignee 的 task）后打开详情页 |
| **未捕获 Promise** | diff_patch：删 `.catch()` / `try-catch` | 触发会 reject 的异步调用（如请求失败） |
| **状态错乱** | diff_patch：破坏 store 更新逻辑（如直接 mutate） | 连续操作触发状态不一致 |

**必需日志信号**（→ 依赖 P2-1 前端错误上报）：
```json
// 通过 /api/client-logs 上报，打 service_name="demo-frontend"
{"event":"client_error","message":"Cannot read properties of null (reading 'name')",
 "component_stack":"at TaskDetailPage ...","route":"/tasks/123","user_action":"open_detail","trace_id":"..."}
```

**必需 Trace 信号**：
- 前端崩溃本身不产后端 trace；但若崩溃前有失败的 API 调用，对应后端 span 可作为辅助证据（用 `trace_id` 关联前后端）。

**demo-app 需补的埋点**：**P2-1 是硬前提** —— 前端加 `window.onerror` + React `ErrorBoundary`，捕获错误（含 message、组件栈、当前路由、触发操作）上报到后端 `/api/client-logs`，再转发 Loki。**没有这个通道，frontend_crash 类 bug 在证据里完全不可见**（这也是 FE 类 case 当前最大缺口）。前端触发依赖 `ui_click`（Playwright），需补对应测试与运行环境。

---

### 13.7 各类别"信号-埋点"依赖速查表

| category | 核心信号载体 | 必需埋点（第 10 节） | 当前缺口 |
|----------|--------------|---------------------|----------|
| `performance` | SQL trace span + 慢查询日志 | P0-1 + P0-2 + P1-3 | SQL 既不进日志也不进 trace |
| `backend_error` | 异常结构化日志 + ERROR span | P0-3 + P0-4 | 无异常处理器 |
| `logic` | 业务语义日志 + 响应体 | P1-2 + 触发器保存响应 | 业务零日志 |
| `data` | 含**具体字段值**的业务日志 | P1-2（记录值） | 业务零日志 |
| `config` | 启动日志 + 依赖错误日志 | 启动埋点 + 扩大采集时间窗 | 启动日志未采集 |
| `frontend_crash` | 前端错误上报日志 | P2-1（前端上报通道） | 前端无上报，最大缺口 |

> **创作铁律**：写任何 recipe 前，先在本表确认该类别的"必需埋点"是否已落地。**未落地 → 先补埋点（P 级改造）→ 再写 recipe**。否则注入再成功，证据也是空的，bug 一律低质量。

---

## 附录：最小落地顺序建议

1. **P0-1**（打开 SQL echo）—— 改 1 行，让 N+1 的 SQL 直接进 Loki，收益最大。
2. **P0-2**（`instrument_sqlalchemy()`）—— 让 SQL 进 trace span。
3. **P2-2**（恢复 tasks.py 健康 baseline）—— 解除工作区污染。
4. **P0-3 / P0-4**（异常处理器 + 访问日志）—— 让 backend_error 可诊断。
5. 用 **diff_patch 重写 BE-001 / LOGIC-001** 两个 recipe（第 4、7 节）。
6. 真实跑通 BE-001 全链路，确认第 11 节的信号真的出现。
7. 加 **evidence_validator**（第 12 节），把验证固化进流水线。
8. 其余 recipe 依次按第 13 节的分类目录 + 第 8 节清单重写、验证。
