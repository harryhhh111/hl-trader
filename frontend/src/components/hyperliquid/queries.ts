import type { QueryClient } from '@tanstack/react-query';

import { hyperliquidPairsApi } from '../../lib/api/client.ts';
import type {
  HlPairDirectory,
  HlPairSnapshot,
} from '../../lib/types/hyperliquid';

export function hlDirectoryQueryOptions(
  queryFn: () => Promise<HlPairDirectory> = hyperliquidPairsApi.directory,
) {
  return {
    queryKey: ['hl-directory'] as const,
    queryFn,
    staleTime: 30_000,
    retry: false as const,
    refetchInterval: false as const,
    refetchOnWindowFocus: false as const,
  };
}

export function hlPairQueryOptions(
  revision: string,
  pairId: string,
  queryFn: () => Promise<HlPairSnapshot> = () => hyperliquidPairsApi.pairDashboard(pairId),
) {
  return {
    queryKey: ['hl-pair', revision, pairId] as const,
    queryFn,
    staleTime: 30_000,
    retry: false as const,
    refetchInterval: false as const,
    refetchOnWindowFocus: false as const,
  };
}

type RefreshApi = {
  directory: () => Promise<HlPairDirectory>;
  pairDashboard: (pairId: string) => Promise<HlPairSnapshot>;
};

/** 目录成功后才按新身份刷新快照；目录失败时保留旧身份内容并抛错。 */
export async function refreshHyperliquid(
  qc: QueryClient,
  pairId?: string,
  api: RefreshApi = hyperliquidPairsApi,
): Promise<HlPairDirectory> {
  const directory = await qc.fetchQuery({
    ...hlDirectoryQueryOptions(api.directory),
    staleTime: 0,
  });
  const pairIds = pairId == null
    ? directory.pairs.map((pair) => pair.id)
    : directory.pairs.some((pair) => pair.id === pairId) ? [pairId] : [];
  await Promise.all(pairIds.map((id) => qc.fetchQuery({
    ...hlPairQueryOptions(directory.config_revision, id, () => api.pairDashboard(id)),
    staleTime: 0,
  })));
  if (pairId == null) {
    await qc.invalidateQueries({ queryKey: ['hl-announcements'] });
  }
  return directory;
}
