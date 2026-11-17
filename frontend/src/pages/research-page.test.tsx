import assert from "node:assert/strict";
import test from "node:test";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { ReportBadges, ResearchPage } from "./research-page";
import { ResearchReportPage } from "./research-report-page";
import {
  CONTENT_STATUS_LABEL,
  REPORT_TYPE_LABEL,
  filterResearchCompanies,
  companyEntryReport,
  formatResearchDate,
  safeExternalUrl,
} from "@/lib/types/research";
import type {
  ResearchCompany,
  ResearchCompanyList,
  ResearchReportDetail,
  ResearchReportSummary,
} from "@/lib/types/research";

function report(overrides: Partial<ResearchReportSummary> = {}): ResearchReportSummary {
  return {
    report_id: "msft-main",
    entity_id: "company:microsoft",
    title: "微软：软件现金流与AI资本回报",
    report_type: "company_research",
    status: "draft",
    content_status: "unreviewed",
    document_updated_at: null,
    financial_as_of: null,
    market_as_of: null,
    ...overrides,
  };
}

const microsoft: ResearchCompany = {
  entity_id: "company:microsoft",
  name: "Microsoft",
  aliases: ["MSFT", "微软"],
  securities: [{ market: "US", mic: "XNAS", symbol: "MSFT" }],
  report_count: 2,
  reports: [report(), report({ report_id: "msft-topic", title: "微软：传统软件与现金流", report_type: "topic" })],
};

const qxo: ResearchCompany = {
  entity_id: "company:qxo",
  name: "QXO公司研究",
  aliases: [],
  securities: [],
  report_count: 1,
  reports: [
    report({
      report_id: "qxo-main",
      entity_id: "company:qxo",
      title: "QXO公司研究",
      content_status: "partial",
      document_updated_at: "2026-11-17",
      financial_as_of: "2026-09-30",
      market_as_of: "2026-11-16",
    }),
  ],
};

const companyList: ResearchCompanyList = {
  snapshot_id: "s" + "1".repeat(32),
  generated_at: "2026-11-17T00:00:00Z",
  totals: { companies: 2, reports: 3 },
  companies: [microsoft, qxo],
  diagnostics: { excluded_count: 1, excluded: [{ report_id: "demo", reason: "报告类型 demo 不进入公司研究列表" }], source: { name: "stock-research", commit: "abc", dirty: false } },
};

const detail: ResearchReportDetail = {
  snapshot_id: "s" + "1".repeat(32),
  generated_at: "2026-11-17T00:00:00Z",
  report: report(),
  company: { entity_id: "company:microsoft", name: "Microsoft", aliases: ["MSFT"], securities: microsoft.securities },
  sibling_reports: [report({ report_id: "msft-topic", title: "微软：传统软件与现金流", report_type: "topic" })],
  content_url: "/api/v1/research/content/s11111111111111111111111111111111/MSFT/MSFT.html",
  document_html: "<!doctype html><html><body>报告正文</body></html>",
  unpublished_links: [{ path: "MSFT/factcheck/data/inputs.json", reference: "factcheck/data/inputs.json", reason: "发布范围尚未审核" }],
  external_links: [{ url: "https://www.microsoft.com/investor", label: "微软投资者关系" }],
  diagnostics: [{ code: "script_present", severity: "info", message: "报告包含脚本；静态阅读模式不执行脚本。" }],
  assets: [],
};

