import assert from "node:assert/strict";
import test from "node:test";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { WatchlistPage } from "./watchlist-page";
import type { QuoteBatch } from "@/lib/types/watchlist";

function quote(market: "US" | "CN_A", close: number): QuoteBatch {
  return {
    market,
    currency: market === "US" ? "USD" : "CNY",
    source: "database",
    price_basis: "raw_unadjusted",
    fetched_at: "2026-10-02T23:00:00Z",
    quotes: [{
      stock_code: "000001",
      close,
      change_pct: 2,
      quote_date: "2026-10-02",
      previous_quote_date: "2026-10-01",
      status: "ok",
      warning: "",
      stale: false,
      price_basis: "raw_unadjusted",
      trend: [{ date: "2026-10-01", close: close / 1.02 }, { date: "2026-10-02", close }],
    }],
  };
}

test("三市场表格按市场隔离同码证券且无新增移除分析入口", () => {
  const qc = new QueryClient();
  const base = {
    id: 1,
    stock_code: "000001",
    stock_name: "示例",
    group_name: "研究",
    note: "理由",
    research_url: "",
    sort_order: 0,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    quote_source: "tencent",
  } as const;
  qc.setQueryData(["watchlist"], [
    { ...base, id: 1, market: "US", currency: "USD" },
    { ...base, id: 2, market: "CN_A", currency: "CNY" },
  ]);
  qc.setQueryData(["watchlist-quotes", "US", ["000001"]], quote("US", 102));
  qc.setQueryData(["watchlist-quotes", "CN_A", ["000001"]], quote("CN_A", 10));
  const html = renderToString(
    <QueryClientProvider client={qc}><MemoryRouter><WatchlistPage /></MemoryRouter></QueryClientProvider>,
  );
  assert.match(html, /102/);
  assert.match(html, />10</);
  assert.doesNotMatch(html, />添加股票<|>移除<|analyzer|StockSearch/);
});
