import { useState } from "react";
import { Link, useNavigate, useLocation } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import api from "@/services/api";
import type { LoginResponse, User } from "@/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const setAuth = useAuthStore((s) => s.setAuth);

  const from = (location.state as { from?: string })?.from || "/projects";

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const loginRes = await api.post<LoginResponse>("/api/auth/login", {
        email,
        password,
        display_name: email, // Backend requires display_name but ignores it on login
      });

      const token = loginRes.data.access_token;

      // Fetch current user profile
      const meRes = await api.get<User>("/api/auth/me", {
        headers: { Authorization: `Bearer ${token}` },
      });

      setAuth(token, meRes.data);
      navigate(from, { replace: true });
    } catch (err: unknown) {
      const message =
        err instanceof Error
          ? err.message
          : (err as { response?: { data?: { detail?: string } } })?.response?.data
              ?.detail || "登录失败，请检查邮箱和密码";
      console.error("[LOGIN_FAIL]", { email, error: message });
      setError(message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <Card className="w-full max-w-md" size="default">
        <CardHeader>
          <CardTitle className="text-2xl">登录 TaskFlow</CardTitle>
          <CardDescription>输入您的邮箱和密码登录</CardDescription>
        </CardHeader>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4 px-(--card-spacing) pb-(--card-spacing)">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="email">邮箱</Label>
            <Input
              id="email"
              type="email"
              placeholder="admin@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoComplete="email"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="password">密码</Label>
            <Input
              id="password"
              type="password"
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoComplete="current-password"
            />
          </div>
          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}
          <Button type="submit" disabled={loading} className="w-full">
            {loading ? "登录中..." : "登录"}
          </Button>
          <p className="text-center text-sm text-muted-foreground">
            还没有账号？{" "}
            <Link to="/register" className="text-primary hover:underline">
              注册
            </Link>
          </p>
        </form>
      </Card>
    </div>
  );
}
