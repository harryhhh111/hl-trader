export type ResearchReportType = "company_research" | "topic" | "update";
export type ResearchStatus = "draft" | "ready";
export type ResearchContentStatus = "unreviewed" | "partial" | "verified" | "synthetic";

export interface ResearchSecurity {
  market: string;
  mic: string;
  symbol: string;
}

export interface ResearchReportSummary {
  report_id: string;
  entity_id: string;
  title: string;
  report_type: ResearchReportType;
  status: ResearchStatus;
  content_status: ResearchContentStatus;
  document_updated_at: string | null;
  financial_as_of: string | null;
  market_as_of: string | null;
}

export interface ResearchCompany {
  entity_id: string;
  name: string;
  aliases: string[];
  securities: ResearchSecurity[];
  report_count: number;
  reports: ResearchReportSummary[];
}

export interface ResearchCompanyList {
  snapshot_id: string;
  generated_at: string;
  totals: { companies: number; reports: number };
  companies: ResearchCompany[];
  diagnostics: {
    excluded_count: number;
    excluded: { report_id: string; reason: string }[];
    source: { name: string | null; commit: string | null; dirty: boolean | null };
  };
}

export interface ResearchReportDetail {
  snapshot_id: string;
  generated_at: string;
  report: ResearchReportSummary;
  company: {
    entity_id: string;
    name: string;
    aliases: string[];
    securities: ResearchSecurity[];
  };
  sibling_reports: ResearchReportSummary[];
  content_url: string;
  document_html: string;
  unpublished_links: { path: string; reference: string; reason: string }[];
  external_links: { url: string; label: string }[];
  diagnostics: { code: string; severity: string; message: string }[];
  assets: { path: string; sha256: string; size: number; media_type: string }[];
}

export const REPORT_TYPE_LABEL: Record<ResearchReportType, string> = {
  company_research: "主报告",
  topic: "专题",
  update: "局部更新",
};

export const CONTENT_STATUS_LABEL: Record<ResearchContentStatus, string> = {
  unreviewed: "尚未核验",
  partial: "有研究缺口",
  verified: "已核验",
  synthetic: "合成数据",
};

/** 未知日期保持未知，不使用文件时间或快照生成时间替代。 */
export function formatResearchDate(value: string | null): string {
  return value ?? "未登记";
}

/** 列表搜索作用于公司名称、别名、显式证券代码和报告标题。 */
export function matchesResearchQuery(company: ResearchCompany, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  const haystacks = [
    company.name,
    ...company.aliases,
    ...company.securities.map((item) => item.symbol),
    ...company.reports.map((item) => item.title),
  ];
  return haystacks.some((value) => value.toLowerCase().includes(needle));
}

export function filterResearchCompanies(companies: ResearchCompany[], query: string): ResearchCompany[] {
  // Search selects companies, without changing their entry or hiding siblings.
  return companies.filter(company => matchesResearchQuery(company, query));
}

export function companyEntryReport(company: ResearchCompany): ResearchReportSummary | undefined {
  // The API orders reports by type/date/ID. Prefer the registered main report;
  // a company with topics or updates only still has one usable entry.
  return company.reports.find(report => report.report_type === "company_research") ?? company.reports[0];
}

/** 只允许 http/https 外部来源，受控外层入口使用。 */
export function safeExternalUrl(url: string): string | null {
  return /^https?:\/\//.test(url) ? url : null;
}

/** Blob documents have no HTTP base URL; resolve only the registered reader links. */
export function absoluteReaderLinks(html: string, origin: string): string {
  return html.replace(/(\bhref=")(\/research\/reports\/[^\"]*)"/gi,
    (_match, prefix, path) => `${prefix}${origin}${path}"`);
}
