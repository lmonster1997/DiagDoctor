## 工具速查

> DiagDoctor V3 统一工具集。共 5 个工具，覆盖日志/ Trace/代码搜索/前端分析/文件读取。

---

### search_observability — 统一可观测性查询 ⭐ 优先使用

统一查询入口，合并了日志查询和 Trace 查询。**首选工具**。

```
# 查日志
search_observability(source="loki", query='{service_name="demo-backend"} |= "error"', start="2026-06-28T10:00:00Z", end="2026-06-28T14:00:00Z")

# 查 Trace（用 trace_id）
search_observability(source="tempo", query="<32位hex trace_id>")

# 按服务名搜索 Trace
search_observability(source="tempo", query="demo-backend", start=..., end=...)

# 自动关联：先查 Loki → 提取 trace_id → 自动查 Tempo → 分析
search_observability(source="auto", query='{service_name="demo-backend"} |= "error"', start=..., end=..., analysis="full")
```

| 参数 | 说明 |
|------|------|
| `source` | `"loki"` 查日志 / `"tempo"` 查 Trace / `"auto"` 自动关联 |
| `query` | LogQL（loki/auto）或 trace_id/服务名（tempo） |
| `start` | ISO 格式起始时间（可选，默认 1 小时前） |
| `end` | ISO 格式结束时间（可选，默认当前） |
| `analysis` | `"raw"` 原始 / `"n_plus_one"` N+1检测 / `"bottlenecks"` 瓶颈 / `"errors"` 错误span / `"full"` 全部 |
| `limit` | 最大返回条数（默认 20） |

**时间范围限制**：跨度不超过 4 小时。

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
