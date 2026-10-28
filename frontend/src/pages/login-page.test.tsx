import assert from "node:assert/strict";
import test from "node:test";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { LoginPage } from "./login-page";

function render(authenticated: boolean) {
  const qc = new QueryClient();
  qc.setQueryData(["session"], { authenticated });
  return renderToString(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/login"]}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/hyperliquid" element={<div>Hyperliquid 首页</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

test("登录页使用密码输入且不包含分析或股票新增入口", () => {
  const html = render(false);
  assert.match(html, /type="password"/);
  assert.match(html, /不会保存到浏览器存储/);
  assert.doesNotMatch(html, /analyzer|新增证券|移除证券/);
});

test("已登录访问登录页发起重定向而不渲染表单", () => {
  assert.equal(render(true), "");
});
