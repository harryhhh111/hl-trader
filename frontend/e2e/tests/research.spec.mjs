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

async function snapshotId(page) {
  // page.request shares the browser context cookies; the `request` fixture is isolated.
  const response = await page.request.get("/api/v1/research/companies");
  expect(response.status()).toBe(200);
  return (await response.json()).data.snapshot_id;
}

test("公司研究列表紧凑展示，每家公司一个入口并支持搜索", async ({ page }, testInfo) => {
  await login(page);
  await page.getByRole("link", { name: "公司研究", exact: true }).click();
  await expect(page).toHaveURL(/\/research$/);
  await expect(page.getByRole("heading", { name: "公司研究", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Oracle", exact: true })).toBeVisible();
  await expect(page.getByText("ORCL", { exact: true })).toBeVisible();
  const companies = page.getByRole('list', { name: '公司研究列表' });
  await expect(companies.getByRole('link')).toHaveCount(1);
  await expect(companies.getByRole('listitem')).toHaveCount(1);
  await expect(companies.getByRole('link')).toHaveAttribute('href', '/research/reports/orcl-main');
  expect((await companies.getByRole('listitem').boundingBox()).height).toBeLessThanOrEqual(80);
  await expect(page.getByText("甲骨文：OCI 与 RPO", { exact: true })).toHaveCount(0);
  await expect(page.getByText("更新 2026-11-17", { exact: true })).toHaveCount(1);
  await expect(page.getByText("草稿 · 尚未核验", { exact: true })).toBeVisible();
  // 演示报告不进入公司列表
  await expect(page.getByText("演示报告", { exact: true })).toHaveCount(0);

  await page.getByLabel("搜索公司或报告").fill("甲骨文");
  await expect(page).toHaveURL(/\/research\?q=/);
  await expect(page.getByRole("heading", { name: "Oracle", exact: true })).toBeVisible();

  await page.getByLabel("搜索公司或报告").fill("不存在的公司");
  await expect(page.getByText(/没有匹配/)).toBeVisible();
  await page.getByRole("button", { name: "清除搜索" }).click();
  await expect(page).toHaveURL(/\/research$/);
  await expect(page.getByRole("heading", { name: "Oracle", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= (visualViewport?.width ?? innerWidth) + 1)).toBe(true);
  await page.screenshot({ path: `${evidence}/${testInfo.project.name}-research.png`, fullPage: true });
});

test("阅读页展示报告信息、同公司切换和新页面阅读入口", async ({ page }) => {
  await login(page);
  await page.goto("/research?q=%E7%94%B2%E9%AA%A8%E6%96%87");
  await page.getByRole("link", { name: "阅读 Oracle 的公司研究", exact: true }).click();
  await expect(page).toHaveURL(/\/research\/reports\/orcl-main\?q=/);
  await expect(page.getByRole("heading", { name: "甲骨文：OCI 与 RPO", exact: true })).toBeVisible();
  await expect(page.getByText("文档更新日", { exact: true })).toBeVisible();
  await expect(page.getByText("2026-08-31", { exact: true })).toBeVisible();
  await expect(page.getByText("2026-11-14", { exact: true })).toBeVisible();
  await expect(page.getByText("同公司其他报告", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "甲骨文：云合同结构", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "未发布资料", exact: true })).toHaveCount(0);
  await expect(page.getByText("网站附件发布范围尚未审核", { exact: false })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "外部来源", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "新页面阅读", exact: true })).toHaveAttribute("target", "_blank");

  // 同公司切换
  await page.getByRole("link", { name: "甲骨文：云合同结构", exact: true }).click();
  await expect(page).toHaveURL(/orcl-topic/);
  await expect(page.getByRole("heading", { name: "甲骨文：云合同结构", exact: true })).toBeVisible();

  // 返回保留搜索条件
  await page.getByRole("link", { name: "返回公司研究", exact: true }).click();
  await expect(page).toHaveURL(/\/research\?q=/);
  await expect(page.getByLabel("搜索公司或报告")).toHaveValue("甲骨文");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= (visualViewport?.width ?? innerWidth) + 1)).toBe(true);
});

test("搜索专题标题仍进入公司主报告，其他报告在阅读页切换", async ({ page }) => {
  await login(page);
  await page.goto('/research');
  await page.getByLabel('搜索公司或报告').fill('云合同');
  const companies = page.getByRole('list', { name: '公司研究列表' });
  await expect(companies.getByRole('link')).toHaveCount(1);
  await expect(companies.getByRole('link')).toHaveAttribute('href', /\/research\/reports\/orcl-main\?q=/);
  await companies.getByRole('link').click();
  await expect(page.getByRole('heading', { name: '甲骨文：OCI 与 RPO', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: '甲骨文：云合同结构', exact: true })).toBeVisible();
  await page.getByRole('link', { name: '甲骨文：云合同结构', exact: true }).click();
  await expect(page.getByRole('heading', { name: '甲骨文：云合同结构', exact: true })).toBeVisible();
  await page.getByRole('link', { name: '返回公司研究', exact: true }).click();
  await expect(page.getByLabel('搜索公司或报告')).toHaveValue('云合同');
  await expect(page.getByRole('list', { name: '公司研究列表' }).getByRole('link')).toHaveCount(1);
});

