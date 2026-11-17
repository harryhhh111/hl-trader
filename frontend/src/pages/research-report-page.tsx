import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, ExternalLink, Loader2 } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { researchApi } from "@/lib/api/research";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ReportBadges } from "@/pages/research-page";
import {
  formatResearchDate,
  absoluteReaderLinks,
  type ResearchReportSummary,
} from "@/lib/types/research";

const errorText = (error: unknown) =>
  error instanceof ApiError ? error.detail || error.message : error instanceof Error ? error.message : "请求失败";

function DateRow({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className={value ? "" : "text-muted-foreground"}>{formatResearchDate(value)}</span>
    </div>
  );
}

export function ReportDocument({ html, title, fragment }: { html: string; title: string; fragment: string }) {
  const [document, setDocument] = useState<{ html: string; url: string } | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    setFailed(false);
    if (typeof html !== "string" || !html.trim()) {
      setFailed(true);
      return;
    }
    try {
      const documentHtml = absoluteReaderLinks(html, window.location.origin);
      const url = URL.createObjectURL(new Blob([documentHtml], { type: "text/html;charset=utf-8" }));
      setDocument({ html, url });
      return () => URL.revokeObjectURL(url);
    } catch {
      setFailed(true);
    }
  }, [html]);
  if (failed) return <p role="alert">报告阅读区域初始化失败，请刷新页面重试。</p>;
  if (!document || document.html !== html) return <p className="text-sm text-muted-foreground">正在准备报告正文…</p>;
  return (
    <iframe
      title={title}
      src={document.url + fragment}
      sandbox="allow-top-navigation-by-user-activation"
      referrerPolicy="no-referrer"
      className="w-full rounded-md border bg-white"
      style={{ height: "calc(100vh - 220px)", minHeight: "480px" }}
    />
  );
}

export function ResearchReportPage() {
  const { reportId = "" } = useParams();
  const [params] = useSearchParams();
  const query = params.get("q") ?? "";
  const snapshot = params.get("snapshot") ?? "";
  const location = useLocation();
  const backHref = query ? `/research?q=${encodeURIComponent(query)}` : "/research";
  const detail = useQuery({
    queryKey: snapshot ? ["research-report", reportId, snapshot] : ["research-report", reportId],
    queryFn: () => researchApi.report(reportId, snapshot),
    enabled: Boolean(reportId),
    retry: false,
  });

  const data = detail.data;
  const siblingLink = (report: ResearchReportSummary) => {
    const next = new URLSearchParams();
    if (query) next.set("q", query);
    if (data?.snapshot_id) next.set("snapshot", data.snapshot_id);
    return `/research/reports/${encodeURIComponent(report.report_id)}${next.size ? `?${next}` : ""}`;
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button asChild variant="outline" size="sm">
          <Link to={backHref}>
            <ArrowLeft className="w-4 h-4 mr-2" />返回公司研究
          </Link>
        </Button>
        {query && <span className="text-xs text-muted-foreground">返回后保留搜索条件「{query}」</span>}
      </div>

      {detail.isPending && (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />读取报告中…
        </p>
      )}
      {detail.error && (
        <div role="alert" className="text-destructive">
          {errorText(detail.error)}
        </div>
      )}

      {data && (
        <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
          <div className="space-y-4">
            <Card>
              <CardContent className="py-4 space-y-3">
                <div className="space-y-1">
                  <h2 className="text-base font-semibold leading-snug">{data.report.title}</h2>
                  <p className="text-sm text-muted-foreground">{data.company.name}</p>
                </div>
                <ReportBadges report={data.report} />
                <div className="space-y-1 border-t pt-3">
                  <DateRow label="文档更新日" value={data.report.document_updated_at} />
                  <DateRow label="财务截止日" value={data.report.financial_as_of} />
                  <DateRow label="行情截止日" value={data.report.market_as_of} />
                </div>
                {data.company.securities.length > 0 && (
                  <p className="border-t pt-3 text-xs text-muted-foreground">
                    证券：{data.company.securities.map((item) => `${item.symbol}（${item.market}/${item.mic}）`).join("、")}
                  </p>
                )}
              </CardContent>
            </Card>

            {data.sibling_reports.length > 0 && (
              <Card>
                <CardContent className="py-4 space-y-2">
                  <h3 className="text-sm font-semibold">同公司其他报告</h3>
                  <ul className="space-y-1.5">
                    {data.sibling_reports.map((report) => (
                      <li key={report.report_id} className="space-y-0.5">
                        <Link to={siblingLink(report)} className="text-sm hover:text-primary">
                          {report.title}
                        </Link>
                        <div>
                          <ReportBadges report={report} />
                        </div>
                      </li>
                    ))}
                  </ul>
                </CardContent>
              </Card>
            )}

          </div>

          <div className="min-w-0 space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-sm font-semibold">报告正文</h2>
              <Button asChild variant="outline" size="sm">
                <a href={data.content_url + location.hash} target="_blank" rel="noopener noreferrer">
                  <ExternalLink className="mr-2 h-4 w-4" />新页面阅读
                </a>
              </Button>
            </div>
            <ReportDocument html={data.document_html} title={data.report.title} fragment={location.hash} />
          </div>
        </div>
      )}
    </div>
  );
}
