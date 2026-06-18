import { useState, useEffect, useCallback } from "react";
import { useParams, useNavigate } from "react-router-dom";
import api from "@/services/api";
import type { Task, TaskStatus } from "@/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  DndContext,
  DragOverlay,
  closestCorners,
  PointerSensor,
  useSensor,
  useSensors,
  useDroppable,
  type DragStartEvent,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  verticalListSortingStrategy,
  useSortable,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { ArrowLeft, Plus, GripVertical } from "lucide-react";

const COLUMNS: { key: TaskStatus; label: string; color: string }[] = [
  { key: "todo", label: "待办", color: "bg-neutral-200 dark:bg-neutral-700" },
  { key: "doing", label: "进行中", color: "bg-blue-200 dark:bg-blue-800" },
  { key: "done", label: "已完成", color: "bg-green-200 dark:bg-green-800" },
];

// ---- Droppable Column ----
function DroppableColumn({
  column,
  taskCount,
  children,
}: {
  column: (typeof COLUMNS)[0];
  taskCount: number;
  children: React.ReactNode;
}) {
  const { setNodeRef, isOver } = useDroppable({ id: column.key });

  return (
    <div
      ref={setNodeRef}
      id={column.key}
      className={`flex flex-col rounded-xl border border-border p-3 min-h-[200px] transition-colors ${
        isOver ? "bg-muted/60 ring-2 ring-primary/30" : "bg-muted/30"
      }`}
    >
      <div className="mb-3 flex items-center gap-2">
        <span
          className={`inline-block size-2.5 rounded-full ${column.color}`}
        />
        <h3 className="text-sm font-semibold text-foreground">
          {column.label}
        </h3>
        <Badge variant="secondary" className="ml-auto text-xs">
          {taskCount}
        </Badge>
      </div>
      <div className="flex flex-col gap-2">{children}</div>
    </div>
  );
}

// ---- Sortable Task Card ----
function SortableTaskCard({
  task,
  onClick,
}: {
  task: Task;
  onClick: () => void;
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: task.id, data: { task } });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className="group relative"
      {...attributes}
    >
      <Card
        className="cursor-pointer transition-shadow hover:shadow-md"
        size="sm"
        data-testid={`task-link-${task.title}`}
        onClick={onClick}
      >
        <CardHeader>
          <div className="flex items-start gap-2">
            {/* Drag handle — listeners ONLY on this button, NOT overridden */}
            <button
              {...listeners}
              className="mt-0.5 cursor-grab active:cursor-grabbing opacity-0 group-hover:opacity-100 transition-opacity touch-none"
              aria-label="拖拽排序"
              onClick={(e) => e.stopPropagation()}
            >
              <GripVertical className="size-4 text-muted-foreground" />
            </button>
            <div className="flex-1 min-w-0">
              <CardTitle className="text-sm leading-snug">{task.title}</CardTitle>
              {task.priority > 0 && (
                <Badge variant="outline" className="mt-1 text-xs">
                  P{task.priority}
                </Badge>
              )}
            </div>
          </div>
        </CardHeader>
      </Card>
    </div>
  );
}

