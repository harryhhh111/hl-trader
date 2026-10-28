import assert from 'node:assert/strict';
import test from 'node:test';

import { plainDiagnostic, positionLegsLabel } from './format.ts';
import type { HlV2Position } from '../../lib/types/hyperliquid';

test('统一账户权益口径 → 白话', () => {
  assert.equal(
    plainDiagnostic('账户模式 "unifiedAccount" 的权益口径不适用单 DEX 余额（仅展示经验证的持仓）'),
    '该账户为统一账户模式，总权益横跨多个交易区，无法按单一交易区展示；此处仅列出已核验的持仓明细',
  );
});

test('未配置解锁锚点 → 白话', () => {
  assert.equal(plainDiagnostic('未配置解锁锚点'), '尚未配置代币解锁的信息来源，无法展示解锁计划');
});

test('解锁锚点全部无效 → 白话', () => {
  assert.equal(
    plainDiagnostic('解锁锚点全部无效：日期无法解析、来源链接非 HTTP(S) 或核实日期晚于当天'),
    '解锁信息来源配置有误：日期无法识别、链接不是 http(s)、或核实日期晚于今天',
  );
});

test('账户未配置系列 → 白话', () => {
  assert.equal(plainDiagnostic('账户未配置，仓位数据不可用'), '尚未配置账户地址，仓位数据不可用');
  assert.equal(plainDiagnostic('账户未配置，实际资金费收付不可用'), '尚未配置账户地址，实际资金费收付不可用');
});

test('参数化：带币种与原因', () => {
  assert.equal(plainDiagnostic('xyz:NBIS 已下架，实时行情与估算不可用'), 'xyz:NBIS 已下架，实时价格与估算不可用');
  assert.equal(plainDiagnostic('仓位查询失败（HTTP 500）'), '查询持仓失败：HTTP 500');
  assert.equal(plainDiagnostic('HYPE 日线查询失败（timeout）'), 'HYPE 日线查询失败：timeout');
  assert.equal(plainDiagnostic('3 条资金费记录时间无法解析，未展示'), '3 条资金费记录的时间无法识别，已跳过');
});

test('30MA 与日线更新 → 白话', () => {
  assert.equal(plainDiagnostic('连续有效共同收盘日不足 30 个，30MA 不可用'), '有效交易日不足 30 天，30 日均线暂不可用');
  assert.equal(
    plainDiagnostic('日线未更新，最近共同收盘日 2026-10-17，需要 2026-10-18 00:00 UTC'),
    '日线数据未更新：最近共同交易日是 2026-10-17，需要 2026-10-18 00:00（UTC）之后的数据',
  );
});

test('公告 API 源失败 → 白话', () => {
  assert.equal(plainDiagnostic('api 频道公告查询失败（HTTP 302）'), '平台公告的 API 源查询失败（HTTP 302），仅展示 Telegram 源');
});

test('未知诊断原文返回', () => {
  assert.equal(plainDiagnostic('一条全新的诊断'), '一条全新的诊断');
});

function leg(coin: string, side: HlV2Position['side']): HlV2Position {
  return {
    coin, side, size: '1', entry_px: '1', mark_px: '1', notional: '1',
    unrealized_pnl: '0', margin_mode: 'cross', margin_used: '1', liquidation_px: null,
  };
}

test('方向摘要直接展示两腿实际多空', () => {
  assert.equal(
    positionLegsLabel(['HYPE', 'BTC'], [leg('HYPE', 'long'), leg('BTC', 'short')], 'opposite'),
    'HYPE 多 / BTC 空',
  );
  assert.equal(
    positionLegsLabel(['HYPE', 'BTC'], [leg('HYPE', 'long')], 'single_leg'),
    'HYPE 多 / BTC 无仓',
  );
  assert.equal(
    positionLegsLabel(['HYPE', 'BTC'], [leg('HYPE', 'long'), leg('BTC', 'long')], 'same_direction'),
    'HYPE 多 / BTC 多',
  );
  assert.equal(positionLegsLabel(['HYPE', 'BTC'], [], 'flat'), '空仓');
  assert.equal(positionLegsLabel(['HYPE', 'BTC'], null, 'unknown'), '未知');
});
