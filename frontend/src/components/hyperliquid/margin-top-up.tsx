import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { WalletCards } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { ApiError, hyperliquidPairsApi } from '@/lib/api/client';
import type { HlMarginReport, HlPairDescriptor, HlV2Position } from '@/lib/types/hyperliquid';
import {
  createLocalWallet, requestWalletAccount, signLocalTypedData, signTypedData, walletErrorText,
  type LocalWallet,
} from '@/lib/wallet';
import { accountShort } from './format';
import { refreshHyperliquid } from './queries';

const errorText = (error: unknown) => error instanceof ApiError ? error.detail || error.message : walletErrorText(error);

export function marginTopUpEstimate(position: HlV2Position): { ratioPct: number; amount: number } | null {
  if (position.notional == null || position.margin_used == null || !position.notional.trim() || !position.margin_used.trim()) return null;
  const notional = Number(position.notional);
  const margin = Number(position.margin_used);
  if (!Number.isFinite(notional) || !Number.isFinite(margin) || notional <= 0 || margin < 0) return null;
  return { ratioPct: margin / notional * 100, amount: Math.max(0, notional * 0.8 - margin) };
}

export function MarginTopUp({ descriptor, configRevision, position, onChanged }: {
  descriptor: HlPairDescriptor;
  configRevision: string;
  position: HlV2Position;
  onChanged?: () => void;
}) {
  const qc = useQueryClient();
  const account = descriptor.account_address;
  const estimate = marginTopUpEstimate(position);
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<'managed' | 'local' | 'injected'>('managed');
  const [localWallet, setLocalWallet] = useState<LocalWallet | null>(null);
  const [privateKey, setPrivateKey] = useState('');
  const [injectedSigner, setInjectedSigner] = useState<string | null>(null);
  const [report, setReport] = useState<HlMarginReport | null>(null);
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
  const body = { account_address: account ?? '', config_revision: configRevision, coin: position.coin };
  const targetText = `${position.coin} 补足逐仓保证金至80%`;
  const clearLocal = () => { setLocalWallet(null); setPrivateKey(''); };
  const finished = async (result: HlMarginReport) => {
    setReport(result);
    clearLocal();
    if (result.status === 'succeeded') {
      onChanged?.();
      setOpen(false);
    }
    if (result.managed_status !== 'running') await refreshHyperliquid(qc, descriptor.id);
  };
  const browser = useMutation({
    mutationFn: async () => {
      if (!signer) throw new Error(mode === 'local' ? '请先导入 API Wallet' : '请先连接 API Wallet');
      setError('');
      setProgress('正在读取最新逐仓仓位并计算追加金额…');
      const prepared = await hyperliquidPairsApi.prepareMarginTopUp(descriptor.id, { ...body, expected_signer: signer });
      setReport(prepared);
      if (!prepared.action_id || !prepared.typed_data) throw new Error('服务端未返回保证金签名数据');
      setProgress(mode === 'local' ? '正在本地签名…' : '请在钱包中确认追加保证金签名…');
      const signature = mode === 'local'
        ? await signLocalTypedData(localWallet!, prepared.typed_data)
        : await signTypedData(signer, prepared.typed_data);
      setProgress('正在提交并核对实际保证金…');
      return hyperliquidPairsApi.submitMarginTopUp(descriptor.id, {
        ...body, expected_signer: signer, action_id: prepared.action_id, signature,
      });
    },
    onSuccess: finished,
    onError: (cause) => setError(errorText(cause)),
    onSettled: () => setProgress(''),
  });
  const managed = useMutation({
    mutationFn: () => hyperliquidPairsApi.executeManagedMarginTopUp(descriptor.id, {
      ...body, request_id: requestId, wallet_version: wallet.data?.wallet_version ?? 0,
      confirmation: confirmation.trim(),
    }),
    onSuccess: finished,
    onError: (cause) => setError(errorText(cause)),
    onSettled: () => setProgress(''),
  });
  useEffect(() => {
    if (report?.managed_status !== 'running' || !report.request_id) return;
    let active = true;
    const timer = window.setInterval(() => {
      void hyperliquidPairsApi.managedMarginTopUp(descriptor.id, report.request_id!).then(async (result) => {
        if (!active) return;
        if (result.managed_status !== 'running') await finished(result);
        else setReport(result);
      }).catch((cause) => { if (active) setError(errorText(cause)); });
    }, 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, [descriptor.id, qc, report?.managed_status, report?.request_id]);
  useEffect(() => {
    setOpen(false);
    setReport(null);
    setError('');
    setProgress('');
    clearLocal();
  }, [descriptor.id, account, position.coin, configRevision]);
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
  const unresolved = report?.status === 'unknown' || report?.status === 'executing' || report?.managed_status === 'unknown';

  if (position.margin_mode !== 'isolated' || estimate == null || estimate.amount <= 0) return null;
  return (
    <>
      <Button type="button" size="sm" variant="outline" aria-label={`补足 ${position.coin} 逐仓保证金`} onClick={() => {
        setRequestId(crypto.randomUUID());
        if (!unresolved) setReport(null);
        setConfirmation('');
        setError('');
        setOpen(true);
      }}>补足至 80%</Button>
      <Dialog open={open} onOpenChange={(next) => { if (!busy || next) setOpen(next); if (!next) clearLocal(); }} title={`补足 ${position.coin} 逐仓保证金`} className="max-w-xl">
        <div className="space-y-4 text-sm">
          <p className="text-muted-foreground">只追加保证金，不改变仓位数量。Rust 按最新名义价值计算差额，签名前后发生变化会停止提交。</p>
          <div className="grid grid-cols-2 gap-2 rounded-md bg-muted/40 p-3">
            <span>当前保证金 / 名义 <span className="font-mono">{estimate.ratioPct.toFixed(2)}%</span></span>
            <span>预计追加 <span className="font-mono">{estimate.amount.toFixed(6)} USD</span></span>
            <span>目标 <span className="font-mono">≥ 名义价值的 80%</span></span>
            <span>实际金额 <span className="font-mono">{report?.margin_delta ? `${report.margin_delta} USD` : '签名前实时计算'}</span></span>
          </div>
          <div className="flex flex-wrap gap-2" aria-label="签名方式">
            {(['managed', 'local', 'injected'] as const).map((item) => <Button key={item} type="button" size="sm" variant={mode === item ? 'default' : 'outline'} disabled={busy || unresolved} onClick={() => { setMode(item); setReport(null); setError(''); }}>
              {item === 'managed' ? '托管 API Wallet' : item === 'local' ? '浏览器本地私钥' : '浏览器扩展钱包'}
            </Button>)}
          </div>
          {mode === 'managed' && wallet.isSuccess && !wallet.data.configured && <p className="text-amber-600">请先在账户配置中添加托管 API Wallet。</p>}
          {mode === 'managed' && <div className="space-y-2">
            <label className="grid gap-1">输入 <span className="font-mono">{targetText}</span> 确认
              <input aria-label="确认补足保证金" className="rounded-md border bg-background px-3 py-2 font-mono" value={confirmation} disabled={busy} onChange={(event) => setConfirmation(event.target.value)} />
            </label>
            <Button type="button" disabled={busy || unresolved || confirmation.trim() !== targetText || !wallet.data?.configured} onClick={() => { setProgress('正在使用托管 API Wallet 追加并核对…'); managed.mutate(); }}>确认补足保证金</Button>
          </div>}
          {mode === 'local' && <div className="space-y-2">
            <form className="flex items-end gap-2" onSubmit={(event) => { event.preventDefault(); loadLocal(); }}>
              <label className="grid flex-1 gap-1">API Wallet 私钥
                <input aria-label="API Wallet 私钥" className="rounded-md border bg-background px-3 py-2 font-mono" type="password" autoComplete="off" value={privateKey} disabled={busy} onChange={(event) => setPrivateKey(event.target.value)} required />
              </label>
              <Button type="submit" variant="outline" disabled={busy || !privateKey.trim()}>加载</Button>
            </form>
            <p className="text-xs text-muted-foreground">私钥仅保存在页面内存，关闭弹窗即清除，不上传服务器。</p>
          </div>}
          {mode === 'injected' && <Button type="button" variant="outline" disabled={busy} onClick={() => void connect()}><WalletCards className="mr-2 h-4 w-4" />{injectedSigner ? '更换 API Wallet' : '连接 API Wallet'}</Button>}
          {signer && <p>签名账户 <span className="font-mono">{accountShort(signer)}</span></p>}
          {mode !== 'managed' && signer && <Button type="button" disabled={busy || unresolved} onClick={() => browser.mutate()}>签名并补足保证金</Button>}
          {progress && <p role="status" className="text-sky-600">{progress}</p>}
          {error && <p role="alert" className="text-rose-600">{error}</p>}
          {report && <div className="rounded-md border p-3">
            <p className="font-medium">{report.managed_status === 'running' || report.status === 'executing' ? '正在追加' : report.managed_status === 'failed' ? '保证金未追加' : report.status === 'succeeded' ? '保证金已追加并核对' : report.status === 'rejected' ? '交易所拒绝追加' : unresolved ? '结果未知，禁止直接重试' : report.status === 'incomplete' || report.managed_status === 'incomplete' ? '保证金调整未完成，请核对原因' : '等待签名'}</p>
            {report.result && <p className="mt-1 text-muted-foreground">{report.result}</p>}
            {report.diagnostics?.map((message, index) => <p key={index} className="mt-1 text-amber-600">{message}</p>)}
            {report.action_id && <p className="mt-1 break-all font-mono text-xs text-muted-foreground">{report.action_id}</p>}
            {report.request_id && <p className="mt-1 break-all font-mono text-xs text-muted-foreground">request_id {report.request_id}</p>}
          </div>}
        </div>
      </Dialog>
    </>
  );
}
