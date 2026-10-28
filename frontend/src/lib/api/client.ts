export class ApiError extends Error {
  readonly detail: string;

  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
    this.detail = message;
  }
}

function csrfToken(): string | null {
  const entry = document.cookie.split("; ").find((value) => value.startsWith("hl_csrf="));
  return entry ? decodeURIComponent(entry.slice("hl_csrf=".length)) : null;
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (!path.startsWith("/api/v1/")) {
    throw new Error("API paths must use the same-origin /api/v1/ prefix");
  }
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const method = (init.method || "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = csrfToken();
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }
  const response = await fetch(path, { ...init, credentials: "same-origin", headers });
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new ApiError("服务返回了无效响应", response.status);
  }
  if (!response.ok) {
    const detail = payload && typeof payload === "object" && "detail" in payload
      ? (payload as { detail?: unknown }).detail
      : undefined;
    throw new ApiError(typeof detail === "string" ? detail : "请求失败", response.status);
  }
  if (!payload || typeof payload !== "object" || !("data" in payload)) {
    throw new ApiError("服务响应缺少 data", response.status);
  }
  return (payload as { data: T }).data;
}

export const sessionApi = {
  session: () => apiFetch<{ authenticated: boolean; expires_at?: string }>("/api/v1/session"),
  login: (token: string) => apiFetch<{ authenticated: boolean; expires_at: string }>("/api/v1/session/login", {
    method: "POST",
    body: JSON.stringify({ token }),
  }),
  logout: () => apiFetch<{ authenticated: false }>("/api/v1/session/logout", { method: "POST" }),
};

export const hyperliquidPairsApi = {
  directory: () => apiFetch<import("@/lib/types/hyperliquid").HlPairDirectory>("/api/v1/hyperliquid/pairs"),
  pairDashboard: (pairId: string) => apiFetch<import("@/lib/types/hyperliquid").HlPairSnapshot>(
    `/api/v1/hyperliquid/pairs/${encodeURIComponent(pairId)}/dashboard`,
  ),
  announcements: () => apiFetch<import("@/lib/types/hyperliquid").HlV2Announcements>("/api/v1/hyperliquid/announcements"),
};

export const watchlistApi = {
  list: () => apiFetch<import("@/lib/types/watchlist").WatchItem[]>("/api/v1/watchlist"),
  save: (item: Pick<import("@/lib/types/watchlist").WatchItem, "id" | "group_name" | "note" | "research_url" | "sort_order">) =>
    apiFetch<{ id: number }>(`/api/v1/watchlist/${item.id}`, {
      method: "PATCH",
      body: JSON.stringify({
        group_name: item.group_name,
        note: item.note,
        research_url: item.research_url,
        sort_order: item.sort_order,
      }),
    }),
  quotes: (market: import("@/lib/types/common").Market, codes: string[]) =>
    apiFetch<import("@/lib/types/watchlist").QuoteBatch>("/api/v1/watchlist/quotes", {
      method: "POST",
      body: JSON.stringify({ market, codes }),
    }),
  suggest: (item: { market: import("@/lib/types/common").Market; stock_code: string; stock_name: string }) =>
    apiFetch<import("@/lib/types/watchlist").WatchSuggestion>("/api/v1/watchlist/suggest", {
      method: "POST",
      body: JSON.stringify(item),
    }),
  announcements: (days = 7) => apiFetch<import("@/lib/types/watchlist").AnnouncementSummary>(`/api/v1/watchlist/announcements?days=${days}`),
  itemAnnouncements: (itemId: number, params: { days?: number; offset?: number; limit?: number; source?: string } = {}) => {
    const query = new URLSearchParams({
      days: String(params.days ?? 90),
      offset: String(params.offset ?? 0),
      limit: String(params.limit ?? 20),
      source: params.source ?? "all",
    });
    return apiFetch<import("@/lib/types/watchlist").AnnouncementDetail>(`/api/v1/watchlist/${itemId}/announcements?${query}`);
  },
};

export const newsApi = {
  feed: (params: { date?: string; stock?: string; kind?: string; offset?: number; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.date) query.set("date", params.date);
    if (params.stock) query.set("stock", params.stock);
    query.set("kind", params.kind ?? "all");
    query.set("offset", String(params.offset ?? 0));
    query.set("limit", String(params.limit ?? 200));
    return apiFetch<import("@/lib/types/news").NewsFeed>(`/api/v1/news/feed?${query}`);
  },
  pinned: () => apiFetch<import("@/lib/types/news").NewsPinned>("/api/v1/news/pinned"),
};
