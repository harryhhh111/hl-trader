import { test, expect } from '@playwright/test';

const account = '0x0000000000000000000000000000000000000043';
const wallet = '0x1111111111111111111111111111111111111111';
const json = (route, data) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ data }) });

async function setup(page, settings) {
  await page.route('**/api/v1/hyperliquid/pairs/nbis-crwv/adjustment-preflight?*', (route) => {
    if (settings.failed) return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'fixture unavailable' }) });
    const url = new URL(route.request().url());
    const ready = settings.leverage <= 3 && (settings.mode === 'cross' || settings.onlyIsolated);
    return json(route, {
      schema_version: 1, pair_id: 'nbis-crwv', network: 'mainnet', account_address: account,
      config_revision: url.searchParams.get('config_revision'), queried_at: new Date().toISOString(),
      policy_max_leverage: 3, ready_for_increase: ready,
      legs: ['xyz:NBIS', 'xyz:CRWV'].map((coin) => ({
        coin, margin_mode: settings.mode, leverage: settings.leverage, market_max_leverage: 5,
        cross_available: !settings.onlyIsolated, ready_for_increase: ready,
        diagnostics: ready ? [] : ['请手动切换为全仓，并降至不超过 3x'],
      })),
    });
  });
  await page.route('**/api/v1/hyperliquid/pairs/nbis-crwv/api-wallet?*', (route) => json(route, {
    configured: true, network: 'mainnet', pair_id: 'nbis-crwv', account_address: account,
    wallet_address: wallet, wallet_version: 1, active_execution: null,
  }));
  await page.route('**/api/v1/hyperliquid/pairs/nbis-crwv/adjustment-quote?*', (route) => json(route, {
    queried_at: new Date().toISOString(), legs: ['xyz:NBIS', 'xyz:CRWV'].map((coin) => ({ coin, mid: '100', bid: '99', ask: '101', book_time: new Date().toISOString() })),
  }));
  await page.route('**/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/preview', (route) => {
    const body = route.request().postDataJSON();
    const reducing = body.target_notional === '0';
    return json(route, {
      schema_version: 1, status: 'preview', target_notional: body.target_notional, diagnostics: [],
      legs: ['xyz:NBIS', 'xyz:CRWV'].map((coin, index) => ({
        coin, position_side: index ? 'short' : 'long', current_notional: '1000',
        estimated_target_notional: body.target_notional, action: index ? 'sell' : 'buy',
        reduce_only: reducing, order_qty: '1', mid_px: '100', protect_px: '100', status: 'preview',
      })),
    });
  });
  await page.goto('/login');
  await page.getByLabel('登录 token').fill('a'.repeat(64));
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page).toHaveURL(/\/hyperliquid$/);
  await page.goto('/hyperliquid/pairs/nbis-crwv');
  await page.getByRole('button', { name: '调整仓位', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '调整两腿仓位', exact: true });
  await dialog.getByRole('button', { name: '预览调仓差额' }).click();
  await dialog.getByLabel('API Wallet 私钥').fill(`0x${'0'.repeat(63)}1`);
  await dialog.getByRole('button', { name: '加载 API Wallet', exact: true }).click();
  return dialog;
}

test('超限和逐仓阻止增仓，手动修改遵守 3x 上限并清除旧预览', async ({ page }) => {
  const settings = { leverage: 4, mode: 'isolated' };
  let changes = 0;
  await page.route('**/api/v1/hyperliquid/pairs/nbis-crwv/leverage/managed-execute', (route) => {
    const body = route.request().postDataJSON();
    expect(body.leverage).toBeLessThanOrEqual(3);
    changes += 1;
    settings.leverage = body.leverage;
    settings.mode = body.margin_mode;
    return json(route, { status: 'succeeded', managed_status: 'succeeded', result: '已核对' });
  });
  const dialog = await setup(page, settings);
  await expect(dialog.getByText('当前设置').first()).toContainText('4x');
  await expect(dialog.getByRole('button', { name: '本地签名并下单', exact: true })).toBeDisabled();
  await dialog.getByRole('button', { name: '调整 xyz:NBIS 杠杆', exact: true }).click();
  const editor = page.getByRole('dialog', { name: '调整 xyz:NBIS 杠杆', exact: true });
  await expect(editor.getByLabel('保证金模式')).toHaveValue('cross');
  await expect(editor.getByLabel('目标杠杆')).toHaveValue('3');
  await editor.getByLabel('目标杠杆').fill('4');
  await expect(editor.getByRole('button', { name: '确认调整杠杆', exact: true })).toHaveCount(0);
  expect(changes).toBe(0);
  await editor.getByLabel('目标杠杆').fill('2');
  await editor.getByLabel('确认杠杆调整').fill('xyz:NBIS cross 2x');
  await editor.getByRole('button', { name: '确认调整杠杆', exact: true }).click();
  await expect(dialog.getByText('调仓预览（未下单）', { exact: true })).toHaveCount(0);
  // Updated settings can unmount the completed editor when its current-value props refresh.
  if (await editor.count()) await editor.getByRole('button', { name: '关闭弹窗', exact: true }).click();
  await dialog.getByRole('button', { name: '预览调仓差额' }).click();
  await expect(dialog.getByRole('button', { name: '本地签名并下单', exact: true })).toBeEnabled();
  expect(changes).toBe(1);
});

test('点击下单时重新检查，变化后的超限状态不会进入签名准备', async ({ page }) => {
  const settings = { leverage: 3, mode: 'cross' };
  let prepares = 0;
  await page.route('**/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/browser-prepare', (route) => {
    prepares += 1;
    return route.abort();
  });
  const dialog = await setup(page, settings);
  const execute = dialog.getByRole('button', { name: '本地签名并下单', exact: true });
  await expect(execute).toBeEnabled();
  settings.leverage = 4;
  await execute.click();
  await expect(dialog.getByRole('alert').filter({ hasText: '下单前检查未通过' })).toBeVisible();
  await expect(execute).toBeDisabled();
  expect(prepares).toBe(0);
});

test('检查失败禁止增仓，但纯减仓预览仍可执行；仅逐仓市场允许例外', async ({ page }) => {
  const settings = { leverage: 3, mode: 'isolated', onlyIsolated: true };
  const dialog = await setup(page, settings);
  const execute = dialog.getByRole('button', { name: '本地签名并下单', exact: true });
  await expect(execute).toBeEnabled();
  settings.failed = true;
  await dialog.getByRole('button', { name: '重新检查', exact: true }).click();
  await expect(dialog.getByRole('alert').filter({ hasText: '检查失败' })).toBeVisible();
  await expect(execute).toBeDisabled();
  await dialog.getByLabel('每腿目标名义').fill('0');
  await dialog.getByRole('button', { name: '预览调仓差额' }).click();
  await expect(execute).toBeEnabled();
});
