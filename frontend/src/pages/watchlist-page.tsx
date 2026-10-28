import { useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Pencil, RefreshCw, Star } from "lucide-react";
import { ApiError, watchlistApi } from "@/lib/api/client";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import type { Market } from "@/lib/types/common";
import type { WatchItem, WatchQuote } from "@/lib/types/watchlist";

const markets: Market[] = ["US", "CN_HK", "CN_A"];
const currency = { US: "USD", CN_HK: "HKD", CN_A: "CNY" };
const errorText = (error: unknown) => error instanceof ApiError ? error.detail : error instanceof Error ? error.message : "请求失败";

function Sparkline({ quote }: { quote?: WatchQuote }) {
  if (quote?.status === "review_price_basis") return <span className="text-muted-foreground">价格口径待核对</span>;
  if (!quote || quote.trend.length === 0) return <span className="text-muted-foreground">暂无走势数据</span>;
  if (quote.trend.length < 2) return <span className="text-muted-foreground">历史不足</span>;
  const values = quote.trend.map((point) => point.close);
  const valid = values.filter((value): value is number => value != null);
  if (valid.length < 2) return <span className="text-muted-foreground">有效收盘不足</span>;
  const min = Math.min(...valid);
  const max = Math.max(...valid);
  const segments: string[][] = [[]];
  values.forEach((value, index) => {
    if (value == null) segments.push([]);
    else segments[segments.length - 1].push(`${index * 110 / (values.length - 1)},${30 - (value - min) / (max - min || 1) * 26}`);
  });
  return (
    <div>
      <svg viewBox="0 0 112 34" className="w-28 h-9 text-primary" role="img" aria-label="近20条未复权日线">
        {segments.map((points, index) => <polyline key={index} points={points.join(" ")} fill="none" stroke="currentColor" strokeWidth="1.8" />)}
      </svg>
      <span className="text-xs text-muted-foreground">{quote.trend.length}/20 日记录</span>
    </div>
  );
}