// ---- Main Page ----
export default function TaskBoardPage() {
  const { id: projectId } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [projectName, setProjectName] = useState("");

  // New task form state
  const [newTitle, setNewTitle] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [creating, setCreating] = useState(false);

  // Drag state
  const [activeTask, setActiveTask] = useState<Task | null>(null);

  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: { distance: 8 },
    })
  );

  const fetchTasks = useCallback(async () => {
    if (!projectId) return;
    try {
      setError("");
      const res = await api.get<Task[]>(`/api/projects/${projectId}/tasks`);
      setTasks(res.data);
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response
        ?.data?.detail || "加载任务失败";
      console.error("[TASK_LOAD_FAIL]", { projectId, error: msg });
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    fetchTasks();
  }, [fetchTasks]);

  // Fetch project name
  useEffect(() => {
    if (!projectId) return;
    api
      .get(`/api/projects/${projectId}`)
      .then((res) => setProjectName(res.data.name))
      .catch(() => {
        console.warn("[PROJECT_NAME_LOAD_FAIL]", { projectId });
      });
  }, [projectId]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newTitle.trim() || !projectId) return;
    setCreating(true);
    try {
      const res = await api.post<Task>(`/api/projects/${projectId}/tasks`, {
        title: newTitle.trim(),
        description: newDesc.trim() || null,
        status: "todo",
        priority: 0,
      });
      setTasks((prev) => [...prev, res.data]);
      setNewTitle("");
      setNewDesc("");
      setDialogOpen(false);
      console.log("[TASK_CREATED]", { taskId: res.data.id, title: res.data.title });
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response
        ?.data?.detail || "创建任务失败";
      console.error("[TASK_CREATE_FAIL]", { projectId, error: msg });
    } finally {
      setCreating(false);
    }
  };

  const handleDragStart = (event: DragStartEvent) => {
    const task = tasks.find((t) => t.id === event.active.id);
    setActiveTask(task || null);
  };

  const handleDragEnd = async (event: DragEndEvent) => {
    setActiveTask(null);
    const { active, over } = event;
    if (!over || !projectId) return;

    const activeId = active.id as string;
    const task = tasks.find((t) => t.id === activeId);
    if (!task) return;

    // Determine target column
    let targetStatus: TaskStatus;
    const overTask = tasks.find((t) => t.id === over.id);
    if (overTask) {
      targetStatus = overTask.status;
    } else {
      // Dropped on a column droppable area
      targetStatus = over.id as TaskStatus;
      if (!["todo", "doing", "done"].includes(targetStatus)) return;
    }

    if (task.status === targetStatus) return; // No change

    // Optimistic update
    const prevTasks = [...tasks];
    setTasks((prev) =>
      prev.map((t) => (t.id === activeId ? { ...t, status: targetStatus } : t))
    );

    try {
      await api.patch(`/api/tasks/${activeId}`, { status: targetStatus });
      console.log("[TASK_STATUS_UPDATED]", {
        taskId: activeId,
        from: task.status,
        to: targetStatus,
      });
    } catch (err: unknown) {
      // Revert on failure
      setTasks(prevTasks);
      const msg = (err as { response?: { data?: { detail?: string } } })?.response
        ?.data?.detail || "更新任务状态失败";
      console.error("[TASK_STATUS_UPDATE_FAIL]", {
        taskId: activeId,
        error: msg,
      });
    }
  };

  const getColumnTasks = (status: TaskStatus) =>
    tasks.filter((t) => t.status === status);

  return (
    <div className="min-h-screen bg-background">
      {/* Header */}
      <header className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
          <div className="flex items-center gap-3">
            <Button variant="ghost" size="icon" onClick={() => navigate("/projects")}>
              <ArrowLeft className="size-4" />
            </Button>
            <h1 className="text-lg font-semibold text-foreground">
              {projectName || "任务看板"}
            </h1>
          </div>
          <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
            <DialogTrigger className="inline-flex h-8 shrink-0 items-center justify-center gap-1.5 rounded-lg bg-primary px-2.5 text-sm font-medium text-primary-foreground hover:bg-primary/80 transition-colors">
              <Plus className="size-4" />
              新建任务
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>新建任务</DialogTitle>
              </DialogHeader>
              <form onSubmit={handleCreate} className="flex flex-col gap-4">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="taskTitle">任务标题</Label>
                  <Input
                    id="taskTitle"
                    placeholder="输入任务标题"
                    value={newTitle}
                    onChange={(e) => setNewTitle(e.target.value)}
                    required
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="taskDesc">任务描述（可选）</Label>
                  <Input
                    id="taskDesc"
                    placeholder="简单描述这个任务"
                    value={newDesc}
                    onChange={(e) => setNewDesc(e.target.value)}
                  />
                </div>
                <div className="flex justify-end gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => setDialogOpen(false)}
                  >
                    取消
                  </Button>
                  <Button type="submit" disabled={creating || !newTitle.trim()}>
                    {creating ? "创建中..." : "创建"}
                  </Button>
                </div>
              </form>
            </DialogContent>
          </Dialog>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-4 py-6">
        {error && (
          <div className="mb-4 rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
            {error}
            <Button variant="link" size="sm" onClick={fetchTasks} className="ml-2">
              重试
            </Button>
          </div>
        )}

        {loading ? (
          <p className="text-center text-muted-foreground py-12">加载中...</p>
        ) : (
          <DndContext
            sensors={sensors}
            collisionDetection={closestCorners}
            onDragStart={handleDragStart}
            onDragEnd={handleDragEnd}
          >
            <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
              {COLUMNS.map((col) => {
                const colTasks = getColumnTasks(col.key);
                return (
                  <DroppableColumn key={col.key} column={col} taskCount={colTasks.length}>
                    <SortableContext
                      items={colTasks.map((t) => t.id)}
                      strategy={verticalListSortingStrategy}
                    >
                      {colTasks.map((task) => (
                        <SortableTaskCard
                          key={task.id}
                          task={task}
                          onClick={() => navigate(`/tasks/${task.id}`)}
                        />
                      ))}
                    </SortableContext>
                  </DroppableColumn>
                );
              })}
            </div>

            <DragOverlay>
              {activeTask ? (
                <Card size="sm" className="rotate-2 shadow-xl">
                  <CardHeader>
                    <CardTitle className="text-sm">{activeTask.title}</CardTitle>
                  </CardHeader>
                </Card>
              ) : null}
            </DragOverlay>
          </DndContext>
        )}
      </main>
    </div>
  );
}
