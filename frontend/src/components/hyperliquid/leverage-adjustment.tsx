import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Gauge, WalletCards } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { ApiError, hyperliquidPairsApi } from '@/lib/api/client';
import type { HlLeverageReport, HlPairDescriptor } from '@/lib/types/hyperliquid';
import {
  createLocalWallet, requestWalletAccount, signLocalTypedData, signTypedData, walletErrorText,
  type LocalWallet,
} from '@/lib/wallet';
import { accountShort } from './format';
import { refreshHyperliquid } from './queries';

const errorText = (error: unknown) => error instanceof ApiError ? error.detail || error.message : walletErrorText(error);

export function LeverageAdjustment({ descriptor, configRevision, coin, currentLeverage = null, currentMode = 'cross', maxLeverage = null }: {
  descriptor: HlPairDescriptor;
  configRevision: string;
  coin: string;
  currentLeverage?: number | null;
  currentMode?: 'cross' | 'isolated' | null;
  maxLeverage?: number | null;
}) {
  const qc = useQueryClient();
  const account = descriptor.account_address;
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<'managed' | 'local' | 'injected'>('managed');
  const [leverage, setLeverage] = useState(currentLeverage == null ? '' : String(currentLeverage));
  const [marginMode, setMarginMode] = useState<'cross' | 'isolated'>(currentMode ?? 'cross');
  const [localWallet, setLocalWallet] = useState<LocalWallet | null>(null);
  const [privateKey, setPrivateKey] = useState('');
  const [injectedSigner, setInjectedSigner] = useState<string | null>(null);
  const [report, setReport] = useState<HlLeverageReport | null>(null);
  const [requestId, setRequestId] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [error, setError] = useState('');
  const [progress, setProgress] = useState('');
  const [walletBusy, setWalletBusy] = useState(false);
  const wallet = useQuery({
    queryKey: ['hl-managed-wallet', descriptor.id, account],
    queryFn: () => hyperliquidPairsApi.managedWallet(descriptor.id, account ?? ''),
    enabled: open && account != null,
    retry: false,
    refetchOnWindowFocus: false,
  });
  const signer = mode === 'local' ? localWallet?.address.toLowerCase() ?? null : mode === 'injected' ? injectedSigner : null;
  const numericLeverage = Number(leverage);
  const valid = Number.isInteger(numericLeverage) && numericLeverage >= 1 && (maxLeverage == null || numericLeverage <= maxLeverage);
  const body = {
    account_address: account ?? '', config_revision: configRevision, coin,
    leverage: numericLeverage, margin_mode: marginMode,
  };
  const targetText = `${coin} ${marginMode} ${numericLeverage}x`;
  const clearLocal = () => { setLocalWallet(null); setPrivateKey(''); };
  const finished = async (result: HlLeverageReport) => {
    setReport(result);
    clearLocal();
    if (result.managed_status !== 'running') await refreshHyperliquid(qc, descriptor.id);
  };
  const browser = useMutation({
    mutationFn: async () => {
      if (!signer) throw new Error(mode === 'local' ? '请先导入 API Wallet' : '请先连接 API Wallet');
      setError('');
      setProgress('正在读取最新杠杆并生成签名…');
      const prepared = await hyperliquidPairsApi.prepareLeverage(descriptor.id, { ...body, expected_signer: signer });
      setReport(prepared);
      if (!prepared.action_id || !prepared.typed_data) throw new Error('服务端未返回杠杆签名数据');
      setProgress(mode === 'local' ? '正在本地签名…' : '请在钱包中确认杠杆签名…');
      const signature = mode === 'local'
        ? await signLocalTypedData(localWallet!, prepared.typed_data)
        : await signTypedData(signer, prepared.typed_data);
      setProgress('正在提交并核对实际杠杆…');
      return hyperliquidPairsApi.submitLeverage(descriptor.id, {
        ...body, expected_signer: signer, action_id: prepared.action_id, signature,
      });
    },
    onSuccess: finished,
    onError: (cause) => setError(errorText(cause)),
    onSettled: () => setProgress(''),
  });
  const managed = useMutation({
    mutationFn: () => hyperliquidPairsApi.executeManagedLeverage(descriptor.id, {
      ...body, request_id: requestId, wallet_version: wallet.data?.wallet_version ?? 0, confirmation: confirmation.trim(),
    }),
    onSuccess: finished,
    onError: (cause) => setError(errorText(cause)),
    onSettled: () => setProgress(''),
  });
  useEffect(() => {
    if (report?.managed_status !== 'running' || !report.request_id) return;
    let active = true;
    const timer = window.setInterval(() => {
      void hyperliquidPairsApi.managedLeverage(descriptor.id, report.request_id!).then(async (result) => {
        if (!active) return;
        setReport(result);
        if (result.managed_status !== 'running') await refreshHyperliquid(qc, descriptor.id);
      }).catch((cause) => { if (active) setError(errorText(cause)); });
    }, 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, [descriptor.id, qc, report?.managed_status, report?.request_id]);
  useEffect(() => {
    setOpen(false);
    setLeverage(currentLeverage == null ? '' : String(currentLeverage));
    setMarginMode(currentMode ?? 'cross');
    setReport(null);
    setError('');
    setProgress('');
    clearLocal();
  }, [descriptor.id, account, coin, currentLeverage, currentMode]);
  const connect = async () => {
    setWalletBusy(true);
    setError('');
    try {
      const selected = await requestWalletAccount();
      if (selected === account?.toLowerCase()) throw new Error('不能使用原交易账户签名，请切换 API Wallet');
      setInjectedSigner(selected);
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setWalletBusy(false);
    }
  };
  const loadLocal = () => {
    setError('');
    try {
      const loaded = createLocalWallet(privateKey);
      setPrivateKey('');
      if (loaded.address.toLowerCase() === account?.toLowerCase()) throw new Error('禁止导入原交易账户私钥');
      setLocalWallet(loaded);
    } catch (cause) {
      setLocalWallet(null);
      setError(errorText(cause));
    }
  };
  const busy = browser.isPending || managed.isPending || walletBusy || report?.managed_status === 'running';

  return (
    <>
      <Button type="button" size="sm" variant="outline" aria-label={`调整 ${coin} 杠杆`} onClick={() => {
        setRequestId(crypto.randomUUID());
        setReport(null);
        setConfirmation('');
        setError('');
        setOpen(true);
      }}><Gauge className="mr-1 h-3.5 w-3.5" />调杠杆</Button>
      <Dialog open={open} onOpenChange={(next) => { if (!busy || next) setOpen(next); if (!next) clearLocal(); }} title={`调整 ${coin} 杠杆`} className="max-w-xl">
        <div className="space-y-4 text-sm">
          <p className="text-muted-foreground">逐合约设置保证金模式和整数杠杆。不会改变另一腿，也不会自动下单；提交后重新读取交易所状态确认。</p>
          <div className="grid grid-cols-2 gap-2 rounded-md bg-muted/40 p-3">
            <span>当前杠杆 <span className="font-mono">{currentLeverage == null ? '—' : `${currentLeverage}x`}</span></span>
            <span>市场上限 <span className="font-mono">{maxLeverage == null ? '以交易所实时校验为准' : `${maxLeverage}x`}</span></span>
          </div>
          <div className="flex flex-wrap gap-2" aria-label="签名方式">
            {(['managed', 'local', 'injected'] as const).map((item) => <Button key={item} type="button" size="sm" variant={mode === item ? 'default' : 'outline'} disabled={busy} onClick={() => { setMode(item); setReport(null); setError(''); }}>
              {item === 'managed' ? '托管 API Wallet' : item === 'local' ? '浏览器本地私钥' : '浏览器扩展钱包'}
            </Button>)}
          </div>
          <form className="grid grid-cols-[1fr_1fr] gap-3" onSubmit={(event) => event.preventDefault()}>
            <label className="grid gap-1">目标杠杆
              <input aria-label="目标杠杆" className="rounded-md border bg-background px-3 py-2 font-mono" type="number" min="1" max={maxLeverage ?? undefined} step="1" value={leverage} disabled={busy} onChange={(event) => { setLeverage(event.target.value); setReport(null); }} required />
            </label>
            <label className="grid gap-1">保证金模式
              <select aria-label="保证金模式" className="rounded-md border bg-background px-3 py-2" value={marginMode} disabled={busy} onChange={(event) => { setMarginMode(event.target.value as 'cross' | 'isolated'); setReport(null); }}>
                <option value="cross">全仓 cross</option><option value="isolated">逐仓 isolated</option>
              </select>
            </label>
          </form>
          {!valid && <p role="alert" className="text-rose-600">请输入正整数{maxLeverage == null ? '' : `，且不能超过 ${maxLeverage}x`}。</p>}
          {mode === 'managed' && wallet.isSuccess && !wallet.data.configured && <p className="text-amber-600">请先在账户配置中添加托管 API Wallet。</p>}
          {mode === 'managed' && valid && <div className="space-y-2">
            <label className="grid gap-1">输入 <span className="font-mono">{targetText}</span> 确认
              <input aria-label="确认杠杆调整" className="rounded-md border bg-background px-3 py-2 font-mono" value={confirmation} disabled={busy} onChange={(event) => setConfirmation(event.target.value)} />
            </label>
            <Button type="button" variant="destructive" disabled={busy || confirmation.trim() !== targetText || !wallet.data?.configured} onClick={() => { setProgress('正在使用托管 API Wallet 调整并核对…'); managed.mutate(); }}>确认调整杠杆</Button>
          </div>}
          {mode === 'local' && valid && <div className="space-y-2">
            <form className="flex items-end gap-2" onSubmit={(event) => { event.preventDefault(); loadLocal(); }}>
              <label className="grid flex-1 gap-1">API Wallet 私钥
                <input aria-label="API Wallet 私钥" className="rounded-md border bg-background px-3 py-2 font-mono" type="password" autoComplete="off" value={privateKey} disabled={busy} onChange={(event) => setPrivateKey(event.target.value)} required />
              </label>
              <Button type="submit" variant="outline" disabled={busy || !privateKey.trim()}>加载</Button>
            </form>
            <p className="text-xs text-muted-foreground">私钥仅保存在页面内存，关闭弹窗即清除，不上传服务器。</p>
          </div>}
          {mode === 'injected' && valid && <Button type="button" variant="outline" disabled={busy} onClick={() => void connect()}><WalletCards className="mr-2 h-4 w-4" />{injectedSigner ? '更换 API Wallet' : '连接 API Wallet'}</Button>}
          {signer && <p>签名账户 <span className="font-mono">{accountShort(signer)}</span></p>}
          {mode !== 'managed' && signer && valid && <Button type="button" variant="destructive" disabled={busy} onClick={() => browser.mutate()}>签名并调整杠杆</Button>}
          {progress && <p role="status" className="text-sky-600">{progress}</p>}
          {error && <p role="alert" className="text-rose-600">{error}</p>}
          {report && <div className="rounded-md border p-3">
            <p className="font-medium">{report.managed_status === 'running' || report.status === 'executing' ? '正在调整' : report.status === 'succeeded' ? '杠杆已调整并核对' : report.status === 'rejected' ? '交易所拒绝调整' : report.status === 'unknown' ? '结果未知，禁止直接重试' : '等待签名'}</p>
            {report.result && <p className="mt-1 text-muted-foreground">{report.result}</p>}
            {report.action_id && <p className="mt-1 break-all font-mono text-xs text-muted-foreground">{report.action_id}</p>}
            {report.request_id && <p className="mt-1 break-all font-mono text-xs text-muted-foreground">request_id {report.request_id}</p>}
          </div>}
        </div>
      </Dialog>
    </>
  );
}
