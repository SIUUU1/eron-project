import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { AlertCircle, Info } from "lucide-react";
import { useState } from "react";

import { dischargeLabel, formatDateTime, sexLabel, toPercent, toRiskBand } from "@/api/display";
import { edStayKeys, getEdStays } from "@/api/ed-stays";
import type { EdStayListItem } from "@/api/types";
import { PatientListTable, type PatientRow } from "@/components/patient-list-table";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Pagination,
  PaginationContent,
  PaginationEllipsis,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import { Skeleton } from "@/components/ui/skeleton";

export const Route = createFileRoute("/monitoring/")({
  head: () => ({
    meta: [
      { title: "환자 모니터링 · ER-GUARD AI" },
      {
        name: "description",
        content: "응급실 재실 환자를 내원시간 순으로 확인하고 악화 예측 상태를 조회합니다.",
      },
      { property: "og:title", content: "환자 모니터링 · ER-GUARD AI" },
      {
        property: "og:description",
        content: "응급실 재실 환자 목록과 악화 예측 상태를 확인하는 모니터링 화면.",
      },
    ],
  }),
  component: MonitoringListPage,
});

const PAGE_SIZE = 20;

function toRow(item: EdStayListItem): PatientRow {
  return {
    id: item.stay_id,
    name: item.display_name ?? `ED-${item.stay_id}`,
    sex: sexLabel(item.sex),
    age: item.age,
    arrivedLabel: formatDateTime(item.arrived_at),
    discharge: dischargeLabel(item.discharge_type),
    ktas: item.acuity,
    chiefComplaint: item.chief_complaint ?? "-",
    risk: toRiskBand(item.risk_band),
    probability: toPercent(item.risk_probability),
    reviewed: item.reviewed,
    recordStatus:
      item.record_status === "SIGNED"
        ? "인증 완료"
        : item.record_status === "DRAFT"
          ? "임시저장"
          : "미작성",
  };
}

/** 1 … c-1 c c+1 … last 형태로 줄인다. 0 은 생략 표시. */
function pageWindow(current: number, last: number): number[] {
  if (last <= 7) return Array.from({ length: last }, (_, i) => i + 1);
  const pages = new Set([1, last, current, current - 1, current + 1]);
  const sorted = [...pages].filter((p) => p >= 1 && p <= last).sort((a, b) => a - b);
  const out: number[] = [];
  let prev = 0;
  for (const p of sorted) {
    if (prev && p - prev > 1) out.push(0);
    out.push(p);
    prev = p;
  }
  return out;
}

function MonitoringListPage() {
  const [page, setPage] = useState(1);
  const query = { page, pageSize: PAGE_SIZE, sort: "acuity_mix" as const };

  const { data, isPending, isError, error, refetch, isPlaceholderData } = useQuery({
    queryKey: edStayKeys.list(query),
    queryFn: ({ signal }) => getEdStays(query, signal),
    placeholderData: keepPreviousData,
  });

  const lastPage = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;
  const go = (p: number) => setPage(Math.min(Math.max(p, 1), lastPage));

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">환자 모니터링</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          응급실 재실 환자의 현재 위험도와 AI 악화 예측 확률을 확인합니다.
        </p>
      </div>

      {data && !data.meta.model_connected && (
        <p className="flex items-center gap-1.5 rounded-md border bg-secondary/50 px-3 py-2 text-xs text-muted-foreground">
          <Info className="size-3.5 shrink-0" />
          악화 예측 모델이 연동되지 않아 위험도·확률이 비어 있습니다. 환자·활력징후 정보는 실제
          MIMIC-IV 데이터이며, 환자명은 비식별 처리된 표기입니다.
        </p>
      )}

      <Card>
        <CardHeader className="border-b py-3">
          <CardTitle className="text-base">
            응급실 환자 목록
            {data ? (
              <span className="ml-2 text-sm font-normal text-muted-foreground">
                전체 {data.total}명 · {(data.page - 1) * data.page_size + 1}–
                {(data.page - 1) * data.page_size + data.items.length}
              </span>
            ) : null}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isPending ? (
            <div className="space-y-2 p-4">
              {Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </div>
          ) : isError ? (
            <div className="flex flex-col items-center gap-3 p-10 text-center">
              <AlertCircle className="size-8 text-risk-critical" />
              <p className="text-sm font-medium">환자 목록을 불러오지 못했습니다</p>
              <p className="text-xs text-muted-foreground">{error.message}</p>
              <Button size="sm" variant="outline" onClick={() => void refetch()}>
                다시 시도
              </Button>
            </div>
          ) : data.items.length === 0 ? (
            <p className="p-10 text-center text-sm text-muted-foreground">
              재실 중인 환자가 없습니다.
            </p>
          ) : (
            <div className={isPlaceholderData ? "opacity-60 transition-opacity" : undefined}>
              <PatientListTable base="/monitoring" rows={data.items.map(toRow)} />
            </div>
          )}
        </CardContent>
      </Card>

      {data && lastPage > 1 && (
        <Pagination>
          <PaginationContent>
            <PaginationItem>
              <PaginationPrevious
                href="#"
                aria-disabled={page === 1}
                className={page === 1 ? "pointer-events-none opacity-50" : undefined}
                onClick={(e) => {
                  e.preventDefault();
                  go(page - 1);
                }}
              />
            </PaginationItem>

            {pageWindow(page, lastPage).map((p, i) =>
              p === 0 ? (
                <PaginationItem key={`gap-${i}`}>
                  <PaginationEllipsis />
                </PaginationItem>
              ) : (
                <PaginationItem key={p}>
                  <PaginationLink
                    href="#"
                    isActive={p === page}
                    onClick={(e) => {
                      e.preventDefault();
                      go(p);
                    }}
                  >
                    {p}
                  </PaginationLink>
                </PaginationItem>
              ),
            )}

            <PaginationItem>
              <PaginationNext
                href="#"
                aria-disabled={page === lastPage}
                className={page === lastPage ? "pointer-events-none opacity-50" : undefined}
                onClick={(e) => {
                  e.preventDefault();
                  go(page + 1);
                }}
              />
            </PaginationItem>
          </PaginationContent>
        </Pagination>
      )}
    </div>
  );
}
