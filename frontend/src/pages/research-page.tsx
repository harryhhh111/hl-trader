import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { BookOpen, ChevronRight, RefreshCw, Search } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { researchApi } from "@/lib/api/research";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  CONTENT_STATUS_LABEL,
  REPORT_TYPE_LABEL,
  filterResearchCompanies,
  companyEntryReport,
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
        description="按公司查看研究报告"
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
            {query ? `匹配 ${visible.length} 家公司` : `共 ${companies.data.totals.companies} 家公司`}
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

      {visible.length > 0 && (
        <ul aria-label="公司研究列表" className="divide-y rounded-lg border bg-card">
          {visible.map(company => {
            const report = companyEntryReport(company);
            if (!report) return null;
            return (
              <li key={company.entity_id}>
                <Link
                  to={`/research/reports/${encodeURIComponent(report.report_id)}${query ? `?q=${encodeURIComponent(query)}` : ""}`}
                  aria-label={`阅读 ${company.name} 的公司研究`}
                  className="flex items-center gap-3 px-4 py-3 transition-colors hover:bg-muted/50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                      <h3 className="break-words text-sm font-medium">{company.name}</h3>
                      {company.securities.length > 0 && (
                        <span className="text-xs text-muted-foreground">
                          {company.securities.map(item => item.symbol).join("、")}
                        </span>
                      )}
                    </div>
                  </div>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {report.status === "draft" ? "草稿" : "可阅读"} · {CONTENT_STATUS_LABEL[report.content_status]}
                  </span>
                  <span className="hidden shrink-0 text-xs text-muted-foreground sm:inline">
                    更新 {formatResearchDate(report.document_updated_at)}
                  </span>
                  <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
                </Link>
              </li>
            );
          })}
        </ul>
      )}

      {companies.data && companies.data.diagnostics.excluded_count > 0 && (
        <details className="space-y-1 text-xs text-muted-foreground">
          <summary className="cursor-pointer">收录说明</summary>
          <ul className="space-y-0.5 text-xs text-muted-foreground">
            {companies.data.diagnostics.excluded.map((item, index) => (
              <li key={`${item.report_id}-${index}`}>
                {item.report_id ? `${item.report_id}：` : ""}
                {item.reason}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
