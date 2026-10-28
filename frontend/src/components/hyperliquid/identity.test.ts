import assert from 'node:assert/strict';
import test from 'node:test';

import { snapshotIdentityMatches } from './identity.ts';
import type { HlPairDescriptor } from '../../lib/types/hyperliquid';

const DESCRIPTOR: HlPairDescriptor = {
  id: 'nbis-crwv', label: 'NBIS / CRWV', network: 'mainnet', dex: 'xyz',
  coins: ['xyz:NBIS', 'xyz:CRWV'],
  account_address: '0x7f2120f612b78d579197762196b8a202ab654096',
  unlocks_enabled: false,
};

const withPair = (pair: HlPairDescriptor) => ({ config_revision: 'rev1', pair });
const SNAPSHOT = withPair({ ...DESCRIPTOR, account_address: '0x7F2120F612B78D579197762196B8A202AB654096' });

test('完全匹配（地址大小写不同仍通过）', () => {
  assert.equal(snapshotIdentityMatches(SNAPSHOT, 'rev1', DESCRIPTOR), true);
});

test('两组地址都为 null 时匹配', () => {
  assert.equal(snapshotIdentityMatches(withPair({ ...DESCRIPTOR, account_address: null }), 'rev1',
    { ...DESCRIPTOR, account_address: null }), true);
});

test('config_revision 不同即拒绝', () => {
  assert.equal(snapshotIdentityMatches(SNAPSHOT, 'rev2', DESCRIPTOR), false);
});

test('同 ID 换账户即拒绝', () => {
  assert.equal(snapshotIdentityMatches(SNAPSHOT, 'rev1',
    { ...DESCRIPTOR, account_address: '0x' + '9'.repeat(40) }), false);
});

test('目录 null 账户但快照有地址即拒绝（反之亦然）', () => {
  assert.equal(snapshotIdentityMatches(SNAPSHOT, 'rev1',
    { ...DESCRIPTOR, account_address: null }), false);
});

test('网络不同即拒绝', () => {
  assert.equal(snapshotIdentityMatches(withPair({ ...DESCRIPTOR, network: 'testnet' }), 'rev1', DESCRIPTOR), false);
});

test('合约顺序不同即拒绝（比价方向不同）', () => {
  assert.equal(snapshotIdentityMatches(
    withPair({ ...DESCRIPTOR, coins: ['xyz:CRWV', 'xyz:NBIS'] }), 'rev1', DESCRIPTOR), false);
});

test('dex 不同即拒绝', () => {
  assert.equal(snapshotIdentityMatches(withPair({ ...DESCRIPTOR, dex: '' }), 'rev1', DESCRIPTOR), false);
});

test('pair id 不同即拒绝', () => {
  assert.equal(snapshotIdentityMatches(withPair({ ...DESCRIPTOR, id: 'hype-btc' }), 'rev1', DESCRIPTOR), false);
});
