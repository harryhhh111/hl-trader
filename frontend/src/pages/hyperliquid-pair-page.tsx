import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Flame, RefreshCw } from 'lucide-react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { EChartsWrapper } from '@/components/charts/echarts-wrapper';
import { PageHeader } from '@/components/layout/page-header';
import {
  accountShort, dash, dexLabel, formatTime, NETWORK_LABEL, pctFromFraction, pctFromNumber,
  plainDiagnostic, positionLegsLabel, signed,
} from '@/components/hyperliquid/format';
import { snapshotIdentityMatches } from '@/components/hyperliquid/identity';
import {
  hlDirectoryQueryOptions, hlPairQueryOptions, refreshHyperliquid,
} from '@/components/hyperliquid/queries';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { ApiError } from '@/lib/api/client';
import type { HlPairSnapshot, HlPairDescriptor, HlRatioPoint } from '@/lib/types/hyperliquid';

const errorText = (error: unknown) => error instanceof ApiError ? error.detail || error.message : error instanceof Error ? error.message : '请求失败';

function BlockFrame({ title, block, hint, children }: {
  title: string;
  block: { status: string; diagnostics?: string[] } | null;
  hint?: string;
  children: React.ReactNode;
}) {
  const statusClass = block?.status === 'ok' ? 'bg-emerald-500/10 text-emerald-600'
    : block?.status === 'partial' ? 'bg-amber-500/10 text-amber-600'
    : 'bg-rose-500/10 text-rose-600';
  const statusText = block?.status === 'ok' ? '正常' : block?.status === 'partial' ? '部分可用' : '不可用';
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-base">{title}</CardTitle>
          {block && <Badge variant="secondary" className={statusClass}>{statusText}</Badge>}
        </div>
        {hint && <CardDescription>{hint}</CardDescription>}
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {children}
        {block?.diagnostics?.map((d, i) => <p key={i} className="text-xs text-amber-600">{plainDiagnostic(d)}</p>)}
      </CardContent>
    </Card>
  );
}

function RatioChart({ points, coins }: { points: HlRatioPoint[] | null; coins: [string, string] }) {
  const [range, setRange] = useState<30 | 90>(30);
  const visible = useMemo(
    () => (points == null ? [] : range === 30 ? points.slice(-30) : points),
    [points, range],
  );
  const option = useMemo(() => ({
    tooltip: {
      trigger: 'axis' as const,
      formatter: (params: { dataIndex: number }[]) => {
        const p = visible[params[0]?.dataIndex ?? 0];
        if (!p) return '';
        return [
          p.date,
          `${coins[0]} 收盘：${p.first_close ?? '—'}`,
          `${coins[1]} 收盘：${p.second_close ?? '—'}`,
          `比价：${p.ratio ?? '—'}`,
          `30MA：${p.ma30 ?? '—'}`,
        ].join('<br/>');
      },
    },
    legend: { data: ['比价', '30MA'] },
    grid: { left: 56, right: 16, top: 32, bottom: 24 },
    xAxis: { type: 'category' as const, data: visible.map((p) => p.date) },
    yAxis: { type: 'value' as const, scale: true },
    dataZoom: [{ type: 'inside' as const }],
    series: [
      { name: '比价', type: 'line' as const, showSymbol: false, connectNulls: false, data: visible.map((p) => p.ratio) },
      { name: '30MA', type: 'line' as const, showSymbol: false, connectNulls: false, data: visible.map((p) => p.ma30) },
    ],
  }), [visible, coins]);
  if (points == null) return <p className="text-muted-foreground">比价序列不可用</p>;
  return (
    <div className="space-y-2">
      <div className="flex gap-1">
        {([30, 90] as const).map((r) => (
          <Button key={r} size="sm" variant={range === r ? 'default' : 'outline'} onClick={() => setRange(r)}>{r} 天</Button>
        ))}
      </div>
      <EChartsWrapper option={option} style={{ height: 320 }} />
    </div>
  );
}

