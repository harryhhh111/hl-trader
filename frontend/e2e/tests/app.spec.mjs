import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const token = "a".repeat(64);
const evidence = path.resolve("docs/migration/evidence");
fs.mkdirSync(evidence, { recursive: true });

async function login(page) {
  await page.goto("/login");
  await page.getByLabel("登录 token").fill(token);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/hyperliquid$/);
}

test("登录、pair 切换、降级块与刷新失败保留旧读数", async ({ page }, testInfo) => {
  await login(page);
  await expect(page.getByText("HYPE / BTC", { exact: true }).first()).toBeVisible();
  await page.getByRole("link", { name: "HYPE / BTC", exact: true }).first().click();
  await expect(page.locator("p").getByText("解锁锚点不可用", { exact: true })).toBeVisible();
  await page.getByLabel("切换 pair").selectOption("nbis-crwv");
  await expect(page.getByRole("heading", { name: "NBIS / CRWV" })).toBeVisible();
  await page.goto("/hyperliquid");
  await expect(page.getByText("HYPE / BTC", { exact: true }).first()).toBeVisible();
  await page.route("**/api/v1/hyperliquid/pairs", (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "fixture directory failure" }) }));
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(page.getByRole("alert").first()).toContainText("目录加载失败");
  await expect(page.getByText("HYPE / BTC", { exact: true }).first()).toBeVisible();
  await page.screenshot({ path: `${evidence}/${testInfo.project.name}-hyperliquid.png`, fullPage: true });
});

test("账户添加与调仓表单按需弹窗展示", async ({ page }) => {
  await login(page);
  await page.goto("/hyperliquid/pairs/nbis-crwv");
  await expect(page.getByRole("heading", { name: "NBIS / CRWV" })).toBeVisible();
  await expect(page.getByLabel("账户名称")).toHaveCount(0);
  await expect(page.getByLabel("每腿目标名义")).toHaveCount(0);
  await expect(page.getByText("托管 API Wallet", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "调整仓位", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= (visualViewport?.width ?? innerWidth) + 1)).toBe(true);

  await page.getByRole("button", { name: "添加账户", exact: true }).click();
  const accountDialog = page.getByRole("dialog", { name: "添加账户", exact: true });
  await expect(accountDialog).toBeVisible();
  await expect(accountDialog.getByLabel("账户名称")).toBeVisible();
  await expect(accountDialog.getByLabel("账户地址")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(accountDialog).toHaveCount(0);

  await page.getByRole("button", { name: "调整仓位", exact: true }).click();
  const adjustmentDialog = page.getByRole("dialog", { name: "调整两腿仓位", exact: true });
  await expect(adjustmentDialog).toBeVisible();
  await expect(adjustmentDialog.getByLabel("每腿目标名义")).toHaveValue("2000");
  await expect(adjustmentDialog.getByRole("button", { name: "预览调仓差额" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= (visualViewport?.width ?? innerWidth) + 1)).toBe(true);
  await adjustmentDialog.getByRole("button", { name: "关闭弹窗" }).click();
  await expect(adjustmentDialog).toHaveCount(0);
});

test("pair 页面底部展示调仓交易历史", async ({ page }) => {
  const actionId = `adjust-0x${"ab".repeat(16)}`;
  await page.route("**/api/v1/hyperliquid/pairs/hype-btc/trade-history?*", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ data: {
      pair_id: "hype-btc",
      account_address: "0x0000000000000000000000000000000000000001",
      items: [{
        action_id: actionId,
        request_id: "4d37d2a5-1da5-47a1-a2ce-b17365ea566f",
        execution_mode: "managed",
        status: "succeeded",
        target_notional: "2000",
        created_at: "2026-11-04T02:00:00Z",
        legs: [{
          coin: "HYPE", position_side: "long", action: "buy", reduce_only: false,
          order_qty: "10", protect_px: "41", status: "filled", confirmed_qty: "10",
          avg_px: "40.5", oid: 12345, cloid: null, result: "全部成交",
        }, {
          coin: "BTC", position_side: "short", action: "sell", reduce_only: false,
          order_qty: "0.02", protect_px: "100000", status: "filled", confirmed_qty: "0.02",
          avg_px: "100100", oid: 12346, cloid: null, result: "全部成交",
        }],
        diagnostics: [],
      }],
      has_more: false,
      skipped_reports: 0,
    } }),
  }));
  await login(page);
  await page.goto("/hyperliquid/pairs/hype-btc");

  const history = page.getByRole("region", { name: `调仓 ${actionId}` });
  await expect(history).toBeVisible();
  await expect(history).toContainText("全部成交");
  await expect(history).toContainText("12345");
  await expect(history).toContainText("2000 USD");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= (visualViewport?.width ?? innerWidth) + 1)).toBe(true);
});

