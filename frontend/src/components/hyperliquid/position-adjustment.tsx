import { useEffect, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { SlidersHorizontal } from 'lucide-react';
import { Link } from 'react-router-dom';

import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { ApiError, hyperliquidPairsApi } from '@/lib/api/client';
import type { HlAdjustmentReport, HlPairDescriptor } from '@/lib/types/hyperliquid';
import { accountShort, dash, formatTime, slippageLabel } from './format';
import { refreshHyperliquid } from './queries';
import { OrderPreflightPanel, preflightQueryOptions } from './order-preflight';

const errorText = (error: unknown) => error instanceof ApiError ? error.detail || error.message : error instanceof Error ? error.message : '请求失败';
const actionLabel = (action: string, reduceOnly: boolean) => {
  if (action === 'none') return '无需交易';
  return `${action === 'buy' ? '买入' : '卖出'}${reduceOnly ? '（只减仓）' : '（增仓）'}`;
};

export function PositionAdjustment({ descriptor, configRevision, initialTarget = '2000', buttonLabel = '调整仓位', legIndices }: {
  descriptor: HlPairDescriptor;
  configRevision: string;
  initialTarget?: string;
  buttonLabel?: string;
  legIndices?: number[];
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState(initialTarget);
  const [slippageBps, setSlippageBps] = useState(5);
  const [confirmation, setConfirmation] = useState('');
  const [report, setReport] = useState<HlAdjustmentReport | null>(null);
  const [actionError, setActionError] = useState('');
  const [progress, setProgress] = useState('');
  const account = descriptor.account_address;
  const managedWallet = useQuery({
    queryKey: ['hl-managed-wallet', descriptor.id, account],
    queryFn: () => hyperliquidPairsApi.managedWallet(descriptor.id, account ?? ''),
    enabled: open && account != null,
    retry: false,
    refetchOnWindowFocus: false,
  });
  const body = {
    target_notional: target.trim(),
    max_slippage_bps: slippageBps,
    account_address: account ?? '',
    config_revision: configRevision,
    ...(legIndices == null ? {} : { leg_indices: legIndices }),
  };
  const preview = useMutation({
    mutationFn: () => {
      const wallet = managedWallet.data;
      if (!wallet?.configured) throw new Error('请先在账户配置中添加托管 API Wallet');
      return hyperliquidPairsApi.previewManagedAdjustment(descriptor.id, {
        ...body,
        request_id: crypto.randomUUID(),
        wallet_version: wallet.wallet_version,
      });
    },
    onSuccess: setReport,
  });
  const managed = useMutation({
    mutationFn: async () => {
      if (!report?.request_id || report.wallet_version == null) {
        throw new Error('托管调仓预览身份缺失，请重新预览');
      }
      await requirePreflight();
      setActionError('');
      setProgress('正在使用托管 API Wallet 执行并核对成交…');
      const request = {
        ...body,
        request_id: report.request_id,
        wallet_version: report.wallet_version,
        confirmation: confirmation.trim(),
      };
      try {
        return await hyperliquidPairsApi.executeManagedAdjustment(descriptor.id, request);
      } catch (error) {
        try {
          return await hyperliquidPairsApi.managedAdjustment(descriptor.id, report.request_id);
        } catch {
          throw error;
        }
      }
    },
    onSuccess: async (result) => {
      setReport(result);
      if (result.managed_status !== 'running') await refreshHyperliquid(qc, descriptor.id);
    },
    onError: (error) => setActionError(errorText(error)),
    onSettled: () => setProgress(''),
  });

  useEffect(() => {
    setOpen(false);
    setTarget(initialTarget);
    setReport(null);
    setConfirmation('');
    setActionError('');
    setProgress('');
    preview.reset();
    managed.reset();
  }, [descriptor.id, account, configRevision, initialTarget, legIndices]);

  const activeRequest = managedWallet.data?.active_execution;
  useEffect(() => {
    if (!open || report != null || !activeRequest) return;
    let active = true;
    void hyperliquidPairsApi.managedAdjustment(descriptor.id, activeRequest.request_id).then((result) => {
      if (!active) return;
      setReport(result);
    }).catch((error) => {
      if (active) setActionError(errorText(error));
    });
    return () => { active = false; };
  }, [activeRequest, descriptor.id, open, report]);

  useEffect(() => {
    if (report?.managed_status !== 'running' || !report.request_id) return;
    let active = true;
    const timer = window.setInterval(() => {
      void hyperliquidPairsApi.managedAdjustment(descriptor.id, report.request_id!).then(async (result) => {
        if (!active) return;
        setReport(result);
        if (result.managed_status !== 'running') await refreshHyperliquid(qc, descriptor.id);
      }).catch((error) => {
        if (active) setActionError(errorText(error));
      });
    }, 2000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [descriptor.id, qc, report?.managed_status, report?.request_id]);

  const requestPreview = (event: FormEvent) => {
    event.preventDefault();
    setReport(null);
    setConfirmation('');
    setActionError('');
    preview.mutate();
  };
  const busy = preview.isPending || managed.isPending || report?.managed_status === 'running';
  const isExecutionResult = report != null && (
    !['preview', 'prepared'].includes(report.status)
    || ['failed', 'unknown', 'succeeded', 'incomplete'].includes(report.managed_status ?? '')
  );
  const quoteQuery = useQuery({
    queryKey: ['hl-adjustment-quote', descriptor.id, configRevision],
    queryFn: () => hyperliquidPairsApi.adjustmentQuote(descriptor.id, configRevision),
    enabled: open && account != null && !busy,
    refetchInterval: 5000,
    refetchIntervalInBackground: false,
    retry: 1,
  });
  const stalePreview = report != null
    && ['preview', 'prepared'].includes(report.status)
    && quoteQuery.data != null
    && report.legs.some((leg, index) => {
      const quote = quoteQuery.data.legs[index];
      return quote != null && Number(leg.order_qty) > 0 && (leg.action === 'buy'
        ? Number(quote.ask) > Number(leg.protect_px) : Number(quote.bid) < Number(leg.protect_px));
    });
  const preflight = useQuery({
    ...preflightQueryOptions(descriptor, configRevision),
    enabled: open && account != null && !busy,
  });
  const needsIncrease = report == null || report.legs.some((leg) => !leg.reduce_only && Number(leg.order_qty) > 0);
  const policyBlocked = needsIncrease && (preflight.isPending || preflight.isError || !preflight.data?.ready_for_increase);
  const requirePreflight = async () => {
    if (!needsIncrease) return;
    const latest = await preflight.refetch();
    if (latest.isError || !latest.data?.ready_for_increase) {
      throw new Error('下单前检查未通过：请确认两腿均使用支持的模式且杠杆不超过 3x，再重新预览。');
    }
  };
  const settingsChanged = () => {
    if (isExecutionResult) return;
    setReport(null);
    setConfirmation('');
    setActionError('保证金或杠杆已修改，请重新预览调仓差额。');
    preview.reset();
  };

  return (
    <>
      <Button type="button" size="sm" variant="outline" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(true)}>
        <SlidersHorizontal className="mr-2 h-4 w-4" />{buttonLabel}
      </Button>
      <Dialog
        open={open}
        onOpenChange={(nextOpen) => {
          if (!busy || nextOpen) setOpen(nextOpen);
        }}
        title={legIndices == null ? '调整两腿仓位' : '调整指定腿仓位'}
        className="max-w-4xl"
      >
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            目标为每腿绝对名义（USD）；默认方向 {descriptor.coins[0]} 多 / {descriptor.coins[1]} 空。使用账户配置中的托管 API Wallet，先预览差额，再输入目标金额确认执行。
            {legIndices != null && <span className="text-amber-700">本次仅调整 {legIndices.map((index) => descriptor.coins[index]).join('、')}，另一腿保持不动。</span>}
            {' '}<Link className="text-primary underline underline-offset-4" to="/hyperliquid/adjustment-guide">查看使用说明</Link>
          </p>
          <p className="text-sm" aria-label="签名方式">签名方式：托管 API Wallet</p>
          {managedWallet.isSuccess && !managedWallet.data.configured && (
            <p className="text-sm text-amber-600">当前账户未配置托管 API Wallet，请先在账户配置中添加。</p>
          )}
          {managedWallet.isError && <div className="flex flex-wrap items-center gap-2 text-sm" role="alert">
            <span className="text-rose-600">托管钱包读取失败：{errorText(managedWallet.error)}</span>
            <Button type="button" variant="outline" size="sm" onClick={() => void managedWallet.refetch()}>重新读取托管钱包</Button>
          </div>}
          {account == null ? (
            <p className="text-sm text-muted-foreground">请先添加并选择账户，再计算调仓差额。</p>
          ) : (
            <form className="flex flex-col items-stretch gap-2 sm:flex-row sm:flex-wrap sm:items-end" onSubmit={requestPreview}>
              <label className="grid w-full gap-1 text-sm sm:w-auto">
                <span>最大允许滑点</span>
                <select aria-label="最大允许滑点" className="h-10 rounded-md border bg-background px-3" value={slippageBps} disabled={busy} onChange={(event) => {
                  setSlippageBps(Number(event.target.value)); setReport(null); setConfirmation(''); preview.reset();
                }}>
                  {[2, 5, 10, 20, 50].map((bps) => <option key={bps} value={bps}>{(bps / 100).toFixed(2)}%</option>)}
                </select>
              </label>
              <label className="grid w-full gap-1 text-sm sm:w-auto">
                <span>每腿目标名义（USD）</span>
                <input
                  aria-label="每腿目标名义"
                  className="w-full rounded-md border bg-background px-3 py-2 font-mono sm:w-52"
                  type="number"
                  min="0"
                  step="any"
                  value={target}
                  disabled={busy}
                  onChange={(event) => {
                    setTarget(event.target.value);
                    setReport(null);
                    setActionError('');
                  }}
                  required
                />
              </label>
              <Button type="submit" variant="outline" disabled={busy || !target.trim() || !managedWallet.data?.configured}>
                {preview.isPending ? '正在计算…' : '预览调仓差额'}
              </Button>
            </form>
          )}
          {preview.isError && <p className="text-sm text-rose-600" role="alert">预览失败：{errorText(preview.error)}</p>}
          {account != null && <OrderPreflightPanel descriptor={descriptor} configRevision={configRevision}
            data={preflight.data} error={preflight.error} loading={preflight.isPending} refreshing={preflight.isFetching}
            busy={busy} onRefresh={() => void preflight.refetch()} onSettingsChanged={settingsChanged} />}

          <p className="text-xs text-muted-foreground">每腿保护价受计划中间价 ±{(slippageBps / 100).toFixed(2)}% 限制，预览和发送前均检查整笔数量的可见盘口深度。托管执行会按最新盘口重算计划；深度检查通过仍可能因行情变化而部分成交。</p>
          <section aria-label="实时盘口" className="rounded-md border p-3 text-sm">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium">实时盘口（每 5 秒更新）</span>
              {quoteQuery.data && <span className="text-xs text-muted-foreground">查询于 {formatTime(quoteQuery.data.queried_at)}</span>}
            </div>
            {quoteQuery.isPending && <p className="text-muted-foreground">正在读取盘口…</p>}
            {quoteQuery.error && <p role="alert" className="text-amber-600">盘口更新失败：{errorText(quoteQuery.error)}</p>}
            {quoteQuery.data && <div className="grid gap-2 sm:grid-cols-2">
              {quoteQuery.data.legs.map((leg) => <div key={leg.coin} className="rounded bg-muted/40 p-2">
                <div className="font-medium">{leg.coin}</div>
                <div className="mt-1 grid grid-cols-3 gap-2 font-mono text-xs">
                  <span>买 {leg.bid}</span><span>卖 {leg.ask}</span><span>中 {leg.mid}</span>
                </div>
                <div className="mt-1 text-xs text-muted-foreground">盘口时间 {formatTime(leg.book_time)}</div>
              </div>)}
            </div>}
          </section>

          {report && (
            <div className="space-y-3 rounded-md border p-3">
              <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                <span className="font-medium">
                  {report.managed_status === 'running' ? '托管调仓执行中'
                    : report.managed_status === 'failed' ? '托管调仓未执行'
                    : report.status === 'preview' ? '调仓预览（未下单）'
                    : report.status === 'prepared' ? '等待 API Wallet 签名'
                    : report.status === 'succeeded' ? '调仓已完成'
                    : report.status === 'incomplete' ? '调仓未完成'
                    : '调仓结果未知'}
                </span>
                <span className="font-mono text-xs text-muted-foreground">{report.action_id ?? report.request_id ?? 'dry-run'}</span>
              </div>
              {stalePreview && <div className="flex flex-wrap items-center gap-2 rounded-md bg-amber-500/10 p-2 text-sm text-amber-700" role="alert">
                <span>盘口已超出保护价，当前预览已过期，请重新预览后再执行。</span>
                <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => { setConfirmation(''); preview.mutate(); }}>按最新盘口重新预览</Button>
              </div>}
              <div className="overflow-x-auto">
                <table className="w-full text-sm whitespace-nowrap">
                  <thead><tr className="text-left text-xs text-muted-foreground">
                    {['合约', '当前名义', '预计目标名义', '动作', '交易数量', '预览中间价', '保护价', '成交均价', '滑点', '结果'].map((heading) => <th key={heading} className="py-1 pr-4">{heading}</th>)}
                  </tr></thead>
                  <tbody>{report.legs.map((leg) => (
                    <tr key={leg.coin} className="border-t">
                      <td className="py-1.5 pr-4 font-medium">{leg.coin}（{leg.position_side === 'long' ? '多' : '空'}）</td>
                      <td className="pr-4 font-mono">{leg.current_notional}</td>
                      <td className="pr-4 font-mono">{leg.estimated_target_notional}</td>
                      <td className="pr-4">{actionLabel(leg.action, leg.reduce_only)}</td>
                      <td className="pr-4 font-mono">{leg.order_qty}</td>
                      <td className="pr-4 font-mono">{leg.mid_px}</td>
                      <td className="pr-4 font-mono">{leg.action === 'none' ? '—' : leg.protect_px}</td>
                      <td className="pr-4 font-mono">{dash(leg.avg_px)}</td>
                      <td className="pr-4 font-mono text-xs">{slippageLabel(leg.slippage_pct, leg.slippage_usd)}</td>
                      <td className="pr-4 text-xs">{leg.result ?? (isExecutionResult ? leg.status : report.status === 'prepared' ? '待签名' : '待确认')}</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
              <div className="grid gap-2 sm:grid-cols-2" aria-label="盘口深度检查">{report.legs.map((leg) => leg.depth && (
                <div key={leg.coin} className="rounded bg-muted/40 p-2 text-xs">
                  <span className="font-medium">{leg.coin} 深度检查通过</span>
                  <p>需求 {leg.depth.required_qty} / 限价内可见数量 {leg.depth.available_qty}</p>
                  <p>预计均价 {leg.depth.estimated_avg_px} · 保护价 {leg.protect_px}</p>
                  <p className="text-muted-foreground">盘口时间 {formatTime(new Date(leg.depth.book_time_ms).toISOString())}</p>
                </div>
              ))}</div>
              <ul className="space-y-1 text-xs text-muted-foreground">
                {report.diagnostics.map((diagnostic, index) => <li key={index}>{diagnostic}</li>)}
              </ul>

              {['preview', 'prepared'].includes(report.status) && (
                <div className="space-y-3 border-t pt-3">
                  <p className="text-sm text-muted-foreground">交易账户 <span className="font-mono">{accountShort(account ?? '')}</span></p>
                  <p className="text-sm">托管签名账户 <span className="font-mono">{accountShort(managedWallet.data?.wallet_address ?? report.wallet_address ?? '')}</span> · v{report.wallet_version}</p>
                  <p className="text-sm text-muted-foreground">请核对两腿订单，并输入目标金额确认使用托管 API Wallet 执行。</p>
                  <label className="grid gap-1 text-sm">
                    <span>确认目标金额（USD）</span>
                    <input aria-label="确认目标金额" className="w-full rounded-md border bg-background px-3 py-2 font-mono sm:w-52"
                      value={confirmation} onChange={(event) => setConfirmation(event.target.value)} disabled={busy} required />
                  </label>
                  <Button type="button" variant="destructive" disabled={busy || stalePreview || policyBlocked || !managedWallet.data?.configured || confirmation.trim() !== target.trim()} onClick={() => managed.mutate()}>
                    {managed.isPending || report.managed_status === 'running' ? '正在执行或查询原请求…' : '使用托管钱包下单'}
                  </Button>
                </div>
              )}
              {progress && <p className="text-sm text-sky-600" role="status">{progress}</p>}
              {actionError && <p className="text-sm text-rose-600" role="alert">{actionError}</p>}
              {report.status === 'unknown' && <p className="text-sm text-rose-600">禁止直接重试。请先按 request_id、action ID、cloid 和交易所实际持仓人工核对。</p>}
              {report.status === 'incomplete' && report.managed_status !== 'failed' && <p className="text-sm text-amber-600">部分订单未全部成交。可重新预览实际差额，但不会自动补单。</p>}
              {report.managed_status === 'failed' && <p className="text-sm text-amber-600">本次请求在发送前失败，没有执行订单。</p>}
            </div>
          )}
        </div>
      </Dialog>
    </>
  );
}
