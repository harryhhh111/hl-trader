import { useEffect, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { SlidersHorizontal, WalletCards } from 'lucide-react';
import { Link } from 'react-router-dom';

import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { ApiError, hyperliquidPairsApi } from '@/lib/api/client';
import type { HlAdjustmentReport, HlPairDescriptor } from '@/lib/types/hyperliquid';
import {
  createLocalWallet,
  requestWalletAccount,
  signLocalTypedData,
  signTypedData,
  walletErrorText,
  type LocalWallet,
} from '@/lib/wallet';
import { accountShort, dash, formatTime, slippageLabel } from './format';
import { refreshHyperliquid } from './queries';

const errorText = (error: unknown) => error instanceof ApiError ? error.detail || error.message : walletErrorText(error);
const actionLabel = (action: string, reduceOnly: boolean) => {
  if (action === 'none') return '无需交易';
  return `${action === 'buy' ? '买入' : '卖出'}${reduceOnly ? '（只减仓）' : '（增仓）'}`;
};

export function PositionAdjustment({ descriptor, configRevision, initialTarget = '2000', buttonLabel = '调整仓位' }: {
  descriptor: HlPairDescriptor;
  configRevision: string;
  initialTarget?: string;
  buttonLabel?: string;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState(initialTarget);
  const [signerMode, setSignerMode] = useState<'managed' | 'local' | 'injected'>('local');
  const [injectedSigner, setInjectedSigner] = useState<string | null>(null);
  const [localWallet, setLocalWallet] = useState<LocalWallet | null>(null);
  const [privateKey, setPrivateKey] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [report, setReport] = useState<HlAdjustmentReport | null>(null);
  const [walletBusy, setWalletBusy] = useState(false);
  const [walletError, setWalletError] = useState('');
  const [progress, setProgress] = useState('');
  const account = descriptor.account_address;
  const managedWallet = useQuery({
    queryKey: ['hl-managed-wallet', descriptor.id, account],
    queryFn: () => hyperliquidPairsApi.managedWallet(descriptor.id, account ?? ''),
    enabled: open && account != null,
    retry: false,
    refetchOnWindowFocus: false,
  });
  const signer = signerMode === 'local'
    ? localWallet?.address.toLowerCase() ?? null
    : signerMode === 'injected' ? injectedSigner : null;
  const body = {
    target_notional: target.trim(),
    account_address: account ?? '',
    config_revision: configRevision,
  };
  const clearLocalWallet = () => {
    setLocalWallet(null);
    setPrivateKey('');
  };
  const preview = useMutation({
    mutationFn: () => {
      if (signerMode === 'managed') {
        const wallet = managedWallet.data;
        if (!wallet?.configured) throw new Error('请先在账户配置中添加托管 API Wallet');
        return hyperliquidPairsApi.previewManagedAdjustment(descriptor.id, {
          ...body,
          request_id: crypto.randomUUID(),
          wallet_version: wallet.wallet_version,
        });
      }
      return hyperliquidPairsApi.previewAdjustment(descriptor.id, body);
    },
    onSuccess: setReport,
  });
  const browser = useMutation({
    mutationFn: async () => {
      if (!signer) throw new Error(signerMode === 'local' ? '请先导入 API Wallet' : '请先连接 API Wallet');
      setWalletError('');
      setProgress('正在生成待签名订单…');
      const prepared = await hyperliquidPairsApi.prepareBrowserAdjustment(descriptor.id, {
        ...body,
        expected_signer: signer,
      });
      setReport(prepared);
      if (prepared.status === 'succeeded') return prepared;
      if (!prepared.action_id || !prepared.signing_requests?.length) {
        throw new Error('服务端未返回待签名订单');
      }
      const signatures: { leg_index: number; signature: string }[] = [];
      for (const [index, request] of prepared.signing_requests.entries()) {
        setProgress(signerMode === 'local'
          ? `正在本地签名（${index + 1}/${prepared.signing_requests.length}）…`
          : `请在钱包中签名（${index + 1}/${prepared.signing_requests.length}）…`);
        const signature = signerMode === 'local'
          ? await signLocalTypedData(localWallet!, request.typed_data)
          : await signTypedData(signer, request.typed_data);
        signatures.push({ leg_index: request.leg_index, signature });
      }
      setProgress('签名已完成，正在提交订单并核对成交…');
      return hyperliquidPairsApi.submitBrowserAdjustment(descriptor.id, {
        ...body,
        expected_signer: signer,
        action_id: prepared.action_id,
        signatures,
      });
    },
    onSuccess: async (result) => {
      setReport(result);
      clearLocalWallet();
      await refreshHyperliquid(qc, descriptor.id);
    },
    onError: (error) => setWalletError(errorText(error)),
    onSettled: () => setProgress(''),
  });
  const managed = useMutation({
    mutationFn: async () => {
      if (!report?.request_id || report.wallet_version == null) {
        throw new Error('托管调仓预览身份缺失，请重新预览');
      }
      setWalletError('');
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
    onError: (error) => setWalletError(errorText(error)),
    onSettled: () => setProgress(''),
  });

  useEffect(() => {
    setOpen(false);
    setTarget(initialTarget);
    setReport(null);
    setInjectedSigner(null);
    setLocalWallet(null);
    setPrivateKey('');
    setConfirmation('');
    setWalletError('');
    setProgress('');
    preview.reset();
    browser.reset();
    managed.reset();
  }, [descriptor.id, account, configRevision, initialTarget]);

  const activeRequest = managedWallet.data?.active_execution;
  useEffect(() => {
    if (!open || report != null || !activeRequest) return;
    let active = true;
    void hyperliquidPairsApi.managedAdjustment(descriptor.id, activeRequest.request_id).then((result) => {
      if (!active) return;
      setSignerMode('managed');
      setReport(result);
    }).catch((error) => {
      if (active) setWalletError(errorText(error));
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
        if (active) setWalletError(errorText(error));
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
    setWalletError('');
    preview.mutate();
  };
  const connect = async () => {
    setWalletBusy(true);
    setWalletError('');
    try {
      const selected = await requestWalletAccount();
      if (selected === account?.toLowerCase()) {
        throw new Error('当前连接的是原交易账户。请切换到已导入的 API Wallet 账户，不能使用原账户签名。');
      }
      setInjectedSigner(selected);
    } catch (error) {
      setWalletError(errorText(error));
    } finally {
      setWalletBusy(false);
    }
  };
  const loadLocalWallet = () => {
    setWalletError('');
    try {
      const wallet = createLocalWallet(privateKey);
      setPrivateKey('');
      if (wallet.address.toLowerCase() === account?.toLowerCase()) {
        setLocalWallet(null);
        throw new Error('导入的是原交易账户。只能导入已在 Hyperliquid 获得授权的 API Wallet。');
      }
      setLocalWallet(wallet);
    } catch (error) {
      setLocalWallet(null);
      setWalletError(errorText(error));
    }
  };
  const busy = preview.isPending || browser.isPending || managed.isPending || walletBusy || report?.managed_status === 'running';
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
    && report.legs.some((leg, index) => quoteQuery.data.legs[index]?.mid !== leg.mid_px);

  return (
    <>
      <Button type="button" size="sm" variant="outline" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(true)}>
        <SlidersHorizontal className="mr-2 h-4 w-4" />{buttonLabel}
      </Button>
      <Dialog
        open={open}
        onOpenChange={(nextOpen) => {
          if (!busy || nextOpen) setOpen(nextOpen);
          if (!nextOpen) clearLocalWallet();
        }}
        title="调整两腿仓位"
        className="max-w-4xl"
      >
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            目标为每腿绝对名义（USD）；默认方向 {descriptor.coins[0]} 多 / {descriptor.coins[1]} 空。先计算差额，再选择托管或浏览器 API Wallet 签名。
            {' '}<Link className="text-primary underline underline-offset-4" to="/hyperliquid/adjustment-guide">查看使用说明</Link>
          </p>
          <div className="flex flex-wrap items-center gap-2" aria-label="签名方式">
            {(['managed', 'local', 'injected'] as const).map((mode) => (
              <Button key={mode} type="button" size="sm" variant={signerMode === mode ? 'default' : 'outline'}
                aria-pressed={signerMode === mode} disabled={busy} onClick={() => {
                  setSignerMode(mode);
                  setReport(null);
                  setConfirmation('');
                  setWalletError('');
                }}>
                {mode === 'managed' ? '托管 API Wallet' : mode === 'local' ? '浏览器本地私钥' : '浏览器扩展钱包'}
              </Button>
            ))}
          </div>
          {signerMode === 'managed' && managedWallet.isSuccess && !managedWallet.data.configured && (
            <p className="text-sm text-amber-600">当前账户未配置托管 API Wallet，请先在账户配置中添加。</p>
          )}
          {account == null ? (
            <p className="text-sm text-muted-foreground">请先添加并选择账户，再计算调仓差额。</p>
          ) : (
            <form className="flex flex-col items-stretch gap-2 sm:flex-row sm:flex-wrap sm:items-end" onSubmit={requestPreview}>
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
                    setWalletError('');
                  }}
                  required
                />
              </label>
              <Button type="submit" variant="outline" disabled={busy || !target.trim()}>
                {preview.isPending ? '正在计算…' : '预览调仓差额'}
              </Button>
            </form>
          )}
          {preview.isError && <p className="text-sm text-rose-600" role="alert">预览失败：{errorText(preview.error)}</p>}

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
                <span>盘口已变化，当前预览已过期，请重新预览后再执行。</span>
                <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => preview.mutate()}>按最新盘口重新预览</Button>
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
              <ul className="space-y-1 text-xs text-muted-foreground">
                {report.diagnostics.map((diagnostic, index) => <li key={index}>{diagnostic}</li>)}
              </ul>

              {['preview', 'prepared'].includes(report.status) && (
                <div className="space-y-3 border-t pt-3">
                  <p className="text-sm text-muted-foreground">交易账户 <span className="font-mono">{accountShort(account ?? '')}</span>；只能使用已授权且不同于交易账户的 API Wallet。</p>
                  {signerMode === 'managed' ? (
                    <div className="space-y-2">
                      <p className="text-sm">托管签名账户 <span className="font-mono">{accountShort(managedWallet.data?.wallet_address ?? report.wallet_address ?? '')}</span> · v{report.wallet_version}</p>
                      <p className="text-xs text-amber-600">服务端将在本次请求内解密私钥并调用 Rust 执行器。请核对两腿订单，并输入目标金额确认。</p>
                      <label className="grid gap-1 text-sm">
                        <span>确认目标金额（USD）</span>
                        <input aria-label="确认目标金额" className="w-full rounded-md border bg-background px-3 py-2 font-mono sm:w-52"
                          value={confirmation} onChange={(event) => setConfirmation(event.target.value)} disabled={busy} required />
                      </label>
                      <Button type="button" variant="destructive" disabled={busy || stalePreview || confirmation.trim() !== target.trim()} onClick={() => managed.mutate()}>
                        {managed.isPending || report.managed_status === 'running' ? '正在执行或查询原请求…' : '使用托管钱包下单'}
                      </Button>
                    </div>
                  ) : signerMode === 'local' ? (
                    <div className="space-y-2">
                      <p className="text-sm text-muted-foreground">导入已授权的 API Wallet，由当前页面直接签名，不受 Rabby 等钱包的 Chain ID 检查限制。私钥只保存在页面内存，关闭弹窗、刷新或切换账户即清除，不写入本地存储。</p>
                      <form className="flex flex-col items-stretch gap-2 sm:flex-row sm:items-end" onSubmit={(event) => { event.preventDefault(); loadLocalWallet(); }}>
                        <label className="grid w-full gap-1 text-sm">
                          <span>API Wallet 私钥</span>
                          <input aria-label="API Wallet 私钥" className="w-full rounded-md border bg-background px-3 py-2 font-mono"
                            type="password" autoComplete="off" autoCapitalize="none" spellCheck={false} placeholder="0x…"
                            value={privateKey} disabled={busy} onChange={(event) => setPrivateKey(event.target.value)} required />
                        </label>
                        <Button type="submit" variant="outline" disabled={busy || !privateKey.trim()}>加载 API Wallet</Button>
                      </form>
                      <p className="text-xs text-amber-600">只能输入 API Wallet 私钥，禁止输入原交易账户私钥。页面或浏览器环境被恶意代码控制时，内存中的私钥仍可能泄露。</p>
                    </div>
                  ) : (
                    <Button type="button" variant="outline" onClick={() => void connect()} disabled={busy}>
                      <WalletCards className="mr-2 h-4 w-4" />{walletBusy ? '正在连接…' : injectedSigner ? '更换 API Wallet' : '连接 API Wallet'}
                    </Button>
                  )}
                  {signer && <p className="text-sm">签名账户 <span className="font-mono">{accountShort(signer)}</span></p>}
                  {signer && (
                    <>
                      <p className="text-sm text-amber-600">{signerMode === 'local'
                        ? '本地模式会逐腿计算签名，不弹出钱包确认；点击签名按钮前请核对订单。全部签名完成前不会提交任何订单。'
                        : '钱包将逐腿显示订单签名，可能弹窗一到两次。全部签名完成前不会提交任何订单。'}</p>
                      <Button type="button" variant="destructive" disabled={busy || stalePreview} onClick={() => browser.mutate()}>
                        {browser.isPending ? '正在签名或提交…'
                          : report.status === 'prepared' ? '重新生成并签名'
                          : signerMode === 'local' ? '本地签名并下单' : '调起钱包签名并下单'}
                      </Button>
                    </>
                  )}
                </div>
              )}
              {progress && <p className="text-sm text-sky-600" role="status">{progress}</p>}
              {walletError && <p className="text-sm text-rose-600" role="alert">{walletError}</p>}
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
