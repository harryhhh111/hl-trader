import type { HlPositionState, HlV2Position } from '@/lib/types/hyperliquid';

export const dash = (value: string | null | undefined) =>
  value == null || value === '' ? '—' : value;

export const signed = (value: string | null | undefined) =>
  value == null || value === '' ? '—' : `${Number(value) >= 0 ? '+' : ''}${value}`;

/** 小数字符串转百分数（如 "0.00001" → "0.00100%"）。 */
export const pctFromFraction = (value: string | null | undefined, dp = 5) =>
  value == null || value === '' ? '—' : `${(Number(value) * 100).toFixed(dp)}%`;

/** 已是百分数数值的字符串（如 "1.50"、"10.77"）。 */
export const pctFromNumber = (value: string | null | undefined, withSign = false) =>
  value == null || value === '' ? '—' : `${withSign ? signed(value) : value}%`;

export function formatTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN', { hour12: false });
}

export const POSITION_STATE_LABEL: Record<HlPositionState, string> = {
  flat: '空仓',
  single_leg: '单腿',
  opposite: '两腿反向',
  same_direction: '同向持仓',
  unknown: '未知',
};

const POSITION_SIDE_LABEL: Record<HlV2Position['side'], string> = {
  long: '多',
  short: '空',
};

/** 按 pair 配置顺序展示两腿实际方向，例如“HYPE 多 / BTC 空”。 */
export function positionLegsLabel(
  coins: readonly string[],
  items: HlV2Position[] | null,
  state: HlPositionState,
): string {
  if (items == null) return POSITION_STATE_LABEL[state];
  if (items.length === 0) return POSITION_STATE_LABEL.flat;
  return coins.map((coin) => {
    const item = items.find((p) => p.coin === coin);
    return `${coin} ${item ? POSITION_SIDE_LABEL[item.side] : '无仓'}`;
  }).join(' / ');
}

export const accountShort = (address: string) =>
  `${address.slice(0, 8)}…${address.slice(-6)}`;

export const NETWORK_LABEL: Record<string, string> = { mainnet: '主网', testnet: '测试网' };

export const dexLabel = (dex: string) => (dex === '' ? '原生' : dex);

/** 后端诊断文案 → 白话。按顺序匹配，未命中则原文展示。 */
const DIAGNOSTIC_RULES: Array<[RegExp, (m: RegExpMatchArray) => string]> = [
  [/^账户模式 "(.*)" 的权益口径不适用单 DEX 余额（仅展示经验证的持仓）$/,
    () => '该账户为统一账户模式，总权益横跨多个交易区，无法按单一交易区展示；此处仅列出已核验的持仓明细'],
  [/^未配置解锁锚点$/, () => '尚未配置代币解锁的信息来源，无法展示解锁计划'],
  [/^解锁锚点全部无效：日期无法解析、来源链接非 HTTP\(S\) 或核实日期晚于当天$/,
    () => '解锁信息来源配置有误：日期无法识别、链接不是 http(s)、或核实日期晚于今天'],
  [/^账户未配置，(.*)不可用$/, (m) => `尚未配置账户地址，${m[1]}不可用`],
  [/^持仓查询失败（(.*)），日收付估算不可用$/, (m) => `查询持仓失败：${m[1]}，每日收付估算不可用`],
  [/^仓位查询失败（(.*)）$/, (m) => `查询持仓失败：${m[1]}`],
  [/^挂单查询失败（(.*)）$/, (m) => `查询挂单失败：${m[1]}`],
  [/^账户模式查询失败（(.*)）$/, (m) => `查询账户模式失败：${m[1]}`],
  [/^账户余额查询失败（(.*)）$/, (m) => `查询账户余额失败：${m[1]}`],
  [/^市场行情查询失败（(.*)）$/, (m) => `查询市场行情失败：${m[1]}`],
  [/^实时行情查询失败（(.*)）$/, (m) => `查询实时行情失败：${m[1]}`],
  [/^资金费历史查询失败（(.*)）$/, (m) => `查询资金费历史失败：${m[1]}`],
  [/^资金费历史获取不完整（(.*?)），(.*)$/, (m) => `资金费历史不完整：${m[2]}`],
  [/^(\d+) 条资金费记录时间无法解析，未展示$/, (m) => `${m[1]} 条资金费记录的时间无法识别，已跳过`],
  [/^(\S+) 已下架，实时行情与估算不可用$/, (m) => `${m[1]} 已下架，实时价格与估算不可用`],
  [/^(\S+) 已下架，当前费率与日收付估算不可用$/, (m) => `${m[1]} 已下架，当前费率与每日收付估算不可用`],
  [/^(\S+) 标记价格缺失、非正数或已下架，名义不可用$/, (m) => `${m[1]} 标记价格异常，名义价值不可用`],
  [/^(\S+) 资金费率缺失或非法，当前费率不可用$/, (m) => `${m[1]} 资金费率数据异常，当前费率不可用`],
  [/^(\S+) 不在市场元数据中，当前费率不可用$/, (m) => `${m[1]} 不在市场列表中，当前费率不可用`],
  [/^(\S+) 日线查询失败（(.*)）$/, (m) => `${m[1]} 日线查询失败：${m[2]}`],
  [/^两腿标记价格非正、缺失、未上市或已下架，实时比价不可用$/,
    () => '两个币种的标记价格异常（缺失、未上市或已下架），实时比价不可用'],
  [/^没有共同收盘日，比价序列不可用$/, () => '两个币种没有共同的交易日，比价图不可用'],
  [/^日线未更新，最近共同收盘日 (.*?)，需要 (.*?) UTC$/,
    (m) => `日线数据未更新：最近共同交易日是 ${m[1]}，需要 ${m[2]}（UTC）之后的数据`],
  [/^连续有效共同收盘日不足 30 个，30MA 不可用$/, () => '有效交易日不足 30 天，30 日均线暂不可用'],
  [/^api 频道公告查询失败（HTTP (\d+)）$/, (m) => `平台公告的 API 源查询失败（HTTP ${m[1]}），仅展示 Telegram 源`],
];

export function plainDiagnostic(raw: string): string {
  for (const [re, fmt] of DIAGNOSTIC_RULES) {
    const m = raw.match(re);
    if (m) return fmt(m);
  }
  return raw;
}
