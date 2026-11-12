import { useQuery } from '@tanstack/react-query';

import { Button } from '@/components/ui/button';
import { hyperliquidPairsApi } from '@/lib/api/client';
import type { HlOrderPreflight, HlPairDescriptor } from '@/lib/types/hyperliquid';
import { formatTime } from './format';
import { LeverageAdjustment } from './leverage-adjustment';

export function preflightQueryOptions(descriptor: HlPairDescriptor, revision: string) {
  return {
    queryKey: ['hl-preflight', revision, descriptor.id, descriptor.account_address] as const,
    queryFn: () => hyperliquidPairsApi.adjustmentPreflight(descriptor.id, descriptor.account_address ?? '', revision),
    staleTime: 0,
    retry: false as const,
    refetchInterval: 10_000,
    refetchIntervalInBackground: false,
  };
}

export function OrderPreflightPanel({ descriptor, configRevision, data, error, loading, refreshing, onRefresh, onSettingsChanged, busy = false }: {
  descriptor: HlPairDescriptor;
  configRevision: string;
  data?: HlOrderPreflight;
  error: unknown;
  loading: boolean;
  refreshing: boolean;
  onRefresh: () => void;
  onSettingsChanged?: () => void;
  busy?: boolean;
}) {
  return <section aria-label="下单前检查" className="space-y-3 rounded-md border p-3 text-sm">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <span className="font-medium">下单前检查 · Cross 优先 · 最高 3x</span>
      <Button type="button" size="sm" variant="outline" disabled={busy || refreshing} onClick={onRefresh}>重新检查</Button>
    </div>
    <p className="text-xs text-muted-foreground">逐合约设置杠杆上限为 3x；支持全仓的合约须使用 Cross。仅支持逐仓的市场作为例外。修改需手动确认，修改后请重新预览订单。</p>
    {loading && <p role="status">正在读取保证金模式和杠杆…</p>}
    {!!error && <p role="alert" className="text-rose-600">检查失败，禁止增仓；可以重试检查，只减仓和平仓仍可操作。</p>}
    {data && <>
      <p className={!error && data.ready_for_increase ? 'text-emerald-600' : 'text-amber-600'}>
        {error ? '以下为上次数据，不能用于增仓' : data.ready_for_increase ? '两腿检查通过，可继续预览并确认订单' : '检查未通过，请手动修改后再增仓'}
        <span className="ml-2 text-xs text-muted-foreground">查询于 {formatTime(data.queried_at)}</span>
      </p>
      <div className="grid gap-2 sm:grid-cols-2">
        {data.legs.map((leg) => <div key={leg.coin} className="space-y-2 rounded bg-muted/40 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-medium">{leg.coin}</span>
            <LeverageAdjustment descriptor={descriptor} configRevision={configRevision} coin={leg.coin}
              currentLeverage={leg.leverage} currentMode={leg.margin_mode} maxLeverage={leg.market_max_leverage}
              crossAvailable={leg.cross_available} onSettingsChanged={onSettingsChanged} disabled={busy || !!error} />
          </div>
          <p>保证金模式 <span className="font-mono">{leg.margin_mode === 'cross' ? 'Cross（全仓）' : 'Isolated（逐仓）'}</span></p>
          <p>当前设置 <span className={leg.leverage > 3 ? 'font-mono text-rose-600' : 'font-mono'}>{leg.leverage}x</span>
            <span className="ml-3 text-xs text-muted-foreground">应用 ≤3x · 市场 ≤{leg.market_max_leverage}x</span>
          </p>
          {leg.diagnostics.map((message, index) => <p key={index} className="text-xs text-amber-600">{message}</p>)}
        </div>)}
      </div>
    </>}
  </section>;
}

export function PairOrderPreflight({ descriptor, configRevision }: { descriptor: HlPairDescriptor; configRevision: string }) {
  const query = useQuery({ ...preflightQueryOptions(descriptor, configRevision), enabled: descriptor.account_address != null });
  if (descriptor.account_address == null) return null;
  return <OrderPreflightPanel descriptor={descriptor} configRevision={configRevision} data={query.data} error={query.error}
    loading={query.isPending} refreshing={query.isFetching} onRefresh={() => void query.refetch()} />;
}
