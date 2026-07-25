## 工具速查

> DiagDoctor V3 统一工具集。共 6 个工具，覆盖日志/ Trace/代码搜索/前端分析/文件读取 + 历史根因检索。

---

### search_observability — 统一可观测性查询 ⭐ 优先使用

统一查询入口，合并了日志查询和 Trace 查询。**首选工具**。

```
# 查日志 —— ⚠️ 省略 start/end，工具自动取 trigger_time ± 5min 窗口（精确覆盖本次触发的事件）。
#            绝不要自己编造/照抄示例里的日期，那会查到过期空结果，或混入其他 case 的日志。
search_observability(source="loki", query='{service_name="demo-backend"} |= "error"')

# 查 Trace（用 trace_id）—— 不需要时间范围
search_observability(source="tempo", query="<32位hex trace_id>")

# 按服务名搜索 Trace —— 同样省略 start/end
search_observability(source="tempo", query="demo-backend")

# 自动关联：先查 Loki → 提取 trace_id → 自动查 Tempo → 分析
search_observability(source="auto", query='{service_name="demo-backend"} |= "error"', analysis="full")
```

| 参数 | 说明 |
|------|------|
| `source` | `"loki"` 查日志 / `"tempo"` 查 Trace / `"auto"` 自动关联 |
| `query` | LogQL（loki/auto）或 trace_id/服务名（tempo） |
| `start` | ISO 起始时间。**默认省略**——工具自动取 `trigger_time ± 5min`（只看本次触发附近，避免混入其他 case 的信号） |
| `end` | ISO 结束时间。**默认省略**——同上 |
| `analysis` | `"raw"` 原始 / `"n_plus_one"` N+1检测 / `"bottlenecks"` 瓶颈 / `"errors"` 错误span / `"full"` 全部 |
| `limit` | 最大返回条数（默认 20） |

**时间范围**：跨度不超过 4 小时。**绝大多数情况下省略 start/end**——工具会自动锁定本次 `trigger_time ± 5min`。若你显式传入的窗口整体早于当前 1 小时，工具会判定为过期并自动改用默认窗口（结果 metadata 里会标注 `time_range_auto_corrected`）。

**返回 JSON 结构**：`{ source, query, time_range, logs: [...], traces: [...], analysis: { n_plus_one, bottlenecks, error_spans, summary } }`

---

### code_search — 精确搜索代码库

使用 ripgrep 在 demo-app 代码库中精确搜索函数名、类名、变量名、错误信息等。

```
code_search(query="list_tasks", k=5)
```

| 参数 | 说明 |
|------|------|
| `query` | 搜索关键词（函数名/类名/变量名/错误信息片段） |
| `k` | 返回结果数（默认 5） |

**提示**：用具体的标识符搜索（如 `TaskResponse`、`list_tasks`），而非自然语言描述。

---

### db_query — 只读数据库查询

对 demo-app 数据库执行**只读** SQL 查询。仅允许 SELECT 语句。

```
db_query(sql="SELECT id, status, title FROM tasks WHERE id = '...'")
```

| 参数 | 说明 |
|------|------|
| `sql` | SELECT 查询语句。禁止 INSERT/UPDATE/DELETE/DROP 等写操作 |

---

### inspect_frontend_error — 前端错误分析

一站式前端错误分析工具。输入 browser_errors.json 内容，输出结构化分析。

```
inspect_frontend_error(browser_errors="<browser_errors.json 的 JSON 字符串内容>")
```

| 参数 | 说明 |
|------|------|
| `browser_errors` | browser_errors.json 的完整 JSON 字符串 |
| `resolve_sourcemap` | 是否还原 source map（默认 true） |

**返回 JSON 结构**：
```json
{
  "errors": [{
    "type": "TypeError(undefined_access)",
    "message": "...",
    "stack_frames": [{"file": "/src/pages/TaskBoardPage.tsx", "line": 148, "component": "SortableTaskCard"}],
    "cross_layer_hint": "该错误是读取 undefined 属性，建议检查后端 API 响应是否缺字段"
  }],
  "summary": "共 2 个前端错误，其中 1 个疑似跨层根因",
  "total": 2,
  "cross_layer_count": 1
}
```

**跨层提示**：自动检测以下模式并生成跨层诊断建议：
- `Cannot read properties of undefined` → 可能后端 API 缺字段
- `Cannot read properties of null` → 可能 API 返回空
- 消息中含 API/fetch/response → 可能后端响应异常

---

### get_file_content — 读取代码文件

读取 demo-app 代码库中的指定文件，支持行范围截取。

```
get_file_content(file_path="app/services/task_service.py", start_line=40, end_line=60)
```

| 参数 | 说明 |
|------|------|
| `file_path` | 相对于 demo-app 根目录的路径。如 `"app/services/task_service.py"` 或 `"src/pages/TaskBoardPage.tsx"` |
| `start_line` | 起始行号（1-based，可选） |
| `end_line` | 结束行号（1-based，可选） |

**限制**：
- 最大返回 200 行（超出自动截断）
- 文件最大 500KB（超出拒绝读取）
- 路径必须在 demo-app 范围内（拒绝目录遍历攻击）
- 二进制文件返回友好错误

---

### search_historical_root_cause - 历史根因检索（知识库）

按**根因假设**检索该项目知识库里 👍 入库的已解决历史 Bug（走独立的根因向量，**非症状相似**）。当你已经形成一个**具体根因假设**时调用，拿回根因相似的历史诊断思路作参考。

```
search_historical_root_cause(hypothesis="N+1 查询: list_tasks 对每个 task 单独查 comments")
search_historical_root_cause(hypothesis="空值未判空: assignee_id 为 null 时调 .hex 报 AttributeError")
search_historical_root_cause(hypothesis="IDOR 越权: get_project 缺 owner_id 过滤")
```

| 参数 | 说明 |
|------|------|
| `hypothesis` | 一句话根因假设（中文即可）。**说清机制而非症状**——"N+1 查询"优于"页面慢"；"空值未判空"优于"500 错误" |
| `k` | 返回结果数（默认 3） |

**何时调用**：调查中段、你已经对根因有了一个具体假设（不是刚起步只看到症状时）。它和系统自动注入的「症状相似历史 case」互补——症状相似帮你找表面像的，根因相似帮你找**同一个坑**。

**返回**：历史相似 case 列表（根因/修复/类别/综合分）。空库或无匹配会明确告知（如 "未找到与该根因假设相似的历史 case"），此时正常继续调查即可。

⚠️ 仅为历史参考，请基于当前实际证据独立判断，勿机械套用。

---

## 工具选择决策表

| 你有的线索 | 应该调用的工具 |
|-----------|-------------|
| 有 trace_id | `search_observability(source="tempo", query="<trace_id>")` |
| 只有时间范围 + 服务名 | `search_observability(source="auto", query="<LogQL>", analysis="full")` |
| 日志指向某函数 | `code_search(query="<函数名>")` |
| 需要看代码细节 | `get_file_content(file_path="...", start_line=N, end_line=M)` |
| 有前端报错 | `inspect_frontend_error(browser_errors="...")` |
| 需验证数据库状态 | `db_query(sql="SELECT ... LIMIT 10")` |
| 需要看 Trace 分析 | `search_observability(source="tempo", query="<trace_id>", analysis="full")` |
| 已形成根因假设，想参考历史相似 bug | `search_historical_root_cause(hypothesis="<一句话根因假设>")` |
