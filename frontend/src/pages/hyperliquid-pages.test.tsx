import assert from 'node:assert/strict';
import test from 'node:test';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderToString } from 'react-dom/server';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import { HyperliquidPage } from './hyperliquid-page';
import { HyperliquidAdjustmentGuidePage } from './hyperliquid-adjustment-guide-page';
import { HyperliquidPairPage } from './hyperliquid-pair-page';
import type {
  HlPairDirectory,
  HlPairSnapshot,
  HlV2UnlockAnchor,
} from '../lib/types/hyperliquid';
import hypeFixture from '../../../tests/fixtures/hyperliquid/v2_snapshot_hype_btc.json';
import nbisFixture from '../../../tests/fixtures/hyperliquid/v2_snapshot_nbis_crwv.json';

Object.assign(globalThis, {
  document: { documentElement: { classList: { contains: () => false } } },
});

function snapshot(value: unknown): HlPairSnapshot {
  return JSON.parse(JSON.stringify(value)) as HlPairSnapshot;
}

function directoryFor(value: HlPairSnapshot): HlPairDirectory {
  return {
    schema_version: 1,
    config_revision: value.config_revision,
    pairs: [value.pair],
  };
}

function seed(value: HlPairSnapshot, directory = directoryFor(value)) {
  const qc = new QueryClient();
  qc.setQueryData(['hl-directory'], directory);
  qc.setQueryData(['hl-pair', directory.config_revision, value.pair.id], value);
  qc.setQueryData(['hl-accounts', value.pair.id], {
    pair_id: value.pair.id,
    accounts: value.pair.account_address == null ? [] : [{
      id: null,
      label: '默认账户',
      address: value.pair.account_address,
      is_active: true,
      source: 'config',
      created_at: null,
    }],
  });
  if (value.pair.account_address != null) {
    qc.setQueryData(['hl-trade-history', value.pair.id, value.pair.account_address], {
      pair_id: value.pair.id,
      account_address: value.pair.account_address,
      items: [],
      has_more: false,
      skipped_reports: 0,
    });
    qc.setQueryData(['hl-managed-wallet', value.pair.id, value.pair.account_address], {
      configured: true,
      network: value.pair.network,
      pair_id: value.pair.id,
      account_address: value.pair.account_address,
      wallet_address: '0x1111111111111111111111111111111111111111',
      wallet_version: 1,
      authorization_checked_at: '2026-11-04T00:00:00Z',
      created_at: null,
      updated_at: null,
      active_execution: null,
    });
  }
  return qc;
}

