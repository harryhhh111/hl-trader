import { FormEvent, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError, sessionApi } from "@/lib/api/client";

export function LoginPage() {
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const session = useQuery({ queryKey: ["session"], queryFn: sessionApi.session, retry: false });
  const qc = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();

  if (session.data?.authenticated) return <Navigate to="/hyperliquid" replace />;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      await sessionApi.login(token);
      setToken("");
      await qc.invalidateQueries({ queryKey: ["session"] });
      const from = (location.state as { from?: string } | null)?.from || "/hyperliquid";
      navigate(from, { replace: true });
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : "登录失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="min-h-screen flex items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle>hl-app 登录</CardTitle>
          <CardDescription>输入管理员提供的随机登录 token。token 不会保存到浏览器存储。</CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={submit}>
            <label className="block text-sm space-y-2">
              <span>登录 token</span>
              <input
                aria-label="登录 token"
                className="w-full border rounded-md bg-background px-3 py-2"
                type="password"
                autoComplete="off"
                value={token}
                onChange={(event) => setToken(event.target.value)}
                required
              />
            </label>
            {error && <p className="text-sm text-rose-600" role="alert">{error}</p>}
            <Button className="w-full" disabled={submitting || !token} type="submit">
              {submitting ? "登录中…" : "登录"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}