test("报告正文在隔离区域实际渲染并保留中文、表格、目录锚点与折叠区", async ({ page }) => {
  await login(page);
  await page.goto("/research/reports/orcl-main");
  const frame = page.frameLocator("iframe");
  await expect(frame.getByRole("heading", { name: "甲骨文：OCI 与 RPO", exact: true })).toBeVisible();
  await expect(frame.getByText("52,961", { exact: true })).toBeVisible();
  await expect(frame.getByRole("link", { name: "核心判断", exact: true })).toBeVisible();
  await frame.getByRole("link", { name: "核心判断", exact: true }).click();
  await expect(frame.getByRole("heading", { name: "核心判断", exact: true })).toBeVisible();
  // 原生 details 折叠不需要脚本即可展开
  await expect(frame.getByText("展开查看口径", { exact: true })).toBeVisible();
  await frame.getByText("展开查看口径", { exact: true }).click();
  await expect(frame.getByText("口径为美元百万。", { exact: true })).toBeVisible();
});

test("获准图片在隔离区域内真实加载，不依赖 frame 内的会话请求", async ({ page }) => {
  await login(page);
  const requests = [];
  page.on("response", (response) => {
    if (response.url().includes("/research/content/")) {
      requests.push({ status: response.status(), url: response.url() });
    }
  });
  await page.goto("/research/reports/orcl-main");
  const frame = page.frameLocator("iframe");
  await expect(frame.getByRole("heading", { name: "甲骨文：OCI 与 RPO", exact: true })).toBeVisible();

  // 破图时 toBeVisible() 仍会通过，必须检查真实解码尺寸
  const image = await frame.locator("img[alt='收入趋势图']").evaluate((node) => ({
    complete: node.complete,
    naturalWidth: node.naturalWidth,
    naturalHeight: node.naturalHeight,
    scheme: node.currentSrc.split(":")[0],
  }));
  expect(image.complete).toBe(true);
  expect(image.naturalWidth).toBeGreaterThan(0);
  expect(image.naturalHeight).toBeGreaterThan(0);
  expect(image.scheme).toBe("data");

  // 正文文档必须真实返回 200，隔离区域内不得出现未授权的 401
  expect(requests).toEqual([]);
  for (const request of requests) {
    expect(request.status).toBe(200);
  }
});

test("独立样式表与递归样式依赖在隔离区域内真实生效", async ({ page }) => {
  await login(page);
  await page.goto("/research/reports/orcl-main");
  const frame = page.frameLocator("iframe");
  const styled = frame.locator("#style-proof");
  await expect(styled).toBeVisible();
  await expect(styled).toHaveText("样式加载确认");
  const color = await styled.evaluate((node) => getComputedStyle(node).color);
  expect(color).toBe("rgb(1, 2, 3)");
  // 递归依赖的样式（nested.css 里的 url(chart.png)）也必须解析成功
  const background = await styled.evaluate((node) => getComputedStyle(node).backgroundImage);
  expect(background).toContain("data:image/png");
});

test("报告内的同公司链接在隔离区域中真实点击并完成导航", async ({ page }) => {
  await login(page);
  await page.goto("/research/reports/orcl-main");
  const frame = page.frameLocator("iframe");
  await expect(frame.getByRole("heading", { name: "甲骨文：OCI 与 RPO", exact: true })).toBeVisible();
  const link = frame.getByRole("link", { name: "同公司专题", exact: true });
  await expect(link).toBeVisible();
  await link.click();
  // 链接由外层应用接管，读者落到目标报告页面而不是未登录的正文响应
  await expect(page).toHaveURL(/\/research\/reports\/orcl-topic/);
  await expect(page.getByRole("heading", { name: "甲骨文：云合同结构", exact: true })).toBeVisible();
});

test("正文、资源与未登记路径都需要登录且不泄露文件", async ({ page, request }) => {
  const anonymous = await request.get("/api/v1/research/companies");
  expect(anonymous.status()).toBe(401);

  await login(page);
  const id = await snapshotId(page);
  const contentUrl = `/api/v1/research/content/${id}/ORCL/reports/main.html`;

  const anonymousContent = await request.get(contentUrl);
  expect(anonymousContent.status()).toBe(401);

  const unregistered = await page.request.get(`/api/v1/research/content/${id}/ORCL/evidence/data.json`);
  expect(unregistered.status()).toBe(404);
  expect(unregistered.headers()["content-type"]).toContain("application/json");

  for (const candidate of [
    `/api/v1/research/content/${id}/..%2f..%2fcurrent.json`,
    `/api/v1/research/content/${id}/%2Fetc%2Fpasswd`,
    `/api/v1/research/content/s${"0".repeat(32)}/ORCL/reports/main.html`,
  ]) {
    const response = await page.request.get(candidate);
    expect(response.status()).toBe(404);
  }

  // 获准的相对资源可通过同一快照路由读取
  const asset = await page.request.get(`/api/v1/research/content/${id}/ORCL/assets/chart.png`);
  expect(asset.status()).toBe(200);
  expect(asset.headers()["content-type"]).toBe("image/png");
});

test("直接打开正文 URL 仍受响应头隔离，报告脚本与表单提交被阻止", async ({ page }) => {
  await login(page);
  const id = await snapshotId(page);
  const contentUrl = `/api/v1/research/content/${id}/ORCL/reports/main.html`;

  const response = await page.request.get(contentUrl);
  expect(response.status()).toBe(200);
  const policy = response.headers()["content-security-policy"];
  expect(policy).toContain("sandbox");
  expect(policy).toContain("script-src 'none'");
  expect(policy).toContain("form-action 'none'");
  expect(response.headers()["cache-control"]).toBe("private, no-store");
  expect(response.headers()["x-content-type-options"]).toBe("nosniff");

  await page.goto(contentUrl);
  // 报告内脚本不得执行，标题不能被脚本改写
  await expect(page).toHaveTitle("甲骨文：OCI 与 RPO");
  expect(await page.title()).not.toContain("已泄漏");
});
