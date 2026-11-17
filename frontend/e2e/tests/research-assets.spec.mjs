import { test, expect } from "@playwright/test";

async function openReport(page) {
  await page.goto("/login");
  await page.getByLabel("登录 token").fill("a".repeat(64));
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/hyperliquid$/);
  await page.goto("/research/reports/orcl-main");
  await expect(page.frameLocator("iframe").getByRole("heading", { name: "甲骨文：OCI 与 RPO", exact: true })).toBeVisible();
}

test("隔离报告实际解码图片、加载递归样式，断网仍能阅读", async ({ page, context }) => {
  const failures = [];
  page.on("response", response => {
    if (response.url().includes("/research/content/") && response.status() >= 400) {
      failures.push(`${response.status()} ${response.url()}`);
    }
  });
  await openReport(page);
  const frame = page.frameLocator("iframe");
  const image = frame.getByAltText("收入趋势图", { exact: true });
  await expect.poll(() => image.evaluate(img => img.complete && img.naturalWidth > 0)).toBe(true);
  await expect(image).toHaveAttribute("src", /^data:image\/png;base64,/);
  await expect(frame.locator("#style-proof")).toHaveCSS("color", "rgb(1, 2, 3)");
  await expect(frame.locator("#style-proof")).toHaveCSS("background-image", /data:image\/png;base64,/);
  expect(failures).toEqual([]);
  await expect(page.locator("iframe")).toHaveAttribute("sandbox", "allow-top-navigation-by-user-activation");
  await context.setOffline(true);
  try {
    await expect(frame.getByText("52,961", { exact: true })).toBeVisible();
    expect(await image.evaluate(img => img.naturalWidth)).toBeGreaterThan(0);
    await frame.getByText("展开查看口径", { exact: true }).click();
    await expect(frame.getByText("口径为美元百万。", { exact: true })).toBeVisible();
  } finally {
    await context.setOffline(false);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
});

test("正文中的报告链接打开同一快照阅读页并保留章节锚点", async ({ page }) => {
  await openReport(page);
  const metadata = await (await page.request.get("/api/v1/research/reports/orcl-main")).json();
  await page.frameLocator("iframe").getByRole("link", { name: "同公司专题", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/research/reports/orcl-topic\\?snapshot=${metadata.data.snapshot_id}#contracts$`));
  await expect(page.getByRole("heading", { name: "甲骨文：云合同结构", exact: true })).toBeVisible();
  await expect(page.frameLocator("iframe").locator("#contracts")).toBeVisible();
  await expect(page.locator("iframe")).toHaveAttribute("src", /topic\.html#contracts$/);
});

test("修复资源交付后表单仍被禁止，退出后正文与附件仍需认证", async ({ page }) => {
  await openReport(page);
  const metadata = (await (await page.request.get("/api/v1/research/reports/orcl-main")).json()).data;
  const posts = [];
  page.on("request", request => { if (request.method() === "POST") posts.push(request.url()); });
  await page.frameLocator("iframe").getByRole("button", { name: "不应可提交", exact: true }).click();
  expect((await (await page.request.get("/api/v1/session")).json()).data.authenticated).toBe(true);
  expect(posts).toEqual([]);
  await page.getByRole("button", { name: "退出", exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect((await page.request.get(metadata.content_url)).status()).toBe(401);
  expect((await page.request.get(`/api/v1/research/content/${metadata.snapshot_id}/ORCL/assets/chart.png`)).status()).toBe(401);
});
