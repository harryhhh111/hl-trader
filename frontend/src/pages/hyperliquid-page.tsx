import { useEffect, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Flame, RefreshCw } from 'lucide-react';
import { Link } from 'react-router-dom';

import { PageHeader } from '@/components/layout/page-header';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  accountShort, dash, formatTime, NETWORK_LABEL, pctFromFraction, plainDiagnostic, positionLegsLabel, signed,
} from '@/components/hyperliquid/format';
import { snapshotIdentityMatches } from '@/components/hyperliquid/identity';
import {
  hlDirectoryQueryOptions, hlPairQueryOptions, refreshHyperliquid,
} from '@/components/hyperliquid/queries';
import { hyperliquidPairsApi, ApiError } from '@/lib/api/client';
import type { HlAnnouncementsBlock, HlPairDescriptor } from '@/lib/types/hyperliquid';

const errorText = (error: unknown) => error instanceof ApiError ? error.detail || error.message : error instanceof Error ? error.message : '请求失败';

function PairCard({ descriptor, revision }: { descriptor: HlPairDescriptor; revision: string }) {
  const qc = useQueryClient();
  const retryRef = useRef(0);
  const query = useQuery(hlPairQueryOptions(revision, descriptor.id));
  const snap = query.data;
  // R2：身份检查通过前不渲染任何快照数据；持续不匹配时停止而不是循环刷新。
  const identityOk = snap != null && snapshotIdentityMatches(snap, revision, descriptor);
  useEffect(() => {
    if (snap != null && !identityOk && retryRef.current === 0) {
      retryRef.current = 1;
      void qc.invalidateQueries({ queryKey: ['hl-directory'] });
    }
    if (identityOk) retryRef.current = 0;
  }, [snap, identityOk, qc]);
  const positions = identityOk ? snap?.blocks.positions : undefined;
  const funding = identityOk ? snap?.blocks.funding : undefined;
  const ratio = identityOk ? snap?.blocks.ratio : undefined;

  const legNotional = (coin: string) => {
    if (!positions || positions.items == null) return '—';
    const item = positions.items.find((p) => p.coin === coin);
    return item?.notional == null ? '—' : `${item.notional} USD`;
  };

  const hourly = (coin: string) => {
    if (!funding) return '—';
    return pctFromFraction(funding.hourly_rates?.[coin] ?? null);
  };

  return (
    <Card className={query.isFetching ? 'opacity-70' : ''}>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <CardTitle className="text-base">
            <Link className="hover:text-primary" to={`/hyperliquid/pairs/${descriptor.id}`}>{descriptor.label}</Link>
          </CardTitle>
          <div className="flex gap-1.5">
            <Badge variant="secondary" className="bg-sky-500/10 text-sky-600">{NETWORK_LABEL[descriptor.network] ?? descriptor.network}</Badge>
            <Badge variant="outline">{descriptor.dex === '' ? '原生' : descriptor.dex}</Badge>
            {descriptor.account_address == null
              ? <Badge variant="secondary" className="bg-amber-500/10 text-amber-600">账户未配置</Badge>
              : <Badge variant="outline" title={descriptor.account_address}>{accountShort(descriptor.account_address)}</Badge>}
          </div>
        </div>
        <CardDescription>
          只读快照，
          <Link to={`/hyperliquid/pairs/${descriptor.id}`} className="underline underline-offset-4 hover:text-primary">查看详情</Link>
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {query.isError && !snap && <p className="text-rose-600" role="alert">{errorText(query.error)}</p>}
        {snap && !identityOk && (retryRef.current === 0
          ? <p className="text-sm text-muted-foreground">目录身份更新中…</p>
          : <p className="text-sm text-rose-600" role="alert">快照身份与当前目录持续不一致，已停止展示旧数据。</p>)}
        {snap && identityOk && (
          <>
            {query.isError && (
              <p className="text-amber-600">刷新失败（{errorText(query.error)}），显示上次读数（{formatTime(snap.queried_at)}）。</p>
            )}
            <div className="grid grid-cols-2 gap-x-6 gap-y-1.5">
              <span>方向 <span className="font-medium">{positions ? positionLegsLabel(descriptor.coins, positions.items, positions.position_state) : '—'}</span>
                {positions?.position_state === 'same_direction' && <span className="ml-1 text-xs text-amber-600">（非对冲）</span>}
              </span>
              <span>两腿盈亏合计 <span className="font-mono">{signed(positions?.unrealized_pnl)}</span></span>
              <span>{descriptor.coins[0]} 名义 <span className="font-mono">{legNotional(descriptor.coins[0])}</span></span>
              <span>{descriptor.coins[1]} 名义 <span className="font-mono">{legNotional(descriptor.coins[1])}</span></span>
              <span>比价 <span className="font-mono">{dash(ratio?.latest_close)}</span>
                {ratio?.vs_ma30_pct != null && (
                  <span className={Number(ratio.vs_ma30_pct) >= 0 ? 'text-emerald-600' : 'text-rose-600'}> {signed(ratio.vs_ma30_pct)}%</span>
                )}
              </span>
              <span>小时费率 <span className="font-mono">{hourly(descriptor.coins[0])} / {hourly(descriptor.coins[1])}</span></span>
            </div>
            <p className="text-xs text-muted-foreground">查询时间 {formatTime(snap.queried_at)}</p>
            {['account', 'positions', 'funding', 'ratio'].map((name) => {
              const block = snap.blocks[name as 'account' | 'positions' | 'funding' | 'ratio'];
              return block.status !== 'ok' && block.diagnostics.length > 0
                ? <p key={name} className="text-xs text-amber-600">{name}: {block.diagnostics.map(plainDiagnostic).join('；')}</p>
                : null;
            })}
          </>
        )}
        {query.isPending && <p className="text-muted-foreground">读取中…</p>}
      </CardContent>
    </Card>
  );
}