export function WatchlistPage() {
  const qc = useQueryClient();
  const [market, setMarket] = useState<Market | "all">("all");
  const [group, setGroup] = useState("");
  const [sort, setSort] = useState("default");
  const [editor, setEditor] = useState<WatchItem | null>(null);
  const [actionError, setActionError] = useState("");
  const itemsQuery = useQuery({ queryKey: ["watchlist"], queryFn: watchlistApi.list, retry: false });
  const items = itemsQuery.data ?? [];
  const quoteQueries = useQueries({ queries: markets.map((current) => {
    const codes = items.filter((item) => item.market === current).map((item) => item.stock_code).sort();
    return {
      queryKey: ["watchlist-quotes", current, codes],
      queryFn: () => watchlistApi.quotes(current, codes),
      enabled: codes.length > 0,
      staleTime: 60_000,
      refetchInterval: 300_000,
      retry: 1,
    };
  }) });
  const quotes = new Map<string, WatchQuote>();
  quoteQueries.forEach((query, index) => query.data?.quotes.forEach((quote) => quotes.set(`${markets[index]}:${quote.stock_code}`, quote)));
  const groups = [...new Set(items.map((item) => item.group_name).filter(Boolean))].sort();
  const visible = items.filter((item) => (market === "all" || item.market === market) && (!group || item.group_name === group));
  if (sort !== "default") visible.sort((a, b) => {
    const av = quotes.get(`${a.market}:${a.stock_code}`)?.change_pct;
    const bv = quotes.get(`${b.market}:${b.stock_code}`)?.change_pct;
    if (av == null) return bv == null ? 0 : 1;
    if (bv == null) return -1;
    return sort === "up" ? bv - av : av - bv;
  });
  const save = useMutation({
    mutationFn: (item: WatchItem) => watchlistApi.save(item),
    onSuccess: () => {
      setEditor(null);
      setActionError("");
      void qc.invalidateQueries({ queryKey: ["watchlist"] });
    },
    onError: (error) => setActionError(errorText(error)),
  });

  return (
    <div className="space-y-6">
      <PageHeader icon={Star} title="关注股" description="关注公司、研究理由与未复权日线行情。">
        <Button variant="outline" onClick={() => {
          void qc.invalidateQueries({ queryKey: ["watchlist"] });
          void qc.invalidateQueries({ queryKey: ["watchlist-quotes"] });
        }}><RefreshCw className="w-4 h-4 mr-2" />刷新</Button>
      </PageHeader>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        {[["关注股票", items.length], ["研究分组", groups.length], ["行情口径", "日线收盘"]].map(([label, value]) => (
          <Card key={label}><CardContent className="pt-5"><p className="text-sm text-muted-foreground">{label}</p><p className="text-2xl font-semibold mt-1">{value}</p></CardContent></Card>
        ))}
      </div>
      <div className="flex flex-wrap gap-3">
        <select aria-label="筛选市场" className="h-9 rounded-md border bg-background px-3 text-sm" value={market} onChange={(event) => setMarket(event.target.value as Market | "all")}><option value="all">全部市场</option>{markets.map((item) => <option key={item}>{item}</option>)}</select>
        <select aria-label="筛选分组" className="h-9 rounded-md border bg-background px-3 text-sm" value={group} onChange={(event) => setGroup(event.target.value)}><option value="">全部分组</option>{groups.map((item) => <option key={item}>{item}</option>)}</select>
        <select aria-label="排序" className="h-9 rounded-md border bg-background px-3 text-sm" value={sort} onChange={(event) => setSort(event.target.value)}><option value="default">登记顺序</option><option value="up">涨幅优先</option><option value="down">跌幅优先</option></select>
      </div>
      <p className="text-xs text-muted-foreground">行情只读数据库，不触发采集；涨跌幅比较最新与前一条入库日线，缺失不跨值计算。新增和移除证券由管理员脚本维护。</p>
      {itemsQuery.isPending && <p>正在读取关注列表…</p>}
      {itemsQuery.error && <div role="alert" className="text-destructive">{errorText(itemsQuery.error)}</div>}
      {itemsQuery.isSuccess && items.length === 0 && <Card><CardContent className="py-16 text-center text-muted-foreground">暂无已启用的关注证券</CardContent></Card>}
      {visible.length > 0 && (
        <div className="border rounded-lg bg-card overflow-x-auto">
          <table className="w-full text-sm">
            <thead><tr className="text-left border-b">{["股票", "分组", "最新收盘价", "涨跌幅", "近20日", "行情日期", "关注理由", "操作"].map((heading) => <th key={heading} className="p-3">{heading}</th>)}</tr></thead>
            <tbody>{visible.map((item) => {
              const quote = quotes.get(`${item.market}:${item.stock_code}`);
              return (
                <tr key={item.id} className="border-b last:border-0 align-top">
                  <td className="p-3"><span className="font-medium">{item.stock_name}</span><div className="text-xs text-muted-foreground">{item.stock_code} · {item.market}</div></td>
                  <td className="p-3">{item.group_name || "未分组"}</td>
                  <td className="p-3 font-mono whitespace-nowrap">{quote?.close?.toLocaleString("en-US", { maximumFractionDigits: 4 }) ?? "—"} <span className="text-xs">{currency[item.market]}</span></td>
                  <td className="p-3 min-w-40"><div>{quote?.change_pct == null ? "—" : `${quote.change_pct >= 0 ? "+" : ""}${quote.change_pct.toFixed(2)}%`}</div><div className="text-xs text-muted-foreground">{quote?.previous_quote_date && quote?.quote_date ? `${quote.previous_quote_date} → ${quote.quote_date}` : quote?.warning}</div></td>
                  <td className="p-3"><Sparkline quote={quote} /></td>
                  <td className="p-3 min-w-36"><div>{quote?.quote_date || "—"}</div>{quote?.stale && <span className="text-xs text-amber-600">行情滞后</span>}</td>
                  <td className="p-3 min-w-48 max-w-72 whitespace-pre-wrap">{item.note || <span className="text-muted-foreground">未填写</span>}</td>
                  <td className="p-3"><div className="flex gap-1">{item.research_url && <Button asChild variant="ghost" size="icon"><a href={item.research_url} target="_blank" rel="noopener noreferrer" aria-label="打开研究"><ExternalLink /></a></Button>}<Button variant="ghost" size="icon" aria-label={`编辑${item.stock_code}`} onClick={() => setEditor(item)}><Pencil /></Button></div></td>
                </tr>
              );
            })}</tbody>
          </table>
        </div>
      )}
      {editor && (
        <Card><CardContent className="pt-6">
          <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); save.mutate(editor); }}>
            <h2 className="font-semibold">编辑 {editor.stock_name} · {editor.stock_code}</h2>
            <label className="block text-sm">分组<input className="mt-1 w-full border rounded-md bg-background p-2" maxLength={80} value={editor.group_name} onChange={(event) => setEditor({ ...editor, group_name: event.target.value })} /></label>
            <label className="block text-sm">关注理由<textarea className="mt-1 w-full border rounded-md bg-background p-3 min-h-24" maxLength={2000} value={editor.note} onChange={(event) => setEditor({ ...editor, note: event.target.value })} /></label>
            <label className="block text-sm">研究链接<input className="mt-1 w-full border rounded-md bg-background p-2" type="url" maxLength={2000} value={editor.research_url} onChange={(event) => setEditor({ ...editor, research_url: event.target.value })} /></label>
            {actionError && <p role="alert" className="text-destructive text-sm">{actionError}</p>}
            <div className="flex gap-2"><Button type="submit" disabled={save.isPending}>{save.isPending ? "保存中…" : "保存元数据"}</Button><Button type="button" variant="outline" onClick={() => setEditor(null)}>取消</Button></div>
          </form>
        </CardContent></Card>
      )}
    </div>
  );
}
