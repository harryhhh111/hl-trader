import assert from 'node:assert/strict';
import test from 'node:test';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderToString } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';

import App from '@/App';
import { ChangelogPage } from './changelog-page';


test('更新日志按日期倒序展示功能、体验和安全改动', () => {
  const html = renderToString(<ChangelogPage />);
  assert.match(html, /更新日志/);
  assert.match(html, /2026-11-12/);
  assert.match(html, /Pair 风控与下单前检查/);
  assert.match(html, /同一交易账户可跨 pair/);
  assert.match(html, /每腿发送前刷新盘口/);
  assert.match(html, /未执行的腿明确显示“未发送”/);
  assert.match(html, /2026-11-11/);
  assert.match(html, /调仓过程与结果更清晰/);
  assert.match(html, /每 5 秒更新两腿盘口/);
  assert.match(html, /目标仓位调整与托管 API Wallet/);
  assert.match(html, /新功能/);
  assert.match(html, /体验优化/);
  assert.match(html, /安全/);
  assert.ok(html.indexOf('2026-11-12') < html.indexOf('2026-11-11'));
  assert.ok(html.indexOf('2026-11-11') < html.indexOf('2026-11-04'));
});


test('/changelog 路由保留登录布局并提供主导航入口', () => {
  const qc = new QueryClient();
  qc.setQueryData(['session'], { authenticated: true, auth_mode: 'token' });
  const html = renderToString(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/changelog']}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  assert.match(html, /aria-label="主导航"/);
  assert.match(html, /href="\/changelog"/);
  assert.match(html, /aria-current="page"/);
  assert.match(html, /最新更新排在前面/);
  qc.clear();
});
