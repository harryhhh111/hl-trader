import { mockManagedWallet } from "../managed-adjustment-fixture.mjs";
import { test, expect } from "@playwright/test";

async function setup(page) {
  await mockManagedWallet(page);
  let ask = "100.01";
  const requests = [];
  const reply = (route, data) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data }) });
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/adjustment-quote?*", route => reply(route, {
    schema_version: 1, pair_id: "nbis-crwv", network: "mainnet", config_revision: "fixture", queried_at: new Date().toISOString(),
    legs: [{ coin: "xyz:NBIS", ask, bid: String(Number(ask) - 0.02), mid: String(Number(ask) - 0.01), book_time: new Date().toISOString() },
      { coin: "xyz:CRWV", ask: "50.001", bid: "49.999", mid: "50", book_time: new Date().toISOString() }],
  }));
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/managed-preview", route => {
    const body = route.request().postDataJSON();
    requests.push(body);
    const bps = body.max_slippage_bps;
    return reply(route, {
      schema_version: 1, status: "preview", action_id: null, pair_id: "nbis-crwv", network: "mainnet",
      config_revision: body.config_revision, account_address: body.account_address, target_notional: body.target_notional,
      max_slippage_bps: bps, request_id: body.request_id, wallet_version: body.wallet_version, managed_status: "previewed", diagnostics: [],
      legs: ["xyz:NBIS", "xyz:CRWV"].map((coin, i) => ({
        coin, position_side: i === 0 ? "long" : "short", action: i === 0 ? "buy" : "sell", reduce_only: false,
        current_size: "0", current_notional: "0", mid_px: i === 0 ? "100" : "50", target_size: i === 0 ? "20" : "40",
        estimated_target_notional: "2000", order_qty: i === 0 ? "20" : "40", status: "prepared", confirmed_qty: "0",
        protect_px: ((i === 0 ? 100 : 50) * (1 + (i === 0 ? 1 : -1) * bps / 10000)).toFixed(3),
        avg_px: null, result: null, final_size: null, final_notional: null, asset: i, oid: null, nonce: null, cloid: null,
        depth: { book_time_ms: Date.now(), available_qty: "1000", required_qty: i === 0 ? "20" : "40",
          estimated_avg_px: i === 0 ? "100.01" : "49.999", worst_fill_px: i === 0 ? "100.01" : "49.999" },
      })),
    });
  });
  await page.goto("/login");
  await page.getByLabel("登录 token").fill("a".repeat(64));
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/hyperliquid$/);
  await page.goto("/hyperliquid/pairs/nbis-crwv");
  await page.getByRole("button", { name: "调整仓位", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "调整两腿仓位", exact: true });
  return { dialog, requests, setAsk: value => { ask = value; } };
}

test("滑点默认0.05%，深度明细展示，修改后重新预览", async ({ page }) => {
  const { dialog, requests } = await setup(page);
  await expect(dialog.getByLabel("最大允许滑点")).toHaveValue("5");
  await dialog.getByRole("button", { name: "预览调仓差额" }).click();
  await expect(dialog.getByText("xyz:NBIS 深度检查通过")).toBeVisible();
  await expect(dialog.getByText("需求 20 / 限价内可见数量 1000")).toBeVisible();
  expect(requests.at(-1).max_slippage_bps).toBe(5);
  await dialog.getByLabel("最大允许滑点").selectOption("10");
  await expect(dialog.getByText("调仓预览（未下单）")).toHaveCount(0);
  await dialog.getByRole("button", { name: "预览调仓差额" }).click();
  await expect(dialog.getByText("预计均价 100.01 · 保护价 100.100")).toBeVisible();
  expect(requests.at(-1).max_slippage_bps).toBe(10);
});

test("限价内盘口变化保留预览，超出保护价才标记过期", async ({ page }) => {
  const { dialog, setAsk } = await setup(page);
  await dialog.getByRole("button", { name: "预览调仓差额" }).click();
  await expect(dialog.getByText("调仓预览（未下单）")).toBeVisible();
  setAsk("100.02");
  await expect(dialog.getByText("卖 100.02", { exact: true })).toBeVisible({ timeout: 9000 });
  await expect(dialog.getByText(/当前预览已过期/)).toHaveCount(0);
  setAsk("100.06");
  await expect(dialog.getByText(/当前预览已过期/)).toBeVisible({ timeout: 9000 });
});
