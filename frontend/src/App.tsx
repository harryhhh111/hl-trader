import { Navigate, Route, Routes } from "react-router-dom";

function Placeholder({ title }: { title: string }) {
  return <main><h1>{title}</h1></main>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Placeholder title="登录" />} />
      <Route path="/hyperliquid" element={<Placeholder title="Hyperliquid" />} />
      <Route path="/hyperliquid/pairs/:pairId" element={<Placeholder title="Pair 详情" />} />
      <Route path="/watchlist" element={<Placeholder title="关注股" />} />
      <Route path="/news" element={<Placeholder title="每日资讯" />} />
      <Route path="/" element={<Navigate to="/hyperliquid" replace />} />
      <Route path="*" element={<Navigate to="/hyperliquid" replace />} />
    </Routes>
  );
}
