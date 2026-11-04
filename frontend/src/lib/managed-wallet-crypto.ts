import type { HlWalletChallenge } from './types/hyperliquid';

function base64url(value: ArrayBuffer): string {
  const bytes = new Uint8Array(value);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}

async function importPublicKey(pem: string): Promise<CryptoKey> {
  const match = pem.match(/-----BEGIN PUBLIC KEY-----([\s\S]+?)-----END PUBLIC KEY-----/);
  if (!match) throw new Error('服务端加密公钥格式非法');
  const binary = atob(match[1].replace(/\s/g, ''));
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  return crypto.subtle.importKey(
    'spki', bytes, { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['encrypt'],
  );
}

export async function encryptManagedWallet(
  privateKey: string,
  challenge: HlWalletChallenge,
  network: 'mainnet' | 'testnet',
  pairId: string,
  accountAddress: string,
): Promise<Record<string, unknown>> {
  if (!window.isSecureContext || !crypto.subtle) {
    throw new Error('托管钱包保存需要 HTTPS 和浏览器 Web Crypto 支持');
  }
  const normalized = privateKey.trim().toLowerCase();
  if (!/^0x[0-9a-f]{64}$/.test(normalized) || normalized === `0x${'0'.repeat(64)}`) {
    throw new Error('API Wallet 私钥必须是 0x 开头的 64 位非零十六进制数据');
  }
  const ts = Math.floor(Date.now() / 1000);
  const aad = {
    purpose: 'hyperliquid-api-wallet-v1',
    challenge_id: challenge.challenge_id,
    actor_binding: challenge.actor_binding,
    key_id: challenge.key_id,
    network,
    operation: challenge.operation,
    pair_id: pairId,
    account_address: accountAddress.toLowerCase(),
    wallet_version: challenge.wallet_version,
    ts,
  };
  const encodedAad = new TextEncoder().encode(JSON.stringify(aad));
  const key = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, ['encrypt']);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv, additionalData: encodedAad, tagLength: 128 },
    key,
    new TextEncoder().encode(JSON.stringify({ private_key: normalized })),
  );
  const wrappedKey = await crypto.subtle.encrypt(
    { name: 'RSA-OAEP' },
    await importPublicKey(challenge.public_key),
    await crypto.subtle.exportKey('raw', key),
  );
  return {
    wrapped_key: base64url(wrappedKey),
    iv: base64url(iv.buffer),
    ciphertext: base64url(ciphertext),
    aad: base64url(encodedAad.buffer),
    key_id: challenge.key_id,
    challenge_id: challenge.challenge_id,
    ts,
  };
}
