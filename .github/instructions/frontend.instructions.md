# Frontend Development Instructions

> **applyTo**: `demo-app/frontend/**`
> **description**: "Use when: working on React components, TypeScript types, Zustand stores, API service layer, shadcn/ui components, Tailwind CSS, or Vite configuration."

---

## 组件约定

### 页面组件 (`src/pages/`)
- 每个页面一个文件，命名 `PascalCase`：`ProjectsPage.tsx`, `TaskBoardPage.tsx`
- 页面是路由的目标组件，包含完整页面逻辑
- 页面内自管理状态（`useState` + `useEffect` + API 调用），不从外部注入

### UI 组件 (`src/components/ui/`)
- 基于 shadcn/ui 的原子组件：`button.tsx`, `card.tsx`, `dialog.tsx` 等
- 全部从 `@/components/ui/xxx` 导入，**不要**直接使用底层库（如 `@base-ui/react`）
- 使用 `cn()` from `@/lib/utils` 合并 className

```tsx
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";

<Button variant="default" size="sm">Click</Button>
```

### 功能组件 (`src/components/`)
- `ErrorBoundary.tsx`：全局错误边界，包裹在 App 最外层
- `ProtectedRoute.tsx`：认证守卫，检查 JWT token 是否存在
- 新增可复用组件放在 `src/components/` 下

---

## 状态管理（Zustand）

- 全局状态用 Zustand store，放在 `src/stores/`
- 模式：`create<T>()(persist(...))` 用于需要持久化的状态（如 token）
- 获取 store 状态（在非组件中）：`useAuthStore.getState().token`
- Store 接口定义 `XxxState` 类型

```typescript
import { create } from "zustand";
import { persist } from "zustand/middleware";

interface MyState {
  data: string | null;
  setData: (data: string) => void;
}

export const useMyStore = create<MyState>()(
  persist(
    (set, get) => ({...}),
    { name: "my-storage-key" }
  )
);
```

---

## API 调用层 (`src/services/api.ts`)

- 基于 axios 的单例 `api`，已配置：
  - `baseURL`：`import.meta.env.VITE_API_BASE_URL`
  - 自动注入 Bearer token（request interceptor）
  - 401 自动跳转登录页（response interceptor）
  - 结构化错误日志
- **所有 API 调用都通过此实例**，不要直接使用 `fetch()` 或创建新的 axios 实例
- 不需要额外封装 service 层函数，直接在组件中 `api.get<T>()`

```typescript
const res = await api.get<Project[]>("/api/projects/");
const res = await api.post<Task>("/api/tasks/", payload);
```

---

## 类型定义 (`src/types/index.ts`)

- 前端类型与后端 Pydantic schema 保持一致
- 全部 export interface，字段用 `camelCase`
- 枚举用 union type：`status: "todo" | "doing" | "done"`

```typescript
export interface Task {
  id: string;
  title: string;
  status: "todo" | "doing" | "done";
  priority: number;
  // ...
}
```

---

## 路由 (React Router)

- 路由在 `App.tsx` 中集中定义
- 公开路由：`/login`, `/register`
- 需认证路由：用 `<ProtectedRoute>` 包裹
- 路由路径模式：
  - `/projects` → 项目列表
  - `/projects/:id` → 项目任务看板
  - `/tasks/:id` → 任务详情

---

## 样式（Tailwind CSS + shadcn/ui）

- 使用 Tailwind utility classes，不要写自定义 CSS 文件
- shadcn/ui 组件通过 `variant` / `size` prop 控制样式
- 图标用 `lucide-react`
- 颜色用 Tailwind 语义 token：`bg-background`, `text-foreground`, `border-border`, `bg-card`

---

## 错误处理

- 页面组件中 `try/catch` API 调用，设置局部 `error` state 显示错误信息
- API 层已统一处理 401 → 登出 + 跳转
- 结构化日志：`console.log("[MODULE_ACTION]", { key: value })`

```typescript
try {
  const res = await api.get<Project[]>("/api/projects/");
  setProjects(res.data);
} catch (err: unknown) {
  const msg = (err as { response?: { data?: { detail?: string } } })?.response
    ?.data?.detail || "加载失败";
  console.error("[PROJECTS_LOAD_FAIL]", { error: msg });
  setError(msg);
}
```

---

## 禁止事项

- ❌ 不要引入新的 UI 库（已有 shadcn/ui 全覆盖）
- ❌ 不要使用 `any` 类型（用 `unknown` + type guard）
- ❌ 不要在组件中直接操作 DOM（用 React 状态驱动）
- ❌ 不要硬编码 API URL（用 `import.meta.env.VITE_API_BASE_URL`）
- ❌ 不要绕过 ProtectedRoute 的认证逻辑
- ❌ 不要使用 `fetch()` 替代 axios `api` 实例
