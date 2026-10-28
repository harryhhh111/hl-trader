import assert from 'node:assert/strict';
import test from 'node:test';

import { QueryClient, QueryObserver } from '@tanstack/query-core';

import {
  hlPairQueryOptions,
  refreshHyperliquid,
} from './queries.ts';
import type { HlPairSnapshot } from '../../lib/types/hyperliquid';

function snapshot(pairId: string, revision: string): HlPairSnapshot {
  return {
    config_revision: revision,
    pair: {
      id: pairId,
      label: pairId,
      network: 'mainnet',
      dex: '',
      coins: ['HYPE', 'BTC'],
      account_address: null,
      unlocks_enabled: false,
    },
  } as HlPairSnapshot;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

async function flush() {
  await new Promise<void>((resolve) => setImmediate(resolve));
}

test('QueryObserver 跨 revision/pair 不保留旧快照，同 key 刷新保留旧值', async () => {
  const qc = new QueryClient();
  const first = snapshot('hype-btc', 'rev1');
  const second = snapshot('nbis-crwv', 'rev2');
  const pending = deferred<HlPairSnapshot>();
  const observer = new QueryObserver(
    qc,
    hlPairQueryOptions('rev1', 'hype-btc', async () => first),
  );
  const unsubscribe = observer.subscribe(() => {});
  await observer.refetch();
  assert.equal(observer.getCurrentResult().data, first);

  observer.setOptions(hlPairQueryOptions('rev2', 'nbis-crwv', () => pending.promise));
  assert.equal(observer.getCurrentResult().data, undefined);
  assert.equal(observer.getCurrentResult().isPlaceholderData, false);
  pending.resolve(second);
  await pending.promise;
  await flush();
  assert.equal(observer.getCurrentResult().data, second);

  const refreshing = deferred<HlPairSnapshot>();
  observer.setOptions(hlPairQueryOptions('rev2', 'nbis-crwv', () => refreshing.promise));
  const refetch = observer.refetch();
  assert.equal(observer.getCurrentResult().data, second, '同 key 刷新期间保留当前快照');
  refreshing.resolve(snapshot('nbis-crwv', 'rev2'));
  await refetch;
  unsubscribe();
  qc.clear();
});

test('慢旧请求迟到不会覆盖新 pair 的 QueryObserver 数据', async () => {
  const qc = new QueryClient();
  const slow = deferred<HlPairSnapshot>();
  const current = deferred<HlPairSnapshot>();
  const observer = new QueryObserver(
    qc,
    hlPairQueryOptions('rev1', 'hype-btc', () => slow.promise),
  );
  const unsubscribe = observer.subscribe(() => {});
  observer.setOptions(hlPairQueryOptions('rev2', 'nbis-crwv', () => current.promise));
  const expected = snapshot('nbis-crwv', 'rev2');
  current.resolve(expected);
  await flush();
  assert.equal(observer.getCurrentResult().data, expected);
  slow.resolve(snapshot('hype-btc', 'rev1'));
  await flush();
  assert.equal(observer.getCurrentResult().data, expected);
  unsubscribe();
  qc.clear();
});

test('手动刷新在目录失败时不请求任何 pair 快照', async () => {
  const qc = new QueryClient();
  let calls = 0;
  await assert.rejects(refreshHyperliquid(qc, undefined, {
    directory: async () => { throw new Error('directory unavailable'); },
    pairDashboard: async (id) => {
      calls += 1;
      return snapshot(id, 'unused');
    },
  }), /directory unavailable/);
  assert.equal(calls, 0);
  qc.clear();
});

test('手动刷新按目录返回的新 revision 获取快照', async () => {
  const qc = new QueryClient();
  const calls: string[] = [];
  const directory = await refreshHyperliquid(qc, undefined, {
    directory: async () => ({
      schema_version: 1,
      config_revision: 'rev2',
      pairs: [snapshot('nbis-crwv', 'rev2').pair],
    }),
    pairDashboard: async (id) => {
      calls.push(id);
      return snapshot(id, 'rev2');
    },
  });
  assert.equal(directory.config_revision, 'rev2');
  assert.deepEqual(calls, ['nbis-crwv']);
  assert.equal(
    qc.getQueryData<HlPairSnapshot>(['hl-pair', 'rev2', 'nbis-crwv'])?.pair.id,
    'nbis-crwv',
  );
  qc.clear();
});
