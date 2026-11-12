import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Scale } from 'lucide-react';

import { PositionAdjustment } from '@/components/hyperliquid/position-adjustment';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { ApiError, hyperliquidPairsApi } from '@/lib/api/client';
import type { HlPairDescriptor, HlPairRebalance } from '@/lib/types/hyperliquid';
import { formatTime } from './format';

const errorText = (error: unknown) => error instanceof ApiError ? error.detail || error.message : error instanceof Error ? error.message : '请求失败';
const optionalTime = (value: string | null) => value == null ? '—' : formatTime(value);
const statusLabels: Record<HlPairRebalance['status'], string> = {
  disabled: '未启用',
  monitoring: '监控中',
  candidate: '已生成候选，等待人工确认',
  executing: '人工再平衡执行中',
  cooling: '已完成，冷却中',
  incomplete: '再平衡未完成',
  unknown: '结果未知，禁止直接重试',
  unavailable: '监控数据不可用',
};
const eventLabels: Record<HlPairRebalance['events'][number]['event_type'], string> = {
  rule_updated: '规则已更新',
  candidate: '连续超阈值并生成预览',
  candidate_cleared: '候选已取消',
  execution_updated: '人工再平衡结果',
};

function driftText(value: string | undefined) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${parsed.toFixed(2)}%` : '—';
}

export function RebalanceControls({ descriptor, configRevision }: {
  descriptor: HlPairDescriptor;
  configRevision: string;
}) {
  const account = descriptor.account_address;
  const qc = useQueryClient();
  const queryKey = ['hl-pair-rebalance', descriptor.id, account] as const;
  const ruleQuery = useQuery({
    queryKey,
    queryFn: () => hyperliquidPairsApi.pairRebalance(descriptor.id, account ?? ''),
    enabled: account != null,
    retry: false,
    refetchInterval: 5000,
    refetchIntervalInBackground: false,
  });
  const rule = ruleQuery.data;
  const [enabled, setEnabled] = useState(false);
  const [target, setTarget] = useState('2000');
  const [threshold, setThreshold] = useState('10');
  useEffect(() => {
    setEnabled(rule?.enabled ?? false);
    setTarget(rule?.target_notional ?? '2000');
    setThreshold(rule?.threshold_pct ?? '10');
  }, [account, rule?.rule_revision, rule?.enabled, rule?.target_notional, rule?.threshold_pct]);
  const save = useMutation({
    mutationFn: () => hyperliquidPairsApi.savePairRebalance(descriptor.id, {
      account_address: account ?? '',
      config_revision: configRevision,
      enabled,
      target_notional: target.trim(),
      threshold_pct: threshold.trim(),
    }),
    onSuccess: (result) => {
      qc.setQueryData(queryKey, result);
      void qc.invalidateQueries({ queryKey: ['hl-pair-rebalance', descriptor.id] });
    },
  });
  const hasCandidate = (rule?.candidate_leg_indices.length ?? 0) > 0;
  const canAdjust = hasCandidate && ['candidate', 'incomplete'].includes(rule?.status ?? '')
    && rule?.worker_available && !rule.risk_blocked && rule.open_orders === 0;

  return (
    <Card className={hasCandidate && rule?.status === 'candidate' ? 'border-amber-500' : ''}>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base flex items-center gap-2"><Scale className="h-4 w-4" />阈值再平衡</CardTitle>
          {rule && <Badge variant="secondary" className={hasCandidate ? 'bg-amber-500/10 text-amber-700' : ''}>{statusLabels[rule.status]}</Badge>}
        </div>
        <CardDescription>固定每腿目标名义，连续两次检查达到偏离阈值后生成候选预览；只调整候选腿，不会自动下单。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {account == null ? <p className="text-muted-foreground">请先选择账户。</p> : <>
          {rule?.configured && <div className="grid gap-2 rounded-md bg-muted/40 p-3 sm:grid-cols-2">
            {descriptor.coins.map((coin, index) => <div key={coin} className="flex flex-wrap justify-between gap-2">
              <span>{coin}{rule.candidate_leg_indices.includes(index) ? '（候选腿）' : ''}</span>
              <span className="font-mono">当前 {rule.current_notionals?.[index] ?? '—'} USD · 偏离 {driftText(rule.drift_pct?.[index])}</span>
            </div>)}
            <span>连续确认 <span className="font-mono">{Math.min(rule.consecutive_count, 2)} / 2</span></span>
            <span>检查时间 <span className="font-mono text-xs">{optionalTime(rule.last_checked_at)}</span></span>
          </div>}
          {rule?.enabled && !rule.worker_available && <p role="alert" className="text-amber-600">监控 worker 未运行或状态已过期；不会使用旧读数生成新候选。</p>}
          {rule?.last_error && <p role="alert" className="text-amber-600">{rule.last_error}</p>}
          {rule?.risk_blocked && <p role="alert" className="text-amber-600">Pair 止盈止损已触发或仍有未核清平仓，已暂停再平衡候选与确认。</p>}
          {rule && hasCandidate && <div className="rounded-md bg-amber-500/10 p-3 text-amber-700" role="alert">
            <p className="font-medium">候选腿：{rule.candidate_leg_indices.map((index) => descriptor.coins[index]).join('、')}；目标为每腿 {rule.target_notional} USD。</p>
            <p className="mt-1 text-xs">候选生成于 {optionalTime(rule.candidate_at)}。触发预览只用于提示，实际执行前必须重新读取仓位和盘口。</p>
            {rule.candidate_preview && <ul className="mt-2 space-y-1 text-xs">
              {rule.candidate_preview.legs.filter((leg) => leg.action !== 'none').map((leg) => <li key={leg.coin}>
                触发时预览：{leg.coin} {leg.action === 'buy' ? '买入' : '卖出'} {leg.order_qty}，保护价 {leg.protect_px}
              </li>)}
            </ul>}
            {canAdjust && rule.target_notional && <div className="mt-3"><PositionAdjustment descriptor={descriptor} configRevision={configRevision}
              initialTarget={rule.target_notional} legIndices={rule.candidate_leg_indices} buttonLabel="重新预览并人工再平衡" /></div>}
            {!canAdjust && rule.status !== 'unknown' && <p className="mt-2 text-xs">当前存在挂单、监控过期或风险退出约束，暂不能确认；请先处理后再核对。</p>}
          </div>}
          {rule?.status === 'unknown' && <p className="text-rose-600">已有再平衡结果未知。请先按 action ID、request ID 和交易所记录核对，不要直接重试。</p>}
          {rule?.status === 'incomplete' && <p className="text-amber-600">已有订单拒单或部分成交。重新预览只计算候选腿的最新实际差额，不会自动补单。</p>}
          {rule?.status === 'cooling' && <p className="text-muted-foreground">本次已完成，冷却至 {optionalTime(rule.cooldown_until)}；冷却后仍需重新连续确认。</p>}
          <form className="grid gap-3 lg:grid-cols-[1fr_1fr_auto_auto] lg:items-end" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
            <label className="grid gap-1">
              <span>每腿目标名义（USD）</span>
              <input aria-label="再平衡每腿目标名义" className="rounded-md border bg-background px-3 py-2 font-mono" type="number" min="0" step="any" value={target} onChange={(event) => setTarget(event.target.value)} required />
            </label>
            <label className="grid gap-1">
              <span>偏离阈值（%）</span>
              <input aria-label="再平衡偏离阈值" className="rounded-md border bg-background px-3 py-2 font-mono" type="number" min="0" max="100" step="any" value={threshold} onChange={(event) => setThreshold(event.target.value)} required />
            </label>
            <label className="flex h-10 items-center gap-2 whitespace-nowrap">
              <input aria-label="启用阈值再平衡监控" type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />启用监控
            </label>
            <Button type="submit" disabled={save.isPending}>{save.isPending ? '保存中…' : '保存再平衡规则'}</Button>
          </form>
          {save.isError && <p role="alert" className="text-rose-600">{errorText(save.error)}</p>}
          {save.isSuccess && <p role="status" className="text-emerald-600">规则已保存；连续确认和冷却状态已重新开始。</p>}
          {ruleQuery.isError && <p role="alert" className="text-rose-600">规则读取失败：{errorText(ruleQuery.error)}</p>}
          {(rule?.events.length ?? 0) > 0 && <details className="text-xs text-muted-foreground">
            <summary>最近候选与执行记录</summary>
            <ul className="mt-2 space-y-1">{rule?.events.map((event) => <li key={event.id}>
              {formatTime(event.created_at)} · {eventLabels[event.event_type]}{event.detail ? ` · ${event.detail}` : ''}{event.leg_indices.length > 0 ? ` · ${event.leg_indices.map((index) => descriptor.coins[index]).join('、')}` : ''}
            </li>)}</ul>
          </details>}
          <p className="text-xs text-muted-foreground">空仓不会生成开仓候选；同向持仓、数据缺失或过期不会触发。未超阈值腿保持不动。两腿非原子执行，IOC 拒单或部分成交仍可能留下偏离。</p>
        </>}
      </CardContent>
    </Card>
  );
}
