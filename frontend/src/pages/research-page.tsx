import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { BookOpen, FileText, RefreshCw, Search } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { researchApi } from "@/lib/api/research";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  CONTENT_STATUS_LABEL,
  REPORT_TYPE_LABEL,
  filterResearchCompanies,
  formatResearchDate,
  type ResearchReportSummary,
} from "@/lib/types/research";

const errorText = (error: unknown) =>
  error instanceof ApiError ? error.detail || error.message : error instanceof Error ? error.message : "请求失败";

export function ReportBadges({ report }: { report: ResearchReportSummary }) {
  return (
    <span className="flex flex-wrap items-center gap-1">
      <span className="rounded-sm border px-1.5 py-0.5 text-xs">{REPORT_TYPE_LABEL[report.report_type] ?? report.report_type}</span>
      <span className="rounded-sm border px-1.5 py-0.5 text-xs">{report.status === "draft" ? "草稿" : "可阅读"}</span>
      {report.content_status !== "verified" && (
        <span className="rounded-sm border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-xs text-amber-800">
          {CONTENT_STATUS_LABEL[report.content_status] ?? report.content_status}
        </span>
      )}
    </span>
  );
}

export function ResearchPage() {
  const [params, setParams] = useSearchParams();
  const query = params.get("q") ?? "";
  const companies = useQuery({
    queryKey: ["research-companies"],
    queryFn: researchApi.companies,
    staleTime: 60_000,
    retry: false,
  });

  const visible = useMemo(
    () => filterResearchCompanies(companies.data?.companies ?? [], query),
    [companies.data, query],
  );

  function updateQuery(value: string) {
    const next = new URLSearchParams(params);
    if (value.trim()) next.set("q", value);
    else next.delete("q");
    setParams(next, { replace: true });
  }

  return (
    <div className="space-y-6">
      <PageHeader
        icon={BookOpen}
        title="公司研究"
        description="只展示研究库已登记的公司 HTML 报告；草稿可阅读，未知日期保持未知。"
      >
        <Button variant="outline" onClick={() => void companies.refetch()}>
          <RefreshCw className="w-4 h-4 mr-2" />刷新
        </Button>
      </PageHeader>

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[220px]">
          <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
          <label htmlFor="research-search" className="sr-only">搜索公司或报告</label>
          <input
            id="research-search"
            value={query}
            onChange={(event) => updateQuery(event.target.value)}
            placeholder="搜索公司名称、证券代码或报告标题"
            className="h-9 w-full rounded-md border bg-background pl-8 pr-3 text-sm"
          />
        </div>
        {query && (
          <Button variant="outline" size="sm" onClick={() => updateQuery("")}>
            清除搜索
          </Button>
        )}
        {companies.data && (
          <span className="text-xs text-muted-foreground">
            共 {companies.data.totals.companies} 家公司 · {companies.data.totals.reports} 篇报告
          </span>
        )}
      </div>

      {companies.isPending && <p className="text-sm text-muted-foreground">读取研究快照中…</p>}
      {companies.error && (
        <div role="alert" className="text-destructive">
          {errorText(companies.error)}
        </div>
      )}

      {companies.data && visible.length === 0 && (
        <Card>
          <CardContent className="py-16 text-center text-muted-foreground">
            {query ? `没有匹配「${query}」的公司或报告` : "当前快照没有可展示的公司研究"}
          </CardContent>
        </Card>
      )}

      <div className="space-y-4">
        {visible.map((company) => (
          <Card key={company.entity_id}>
            <CardContent className="py-4 space-y-3">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <h3 className="text-base font-semibold">{company.name}</h3>
                {company.securities.length > 0 && (
                  <span className="text-xs text-muted-foreground">
                    {company.securities.map((item) => `${item.symbol}（${item.market}/${item.mic}）`).join("、")}
                  </span>
                )}
                {company.aliases.length > 0 && (
                  <span className="text-xs text-muted-foreground">别名：{company.aliases.join("、")}</span>
                )}
                <span className="text-xs text-muted-foreground ml-auto">{company.report_count} 篇报告</span>
              </div>
              <ul className="divide-y">
                {company.reports.map((report) => (
                  <li key={report.report_id} className="py-2 flex flex-wrap items-center gap-x-3 gap-y-1">
                    <FileText className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
                    <a
                      href={`/research/reports/${encodeURIComponent(report.report_id)}${query ? `?q=${encodeURIComponent(query)}` : ""}`}
                      className="text-sm font-medium hover:text-primary"
                    >
                      {report.title}
                    </a>
                    <ReportBadges report={report} />
                    <span className="ml-auto text-xs text-muted-foreground">
                      文档更新 {formatResearchDate(report.document_updated_at)} · 财务截止 {formatResearchDate(report.financial_as_of)} · 行情截止{" "}
                      {formatResearchDate(report.market_as_of)}
                    </span>
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>
        ))}
      </div>

      {companies.data && companies.data.diagnostics.excluded_count > 0 && (
        <section className="space-y-1 border-t pt-4">
          <h2 className="text-sm font-semibold">未进入列表的登记项</h2>
          <ul className="space-y-0.5 text-xs text-muted-foreground">
            {companies.data.diagnostics.excluded.map((item, index) => (
              <li key={`${item.report_id}-${index}`}>
                {item.report_id ? `${item.report_id}：` : ""}
                {item.reason}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
