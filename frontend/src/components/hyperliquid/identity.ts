import type { HlPairDescriptor, HlPairSnapshot } from '../../lib/types/hyperliquid';

export type SnapshotIdentitySource = Pick<HlPairSnapshot, 'config_revision' | 'pair'>;

/**
 * 快照身份检查（整改 R2）：渲染任何快照数据前必须通过。
 * 比较 config_revision、pair id、network、dex、有序 coins、归一化地址。
 */
export function snapshotIdentityMatches(
  snapshot: SnapshotIdentitySource,
  directoryRevision: string,
  descriptor: HlPairDescriptor,
): boolean {
  if (snapshot.config_revision !== directoryRevision) return false;
  const snapPair = snapshot.pair;
  if (snapPair.id !== descriptor.id) return false;
  if (snapPair.network !== descriptor.network) return false;
  if (snapPair.dex !== descriptor.dex) return false;
  if (snapPair.coins?.length !== 2 || descriptor.coins?.length !== 2) return false;
  if (snapPair.coins[0] !== descriptor.coins[0]) return false;
  if (snapPair.coins[1] !== descriptor.coins[1]) return false;
  return (snapPair.account_address ?? '').toLowerCase()
    === (descriptor.account_address ?? '').toLowerCase();
}
