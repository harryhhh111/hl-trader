import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ShieldAlert } from 'lucide-react';

import { PositionAdjustment } from '@/components/hyperliquid/position-adjustment';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { ApiError, hyperliquidPairsApi } from '@/lib/api/client';
import type { HlPairDescriptor, HlPairRisk } from '@/lib/types/hyperliquid';
import { formatTime, signed } from './format';

const errorText = (error: unknown) => error instanceof ApiError ? error.detail || error.message : error instanceof Error ? error.message : '请求失败';
const optionalTime = (value: string | null) => value == null ? '—' : formatTime(value);
const statusLabels: Record<HlPairRisk['status'], string> = {
  disabled: '未启用',
  monitoring: '监控中',
  triggered: '已触发，等待人工确认',
  executing: '人工平仓执行中',
  completed: '已完成平仓',
  incomplete: '平仓未完成',
  unknown: '结果未知，禁止直接重试',
  unavailable: '监控数据不可用',
};
const eventLabels: Record<HlPairRisk['events'][number]['event_type'], string> = {
  rule_updated: '规则已更新',
  triggered: '达到阈值',
  trigger_cleared: '已回到阈值内',
  completed: '监控确认空仓',
  execution_updated: '人工平仓结果',
};

function distanceText(rule: HlPairRisk, kind: 'take_profit' | 'stop_loss') {
  if (rule.pair_unrealized_pnl == null) return '—';
  const pnl = Number(rule.pair_unrealized_pnl);
  const threshold = Number(kind === 'take_profit' ? rule.take_profit_usd : rule.stop_loss_usd);
  if (!Number.isFinite(threshold)) return '—';
  const distance = kind === 'take_profit' ? threshold - pnl : pnl + threshold;
  return `${distance.toFixed(2)} USD`;
}

