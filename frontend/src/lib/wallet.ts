import type { TypedDataDefinition } from 'viem';
import { privateKeyToAccount, type PrivateKeyAccount } from 'viem/accounts';

export type LocalWallet = PrivateKeyAccount;

export function createLocalWallet(privateKey: string): LocalWallet {
  const normalized = privateKey.trim();
  if (!/^0x[0-9a-fA-F]{64}$/.test(normalized) || /^0x0{64}$/.test(normalized)) {
    throw new Error('API Wallet 私钥必须是 0x 开头的 64 位非零十六进制数据');
  }
  return privateKeyToAccount(normalized as `0x${string}`);
}

export async function signLocalTypedData(wallet: LocalWallet, typedData: Record<string, unknown>): Promise<string> {
  return wallet.signTypedData(typedData as TypedDataDefinition);
}

export interface Eip1193Provider {
  request<T = unknown>(args: { method: string; params?: unknown[] }): Promise<T>;
}

export function walletErrorText(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === 'object' && error !== null && 'message' in error) {
    const message = (error as { message?: unknown }).message;
    if (typeof message === 'string' && message.trim()) return message;
  }
  return '请求失败';
}

declare global {
  interface Window {
    ethereum?: Eip1193Provider;
  }
}

function provider(): Eip1193Provider {
  if (!window.ethereum) {
    throw new Error('未检测到浏览器钱包。请安装支持 eth_signTypedData_v4 的钱包，并在其中导入 API Wallet。');
  }
  return window.ethereum;
}

function address(value: unknown): string {
  if (typeof value !== 'string' || !/^0x[0-9a-fA-F]{40}$/.test(value)) {
    throw new Error('钱包返回了非法账户地址');
  }
  return value.toLowerCase();
}

export async function requestWalletAccount(): Promise<string> {
  const accounts = await provider().request<unknown>({ method: 'eth_requestAccounts' });
  if (!Array.isArray(accounts) || accounts.length === 0) {
    throw new Error('钱包未授权任何账户');
  }
  return address(accounts[0]);
}

export async function currentWalletAccount(): Promise<string> {
  const accounts = await provider().request<unknown>({ method: 'eth_accounts' });
  if (!Array.isArray(accounts) || accounts.length === 0) {
    throw new Error('钱包未授权任何账户');
  }
  return address(accounts[0]);
}

export async function signTypedData(account: string, typedData: Record<string, unknown>): Promise<string> {
  const current = await currentWalletAccount();
  if (current !== account.toLowerCase()) {
    throw new Error(`钱包当前账户已切换为 ${current}，请切回所选 API Wallet ${account}`);
  }
  const signature = await provider().request<unknown>({
    method: 'eth_signTypedData_v4',
    params: [account, JSON.stringify(typedData)],
  });
  if (typeof signature !== 'string' || !/^0x[0-9a-fA-F]{130}$/.test(signature)) {
    throw new Error('钱包返回了非法签名');
  }
  return signature;
}
