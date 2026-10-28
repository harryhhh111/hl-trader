import { useQuery, useQueryClient } from "@tanstack/react-query";
import { NavLink, Navigate, Outlet, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { HyperliquidPage } from "@/pages/hyperliquid-page";
import { HyperliquidPairPage } from "@/pages/hyperliquid-pair-page";
import { LoginPage } from "@/pages/login-page";
import { WatchlistPage } from "@/pages/watchlist-page";
import { Button } from "@/components/ui/button";
import { sessionApi } from "@/lib/api/client";

function Placeholder({ title }: { title: string }) {
  return <div><h1 className="text-lg font-semibold">{title}</h1></div>;
}

function ProtectedLayout() {
  const session = useQuery({ queryKey: ["session"], queryFn: sessionApi.session, retry: false });
  const location = useLocation();
  const navigate = useNavigate();
  const qc = useQueryClient();

  if (session.isPending) return <main>正在验证登录状态…</main>;
  if (session.isError) return <main role="alert">认证服务不可用</main>;
  if (!session.data?.authenticated) return <Navigate to="/login" replace state={{ from: location.pathname }} />;

  async function logout() {
    await sessionApi.logout();
    qc.clear();
    navigate("/login", { replace: true });
  }

  return (
    <div className="min-h-screen">
      <header className="border-b bg-background">
        <div className="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between gap-4">
          <nav aria-label="主导航" className="flex gap-4 text-sm">
            <NavLink to="/hyperliquid" className={({ isActive }) => isActive ? "font-semibold text-primary" : ""}>Hyperliquid</NavLink>
            <NavLink to="/watchlist" className={({ isActive }) => isActive ? "font-semibold text-primary" : ""}>关注股</NavLink>
            <NavLink to="/news" className={({ isActive }) => isActive ? "font-semibold text-primary" : ""}>每日资讯</NavLink>
          </nav>
          <Button variant="outline" size="sm" onClick={() => void logout()}>退出</Button>
        </div>
      </header>
      <main className="max-w-7xl mx-auto p-4 md:p-6"><Outlet /></main>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<ProtectedLayout />}>
        <Route path="/hyperliquid" element={<HyperliquidPage />} />
        <Route path="/hyperliquid/pairs/:pairId" element={<HyperliquidPairPage />} />
        <Route path="/watchlist" element={<WatchlistPage />} />
        <Route path="/news" element={<Placeholder title="每日资讯" />} />
        <Route path="/" element={<Navigate to="/hyperliquid" replace />} />
        <Route path="*" element={<Navigate to="/hyperliquid" replace />} />
      </Route>
    </Routes>
  );
}
