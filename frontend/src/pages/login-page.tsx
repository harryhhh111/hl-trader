import { FormEvent, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError, sessionApi } from "@/lib/api/client";

export function LoginPage() {
  const [token, setToken] = useState("");
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [passwordConfirmation, setPasswordConfirmation] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const session = useQuery({ queryKey: ["session"], queryFn: sessionApi.session, retry: false });
  const qc = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();

  if (session.isPending) return <main>正在验证登录状态…</main>;
  if (session.data?.authenticated) return <Navigate to="/hyperliquid" replace />;

  async function finish() {
    await qc.invalidateQueries({ queryKey: ["session"] });
    const from = (location.state as { from?: string } | null)?.from || "/hyperliquid";
    navigate(from, { replace: true });
  }

  async function submitToken(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      await sessionApi.login(token);
      setToken("");
      await finish();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : "登录失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function submitAccount(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (mode === "register" && password !== passwordConfirmation) {
      setError("两次输入的密码不一致");
      return;
    }
    setSubmitting(true);
    try {
      if (mode === "register") await sessionApi.accountRegister(email, password);
      else await sessionApi.accountLogin(email, password);
      setPassword("");
      setPasswordConfirmation("");
      await finish();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : mode === "register" ? "注册失败" : "登录失败");
    } finally {
      setSubmitting(false);
    }
  }

  if (session.data?.auth_mode === "account") {
    return (
      <main className="min-h-screen flex items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardHeader>
            <CardTitle>hl-app {mode === "login" ? "登录" : "注册"}</CardTitle>
            <CardDescription>每个账号拥有独立的 Hyperliquid 账户、托管钱包和调仓记录。密码不会保存到浏览器存储。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex gap-2" aria-label="账号操作">
              {(["login", "register"] as const).map((item) => (
                <Button key={item} type="button" variant={mode === item ? "default" : "outline"} className="flex-1"
                  aria-pressed={mode === item} onClick={() => { setMode(item); setError(""); }}>
                  {item === "login" ? "登录" : "注册"}
                </Button>
              ))}
            </div>
            <form className="space-y-4" onSubmit={submitAccount}>
              <label className="block text-sm space-y-2">
                <span>邮箱</span>
                <input aria-label="邮箱" className="w-full border rounded-md bg-background px-3 py-2"
                  type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} required />
              </label>
              <label className="block text-sm space-y-2">
                <span>密码</span>
                <input aria-label="密码" className="w-full border rounded-md bg-background px-3 py-2"
                  type="password" autoComplete={mode === "login" ? "current-password" : "new-password"}
                  minLength={mode === "register" ? 12 : 1} value={password} onChange={(event) => setPassword(event.target.value)} required />
              </label>
              {mode === "register" && <label className="block text-sm space-y-2">
                <span>确认密码</span>
                <input aria-label="确认密码" className="w-full border rounded-md bg-background px-3 py-2"
                  type="password" autoComplete="new-password" minLength={12}
                  value={passwordConfirmation} onChange={(event) => setPasswordConfirmation(event.target.value)} required />
              </label>}
              {mode === "register" && <p className="text-xs text-muted-foreground">密码至少 12 个字符。首个注册用户会接管当前已有资源，之后注册的账号从独立空工作区开始。</p>}
              {error && <p className="text-sm text-rose-600" role="alert">{error}</p>}
              <Button className="w-full" disabled={submitting} type="submit">
                {submitting ? "提交中…" : mode === "login" ? "登录" : "注册并登录"}
              </Button>
            </form>
          </CardContent>
        </Card>
      </main>
    );
  }

  if (session.data?.auth_mode === "cloudflare_access") {
    return (
      <main className="min-h-screen flex items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardHeader>
            <CardTitle>需要 Cloudflare Access 登录</CardTitle>
            <CardDescription>请通过团队 Cloudflare Access 入口完成邮箱验证后再访问本应用，无需输入应用 token。</CardDescription>
          </CardHeader>
        </Card>
      </main>
    );
  }

  return (
    <main className="min-h-screen flex items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle>hl-app 登录</CardTitle>
          <CardDescription>输入管理员提供的随机登录 token。token 不会保存到浏览器存储。</CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={submitToken}>
            <label className="block text-sm space-y-2">
              <span>登录 token</span>
              <input aria-label="登录 token" className="w-full border rounded-md bg-background px-3 py-2"
                type="password" autoComplete="off" value={token} onChange={(event) => setToken(event.target.value)} required />
            </label>
            {error && <p className="text-sm text-rose-600" role="alert">{error}</p>}
            <Button className="w-full" disabled={submitting || !token} type="submit">登录</Button>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}
