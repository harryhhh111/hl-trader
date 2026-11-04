import assert from 'node:assert/strict';
import test from 'node:test';

import { encryptManagedWallet } from './managed-wallet-crypto';
import type { HlWalletChallenge } from './types/hyperliquid';

const PRIVATE_KEY = '0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef';

function bytes(value: string): Uint8Array {
  return Uint8Array.from(atob(value.replace(/-/g, '+').replace(/_/g, '/')), (character) => character.charCodeAt(0));
}

test('托管私钥使用一次性 AES-GCM 和 RSA-OAEP 加密', async () => {
  Object.assign(globalThis, { window: { isSecureContext: true } });
  const pair = await crypto.subtle.generateKey({
    name: 'RSA-OAEP', modulusLength: 3072, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256',
  }, true, ['encrypt', 'decrypt']);
  const publicPem = `-----BEGIN PUBLIC KEY-----\n${btoa(String.fromCharCode(...new Uint8Array(await crypto.subtle.exportKey('spki', pair.publicKey))))}\n-----END PUBLIC KEY-----`;
  const challenge: HlWalletChallenge = {
    challenge_id: 'test-challenge-value',
    actor_binding: 'a'.repeat(64),
    operation: 'save',
    wallet_version: 0,
    key_id: 'transport-key',
    public_key: publicPem,
    algorithm: 'RSA-OAEP-3072',
  };
  const payload = await encryptManagedWallet(PRIVATE_KEY, challenge, 'mainnet', 'nbis-crwv', `0x${'7'.repeat(40)}`);
  assert.equal(JSON.stringify(payload).includes(PRIVATE_KEY), false);
  const dataKey = await crypto.subtle.decrypt({ name: 'RSA-OAEP' }, pair.privateKey, bytes(String(payload.wrapped_key)) as BufferSource);
  const aad = bytes(String(payload.aad));
  const plaintext = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: bytes(String(payload.iv)) as BufferSource, additionalData: aad as BufferSource },
    await crypto.subtle.importKey('raw', dataKey, 'AES-GCM', false, ['decrypt']),
    bytes(String(payload.ciphertext)) as BufferSource,
  );
  assert.equal(new TextDecoder().decode(plaintext), JSON.stringify({ private_key: PRIVATE_KEY }));
  const decodedAad = JSON.parse(new TextDecoder().decode(aad));
  assert.equal(decodedAad.challenge_id, challenge.challenge_id);
  assert.equal(decodedAad.network, 'mainnet');
  assert.equal(decodedAad.wallet_version, 0);
});

test('托管私钥拒绝零密钥和非安全上下文', async () => {
  Object.assign(globalThis, { window: { isSecureContext: false } });
  await assert.rejects(encryptManagedWallet('0x' + '0'.repeat(64), {} as HlWalletChallenge, 'mainnet', 'nbis-crwv', '0x' + '7'.repeat(40)), /HTTPS/);
});
