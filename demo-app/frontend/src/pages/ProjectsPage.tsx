import { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import api from "@/services/api";
import type { Project } from "@/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Plus, LogOut, FolderKanban } from "lucide-react";

export default function ProjectsPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const navigate = useNavigate();
  const { currentUser, logout } = useAuthStore();

  const fetchProjects = useCallback(async () => {
    try {
      setError("");
      const res = await api.get<Project[]>("/api/projects/");
      setProjects(res.data);
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response
        ?.data?.detail || "加载项目列表失败";
      console.error("[PROJECTS_LOAD_FAIL]", { error: msg });
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchProjects();
  }, [fetchProjects]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim()) return;
    setCreating(true);
    try {
      const res = await api.post<Project>("/api/projects/", {
        name: newName.trim(),
        description: newDesc.trim() || null,
      });
      setProjects((prev) => [res.data, ...prev]);
      setNewName("");
      setNewDesc("");
      setDialogOpen(false);
      console.log("[PROJECT_CREATED]", { projectId: res.data.id, name: res.data.name });
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response
        ?.data?.detail || "创建项目失败";
      console.error("[PROJECT_CREATE_FAIL]", { error: msg });
    } finally {
      setCreating(false);
    }
  };

  const handleLogout = () => {
    logout();
    navigate("/login");
  };

  return (
    <div className="min-h-screen bg-background">
      {/* Header */}
      <header className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
          <h1 className="text-lg font-semibold text-foreground">TaskFlow</h1>
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground">
              {currentUser?.display_name || currentUser?.email}
            </span>
            <Button variant="ghost" size="sm" onClick={handleLogout}>
              <LogOut className="size-4" />
              退出
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-4 py-8">
        {/* Page header */}
        <div className="mb-6 flex items-center justify-between">
          <div>
            <h2 className="text-2xl font-bold text-foreground">我的项目</h2>
            <p className="text-sm text-muted-foreground">
              共 {projects.length} 个项目
            </p>
          </div>
          <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
            <DialogTrigger className="inline-flex h-8 shrink-0 items-center justify-center gap-1.5 rounded-lg bg-primary px-2.5 text-sm font-medium text-primary-foreground hover:bg-primary/80 transition-colors">
              <Plus className="size-4" />
              新建项目
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>新建项目</DialogTitle>
              </DialogHeader>
              <form onSubmit={handleCreate} className="flex flex-col gap-4">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="projectName">项目名称</Label>
                  <Input
                    id="projectName"
                    placeholder="输入项目名称"
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    required
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="projectDesc">项目描述（可选）</Label>
                  <Input
                    id="projectDesc"
                    placeholder="简单描述这个项目"
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
                  <Button type="submit" disabled={creating || !newName.trim()}>
                    {creating ? "创建中..." : "创建"}
                  </Button>
                </div>
              </form>
            </DialogContent>
          </Dialog>
        </div>

        {/* Error */}
        {error && (
          <div className="mb-4 rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
            {error}
            <Button variant="link" size="sm" onClick={fetchProjects} className="ml-2">
              重试
            </Button>
          </div>
        )}

        {/* Project list */}
        {loading ? (
          <p className="text-center text-muted-foreground py-12">加载中...</p>
        ) : projects.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
            <FolderKanban className="mb-4 size-12 opacity-30" />
            <p className="text-lg">还没有项目</p>
            <p className="text-sm">点击"新建项目"开始使用</p>
          </div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {projects.map((project) => (
              <Card
                key={project.id}
                className="cursor-pointer transition-shadow hover:shadow-md"
                onClick={() => navigate(`/projects/${project.id}`)}
              >
                <CardHeader>
                  <CardTitle className="text-lg">{project.name}</CardTitle>
                  {project.description && (
                    <CardDescription>{project.description}</CardDescription>
                  )}
                </CardHeader>
                <div className="px-(--card-spacing) pb-(--card-spacing)">
                  <p className="text-xs text-muted-foreground">
                    创建于 {new Date(project.created_at).toLocaleDateString("zh-CN")}
                  </p>
                </div>
              </Card>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
