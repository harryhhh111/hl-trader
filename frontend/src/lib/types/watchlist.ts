import type { Market } from "./common";

export interface ResearchDiagnostic {
  status: "matched" | "no_entry";
  commit_sha: string;
  entity_id: string | null;
  missing_sections: string[];
  sections: { kind: string; path: string; line: number; heading_path: string[]; truncated: boolean; total_chars: number }[];
}

export interface WatchSuggestion {
  group: string;
  note: string;
  source: "research_doc" | "generic";
  docs: string[];
  research?: ResearchDiagnostic;
}

export interface WatchItem {
  id: number;
  market: Market;
  stock_code: string;
  stock_name: string;
  currency: "USD" | "HKD" | "CNY";
  quote_source: string;
  group_name: string;
  note: string;
  research_url: string;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface WatchQuote {
  stock_code: string;
  close: number | null;
  change_pct: number | null;
  quote_date: string | null;
  status: string;
  warning: string;
  stale: boolean;
  previous_quote_date?: string | null;
  price_basis: "raw_unadjusted";
  trend: { date: string; close: number | null }[];
}

export interface QuoteBatch {
  market: Market;
  currency: string;
  source: string;
  price_basis: "raw_unadjusted";
  fetched_at: string;
  quotes: WatchQuote[];
}

export interface AnnouncementSourceState {
  kind: "sec" | "ir" | "none";
  state: string;
  message: string;
  source_status: string | null;
  platform: string | null;
  reason: string | null;
  last_attempt_at: string | null;
  last_attempt_status: string | null;
  last_error_class: string | null;
  last_success_at: string | null;
  next_due_at: string | null;
  checked_through: string | null;
  effective_interval_minutes: number | null;
  open_gaps: number;
  earliest_gap_start: string | null;
}

export interface AnnouncementItem {
  title: string;
  form_type: string;
  url: string;
  source_kind: string;
  published_at: string | null;
  published_precision: string;
  published_raw: string;
  source_timezone: string | null;
  time_unknown: boolean;
  date_only: boolean;
  first_observed_at: string | null;
  revision: number;
  is_backfill: boolean;
}

export interface AnnouncementSummaryItem {
  item_id: number;
  market: Market;
  stock_code: string;
  count: number | null;
  latest: AnnouncementItem | null;
  sources: AnnouncementSourceState[];
}

export interface AnnouncementSummary {
  initialized: boolean;
  message?: string;
  announcements_enabled: boolean;
  days: number;
  window: { start: string; end: string; timezone: string };
  degraded: boolean;
  items: AnnouncementSummaryItem[];
}

export interface AnnouncementDetail {
  item_id: number;
  market: Market;
  stock_code: string;
  stock_name: string;
  supported: boolean;
  initialized: boolean;
  message?: string;
  announcements_enabled: boolean;
  total: number;
  has_more: boolean;
  items: AnnouncementItem[];
  sources: AnnouncementSourceState[];
  days: number;
  offset: number;
  limit: number;
  source: string;
  window: { start: string; end: string; timezone: string };
}