test("本地 API Wallet 签名不会上传或持久化私钥", async ({ page }) => {
  const privateKey = `0x${"0".repeat(63)}1`;
  const signer = "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf";
  const typedData = {
    domain: { name: "Exchange", version: "1", chainId: "0x539", verifyingContract: "0x0000000000000000000000000000000000000000" },
    types: {
      Agent: [{ name: "source", type: "string" }, { name: "connectionId", type: "bytes32" }],
      EIP712Domain: [
        { name: "name", type: "string" }, { name: "version", type: "string" },
        { name: "chainId", type: "uint256" }, { name: "verifyingContract", type: "address" },
      ],
    },
    primaryType: "Agent",
    message: { source: "a", connectionId: `0x${"11".repeat(32)}` },
  };
  const report = (status) => ({
    schema_version: 1,
    status,
    pair_id: "nbis-crwv",
    network: "mainnet",
    account_address: "0x0000000000000000000000000000000000000002",
    target_notional: "2000",
    config_revision: "test-revision",
    action_id: status === "prepared" ? `adjust-0x${"22".repeat(16)}` : null,
    expected_signer: status === "prepared" ? signer : null,
    signing_requests: status === "prepared" ? [
      { leg_index: 0, nonce: 1, typed_data: typedData },
      { leg_index: 1, nonce: 2, typed_data: typedData },
    ] : [],
    legs: ["NBIS", "CRWV"].map((coin, index) => ({
      coin,
      position_side: index === 0 ? "long" : "short",
      current_notional: "1500",
      estimated_target_notional: "2000",
      action: index === 0 ? "buy" : "sell",
      reduce_only: false,
      order_qty: "1",
      mid_px: "100",
      protect_px: "100",
      avg_px: status === "succeeded" ? "100.1" : null,
      slippage_pct: status === "succeeded" ? "0.1" : null,
      slippage_usd: status === "succeeded" ? "0.1" : null,
      result: status === "succeeded" ? "filled" : null,
      status: status === "succeeded" ? "filled" : null,
    })),
    diagnostics: [],
  });
  const adjustmentRequests = [];
  page.on("request", (request) => {
    if (request.url().includes("/adjustments/")) adjustmentRequests.push(request.postData() ?? "");
  });
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/preview", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: report("preview") }) }));
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/browser-prepare", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: report("prepared") }) }));
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/browser-submit", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: report("succeeded") }) }));

  await login(page);
  await page.goto("/hyperliquid/pairs/nbis-crwv");
  await page.getByRole("button", { name: "调整仓位", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "调整两腿仓位", exact: true });
  await dialog.getByRole("button", { name: "预览调仓差额" }).click();
  await dialog.getByLabel("API Wallet 私钥").fill(privateKey);
  await dialog.getByRole("button", { name: "加载 API Wallet" }).click();
  await expect(dialog.getByLabel("API Wallet 私钥")).toHaveValue("");
  await expect(dialog.getByText("签名账户")).toContainText("0x7e5f");
  await dialog.getByRole("button", { name: "本地签名并下单" }).click();
  await expect(dialog.getByText("调仓已完成", { exact: true })).toBeVisible();
  await expect(dialog.getByText("+0.1000% · +0.10 USD（不利）", { exact: true }).first()).toBeVisible();

  expect(adjustmentRequests).toHaveLength(3);
  expect(adjustmentRequests.every((body) => !body.includes(privateKey))).toBe(true);
  const submitted = JSON.parse(adjustmentRequests[2]);
  expect(submitted.expected_signer).toBe(signer);
  expect(submitted.signatures).toHaveLength(2);
  expect(submitted.signatures.every((item) => /^0x[0-9a-f]{130}$/.test(item.signature))).toBe(true);
  expect(await page.evaluate((key) => Object.values(localStorage).some((value) => value.includes(key)), privateKey)).toBe(false);
});

test("关注元数据编辑、AI 草稿修改保存且无新增移除分析入口", async ({ page }, testInfo) => {
  await login(page);
  await page.goto("/watchlist");
  await page.getByRole("button", { name: "编辑ORCL" }).click();
  await page.getByRole("button", { name: "AI 生成理由草稿" }).click();
  const reason = page.getByLabel("关注理由");
  await expect(reason).toHaveValue("研究草稿");
  await reason.fill("人工修改后的理由");
  await page.getByRole("button", { name: "保存元数据" }).click();
  await expect(page.getByRole("cell", { name: "人工修改后的理由" })).toBeVisible();
  await expect(page.getByRole("button", { name: "添加股票" })).toHaveCount(0);
  await expect(page.locator("a[href*='/analyzer']")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /移除/ })).toHaveCount(0);
  await page.screenshot({ path: `${evidence}/${testInfo.project.name}-watchlist.png`, fullPage: true });
});