function AnnouncementsSection() {
  const query = useQuery({
    queryKey: ['hl-announcements'],
    queryFn: hyperliquidPairsApi.announcements,
    staleTime: 30_000,
    retry: false,
    refetchInterval: false,
    refetchOnWindowFocus: false,
  });
  const block: HlAnnouncementsBlock | undefined = query.data?.block;
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">平台公告</CardTitle>
        <CardDescription>Telegram 公开预览，仅最近可见消息；此处统一展示，不在 pair 详情重复。</CardDescription>
      </CardHeader>
      <CardContent className="text-sm space-y-2">
        {query.isError && <p className="text-rose-600" role="alert">{errorText(query.error)}</p>}
        {block && (
          <>
            {block.items == null
              ? <p className="text-muted-foreground">公告不可用</p>
              : block.items.length === 0
                ? <p className="text-muted-foreground">暂无可见公告</p>
                : (
                  <ul className="space-y-2">
                    {block.items.map((a, i) => (
                      <li key={i}>
                        <Badge variant="outline" className="mr-2">{a.kind === 'api' ? 'API' : '公告'}</Badge>
                        <a className="text-primary underline" href={a.url} target="_blank" rel="noopener noreferrer">{a.text}</a>
                        <span className="ml-2 text-xs text-muted-foreground">{a.published_at ?? '时间未知'}</span>
                      </li>
                    ))}
                  </ul>
                )}
            <p className="text-xs text-muted-foreground">查询时间 {block.queried_at ? formatTime(block.queried_at) : '—'}</p>
            {block.diagnostics?.map((d, i) => <p key={i} className="text-xs text-amber-600">{plainDiagnostic(d)}</p>)}
          </>
        )}
      </CardContent>
    </Card>
  );
}

export function HyperliquidPage() {
  const directory = useQuery(hlDirectoryQueryOptions());
  const qc = useQueryClient();

  return (
    <div className="space-y-4">
      <PageHeader icon={Flame} title="Hyperliquid"
        description="多 pair 主网只读看板。各卡片独立加载，互不阻塞；不设置跨 pair 总权益/总收益。">
        <Button variant="outline" onClick={() => {
          void refreshHyperliquid(qc).catch(() => undefined);
        }} disabled={directory.isFetching}>
          <RefreshCw className={`w-4 h-4 mr-2 ${directory.isFetching ? 'animate-spin' : ''}`} />
          {directory.isFetching ? '刷新中…' : '刷新'}
        </Button>
      </PageHeader>

      {directory.isPending && <p className="text-muted-foreground">正在读取 pair 目录…</p>}
      {directory.isError && <p className="text-rose-600" role="alert">目录加载失败:{errorText(directory.error)}{directory.data ? '，显示上次目录与读数。' : ''}</p>}
      {directory.data && directory.data.pairs.length === 0 && (
        <Card><CardContent className="py-10 text-center text-muted-foreground">没有启用的 pair</CardContent></Card>
      )}
      {directory.data && directory.data.pairs.length > 0 && (
        <div className="grid gap-4 lg:grid-cols-2">
          {directory.data.pairs.map((p) => (
            <PairCard key={p.id} descriptor={p} revision={directory.data.config_revision} />
          ))}
        </div>
      )}

      <AnnouncementsSection />
    </div>
  );
}
