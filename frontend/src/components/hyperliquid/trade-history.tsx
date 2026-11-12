import { useQuery, useQueryClient } from '@tanstack/react-query';
import { RefreshCw } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { ApiError, hyperliquidPairsApi } from '@/lib/api/client';
import type { HlTradeHistoryStatus } from '@/lib/types/hyperliquid';
import { dash, formatTime, slippageLabel } from './format';

const STATUS_LABEL: Record<HlTradeHistoryStatus, string> = {
  running: '执行中',
  succeeded: '已完成',
  incomplete: '未完全成交',
  unknown: '结果未知',
};

const LEG_STATUS_LABEL: Record<string, string> = {
  not_required: '无需交易',
  prepared: '待签名',
  not_sent: '未发送',
  pending: '待交易所确认',
  filled: '全部成交',
  partially_filled: '部分成交',
  rejected: '已拒绝',
  unknown: '结果未知',
};

const MODE_LABEL = { managed: '托管 API Wallet', browser: '浏览器签名', server: '服务端钱包' } as const;
const errorText = (error: unknown) => error instanceof ApiError ? error.detail || error.message : '交易历史加载失败';

export function TradeHistory({ pairId, accountAddress }: {
  pairId: string;
  accountAddress: string | null;
}) {
  const qc = useQueryClient();
  const history = useQuery({
    queryKey: ['hl-trade-history', pairId, accountAddress],
    queryFn: () => hyperliquidPairsApi.tradeHistory(pairId, accountAddress ?? ''),
    enabled: accountAddress != null,
    retry: false,
    refetchOnWindowFocus: false,
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">交易历史</CardTitle>
          <Button type="button" size="sm" variant="outline" disabled={accountAddress == null || history.isFetching}
            onClick={() => void qc.invalidateQueries({ queryKey: ['hl-trade-history', pairId, accountAddress] })}>
            <RefreshCw className={`mr-2 h-4 w-4 ${history.isFetching ? 'animate-spin' : ''}`} />
            {history.isFetching ? '刷新中…' : '刷新历史'}
          </Button>
        </div>
        <CardDescription>
          仅记录本系统发起的调仓交易，预览不列入；不包含在 Hyperliquid 或其他系统手工下单的记录。状态来自执行回执，不是完整的交易所成交账单。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {accountAddress == null && <p className="text-muted-foreground">请先配置并选择交易账户。</p>}
        {accountAddress != null && history.isPending && <p className="text-muted-foreground">正在读取交易历史…</p>}
        {history.isError && <p className="text-rose-600" role="alert">{errorText(history.error)}</p>}
        {history.data?.skipped_reports ? (
          <p className="text-xs text-amber-600" role="status">
            有 {history.data.skipped_reports} 份回执无法读取或身份不匹配，已跳过；如需核对请检查服务端回执。
          </p>
        ) : null}
        {history.data?.items.length === 0 && <p className="text-muted-foreground">暂无调仓交易记录。</p>}
        {history.data?.items.map((item) => (
          <section key={item.action_id} className="space-y-2 rounded-md border p-3" aria-label={`调仓 ${item.action_id}`}>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="font-mono text-xs">{formatTime(item.created_at)}</span>
              <Badge variant="secondary" className={item.status === 'succeeded'
                ? 'bg-emerald-500/10 text-emerald-600'
                : item.status === 'unknown' || item.status === 'incomplete'
                  ? 'bg-amber-500/10 text-amber-600' : 'bg-sky-500/10 text-sky-600'}>
                {STATUS_LABEL[item.status]}
              </Badge>
              <span>每腿目标 <span className="font-mono">{item.target_notional} USD</span></span>
              <span className="text-xs text-muted-foreground">{MODE_LABEL[item.execution_mode]}</span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm whitespace-nowrap">
                <thead><tr className="text-left text-xs text-muted-foreground">
                  {['合约', '持仓方向', '操作', '委托数量', '保护价', '成交数量', '成交均价', '滑点', '腿状态', '订单 ID'].map((heading) => (
                    <th key={heading} className="py-1 pr-4">{heading}</th>
                  ))}
                </tr></thead>
                <tbody>{item.legs.map((leg) => (
                  <tr key={leg.coin} className="border-t">
                    <td className="py-1.5 pr-4 font-medium">{leg.coin}</td>
                    <td className="pr-4">{leg.position_side === 'long' ? '多' : '空'}</td>
                    <td className="pr-4">{leg.action === 'none' ? '无需交易' : `${leg.action === 'buy' ? '买入' : '卖出'}${leg.reduce_only ? '（只减仓）' : '（增仓）'}`}</td>
                    <td className="pr-4 font-mono">{leg.order_qty}</td>
                    <td className="pr-4 font-mono">{leg.action === 'none' ? '—' : leg.protect_px}</td>
                    <td className="pr-4 font-mono">{leg.action === 'none' ? '—' : leg.confirmed_qty}</td>
                    <td className="pr-4 font-mono">{dash(leg.avg_px)}</td>
                    <td className="pr-4 font-mono text-xs">{slippageLabel(leg.slippage_pct, leg.slippage_usd)}</td>
                    <td className="pr-4">{['incomplete', 'unknown'].includes(item.status) && leg.status === 'prepared'
                      ? '未发送' : LEG_STATUS_LABEL[leg.status] ?? leg.status}</td>
                    <td className="font-mono">{leg.oid ?? '—'}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
            {item.legs.some((leg) => leg.result) && (
              <ul className="space-y-0.5 text-xs text-muted-foreground">
                {item.legs.filter((leg) => leg.result).map((leg) => <li key={leg.coin}>{leg.coin}：{leg.result}</li>)}
              </ul>
            )}
            {item.diagnostics.length > 0 && (
              <ul className="space-y-0.5 text-xs text-muted-foreground">
                {item.diagnostics.map((diagnostic, index) => <li key={index}>{diagnostic}</li>)}
              </ul>
            )}
            <p className="break-all font-mono text-xs text-muted-foreground" title={item.action_id}>
              动作 ID：{item.action_id}{item.request_id ? ` · request_id：${item.request_id}` : ''}
            </p>
          </section>
        ))}
        {history.data?.has_more && <p className="text-xs text-muted-foreground">仅显示最近 50 次调仓。</p>}
      </CardContent>
    </Card>
  );
}