function DetailBody({ snapshot }: { snapshot: HlPairSnapshot }) {
  const { account, positions, funding, ratio, unlocks } = snapshot.blocks;
  const descriptor = snapshot.pair;
  const [first, second] = descriptor.coins;

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="py-4 text-sm">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-2">
            <span>方向 <span className="font-medium">{positionLegsLabel(descriptor.coins, positions.items, positions.position_state)}</span>
              {positions.position_state === 'same_direction' && <span className="ml-1 text-xs text-amber-600">（同向持仓，非对冲）</span>}
            </span>
            <span>两腿盈亏合计 <span className="font-mono">{signed(positions.unrealized_pnl)}</span></span>
            <span>名义比 <span className="font-mono">{dash(positions.notional_ratio)}</span></span>
            <span>差异 <span className="font-mono">{pctFromNumber(positions.ratio_drift_pct)}</span></span>
            <span>最新收盘比价 <span className="font-mono">{dash(ratio.latest_close)}</span></span>
            <span>30MA <span className="font-mono">{dash(ratio.ma30)}</span>
              {ratio.vs_ma30_pct != null && <span>（{signed(ratio.vs_ma30_pct)}%）</span>}
            </span>
            <span>权益 <span className="font-mono">{dash(account.equity)}</span>
              {account.equity_scope === 'perp_dex' && <span className="text-xs text-muted-foreground">（{dexLabel(descriptor.dex)}永续市场权益）</span>}
            </span>
            <span>挂单数 <span className="font-mono">{positions.open_orders ?? '—'}</span></span>
          </div>
          <p className="mt-2 text-xs text-muted-foreground">只读观察，未接入执行状态；盈亏仅为该地址指定两腿的未实现盈亏，不是独立策略收益。</p>
        </CardContent>
      </Card>

      <BlockFrame title={`${first} / ${second} 比价`} block={ratio} hint="UTC 已收盘日线；缺失日期断线；30/90 天切换。">
        <RatioChart points={ratio.points} coins={descriptor.coins} />
        <p className="text-xs text-muted-foreground">数据截至 {dash(ratio.as_of_date)} UTC 收盘；当日 live {dash(ratio.live)}</p>
      </BlockFrame>

      <BlockFrame title="仓位" block={positions} hint="按交易所实际持仓展示；缺项显示 —；保证金为交易所口径。">
        {positions.items == null
          ? <p className="text-muted-foreground">持仓不可用</p>
          : positions.items.length === 0
            ? <p className="text-muted-foreground">无持仓（已确认空仓）</p>
            : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm whitespace-nowrap">
                  <thead><tr className="text-left text-xs text-muted-foreground">
                    {['合约', '方向', '数量', '开仓均价', '标记价', '名义', '未实现盈亏', '保证金模式', '保证金占用', '强平价'].map((h) => <th key={h} className="py-1 pr-4">{h}</th>)}
                  </tr></thead>
                  <tbody>{positions.items.map((p, i) => (
                    <tr key={i} className="border-t">
                      <td className="py-1.5 pr-4 font-medium">{p.coin}</td>
                      <td className="pr-4">{p.side === 'long' ? '多' : '空'}</td>
                      <td className="pr-4 font-mono">{p.size}</td>
                      <td className="pr-4 font-mono">{dash(p.entry_px)}</td>
                      <td className="pr-4 font-mono">{dash(p.mark_px)}</td>
                      <td className="pr-4 font-mono">{p.notional == null ? '—' : `${p.notional} USD`}</td>
                      <td className={`pr-4 font-mono ${Number(p.unrealized_pnl) >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>{signed(p.unrealized_pnl)}</td>
                      <td className="pr-4">{p.margin_mode ?? '—'}</td>
                      <td className="pr-4 font-mono">{dash(p.margin_used)}</td>
                      <td className="font-mono">{dash(p.liquidation_px)}</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            )}
      </BlockFrame>

      <BlockFrame title="资金费" block={funding} hint="市场费率、日收付估算与实际收付分开标注。">
        <div className="space-y-1">
          {[first, second].map((coin) => (
            <div key={coin} className="flex gap-6">
              <span className="w-24 font-medium">{coin}</span>
              <span className="font-mono">{pctFromFraction(funding.hourly_rates?.[coin] ?? null)}/h</span>
              <span className="text-muted-foreground">年化 <span className="font-mono">{pctFromNumber(funding.annualized_pct?.[coin])}</span></span>
            </div>
          ))}
        </div>
        <p>日净收付估算（费率不变）：
          {funding.daily_estimate
            ? <span className="font-mono"> {funding.daily_estimate.direction === 'flat' ? '持平' : funding.daily_estimate.direction === 'receive' ? '收' : '付'} {funding.daily_estimate.amount} USDC</span>
            : ' —'}
        </p>
        <div>
          <p>{funding.window_complete ? '过去 48 小时实际净收付' : '已获取部分净收付（窗口不完整）'}：
            <span className="font-mono">{signed(funding.actual_48h_net)} USDC</span></p>
          <p className="text-xs text-muted-foreground">窗口 {dash(funding.window_start)} → {dash(funding.window_end)}</p>
          {funding.recent_payments != null && funding.recent_payments.length > 0 && (
            <ul className="mt-1 space-y-0.5 text-xs text-muted-foreground">
              {funding.recent_payments.map((p, i) => (
                <li key={i} className="font-mono">{p.time} {p.coin} {signed(p.usdc)} USDC（费率 {pctFromFraction(p.rate)}/h）</li>
              ))}
            </ul>
          )}
        </div>
      </BlockFrame>

      {unlocks ? (
        <BlockFrame title="解锁锚点" block={unlocks} hint="人工配置并核实；日历不等于实际申领。">
          {unlocks.status === 'unavailable' || !Array.isArray(unlocks.anchors) ? (
            <p className="text-muted-foreground">解锁锚点不可用</p>
          ) : unlocks.anchors.length > 0 ? (
            <ul className="space-y-1.5">
              {unlocks.anchors.map((a, i) => (
                <li key={i}>
                  <span className="font-mono">{a.date}</span>（{a.days_ahead} 天后）{a.label}{' '}
                  <a className="text-primary underline" href={a.source_url} target="_blank" rel="noopener noreferrer">来源</a>
                  <span className="text-xs text-muted-foreground">（{a.verified_utc} 人工核实）</span>
                </li>
              ))}
            </ul>
          ) : <p className="text-muted-foreground">未来 7 天没有已核实的锚点</p>}
          <p className="text-xs text-muted-foreground">{unlocks.note}</p>
        </BlockFrame>
      ) : (
        <Card>
          <CardHeader className="pb-3"><CardTitle className="text-base">解锁锚点</CardTitle></CardHeader>
          <CardContent className="text-sm text-muted-foreground">该 pair 不适用解锁锚点。</CardContent>
        </Card>
      )}
    </div>
  );
}

export function HyperliquidPairPage() {
  const { pairId = '' } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const retryRef = useRef(0);
  const directory = useQuery(hlDirectoryQueryOptions());
  const descriptor: HlPairDescriptor | undefined = directory.data?.pairs.find((p) => p.id === pairId);
  const snapshotQuery = useQuery({
    ...hlPairQueryOptions(directory.data?.config_revision ?? 'no-revision', pairId),
    enabled: descriptor != null,
  });
  const snapshot = snapshotQuery.data;
  // R2：完整身份检查；不匹配时刷新目录一次，持续不匹配明确报错，不渲染旧身份数据
  const identityOk = snapshot != null && descriptor != null && directory.data != null
    && snapshotIdentityMatches(snapshot, directory.data.config_revision, descriptor);
  useEffect(() => {
    retryRef.current = 0;
  }, [pairId]);
  useEffect(() => {
    if (snapshot != null && !identityOk && retryRef.current === 0) {
      retryRef.current = 1;
      void qc.invalidateQueries({ queryKey: ['hl-directory'] });
    }
  }, [snapshot, identityOk, qc]);
  useEffect(() => {
    if (identityOk) retryRef.current = 0;
  }, [identityOk]);

  return (
    <div className="space-y-4">
      <PageHeader icon={Flame} title={descriptor?.label ?? pairId}
        description={`${NETWORK_LABEL[descriptor?.network ?? ''] ?? ''} · ${dexLabel(descriptor?.dex ?? '')} · 只读快照`}>
        <div className="flex items-center gap-2">
          <select aria-label="切换 pair" className="border rounded-md bg-background px-2 py-1.5 text-sm"
            value={pairId} onChange={(e) => navigate(`/hyperliquid/pairs/${e.target.value}`)}>
            {(directory.data?.pairs ?? []).map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
          </select>
          <Button variant="outline" onClick={() => {
            void refreshHyperliquid(qc, pairId).catch(() => undefined);
          }} disabled={snapshotQuery.isFetching}>
            <RefreshCw className={`w-4 h-4 mr-2 ${snapshotQuery.isFetching ? 'animate-spin' : ''}`} />
            {snapshotQuery.isFetching ? '刷新中…' : '刷新'}
          </Button>
        </div>
      </PageHeader>

      {directory.isPending && <p className="text-muted-foreground">正在读取 pair 目录…</p>}
      {directory.isError && <p className="text-rose-600" role="alert">目录加载失败:{errorText(directory.error)}</p>}
      {directory.isSuccess && descriptor == null && (
        <Card><CardContent className="py-10 text-center text-muted-foreground">
          pair「{pairId}」不存在或已停用。<Link className="text-primary underline" to="/hyperliquid">返回总览</Link>
        </CardContent></Card>
      )}

      {descriptor && (
        <>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm text-muted-foreground">
            <Badge variant="secondary" className="bg-sky-500/10 text-sky-600">{NETWORK_LABEL[descriptor.network]}</Badge>
            <span>市场 <span className="font-mono">{descriptor.dex === '' ? '原生' : descriptor.dex}</span></span>
            {descriptor.account_address == null
              ? <Badge variant="secondary" className="bg-amber-500/10 text-amber-600">账户未配置，仅公开行情</Badge>
              : <span>账户 <span className="font-mono" title={descriptor.account_address}>{accountShort(descriptor.account_address)}</span></span>}
            {identityOk && <span>查询时间 <span className="font-mono">{formatTime(snapshot!.queried_at)}</span></span>}
          </div>

          {snapshotQuery.isPending && <p className="text-muted-foreground">读取快照…</p>}
          {snapshotQuery.isError && !snapshot && (
            <Card><CardContent className="py-10 text-center text-rose-600" role="alert">
              快照加载失败:{errorText(snapshotQuery.error)}
            </CardContent></Card>
          )}
          {snapshot && !identityOk && (
            retryRef.current === 0
              ? <p className="text-muted-foreground">正在核对数据身份…</p>
              : <Card><CardContent className="py-10 text-center text-rose-600" role="alert">
                  快照身份（配置修订、账户、网络或合约）与当前目录持续不一致，已停止展示旧数据。
                </CardContent></Card>
          )}
          {snapshot && identityOk && (
            <>
              {snapshotQuery.isError && (
                <p className="text-sm text-amber-600" role="alert">刷新失败（{errorText(snapshotQuery.error)}），显示上次读数（{formatTime(snapshot.queried_at)}）。</p>
              )}
              <DetailBody snapshot={snapshot} />
            </>
          )}
        </>
      )}
    </div>
  );
}
