import { test, expect } from "@playwright/test";

const password = "correct-horse-battery";

test.skip(process.env.HL_APP_E2E_AUTH_MODE !== "account", "仅在账号认证模式运行");

test("账号注册、自动登录、退出和密码登录", async ({ page }, testInfo) => {
  const email = `account-e2e-${testInfo.project.name}@example.com`;
  await page.goto("/login");
  await page.getByRole("button", { name: "注册", exact: true }).click();
  await page.getByLabel("邮箱").fill(email);
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByLabel("确认密码").fill(password);
  await page.getByRole("button", { name: "注册并登录", exact: true }).click();
  await expect(page).toHaveURL(/\/hyperliquid$/);
  await expect(page.getByRole("button", { name: "退出", exact: true })).toBeVisible();

  await page.getByRole("button", { name: "退出", exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel("邮箱").fill(email);
  await page.getByLabel("密码", { exact: true }).fill("wrong-password");
  await page.locator("form").getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("邮箱或密码错误");

  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.locator("form").getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/hyperliquid$/);
});
