import { useState, useEffect, useCallback } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import api from "@/services/api";
import type { Task, Comment } from "@/types";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Card, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { ArrowLeft, Send } from "lucide-react";

const STATUS_LABELS: Record<string, string> = {
  todo: "待办",
  doing: "进行中",
  done: "已完成",
};

const STATUS_VARIANTS: Record<string, "default" | "secondary" | "outline"> = {
  todo: "secondary",
  doing: "default",
  done: "outline",
};

export default function TaskDetailPage() {
  const { id: taskId } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const currentUser = useAuthStore((s) => s.currentUser);

  const [task, setTask] = useState<Task | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [commentText, setCommentText] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const fetchTask = useCallback(async () => {
    if (!taskId) return;
    try {
      setError("");
      const res = await api.get<Task>(`/api/tasks/${taskId}`);
      setTask(res.data);
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response
        ?.data?.detail || "加载任务详情失败";
      console.error("[TASK_DETAIL_LOAD_FAIL]", { taskId, error: msg });
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    fetchTask();
  }, [fetchTask]);

  const handleAddComment = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!commentText.trim() || !taskId) return;
    setSubmitting(true);
    try {
      const res = await api.post<Comment>(`/api/tasks/${taskId}/comments`, {
        content: commentText.trim(),
      });
      // Optimistically add the comment
      setTask((prev) =>
        prev
          ? {
              ...prev,
              comments: [...(prev.comments || []), res.data],
            }
          : prev
      );
      setCommentText("");
      console.log("[COMMENT_ADDED]", { taskId, commentId: res.data.id });
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response
        ?.data?.detail || "添加评论失败";
      console.error("[COMMENT_ADD_FAIL]", { taskId, error: msg });
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <p className="text-muted-foreground">加载中...</p>
      </div>
    );
  }

  if (error || !task) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-background">
        <p className="text-destructive">{error || "任务不存在"}</p>
        <Button variant="outline" onClick={() => navigate(-1)}>
          <ArrowLeft className="size-4" />
          返回
        </Button>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background">
      {/* Header */}
      <header className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-3xl items-center gap-3 px-4 py-3">
          <Button variant="ghost" size="icon" onClick={() => navigate(-1)}>
            <ArrowLeft className="size-4" />
          </Button>
          <h1 className="text-lg font-semibold text-foreground truncate">
            {task.title}
          </h1>
          <Badge variant={STATUS_VARIANTS[task.status] || "secondary"} className="ml-auto">
            {STATUS_LABELS[task.status] || task.status}
          </Badge>
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-4 py-6">
        {/* Task info card */}
        <Card className="mb-6" size="default">
          <CardHeader>
            <CardTitle>{task.title}</CardTitle>
            {task.description && (
              <CardDescription className="whitespace-pre-wrap">
                {task.description}
              </CardDescription>
            )}
          </CardHeader>
          <div className="grid grid-cols-2 gap-2 px-(--card-spacing) pb-(--card-spacing) text-sm">
            <div>
              <span className="text-muted-foreground">优先级：</span>
              <span>{task.priority}</span>
            </div>
            <div>
              <span className="text-muted-foreground">状态：</span>
              <Badge variant={STATUS_VARIANTS[task.status] || "secondary"} className="text-xs">
                {STATUS_LABELS[task.status]}
              </Badge>
            </div>
            {task.due_date && (
              <div>
                <span className="text-muted-foreground">截止日期：</span>
                <span>
                  {new Date(task.due_date).toLocaleDateString("zh-CN")}
                </span>
              </div>
            )}
            <div>
              <span className="text-muted-foreground">创建时间：</span>
              <span>
                {new Date(task.created_at).toLocaleString("zh-CN")}
              </span>
            </div>
          </div>
          {task.tags && task.tags.length > 0 && (
            <div className="flex flex-wrap gap-1 px-(--card-spacing) pb-(--card-spacing)">
              {task.tags.map((tag) => (
                <Badge
                  key={tag.id}
                  variant="outline"
                  style={{ borderColor: tag.color, color: tag.color }}
                >
                  {tag.name}
                </Badge>
              ))}
            </div>
          )}
        </Card>

        {/* Comments section */}
        <div>
          <h2 className="mb-4 text-lg font-semibold text-foreground">
            评论 ({task.comments?.length || 0})
          </h2>

          {/* Comment list */}
          <div className="mb-6 flex flex-col gap-3">
            {task.comments && task.comments.length > 0 ? (
              task.comments.map((comment) => (
                <div
                  key={comment.id}
                  className="rounded-lg border border-border bg-card p-3"
                >
                  <div className="mb-1 flex items-center gap-2 text-xs text-muted-foreground">
                    <span className="font-medium text-foreground">
                      {comment.author_id === currentUser?.id
                        ? "我"
                        : `用户 ${comment.author_id.slice(0, 8)}`}
                    </span>
                    <span>
                      {new Date(comment.created_at).toLocaleString("zh-CN")}
                    </span>
                  </div>
                  <p className="text-sm whitespace-pre-wrap">{comment.content}</p>
                </div>
              ))
            ) : (
              <p className="text-sm text-muted-foreground py-4 text-center">
                暂无评论，成为第一个评论的人
              </p>
            )}
          </div>

          <Separator className="mb-4" />

          {/* Add comment form */}
          <form onSubmit={handleAddComment} className="flex flex-col gap-3">
            <Textarea
              placeholder="添加评论..."
              value={commentText}
              onChange={(e) => setCommentText(e.target.value)}
              rows={3}
              required
            />
            <div className="flex justify-end">
              <Button type="submit" disabled={submitting || !commentText.trim()}>
                <Send className="size-4" />
                {submitting ? "发送中..." : "发表评论"}
              </Button>
            </div>
          </form>
        </div>
      </main>
    </div>
  );
}
