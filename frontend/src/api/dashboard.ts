import { apiGet, buildQuery } from "@/api/client";
import type { AlertsResponse, BedsResponse, DashboardSummary, ReassessResponse } from "@/api/types";

export function getDashboardSummary(signal?: AbortSignal) {
  return apiGet<DashboardSummary>("/api/ed/dashboard/summary", signal);
}

export function getBeds(signal?: AbortSignal) {
  return apiGet<BedsResponse>("/api/ed/dashboard/beds", signal);
}

export function getAlerts(limit = 20, signal?: AbortSignal) {
  return apiGet<AlertsResponse>(`/api/ed/alerts${buildQuery({ limit })}`, signal);
}

export function getReassessQueue(signal?: AbortSignal) {
  return apiGet<ReassessResponse>("/api/ed/reassess-queue", signal);
}

export const dashboardKeys = {
  summary: ["ed", "dashboard", "summary"] as const,
  beds: ["ed", "dashboard", "beds"] as const,
  alerts: ["ed", "alerts"] as const,
  reassess: ["ed", "reassess"] as const,
};
