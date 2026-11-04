import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { KeyRound, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { ApiError, hyperliquidPairsApi } from '@/lib/api/client';
import { encryptManagedWallet } from '@/lib/managed-wallet-crypto';
import { accountShort } from './format';

const errorText = (error: unknown) => error instanceof ApiError
  ? error.detail || error.message
  : error instanceof Error ? error.message : '请求失败';

export function ManagedWalletSettings({ pairId, configRevision, account, network }: {
  pairId: string;
  configRevision: string;
  account: string;
  network: 'mainnet' | 'testnet';
}) {
  const qc = useQueryClient();
  const [saveOpen, setSaveOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [privateKey, setPrivateKey] = useState('');
  const walletQuery = useQuery({
    queryKey: ['hl-managed-wallet', pairId, account],
    queryFn: () => hyperliquidPairsApi.managedWallet(pairId, account),
    retry: false,
    refetchOnWindowFocus: false,
  });
  const wallet = walletQuery.data;
  const refresh = async () => {
    setPrivateKey('');
    setSaveOpen(false);
    setDeleteOpen(false);
    await qc.invalidateQueries({ queryKey: ['hl-managed-wallet', pairId, account] });
  };
  const save = useMutation({
    mutationFn: async () => {
      const operation = wallet?.configured ? 'rotate' : 'save';
      const challenge = await hyperliquidPairsApi.walletChallenge(pairId, {
        account_address: account, config_revision: configRevision, operation,
      });
      const payload = await encryptManagedWallet(privateKey, challenge, network, pairId, account);
      return hyperliquidPairsApi.saveManagedWallet(pairId, {
        account_address: account, config_revision: configRevision, payload,
      });
    },
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: async () => {
      if (!wallet?.configured || !wallet.wallet_address) throw new Error('托管钱包未配置');
      const challenge = await hyperliquidPairsApi.walletChallenge(pairId, {
        account_address: account, config_revision: configRevision, operation: 'delete',
      });
      return hyperliquidPairsApi.deleteManagedWallet(pairId, {
        account_address: account,
        config_revision: configRevision,
        operation: 'delete',
        challenge_id: challenge.challenge_id,
        wallet_address: wallet.wallet_address,
        wallet_version: wallet.wallet_version,
      });
    },
    onSuccess: refresh,
  });
  const busy = save.isPending || remove.isPending;

  return (
    <div className="space-y-2 border-t pt-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <KeyRound className="h-4 w-4" />
        <span className="font-medium">托管 API Wallet</span>
        {walletQuery.isPending && <span className="text-muted-foreground">正在读取…</span>}
        {wallet?.configured ? (
          <span className="font-mono">{accountShort(wallet.wallet_address ?? '')} · v{wallet.wallet_version}</span>
        ) : walletQuery.isSuccess && <span className="text-muted-foreground">未配置</span>}
        {wallet && (
          <>
            <Button type="button" size="sm" variant="outline" disabled={busy || wallet.active_execution != null} onClick={() => setSaveOpen(true)}>
              {wallet.configured ? '更换' : '添加'}
            </Button>
            {wallet.configured && (
              <Button type="button" size="sm" variant="outline" disabled={busy || wallet.active_execution != null} onClick={() => setDeleteOpen(true)}>
                <Trash2 className="mr-1 h-3 w-3" />删除
              </Button>
            )}
          </>
        )}
      </div>
      <p className="text-xs text-muted-foreground">私钥加密存入服务端数据库，仅用于人工确认的托管调仓；接口不会返回私钥。</p>
      {walletQuery.isError && <p className="text-sm text-rose-600" role="alert">托管钱包读取失败：{errorText(walletQuery.error)}</p>}
      {wallet?.active_execution && <p className="text-xs text-amber-600">存在执行中或待核对请求 {wallet.active_execution.request_id}，暂不能变更钱包。</p>}

      <Dialog open={saveOpen} onOpenChange={(open) => { if (!busy || open) setSaveOpen(open); if (!open) setPrivateKey(''); }}
        title={wallet?.configured ? '更换托管 API Wallet' : '添加托管 API Wallet'}
        description="私钥在浏览器内加密后上传，服务端再次加密保存。保存前会验证 Hyperliquid 真实授权关系。">
        <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); if (!busy) save.mutate(); }}>
          <label className="grid gap-1 text-sm">
            <span>API Wallet 私钥</span>
            <input aria-label="托管 API Wallet 私钥" className="w-full rounded-md border bg-background px-3 py-2 font-mono"
              type="password" autoComplete="off" autoCapitalize="none" spellCheck={false} placeholder="0x…"
              value={privateKey} onChange={(event) => setPrivateKey(event.target.value)} disabled={busy} required />
          </label>
          <p className="text-xs text-amber-600">禁止输入原交易账户私钥、助记词或已撤销的 Agent。每个账户和交易进程使用独立 API Wallet。</p>
          {save.isError && <p className="text-sm text-rose-600" role="alert">保存失败：{errorText(save.error)}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" disabled={busy} onClick={() => setSaveOpen(false)}>取消</Button>
            <Button type="submit" disabled={busy || !privateKey.trim()}>{save.isPending ? '加密并校验中…' : '加密保存'}</Button>
          </div>
        </form>
      </Dialog>

      <Dialog open={deleteOpen} onOpenChange={(open) => { if (!busy || open) setDeleteOpen(open); }} title="删除托管 API Wallet"
        description="只删除数据库中的私钥密文，不会撤销 Hyperliquid 授权。请随后在 Hyperliquid 官方界面撤销对应 Agent。">
        <div className="space-y-4">
          <p className="text-sm">确认删除 <span className="font-mono">{accountShort(wallet?.wallet_address ?? '')}</span>（版本 v{wallet?.wallet_version}）？</p>
          {remove.isError && <p className="text-sm text-rose-600" role="alert">删除失败：{errorText(remove.error)}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" disabled={busy} onClick={() => setDeleteOpen(false)}>取消</Button>
            <Button type="button" variant="destructive" disabled={busy} onClick={() => remove.mutate()}>{remove.isPending ? '删除中…' : '确认删除'}</Button>
          </div>
        </div>
      </Dialog>
    </div>
  );
}
