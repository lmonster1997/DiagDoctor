import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import api from "@/services/api";
import type { User } from "@/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";

export default function RegisterPage() {
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    if (password !== confirmPassword) {
      setError("两次输入的密码不一致");
      console.warn("[REGISTER_PASSWORD_MISMATCH]", { email });
      return;
    }

    if (password.length < 8) {
      setError("密码长度至少 8 位");
      return;
    }

    setLoading(true);

    try {
      await api.post<User>("/api/auth/register", {
        email,
        password,
        display_name: displayName,
      });
      console.log("[REGISTER_SUCCESS]", { email });
      navigate("/login", {
        state: { message: "注册成功！请登录" },
      });
    } catch (err: unknown) {
      const message =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail || "注册失败，请重试";
      console.error("[REGISTER_FAIL]", { email, error: message });
      setError(message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <Card className="w-full max-w-md" size="default">
        <CardHeader>
          <CardTitle className="text-2xl">注册 TaskFlow</CardTitle>
          <CardDescription>创建您的账号以开始使用</CardDescription>
        </CardHeader>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4 px-(--card-spacing) pb-(--card-spacing)">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="displayName">用户名</Label>
            <Input
              id="displayName"
              type="text"
              placeholder="您的昵称"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              required
              minLength={1}
              maxLength={100}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="email">邮箱</Label>
            <Input
              id="email"
              type="email"
              placeholder="you@example.com"
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
              placeholder="至少 8 位"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={8}
              autoComplete="new-password"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="confirmPassword">确认密码</Label>
            <Input
              id="confirmPassword"
              type="password"
              placeholder="再次输入密码"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
              autoComplete="new-password"
            />
          </div>
          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}
          <Button type="submit" disabled={loading} className="w-full">
            {loading ? "注册中..." : "注册"}
          </Button>
          <p className="text-center text-sm text-muted-foreground">
            已有账号？{" "}
            <Link to="/login" className="text-primary hover:underline">
              登录
            </Link>
          </p>
        </form>
      </Card>
    </div>
  );
}
