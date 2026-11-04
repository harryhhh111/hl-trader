import { useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Plus, WalletCards } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Dialog } from '@/components/ui/dialog';
import { ApiError, hyperliquidPairsApi } from '@/lib/api/client';
import { accountShort } from './format';
import { ManagedWalletSettings } from './managed-wallet-settings';

const errorText = (error: unknown) => error instanceof ApiError ? error.detail || error.message : error instanceof Error ? error.message : '请求失败';

export function AccountSettings({ pairId, configRevision, network }: {
  pairId: string;
  configRevision: string;
  network: 'mainnet' | 'testnet';
}) {
  const qc = useQueryClient();
  const [addOpen, setAddOpen] = useState(false);
  const [label, setLabel] = useState('');
  const [address, setAddress] = useState('');
  const accountsQuery = useQuery({
    queryKey: ['hl-accounts', pairId],
    queryFn: () => hyperliquidPairsApi.accounts(pairId),
    staleTime: 30_000,
    retry: false,
    refetchOnWindowFocus: false,
  });
  const accounts = accountsQuery.data?.accounts ?? [];
  const active = accounts.find((account) => account.is_active);

  const refreshDashboard = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['hl-accounts', pairId] }),
      qc.invalidateQueries({ queryKey: ['hl-directory'] }),
      qc.invalidateQueries({ queryKey: ['hl-pair'] }),
    ]);
  };
  const activate = useMutation({
    mutationFn: (accountId: number) => hyperliquidPairsApi.activateAccount(pairId, accountId),
    onSuccess: refreshDashboard,
  });
  const add = useMutation({
    mutationFn: () => hyperliquidPairsApi.addAccount(pairId, label.trim(), address.trim()),
    onSuccess: async () => {
      setLabel('');
      setAddress('');
      setAddOpen(false);
      await refreshDashboard();
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!add.isPending) add.mutate();
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0 pb-3">
        <div className="space-y-1.5">
          <CardTitle className="flex items-center gap-2 text-base"><WalletCards className="h-4 w-4" />账户配置</CardTitle>
          <CardDescription>按 pair 保存多个公开地址；切换后重新查询该账户的仓位和资金费，不需要私钥。</CardDescription>
        </div>
        <Button type="button" size="sm" variant="outline" onClick={() => {
          add.reset();
          setAddOpen(true);
        }}>
          <Plus className="mr-2 h-4 w-4" />添加账户
        </Button>
      </CardHeader>
      <CardContent className="space-y-4">
        {accountsQuery.isPending && <p className="text-sm text-muted-foreground">正在读取账户…</p>}
        {accountsQuery.isError && <p className="text-sm text-rose-600" role="alert">账户读取失败：{errorText(accountsQuery.error)}</p>}
        {accountsQuery.isSuccess && (
          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor={`account-${pairId}`} className="text-sm font-medium">当前账户</label>
            <select
              id={`account-${pairId}`}
              aria-label="切换账户"
              className="min-w-56 rounded-md border bg-background px-3 py-2 text-sm"
              value={active?.id == null ? 'config' : String(active.id)}
              disabled={accounts.length <= 1 || activate.isPending}
              onChange={(event) => {
                if (event.target.value !== 'config') activate.mutate(Number(event.target.value));
              }}
            >
              {accounts.length === 0 && <option value="config">未配置账户（仅公开行情）</option>}
              {accounts.map((account) => (
                <option key={account.id ?? 'config'} value={account.id ?? 'config'}>
                  {account.label}（{accountShort(account.address)}）
                </option>
              ))}
            </select>
            {activate.isPending && <span className="text-sm text-muted-foreground">正在切换…</span>}
          </div>
        )}
        {activate.isError && <p className="text-sm text-rose-600" role="alert">切换失败：{errorText(activate.error)}</p>}
        {active && <ManagedWalletSettings pairId={pairId} configRevision={configRevision} account={active.address} network={network} />}
      </CardContent>
      <Dialog
        open={addOpen}
        onOpenChange={(nextOpen) => {
          if (!add.isPending || nextOpen) setAddOpen(nextOpen);
        }}
        title="添加账户"
        description="仅需公开地址，不需要私钥。添加成功后自动切换到该账户。"
        className="max-w-lg"
      >
        <form className="space-y-4" onSubmit={submit}>
          <label className="grid gap-1 text-sm">
            <span>账户名称</span>
            <input
              aria-label="账户名称"
              className="w-full rounded-md border bg-background px-3 py-2"
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              maxLength={80}
              placeholder="例如：主账户"
              disabled={add.isPending}
              required
            />
          </label>
          <label className="grid gap-1 text-sm">
            <span>公开地址</span>
            <input
              aria-label="账户地址"
              className="w-full rounded-md border bg-background px-3 py-2 font-mono text-xs"
              value={address}
              onChange={(event) => setAddress(event.target.value)}
              pattern="0x[0-9a-fA-F]{40}"
              maxLength={42}
              placeholder="0x..."
              disabled={add.isPending}
              required
            />
          </label>
          {add.isError && <p className="text-sm text-rose-600" role="alert">添加失败：{errorText(add.error)}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" disabled={add.isPending} onClick={() => setAddOpen(false)}>取消</Button>
            <Button type="submit" disabled={add.isPending || !label.trim() || !address.trim()}>
              <Plus className="mr-2 h-4 w-4" />{add.isPending ? '添加中…' : '添加并使用'}
            </Button>
          </div>
        </form>
      </Dialog>
    </Card>
  );
}
