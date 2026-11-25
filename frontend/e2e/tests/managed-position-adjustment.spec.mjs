import { test, expect } from "@playwright/test";
import { managedReport, mockManagedWallet, reply } from "../managed-adjustment-fixture.mjs";

async function open(page) {
  await page.goto("/login");
  await page.getByLabel("登录 token").fill("a".repeat(64));
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/hyperliquid$/);
  await page.goto("/hyperliquid/pairs/nbis-crwv");
  await page.getByRole("button", { name: "调整仓位", exact: true }).click();
  return page.getByRole("dialog", { name: "调整两腿仓位", exact: true });
}

test("默认托管、金额确认及原请求轮询，不提供浏览器钱包入口", async ({ page }) => {
  await mockManagedWallet(page);
  let previewBody, executionBody, lookups = 0, executions = 0, browserCalls = 0;
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/browser-*", route => {
    browserCalls += 1; return route.abort();
  });
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/managed-preview", route => {
    previewBody = route.request().postDataJSON();
    return reply(route, managedReport(previewBody));
  });
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/managed-execute", route => {
    executions += 1; executionBody = route.request().postDataJSON();
    return reply(route, managedReport(previewBody, { status: "executing", managed_status: "running" }));
  });
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/managed-executions/*", route => {
    lookups += 1;
    expect(route.request().url()).toContain(previewBody.request_id);
    return reply(route, managedReport(previewBody, { status: "succeeded", managed_status: "succeeded" }));
  });
  const dialog = await open(page);
  await expect(dialog.getByLabel("签名方式")).toHaveText("签名方式：托管 API Wallet");
  await expect(dialog.getByRole("button", { name: /浏览器本地私钥|浏览器扩展钱包|连接 API Wallet/ })).toHaveCount(0);
  await expect(dialog.getByLabel("API Wallet 私钥", { exact: true })).toHaveCount(0);
  await dialog.getByRole("button", { name: "预览调仓差额" }).click();
  const execute = dialog.getByRole("button", { name: "使用托管钱包下单" });
  await expect(execute).toBeDisabled();
  await dialog.getByLabel("确认基准目标金额", { exact: true }).fill("1999");
  await expect(execute).toBeDisabled();
  await dialog.getByLabel("确认基准目标金额", { exact: true }).fill("2000");
  await execute.click();
  await expect(dialog.getByText("调仓已完成", { exact: true })).toBeVisible({ timeout: 8000 });
  expect(executionBody.request_id).toBe(previewBody.request_id);
  expect(executionBody.wallet_version).toBe(1);
  expect(executionBody.confirmation).toBe("2000");
  expect(executionBody.max_slippage_bps).toBe(5);
  expect(executionBody).not.toHaveProperty("signatures");
  expect(executionBody).not.toHaveProperty("private_key");
  expect(executions).toBe(1); expect(lookups).toBeGreaterThan(0); expect(browserCalls).toBe(0);
});

test("未配置托管钱包时提示配置并禁止预览", async ({ page }) => {
  await mockManagedWallet(page, { configured: false });
  const dialog = await open(page);
  await expect(dialog.getByText("当前账户未配置托管 API Wallet，请先在账户配置中添加。", { exact: true })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "预览调仓差额" })).toBeDisabled();
  await expect(dialog.getByRole("textbox", { name: /私钥/ })).toHaveCount(0);
});

test("打开弹框恢复已有执行请求，只查询原请求不重新提交", async ({ page }) => {
  const requestId = "00000000-0000-4000-8000-000000000003";
  await mockManagedWallet(page, { activeExecution: { request_id: requestId, status: "running" } });
  let lookups = 0, writes = 0;
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/managed-preview", route => { writes += 1; return route.abort(); });
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/managed-execute", route => { writes += 1; return route.abort(); });
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/managed-executions/*", route => {
    lookups += 1;
    expect(route.request().url()).toContain(requestId);
    const pending = lookups === 1;
    return reply(route, managedReport({ target_notional: "2000", request_id: requestId }, {
      status: pending ? "executing" : "succeeded", managed_status: pending ? "running" : "succeeded",
    }));
  });
  const dialog = await open(page);
  await expect(dialog.getByText("调仓已完成", { exact: true })).toBeVisible({ timeout: 8000 });
  expect(lookups).toBeGreaterThanOrEqual(2); expect(writes).toBe(0);
});

test("发送前结果未知可核对未发送、解除阻塞并重新预览", async ({ page }) => {
  const requestId = "00000000-0000-4000-8000-000000000004";
  let activeExecution = { request_id: requestId, status: "unknown" };
  let resolved = false;
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/api-wallet?*", route => reply(route, {
    configured: true, network: "mainnet", pair_id: "nbis-crwv",
    account_address: "0x0000000000000000000000000000000000000043",
    wallet_address: "0x1111111111111111111111111111111111111111", wallet_version: 1,
    active_execution: activeExecution,
  }));
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/managed-executions/**", route => {
    if (route.request().method() === "POST" && route.request().url().endsWith("/resolve-not-sent")) {
      resolved = true;
      activeExecution = null;
      return reply(route, managedReport({ request_id: requestId }, {
        status: "incomplete", managed_status: "failed", error_code: "not_sent_confirmed",
      }));
    }
    return reply(route, managedReport({ request_id: requestId }, resolved
      ? { status: "incomplete", managed_status: "failed", error_code: "not_sent_confirmed" }
      : { status: "unknown", managed_status: "unknown", error_code: "execution_unresolved" }));
  });
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/managed-preview", route => {
    return reply(route, managedReport(route.request().postDataJSON()));
  });

  const dialog = await open(page);
  await expect(dialog.getByText("调仓结果未知", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "核对未发送并解除阻塞" }).click();
  await expect(dialog.getByText("托管调仓未执行", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "预览调仓差额" }).click();
  await expect(dialog.getByText("调仓预览（未下单）", { exact: true })).toBeVisible();
  expect(resolved).toBe(true);
});