export function PairRiskControls({ descriptor, configRevision }: {
  descriptor: HlPairDescriptor;
  configRevision: string;
}) {
  const account = descriptor.account_address;
  const qc = useQueryClient();
  const queryKey = ['hl-pair-risk', descriptor.id, account] as const;
  const riskQuery = useQuery({
    queryKey,
    queryFn: () => hyperliquidPairsApi.pairRisk(descriptor.id, account ?? ''),
    enabled: account != null,
    retry: false,
    refetchInterval: 5000,
    refetchIntervalInBackground: false,
  });
  const rule = riskQuery.data;
  const [enabled, setEnabled] = useState(false);
  const [takeProfit, setTakeProfit] = useState('');
  const [stopLoss, setStopLoss] = useState('');
  useEffect(() => {
    setEnabled(rule?.enabled ?? false);
    setTakeProfit(rule?.take_profit_usd ?? '');
    setStopLoss(rule?.stop_loss_usd ?? '');
  }, [account, rule?.rule_revision, rule?.take_profit_usd, rule?.stop_loss_usd, rule?.enabled]);
  const save = useMutation({
    mutationFn: () => hyperliquidPairsApi.savePairRisk(descriptor.id, {
      account_address: account ?? '',
      config_revision: configRevision,
      enabled,
      take_profit_usd: takeProfit.trim() || null,
      stop_loss_usd: stopLoss.trim() || null,
    }),
    onSuccess: (result) => {
      qc.setQueryData(queryKey, result);
      void qc.invalidateQueries({ queryKey: ['hl-pair-risk', descriptor.id] });
    },
  });
  const needsClose = rule?.trigger_kind != null && ['triggered', 'incomplete'].includes(rule.status);

  return (
    <Card className={needsClose ? 'border-amber-500' : ''}>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base flex items-center gap-2"><ShieldAlert className="h-4 w-4" />Pair 止盈止损</CardTitle>
          {rule && <Badge variant="secondary" className={needsClose ? 'bg-amber-500/10 text-amber-700' : ''}>{statusLabels[rule.status]}</Badge>}
        </div>
        <CardDescription>按两腿当前未实现盈亏之和监控；只提醒，不会自动下单。平仓前必须重新预览并由你签名确认。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {account == null ? <p className="text-muted-foreground">请先选择账户。</p> : <>
          {rule?.configured && <div className="grid gap-2 rounded-md bg-muted/40 p-3 sm:grid-cols-2 lg:grid-cols-4">
            <span>最近监控盈亏 <span className="font-mono">{signed(rule.pair_unrealized_pnl)}</span></span>
            <span>距止盈 <span className="font-mono">{distanceText(rule, 'take_profit')}</span></span>
            <span>距止损 <span className="font-mono">{distanceText(rule, 'stop_loss')}</span></span>
            <span>检查时间 <span className="font-mono text-xs">{optionalTime(rule.last_checked_at)}</span></span>
          </div>}
          {rule?.enabled && !rule.worker_available && <p role="alert" className="text-amber-600">监控 worker 未运行或状态已过期；旧读数不会继续触发，也不能代替最新平仓预览。</p>}
          {rule?.last_error && <p role="alert" className="text-amber-600">{rule.last_error}</p>}
          {rule?.trigger_kind != null && <div className="rounded-md bg-amber-500/10 p-3 text-amber-700" role="alert">
            <p className="font-medium">{rule.trigger_kind === 'take_profit' ? '已达到 Pair 整体止盈条件' : '已达到 Pair 整体止损条件'}：触发时盈亏 {signed(rule.trigger_pnl)} USD。</p>
            <p className="mt-1 text-xs">触发时间 {optionalTime(rule.triggered_at)}。这不是已平仓回执；请核对最新仓位、挂单和盘口后人工确认。</p>
            {needsClose && <div className="mt-3"><PositionAdjustment descriptor={descriptor} configRevision={configRevision} initialTarget="0" buttonLabel="预览并人工平仓" /></div>}
          </div>}
          {rule?.status === 'unknown' && <p className="text-rose-600">已有平仓结果未知。请先按 action ID、request ID 和交易所记录核对，不要直接重试。</p>}
          {rule?.status === 'incomplete' && <p className="text-amber-600">已有订单拒单或部分成交。重新预览会按最新实际仓位计算，不会自动补单。</p>}
          <form className="grid gap-3 lg:grid-cols-[1fr_1fr_auto_auto] lg:items-end" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
            <label className="grid gap-1">
              <span>整体止盈金额（USD）</span>
              <input aria-label="整体止盈金额" className="rounded-md border bg-background px-3 py-2 font-mono" type="number" min="0" step="any" value={takeProfit} onChange={(event) => setTakeProfit(event.target.value)} placeholder="例如 300" />
            </label>
            <label className="grid gap-1">
              <span>整体止损金额（USD）</span>
              <input aria-label="整体止损金额" className="rounded-md border bg-background px-3 py-2 font-mono" type="number" min="0" step="any" value={stopLoss} onChange={(event) => setStopLoss(event.target.value)} placeholder="例如 500" />
            </label>
            <label className="flex h-10 items-center gap-2 whitespace-nowrap">
              <input aria-label="启用止盈止损监控" type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />启用监控
            </label>
            <Button type="submit" disabled={save.isPending}>{save.isPending ? '保存中…' : '保存规则'}</Button>
          </form>
          {save.isError && <p role="alert" className="text-rose-600">{errorText(save.error)}</p>}
          {save.isSuccess && <p role="status" className="text-emerald-600">规则已保存；监控 worker 将从新规则重新检查。</p>}
          {riskQuery.isError && <p role="alert" className="text-rose-600">规则读取失败：{errorText(riskQuery.error)}</p>}
          {(rule?.events.length ?? 0) > 0 && <details className="text-xs text-muted-foreground">
            <summary>最近规则与触发记录</summary>
            <ul className="mt-2 space-y-1">{rule?.events.map((event) => <li key={event.id}>
              {formatTime(event.created_at)} · {eventLabels[event.event_type]}{event.detail ? ` · ${event.detail}` : ''}{event.pair_unrealized_pnl != null ? ` · 盈亏 ${signed(event.pair_unrealized_pnl)} USD` : ''}
            </li>)}</ul>
          </details>}
          <p className="text-xs text-muted-foreground">盈亏不包含历史已实现盈亏、资金费和手续费。两腿非原子平仓，IOC 拒单、部分成交和断线仍可能导致裸露仓位；本功能不是强平保护或保证亏损上限。</p>
        </>}
      </CardContent>
    </Card>
  );
}