function renderList(query = "") {
  const qc = new QueryClient();
  qc.setQueryData(["research-companies"], companyList);
  return stripComments(renderToString(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/research${query ? `?q=${encodeURIComponent(query)}` : ""}`]}>
        <ResearchPage />
      </MemoryRouter>
    </QueryClientProvider>,
  ));
}

function stripComments(html: string): string {
  return html.replace(/<!-- -->/g, "");
}

test("未知日期保持未登记，不使用文件时间替代", () => {
  assert.equal(formatResearchDate(null), "未登记");
  assert.equal(formatResearchDate("2026-11-17"), "2026-11-17");
});

test("报告类型与内容状态分别展示", () => {
  assert.equal(REPORT_TYPE_LABEL.company_research, "主报告");
  assert.equal(REPORT_TYPE_LABEL.topic, "专题");
  assert.equal(REPORT_TYPE_LABEL.update, "局部更新");
  assert.equal(CONTENT_STATUS_LABEL.partial, "有研究缺口");
  assert.equal(CONTENT_STATUS_LABEL.unreviewed, "尚未核验");
  const html = renderToString(<ReportBadges report={report()} />);
  assert.match(html, /主报告/);
  assert.match(html, /草稿/);
  assert.match(html, /尚未核验/);
});

test("搜索覆盖公司名称、别名、证券代码与报告标题", () => {
  assert.equal(filterResearchCompanies([microsoft, qxo], "").length, 2);
  assert.deepEqual(filterResearchCompanies([microsoft, qxo], "微软").map((item) => item.entity_id), ["company:microsoft"]);
  assert.deepEqual(filterResearchCompanies([microsoft, qxo], "MSFT").map((item) => item.entity_id), ["company:microsoft"]);
  assert.deepEqual(filterResearchCompanies([microsoft, qxo], "传统软件").map((item) => item.entity_id), ["company:microsoft"]);
  assert.deepEqual(filterResearchCompanies([microsoft, qxo], "QXO").map((item) => item.entity_id), ["company:qxo"]);
  assert.deepEqual(filterResearchCompanies([microsoft, qxo], "不存在的公司"), []);
});

test("按专题标题搜索保留完整公司，入口始终优先主报告", () => {
  const filtered = filterResearchCompanies([microsoft], "传统软件");
  assert.equal(filtered[0].reports.length, 2);
  assert.equal(filtered[0].report_count, 2);
  assert.equal(companyEntryReport(filtered[0])?.report_id, "msft-main");
});

test("每家公司只有一个入口，不展开报告、别名及财务日期", () => {
  const html = renderList();
  assert.match(html, /Microsoft/);
  assert.match(html, /MSFT/);
  assert.match(html, /共 2 家公司/);
  assert.match(html, /更新 未登记/);
  assert.doesNotMatch(html, /篇报告|别名：|财务截止|行情截止/);
  assert.doesNotMatch(html, /微软：软件现金流与AI资本回报|微软：传统软件与现金流/);
  assert.match(html, /有研究缺口/);
  const entries = [...html.matchAll(/href="\/research\/reports\/([^\"]+)"/g)].map(match => match[1]);
  assert.deepEqual(entries, ["msft-main", "qxo-main"]);
});

test("三篇报告仍只有一个公司入口；只有专题的公司也能进入", () => {
  const topic = report({ report_id: "msft-topic", report_type: "topic" });
  const company = { ...microsoft, reports: [topic, report(), report({ report_id: "msft-experiment", report_type: "topic" })] };
  assert.equal(companyEntryReport(company)?.report_id, "msft-main");
  const topicOnly = { ...microsoft, reports: [topic] };
  assert.equal(companyEntryReport(topicOnly)?.report_id, "msft-topic");
  assert.equal(companyEntryReport({ ...microsoft, reports: [] }), undefined);
});

test("搜索条件保留在 URL 中并随报告链接传递", () => {
  const html = renderList("微软");
  assert.match(html, /href="\/research\/reports\/msft-main\?q=%E5%BE%AE%E8%BD%AF"/);
  assert.match(html, /value="微软"/);
});

test("列表为空时给出明确提示而不是伪装成没有研究", () => {
  const html = renderList("不存在的公司");
  assert.match(html, /没有匹配「不存在的公司」的公司或报告/);
  assert.doesNotMatch(html, /Microsoft/);
});

test("阅读页展示报告信息和阅读准备状态，不展示未发布资料", () => {
  const qc = new QueryClient();
  qc.setQueryData(["research-report", "msft-main"], detail);
  const html = renderToString(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/research/reports/msft-main?q=%E5%BE%AE%E8%BD%AF"]}>
        <Routes>
          <Route path="/research/reports/:reportId" element={<ResearchReportPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const markup = stripComments(html);
  assert.match(markup, /微软：软件现金流与AI资本回报/);
  assert.match(markup, /文档更新日/);
  assert.match(markup, /未登记/);
  assert.match(markup, /同公司其他报告/);
  assert.match(markup, /微软：传统软件与现金流/);
  assert.doesNotMatch(markup, /未发布资料/);
  assert.doesNotMatch(markup, /发布范围尚未审核/);
  assert.doesNotMatch(markup, /外部来源/);
  assert.doesNotMatch(markup, /href="https:\/\/www.microsoft.com\/investor"/);
  assert.match(markup, /新页面阅读/);
  assert.match(markup, /href="\/api\/v1\/research\/content\/s11111111111111111111111111111111\/MSFT\/MSFT.html"/);
  assert.match(markup, /target="_blank" rel="noopener noreferrer"/);
  assert.match(markup, /正在准备报告正文/);
  assert.match(markup, /返回公司研究/);
});

test("外部来源只允许 http/https", () => {
  assert.equal(safeExternalUrl("https://example.com/a"), "https://example.com/a");
  assert.equal(safeExternalUrl("http://example.com/a"), "http://example.com/a");
  assert.equal(safeExternalUrl("javascript:alert(1)"), null);
  assert.equal(safeExternalUrl("file:///etc/passwd"), null);
  assert.equal(safeExternalUrl("data:text/html,<script>"), null);
});
