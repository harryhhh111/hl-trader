import { apiFetch } from "./client";
import type { ResearchCompanyList, ResearchReportDetail } from "@/lib/types/research";

export const researchApi = {
  companies: () => apiFetch<ResearchCompanyList>("/api/v1/research/companies"),
  report: (reportId: string, snapshot?: string) =>
    apiFetch<ResearchReportDetail>(`/api/v1/research/reports/${encodeURIComponent(reportId)}${snapshot ? `?snapshot=${encodeURIComponent(snapshot)}` : ""}`),
};
