import assert from "node:assert/strict";
import test from "node:test";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { LoginPage } from "./login-page";

function render(authenticated: boolean, authMode?: "token" | "cloudflare_access" | "account") {
  const qc = new QueryClient();
  qc.setQueryData(["session"], { authenticated, auth_mode: authMode });
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

test("Cloudflare Access 模式不显示应用 token 表单", () => {
  const html = render(false, "cloudflare_access");
  assert.match(html, /需要 Cloudflare Access 登录/);
  assert.match(html, /无需输入应用 token/);
  assert.doesNotMatch(html, /type="password"/);
});


test("账号模式提供登录和网页注册，不需要管理命令", () => {
  const html = render(false, "account");
  assert.match(html, /登录/);
  assert.match(html, /注册/);
  assert.match(html, /aria-label="邮箱"/);
  assert.match(html, /aria-label="密码"/);
  assert.match(html, /独立的 Hyperliquid 账户、托管钱包和调仓记录/);
  assert.doesNotMatch(html, /登录 token/);
});