function renderOverview(qc: QueryClient) {
  return renderToString(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/hyperliquid']}>
        <Routes><Route path="/hyperliquid" element={<HyperliquidPage />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderDetail(qc: QueryClient, pairId: string) {
  return renderToString(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/hyperliquid/pairs/${pairId}`]}>
        <Routes>
          <Route path="/hyperliquid/pairs/:pairId" element={<HyperliquidPairPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

test('详情页标题切换后，即使两组地址都为 null 也不渲染另一 pair 的旧读数', () => {
  const current = snapshot(hypeFixture);
  const stale = snapshot(nbisFixture);
  current.pair.account_address = null;
  stale.pair.account_address = null;
  const directory = directoryFor(current);
  const qc = new QueryClient();
  qc.setQueryData(['hl-directory'], directory);
  qc.setQueryData(['hl-pair', directory.config_revision, 'hype-btc'], stale);
  const html = renderDetail(qc, 'hype-btc');
  assert.match(html, /HYPE \/ BTC/);
  assert.match(html, /正在核对数据身份/);
  assert.doesNotMatch(html, /7\.9604/);
  qc.clear();
});

test('总览卡片只显示当前目录身份，串入的快照读数被隐藏', () => {
  const current = snapshot(hypeFixture);
  const stale = snapshot(nbisFixture);
  const directory = directoryFor(current);
  const qc = new QueryClient();
  qc.setQueryData(['hl-directory'], directory);
  qc.setQueryData(['hl-pair', directory.config_revision, 'hype-btc'], stale);
  const html = renderOverview(qc);
  assert.match(html, /HYPE \/ BTC/);
  assert.match(html, /目录身份更新中/);
  assert.doesNotMatch(html, /7\.9604/);
  qc.clear();
});

test('解锁块不可用或 anchors 损坏时页面保留其他块且不显示正常空态', () => {
  const value = snapshot(hypeFixture);
  if (value.blocks.unlocks == null) throw new Error('fixture 应启用解锁块');
  value.blocks.unlocks.status = 'unavailable';
  value.blocks.unlocks.anchors = null as unknown as HlV2UnlockAnchor[];
  const qc = seed(value);
  const html = renderDetail(qc, value.pair.id);
  assert.match(html, /解锁锚点不可用/);
  assert.doesNotMatch(html, /未来 7 天没有已核实的锚点/);
  assert.match(html, /-45\.81403/);
  qc.clear();
});

test('总览和详情方向摘要显示具体两腿多空', () => {
  const value = snapshot(hypeFixture);
  const qc = seed(value);
  assert.match(renderOverview(qc), /HYPE 多 \/ BTC 空/);
  assert.match(renderDetail(qc, value.pair.id), /HYPE 多 \/ BTC 空/);
  qc.clear();
});

test('详情页保留账户切换，添加账户表单默认收进弹窗', () => {
  const value = snapshot(nbisFixture);
  const qc = seed(value);
  qc.setQueryData(['hl-accounts', value.pair.id], {
    pair_id: value.pair.id,
    accounts: [
      { id: 1, label: '主账户', address: value.pair.account_address, is_active: true, source: 'database', created_at: null },
      { id: 2, label: '备用账户', address: '0x1111111111111111111111111111111111111111', is_active: false, source: 'database', created_at: null },
    ],
  });
  const html = renderDetail(qc, value.pair.id);
  assert.match(html, /账户配置/);
  assert.match(html, /aria-label="切换账户"/);
  assert.match(html, /主账户/);
  assert.match(html, /备用账户/);
  assert.match(html, /添加账户/);
  assert.match(html, /不需要私钥/);
  assert.match(html, /托管 API Wallet/);
  assert.match(html, /0x111111…111111/);
  assert.doesNotMatch(html, /aria-label="账户名称"/);
  assert.doesNotMatch(html, /aria-label="账户地址"/);
  assert.doesNotMatch(html, /添加并使用/);
  qc.clear();
});

test('仓位标题旁展示调仓入口，目标与预览表单默认收进弹窗', () => {
  const value = snapshot(nbisFixture);
  const qc = seed(value);
  const html = renderDetail(qc, value.pair.id);
  assert.match(html, /调整仓位/);
  assert.match(html, /aria-haspopup="dialog"/);
  assert.match(html, /开仓均价/);
  assert.doesNotMatch(html, /aria-label="每腿目标名义"/);
  assert.doesNotMatch(html, /预览调仓差额/);
  assert.doesNotMatch(html, /查看使用说明/);
  assert.doesNotMatch(html, /确认并真实下单/);
  qc.clear();
});

test('交易历史位于详情页底部并展示两腿成交与订单 ID', () => {
  const value = snapshot(hypeFixture);
  const qc = seed(value);
  const actionId = `adjust-0x${'a'.repeat(32)}`;
  qc.setQueryData(['hl-trade-history', value.pair.id, value.pair.account_address], {
    pair_id: value.pair.id,
    account_address: value.pair.account_address,
    items: [{
      action_id: actionId,
      request_id: '4d37d2a5-1da5-47a1-a2ce-b17365ea566f',
      execution_mode: 'managed',
      status: 'succeeded',
      target_notional: '2000',
      created_at: '2026-11-04T02:00:00Z',
      legs: [{
        coin: 'HYPE', position_side: 'long', action: 'buy', reduce_only: false,
        order_qty: '10', protect_px: '41', status: 'filled', confirmed_qty: '10',
        avg_px: '40.5', oid: 12345, cloid: null, result: '全部成交',
      }, {
        coin: 'BTC', position_side: 'short', action: 'sell', reduce_only: false,
        order_qty: '0.02', protect_px: '100000', status: 'filled', confirmed_qty: '0.02',
        avg_px: '100100', oid: 12346, cloid: null, result: '全部成交',
      }],
      diagnostics: [],
    }],
    has_more: false,
    skipped_reports: 0,
  });

  const html = renderDetail(qc, value.pair.id);

  assert.match(html, /交易历史/);
  assert.match(html, /每腿目标/);
  assert.match(html, /40\.5/);
  assert.match(html, /12345/);
  assert.match(html, new RegExp(actionId));
  assert.ok(html.indexOf('交易历史') > html.indexOf('解锁锚点'));
  qc.clear();
});

test('调仓使用说明子页面包含操作步骤、差额示例和异常处理', () => {
  const html = renderToString(
    <MemoryRouter initialEntries={['/hyperliquid/adjustment-guide']}>
      <HyperliquidAdjustmentGuidePage />
    </MemoryRouter>,
  );
  assert.match(html, /调仓使用说明/);
  assert.match(html, /先预览差额/);
  assert.match(html, /1990 USD/);
  assert.match(html, /1995 USD/);
  assert.match(html, /15000 USD/);
  assert.match(html, /API Wallet/);
  assert.match(html, /本地签名/);
  assert.match(html, /不写入 localStorage/);
  assert.match(html, /禁止输入原交易账户私钥/);
  assert.match(html, /调仓结果未知/);
  assert.match(html, /禁止直接重试/);
});
