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

export interface HlPairAccount {
  id: number | null;
  label: string;
  address: string;
  is_active: boolean;
  source: 'config' | 'database';
  created_at: string | null;
}

export interface HlPairAccounts {
  pair_id: string;
  accounts: HlPairAccount[];
}

export interface HlManagedWallet {
  configured: boolean;
  network: 'mainnet' | 'testnet';
  pair_id: string;
  account_address: string;
  wallet_address: string | null;
  wallet_version: number;
  authorization_checked_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  active_execution: { request_id: string; status: 'running' | 'unknown' } | null;
}

export interface HlWalletChallenge {
  challenge_id: string;
  actor_binding: string;
  operation: 'save' | 'rotate' | 'delete';
  wallet_version: number;
  key_id: string;
  public_key: string;
  algorithm: 'RSA-OAEP-3072';
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
  leverage: number | null;
  max_leverage: number | null;
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

export interface HlLeverageReport {
  schema_version: 1;
  status: 'prepared' | 'executing' | 'succeeded' | 'rejected' | 'unknown';
  action_id: string | null;
  request_id?: string;
  pair_id: string;
  network: 'mainnet' | 'testnet';
  config_revision: string;
  account_address: string;
  coin: string;
  current_leverage?: number;
  current_margin_mode?: 'cross' | 'isolated';
  leverage: number;
  margin_mode: 'cross' | 'isolated';
  max_leverage?: number;
  expected_signer?: string;
  nonce?: number;
  typed_data?: Record<string, unknown>;
  result?: string | null;
  managed_status?: 'running' | 'succeeded' | 'incomplete' | 'failed' | 'unknown';
  error_code?: string | null;
}

export type HlRiskStatus = 'disabled' | 'monitoring' | 'triggered' | 'executing' | 'completed' | 'incomplete' | 'unknown' | 'unavailable';
export type HlRiskTrigger = 'take_profit' | 'stop_loss';

export interface HlPairRiskEvent {
  id: number;
  event_type: 'rule_updated' | 'triggered' | 'trigger_cleared' | 'completed' | 'execution_updated';
  trigger_kind: HlRiskTrigger | null;
  pair_unrealized_pnl: string | null;
  action_id: string | null;
  request_id: string | null;
  detail: string | null;
  created_at: string;
}

export interface HlPairRisk {
  configured: boolean;
  pair_id: string;
  account_address: string;
  network?: 'mainnet' | 'testnet';
  pair_config_revision?: string;
  enabled: boolean;
  take_profit_usd: string | null;
  stop_loss_usd: string | null;
  rule_revision: number;
  status: HlRiskStatus;
  worker_available: boolean;
  last_attempt_at: string | null;
  last_checked_at: string | null;
  pair_unrealized_pnl: string | null;
  position_state: HlPositionState | null;
  trigger_kind: HlRiskTrigger | null;
  triggered_at: string | null;
  trigger_pnl: string | null;
  last_action_id: string | null;
  last_request_id: string | null;
  last_error: string | null;
  updated_at?: string;
  events: HlPairRiskEvent[];
}

export type HlAdjustmentStatus = 'preview' | 'prepared' | 'succeeded' | 'incomplete' | 'unknown';
export type HlAdjustmentLegStatus = 'not_required' | 'prepared' | 'pending' | 'filled' | 'partially_filled' | 'rejected' | 'unknown';

export interface HlAdjustmentLeg {
  coin: string;
  position_side: 'long' | 'short';
  current_size: string;
  current_notional: string;
  mid_px: string;
  target_size: string;
  estimated_target_notional: string;
  action: 'buy' | 'sell' | 'none';
  order_qty: string;
  protect_px: string;
  reduce_only: boolean;
  asset: number;
  nonce: number | null;
  cloid: string | null;
  status: HlAdjustmentLegStatus;
  confirmed_qty: string;
  avg_px: string | null;
  slippage_pct?: string | null;
  slippage_usd?: string | null;
  oid: number | null;
  result: string | null;
  final_size: string | null;
  final_notional: string | null;
}

export interface HlSigningRequest {
  leg_index: number;
  nonce: number;
  typed_data: Record<string, unknown>;
}

export interface HlAdjustmentReport {
  schema_version: 1;
  status: HlAdjustmentStatus;
  action_id: string | null;
  pair_id: string;
  network: 'mainnet' | 'testnet';
  config_revision: string;
  account_address: string;
  target_notional: string;
  created_at: string;
  legs: HlAdjustmentLeg[];
  diagnostics: string[];
  expected_signer?: string;
  signing_requests?: HlSigningRequest[];
  execution_enabled?: boolean;
  browser_wallet_enabled?: boolean;
  max_leg_notional_usd?: string;
  request_id?: string;
  wallet_address?: string;
  wallet_version?: number;
  managed_status?: 'previewed' | 'running' | 'succeeded' | 'incomplete' | 'failed' | 'unknown';
  error_code?: string | null;
}

export interface HlAdjustmentQuoteLeg {
  coin: string;
  bid: string;
  ask: string;
  mid: string;
  book_time: string;
}

export interface HlAdjustmentQuote {
  schema_version: 1;
  pair_id: string;
  network: 'mainnet' | 'testnet';
  config_revision: string;
  queried_at: string;
  legs: HlAdjustmentQuoteLeg[];
}

export type HlTradeHistoryStatus = 'running' | 'succeeded' | 'incomplete' | 'unknown';

export interface HlTradeHistoryLeg {
  coin: string;
  position_side: 'long' | 'short';
  action: 'buy' | 'sell' | 'none';
  reduce_only: boolean;
  order_qty: string;
  protect_px: string;
  status: HlAdjustmentLegStatus;
  confirmed_qty: string;
  avg_px: string | null;
  slippage_pct?: string | null;
  slippage_usd?: string | null;
  oid: number | null;
  cloid: string | null;
  result: string | null;
}

export interface HlTradeHistoryItem {
  action_id: string;
  request_id: string | null;
  execution_mode: 'managed' | 'browser' | 'server';
  status: HlTradeHistoryStatus;
  target_notional: string;
  created_at: string;
  legs: HlTradeHistoryLeg[];
  diagnostics: string[];
}

export interface HlTradeHistory {
  pair_id: string;
  account_address: string;
  items: HlTradeHistoryItem[];
  has_more: boolean;
  skipped_reports: number;
}
