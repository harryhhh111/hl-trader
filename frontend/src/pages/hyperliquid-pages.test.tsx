import assert from 'node:assert/strict';
import test from 'node:test';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderToString } from 'react-dom/server';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import { HyperliquidPage } from './hyperliquid-page';
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
