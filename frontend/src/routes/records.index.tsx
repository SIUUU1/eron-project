import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { createFileRoute, useRouterState } from "@tanstack/react-router";
import { AlertCircle } from "lucide-react";
import { useState } from "react";

import { dischargeLabel, formatDateTime, sexLabel, toPercent, toRiskLevel } from "@/api/display";
import { edStayKeys, getEdStays } from "@/api/ed-stays";
import type { EdStayListItem } from "@/api/types";
import { PatientListMobile } from "@/components/patient-list-mobile";
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

export const Route = createFileRoute("/records/")({
  head: () => ({
    meta: [
      { title: "AI 진료기록 및 누락 검사 · ER-GUARD AI" },
      {
        name: "description",
        content:
          "대화 기반 응급진료기록 작성부터 누락 검사, KCD 코드 추천, 의사 인증까지 이어지는 워크플로우.",
      },
      { property: "og:title", content: "AI 진료기록 및 누락 검사 · ER-GUARD AI" },
      {
        property: "og:description",
        content: "기록 작성 → 누락 검사 → 진단코드 → 최종 기록 → 인증 저장 워크플로우.",
      },
    ],
  }),
  component: RecordsListPage,
});

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
    risk: toRiskLevel(item.risk_level),
    probability: toPercent(item.risk_probability),
    recordStatus:
      item.record_status === "SIGNED"
        ? "인증 완료"
        : item.record_status === "DRAFT"
          ? "임시저장"
          : "미작성",
  };
}

const PAGE_SIZE = 20;

function pageWindow(current: number, last: number): number[] {
  if (last <= 7) return Array.from({ length: last }, (_, i) => i + 1);
  const pages = new Set([1, last, current, current - 1, current + 1]);
  const sorted = [...pages].filter((p) => p >= 1 && p <= last).sort((a, b) => a - b);
  const out: number[] = [];
  let previous = 0;
  for (const page of sorted) {
    if (previous && page - previous > 1) out.push(0);
    out.push(page);
    previous = page;
  }
  return out;
}

function RecordsListPage() {
  const isMobileCompact = useRouterState({
    select: (s) =>
      String((s.location.search as Record<string, unknown> | undefined)?.mobile) === "1",
  });
  const [page, setPage] = useState(1);
  const query = {
    page,
    pageSize: PAGE_SIZE,
    sort: "acuity_mix" as const,
  };

  const { data, isPending, isError, error, refetch, isPlaceholderData } = useQuery({
    queryKey: edStayKeys.list(query),
    queryFn: ({ signal }) => getEdStays(query, signal),
    placeholderData: keepPreviousData,
  });
  const lastPage = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;
  const go = (nextPage: number) => setPage(Math.min(Math.max(nextPage, 1), lastPage));

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">AI 진료기록 및 누락 검사</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          기록을 작성할 환자를 선택하세요. 선택 후 기록 작성 → 누락 검사 → 진단코드 → 최종 기록 →
          인증 저장 순서로 진행됩니다.
        </p>
      </div>

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
              {Array.from({ length: 6 }).map((_, index) => (
                <Skeleton key={index} className="h-10 w-full" />
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
              {isMobileCompact ? (
                <PatientListMobile base="/records" rows={data.items.map(toRow)} />
              ) : (
                <PatientListTable base="/records" rows={data.items.map(toRow)} />
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {!isMobileCompact && data && lastPage > 1 && (
        <Pagination>
          <PaginationContent>
            <PaginationItem>
              <PaginationPrevious
                href="#"
                aria-disabled={page === 1}
                className={page === 1 ? "pointer-events-none opacity-50" : undefined}
                onClick={(event) => {
                  event.preventDefault();
                  go(page - 1);
                }}
              />
            </PaginationItem>
            {pageWindow(page, lastPage).map((pageNumber, index) =>
              pageNumber === 0 ? (
                <PaginationItem key={`gap-${index}`}>
                  <PaginationEllipsis />
                </PaginationItem>
              ) : (
                <PaginationItem key={pageNumber}>
                  <PaginationLink
                    href="#"
                    isActive={pageNumber === page}
                    onClick={(event) => {
                      event.preventDefault();
                      go(pageNumber);
                    }}
                  >
                    {pageNumber}
                  </PaginationLink>
                </PaginationItem>
              ),
            )}
            <PaginationItem>
              <PaginationNext
                href="#"
                aria-disabled={page === lastPage}
                className={page === lastPage ? "pointer-events-none opacity-50" : undefined}
                onClick={(event) => {
                  event.preventDefault();
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