test("资讯筛选、置顶与来源状态", async ({ page }, testInfo) => {
  await login(page);
  await page.goto("/news");
  await expect(page.locator("a[href='https://example.com/story-1']").last()).toBeVisible();
  await page.getByLabel("筛选公司").selectOption("ORCL");
  await page.getByLabel("筛选类型").selectOption("media");
  await expect(page.locator("a[href='https://example.com/story-1']").last()).toBeVisible();
  await expect(page.getByText("来源状态")).toBeVisible();
  await page.screenshot({ path: `${evidence}/${testInfo.project.name}-news.png`, fullPage: true });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  expect(overflow).toBe(false);
});

test("更新日志可从主导航访问并展示功能记录", async ({ page }, testInfo) => {
  await login(page);
  await page.getByRole("link", { name: "更新日志", exact: true }).click();
  await expect(page).toHaveURL(/\/changelog$/);
  await expect(page.getByRole("heading", { name: "更新日志", exact: true })).toBeVisible();
  await expect(page.getByText("调仓过程与结果更清晰", { exact: true })).toBeVisible();
  await expect(page.getByText("目标仓位调整与托管 API Wallet", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.screenshot({ path: `${evidence}/${testInfo.project.name}-changelog.png`, fullPage: true });
});

test("退出后受保护页面重新进入登录", async ({ page }) => {
  await login(page);
  await page.getByRole("button", { name: "退出", exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/watchlist");
  await expect(page).toHaveURL(/\/login$/);
});


test("调仓弹窗轮询盘口并使过期预览失效", async ({ page }) => {
  let mid = "123.29";
  let quoteCalls = 0;
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/adjustment-quote?*", (route) => {
    quoteCalls += 1;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data: {
        schema_version: 1,
        pair_id: "nbis-crwv",
        network: "mainnet",
        config_revision: "test-revision",
        queried_at: "2026-11-10T03:34:27Z",
        legs: [
          { coin: "xyz:NBIS", bid: "123.28", ask: "123.30", mid, book_time: "2026-11-10T03:34:26Z" },
          { coin: "xyz:CRWV", bid: "44.64", ask: "44.66", mid: "44.65", book_time: "2026-11-10T03:34:26Z" },
        ],
      } }),
    });
  });
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/preview", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ data: {
      schema_version: 1,
      status: "preview",
      pair_id: "nbis-crwv",
      network: "mainnet",
      account_address: "0x0000000000000000000000000000000000000002",
      target_notional: "2000",
      config_revision: "test-revision",
      action_id: null,
      legs: [
        { coin: "xyz:NBIS", position_side: "long", current_size: "0", current_notional: "0", mid_px: mid,
          target_size: "16", estimated_target_notional: "2000", action: "buy", order_qty: "16", protect_px: "123.31",
          reduce_only: false, asset: 110086, nonce: null, cloid: null, status: "prepared", confirmed_qty: "0",
          avg_px: null, oid: null, result: null, final_size: null, final_notional: null },
        { coin: "xyz:CRWV", position_side: "short", current_size: "0", current_notional: "0", mid_px: "44.65",
          target_size: "45", estimated_target_notional: "2000", action: "sell", order_qty: "45", protect_px: "44.63",
          reduce_only: false, asset: 110037, nonce: null, cloid: null, status: "prepared", confirmed_qty: "0",
          avg_px: null, oid: null, result: null, final_size: null, final_notional: null },
      ],
      diagnostics: [],
    } }),
  }));

  await login(page);
  await page.goto("/hyperliquid/pairs/nbis-crwv");
  await page.getByRole("button", { name: "调整仓位", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "调整两腿仓位", exact: true });
  await expect(dialog.getByText("实时盘口（每 5 秒更新）")).toBeVisible();
  await expect(dialog.getByText("中 123.29", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "预览调仓差额" }).click();
  await dialog.getByLabel("API Wallet 私钥").fill(`0x${"0".repeat(63)}1`);
  await dialog.getByRole("button", { name: "加载 API Wallet" }).click();
  const execute = dialog.getByRole("button", { name: "本地签名并下单" });
  await expect(execute).toBeEnabled();

  mid = "123.31";
  await expect(dialog.getByText("盘口已变化，当前预览已过期，请重新预览后再执行。", { exact: true })).toBeVisible({ timeout: 9000 });
  await expect(execute).toBeDisabled();
  await dialog.getByRole("button", { name: "按最新盘口重新预览" }).click();
  await expect(execute).toBeEnabled();
  expect(quoteCalls).toBeGreaterThanOrEqual(2);

  await dialog.getByRole("button", { name: "关闭弹窗" }).click();
  const callsAfterClose = quoteCalls;
  await page.waitForTimeout(5500);
  expect(quoteCalls).toBe(callsAfterClose);
});
