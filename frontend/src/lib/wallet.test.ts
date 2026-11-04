import assert from 'node:assert/strict';
import test from 'node:test';

import { recoverTypedDataAddress } from 'viem';
import {
  createLocalWallet,
  requestWalletAccount,
  signLocalTypedData,
  signTypedData,
  walletErrorText,
  type Eip1193Provider,
} from './wallet';

const ACCOUNT = '0x1111111111111111111111111111111111111111';
const OTHER = '0x2222222222222222222222222222222222222222';
const SIGNATURE = `0x${'1'.repeat(130)}`;
const LOCAL_KEY = `0x${'0'.repeat(63)}1`;
const LOCAL_ADDRESS = '0x7e5f4552091a69125d5dfcb7b8c2659029395bdf';
const HYPERLIQUID_TYPED_DATA = {
  domain: {
    name: 'Exchange',
    version: '1',
    chainId: '0x539',
    verifyingContract: '0x0000000000000000000000000000000000000000',
  },
  types: {
    Agent: [
      { name: 'source', type: 'string' },
      { name: 'connectionId', type: 'bytes32' },
    ],
    EIP712Domain: [
      { name: 'name', type: 'string' },
      { name: 'version', type: 'string' },
      { name: 'chainId', type: 'uint256' },
      { name: 'verifyingContract', type: 'address' },
    ],
  },
  primaryType: 'Agent',
  message: { source: 'a', connectionId: `0x${'11'.repeat(32)}` },
};
const runtime = globalThis as unknown as { window: { ethereum?: Eip1193Provider } };

function install(accounts: string[]) {
  const calls: { method: string; params?: unknown[] }[] = [];
  runtime.window = {
    ethereum: {
      request: async <T = unknown>({ method, params }: { method: string; params?: unknown[] }): Promise<T> => {
        calls.push({ method, params });
        if (method === 'eth_requestAccounts' || method === 'eth_accounts') return accounts as T;
        if (method === 'eth_signTypedData_v4') return SIGNATURE as T;
        throw new Error(`unexpected method ${method}`);
      },
    },
  };
  return calls;
}

test('连接钱包返回当前账户并统一小写', async () => {
  install([ACCOUNT.toUpperCase().replace('0X', '0x')]);
  assert.equal(await requestWalletAccount(), ACCOUNT);
});

test('API Wallet 使用 eth_signTypedData_v4 签署后端 typed data', async () => {
  const calls = install([ACCOUNT]);
  const typedData = { primaryType: 'HyperliquidTransaction:Agent', message: { source: 'a' } };
  assert.equal(await signTypedData(ACCOUNT, typedData), SIGNATURE);
  const signing = calls.find((call) => call.method === 'eth_signTypedData_v4');
  assert.deepEqual(signing?.params, [ACCOUNT, JSON.stringify(typedData)]);
});

test('签名前账户被切换时拒绝继续', async () => {
  const calls = install([OTHER]);
  await assert.rejects(signTypedData(ACCOUNT, { message: {} }), /钱包当前账户已切换/);
  assert.equal(calls.some((call) => call.method === 'eth_signTypedData_v4'), false);
});

test('没有浏览器钱包时给出明确错误', async () => {
  runtime.window = {};
  await assert.rejects(requestWalletAccount(), /未检测到浏览器钱包/);
});

test('保留 EIP-1193 普通错误对象中的钱包原因', () => {
  assert.equal(walletErrorText({ code: 4001, message: 'User rejected the request.' }), 'User rejected the request.');
  assert.equal(walletErrorText({ code: 4001 }), '请求失败');
});

test('本地 API Wallet 从私钥派生地址且拒绝零私钥', () => {
  assert.equal(createLocalWallet(LOCAL_KEY).address.toLowerCase(), LOCAL_ADDRESS);
  assert.throws(() => createLocalWallet(`0x${'0'.repeat(64)}`), /非零/);
  assert.throws(() => createLocalWallet('invalid'), /64 位/);
});

test('本地签名兼容 Hyperliquid 的 1337 L1 Typed Data', async () => {
  const wallet = createLocalWallet(LOCAL_KEY);
  const signature = await signLocalTypedData(wallet, HYPERLIQUID_TYPED_DATA);
  assert.match(signature, /^0x[0-9a-f]{130}$/);
  const recovered = await recoverTypedDataAddress({
    ...HYPERLIQUID_TYPED_DATA,
    domain: {
      ...HYPERLIQUID_TYPED_DATA.domain,
      chainId: 1337,
      verifyingContract: HYPERLIQUID_TYPED_DATA.domain.verifyingContract as `0x${string}`,
    },
    types: {
      Agent: [
        { name: 'source', type: 'string' },
        { name: 'connectionId', type: 'bytes32' },
      ],
    },
    primaryType: 'Agent',
    message: {
      ...HYPERLIQUID_TYPED_DATA.message,
      connectionId: HYPERLIQUID_TYPED_DATA.message.connectionId as `0x${string}`,
    },
    signature: signature as `0x${string}`,
  });
  assert.equal(recovered.toLowerCase(), LOCAL_ADDRESS);
});
