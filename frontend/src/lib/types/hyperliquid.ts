export type HlBlockStatus = 'ok' | 'partial' | 'unavailable';

export interface HlBlock {
  status: HlBlockStatus;
  queried_at: string;
  diagnostics: string[];
}

export interface HlPosition {
  coin: string;
  side: 'long' | 'short';
  size: string;
  mark_px: string | null;
  unrealized_pnl: string | null;
  notional: string | null;
}

export interface HlPositionsBlock extends HlBlock {
  equity: string | null;
  positions: HlPosition[] | null;
  notional_ratio: string | null;
  ratio_drift: string | null;
  action_state: string | null;
  open_orders: number | null;
}

export interface HlFundingPayment { time: string; coin: string; usdc: string; rate: string }

export interface HlFundingBlock extends HlBlock {
  hourly_rates: Record<string, string | null>;
  annualized: Record<string, string | null>;
  daily_estimate: { direction: 'receive' | 'pay'; amount: string } | null;
  window_start: string | null;
  window_end: string | null;
  window_complete: boolean | null;
  actual_48h_net: string | null;
  recent_payments: HlFundingPayment[] | null;
}

export interface HlRatioBlock extends HlBlock {
  current: string | null;
  ma30: string | null;
  vs_ma30_pct: string | null;
  as_of_date: string | null;
}

export interface HlUnlockAnchor {
  date: string;
  days_ahead: number;
  label: string;
  source_url: string;
  verified_utc: string;
}

export interface HlUnlocksBlock extends HlBlock {
  anchors: HlUnlockAnchor[] | null;
  note: string;
}

export interface HlAnnouncement {
  kind: 'general' | 'api';
  text: string;
  published_at: string | null;
  url: string;
  source: string;
}

export interface HlAnnouncementsBlock extends HlBlock {
  items: HlAnnouncement[] | null;
}

export interface HlSnapshot {
  schema_version: number;
  network: string;
  account_address: string;
  queried_at: string;
  blocks: {
    positions: HlPositionsBlock;
    funding: HlFundingBlock;
    ratio: HlRatioBlock;
    unlocks: HlUnlocksBlock;
    announcements: HlAnnouncementsBlock;
  };
}

// ── v2 多 pair（HYPERLIQUID_MULTIPAIR_PLAN §5.3） ──

export type HlV2Status = 'ok' | 'partial' | 'unavailable';

export interface HlSourceQuery { status: 'ok' | 'unavailable'; queried_at: string | null }

export interface HlV2Block {
  status: HlV2Status;
  queried_at: string;
  source_queries: Record<string, HlSourceQuery>;
  diagnostics: string[];
}

export interface HlPairDescriptor {
  id: string;
  label: string;
  network: 'mainnet' | 'testnet';
  dex: string;
  coins: [string, string];
  account_address: string | null;
  unlocks_enabled: boolean;
}

export interface HlPairDirectory {
  schema_version: number;
  config_revision: string;
  pairs: HlPairDescriptor[];
}

export interface HlV2Position {
  coin: string;
  side: 'long' | 'short';
  size: string;
  entry_px: string | null;
  mark_px: string | null;
  notional: string | null;
  unrealized_pnl: string | null;
  margin_mode: 'cross' | 'isolated' | null;
  margin_used: string | null;
  liquidation_px: string | null;
}

export interface HlV2AccountBlock extends HlV2Block {
  account_mode: string | null;
  equity: string | null;
  equity_scope: 'perp_dex' | 'account' | 'unavailable';
}

export type HlPositionState = 'flat' | 'single_leg' | 'opposite' | 'same_direction' | 'unknown';

export interface HlV2PositionsBlock extends HlV2Block {
  items: HlV2Position[] | null;
  position_state: HlPositionState;
  unrealized_pnl: string | null;
  notional_ratio: string | null;
  ratio_drift_pct: string | null;
  open_orders: number | null;
}

export interface HlV2FundingBlock extends HlV2Block {
  hourly_rates: Record<string, string | null>;
  annualized_pct: Record<string, string | null>;
  daily_estimate: { direction: 'receive' | 'pay' | 'flat'; amount: string } | null;
  window_start: string;
  window_end: string;
  window_complete: boolean;
  actual_48h_net: string | null;
  recent_payments: { time: string; coin: string; usdc: string; rate: string }[] | null;
}

export interface HlRatioPoint {
  date: string;
  first_close: string | null;
  second_close: string | null;
  ratio: string | null;
  ma30: string | null;
}

export interface HlV2RatioBlock extends HlV2Block {
  live: string | null;
  latest_close: string | null;
  as_of_date: string | null;
  ma30: string | null;
  vs_ma30_pct: string | null;
  points: HlRatioPoint[] | null;
}

export interface HlV2UnlockAnchor {
  date: string;
  days_ahead: number;
  label: string;
  source_url: string;
  verified_utc: string;
}

export interface HlV2UnlocksBlock extends HlV2Block {
  anchors: HlV2UnlockAnchor[];
  note: string;
}

export interface HlPairSnapshot {
  schema_version: 2;
  config_revision: string;
  queried_at: string;
  pair: HlPairDescriptor;
  blocks: {
    account: HlV2AccountBlock;
    positions: HlV2PositionsBlock;
    funding: HlV2FundingBlock;
    ratio: HlV2RatioBlock;
    unlocks: HlV2UnlocksBlock | null;
  };
}

export interface HlV2Announcements {
  schema_version: 2;
  queried_at: string;
  block: HlAnnouncementsBlock;
}
