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

test("退出后受保护页面重新进入登录", async ({ page }) => {
  await login(page);
  await page.getByRole("button", { name: "退出", exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/watchlist");
  await expect(page).toHaveURL(/\/login$/);
});
