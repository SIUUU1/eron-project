import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useState } from "react";
import {
  AlertCircle,
  AlertTriangle,
  BedDouble,
  Bell,
  Check,
  ClipboardList,
  Info,
  RefreshCw,
  ShieldCheck,
  TrendingUp,
} from "lucide-react";

import {
  getAlerts,
  getBeds,
  getIncompleteRecords,
  getReassessQueue,
  dashboardKeys,
} from "@/api/dashboard";
import { bandMeta, formatTime, sexLabel } from "@/api/display";
import type { BedItem, BedsResponse, ClinicalRecordRequiredField } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { bedStatusMeta } from "@/lib/mock-data";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      {
        name: "description",
        content: "전체 병상 현황판과 실시간 AI 경고를 한 화면에서 확인하는 응급실 현황 대시보드.",
      },
      { property: "og:title", content: "응급실 현황 · ER:ON(이로운)" },
      {
        property: "og:description",
        content: "전체 병상 현황판과 실시간 AI 경고를 확인하는 응급실 현황 대시보드.",
      },
    ],
  }),
  component: DashboardPage,
});

type BedZoneItem = BedsResponse["zones"][number];

/**
 * 자동 갱신 주기. 헤더의 "30초마다 자동갱신" 표기와 같은 값을 쓴다.
 * 데모 시계가 흐르면 퇴실·경보·위험도가 바뀌므로 화면이 따라가야 한다.
 */
const REFRESH_MS = 30_000;

const incompleteRecordFieldLabels: Record<ClinicalRecordRequiredField, string> = {
  chief_complaint: "주호소",
  pain_assessment: "통증평가",
  history_of_present_illness: "현병력",
  past_history: "과거력",
  medications: "복용약",
  allergy: "알레르기",
  social_history: "사회력",
  review_of_systems: "계통문진",
  physical_examination: "신체검진",
  outcome: "응급진료결과",
};

function formatMissingRecordFields(fields: ClinicalRecordRequiredField[]): string {
  const firstField = fields[0];
  if (!firstField) return "필수 항목 누락";

  const firstLabel = incompleteRecordFieldLabels[firstField];
  if (fields.length === 1) return `${firstLabel} 누락`;

  return `${firstLabel} 외 ${fields.length - 1}개 항목 누락`;
}

/** 현황판 페이지 구성 — 1페이지 48병상, 2페이지 36병상 (전체 84병상). */
const BED_PAGE_SIZES = [48, 36];

/**
 * 구역 단위로 페이지를 나눈다. 한 구역이 두 페이지에 걸치지 않게 하기 위해
 * 병상 수가 목표치에 도달할 때까지 구역을 통째로 담는다.
 * 병상이 늘어 정의된 페이지를 넘치면 마지막 페이지에 붙인다(잘려나가지 않게).
 */
function paginateZones(zones: BedZoneItem[]): BedZoneItem[][] {
  const pages: BedZoneItem[][] = [];
  const remaining = [...zones];

  for (const size of BED_PAGE_SIZES) {
    const page: BedZoneItem[] = [];
    let beds = 0;
    while (remaining.length > 0 && beds < size) {
      const zone = remaining.shift();
      if (!zone) break;
      page.push(zone);
      beds += zone.beds.length;
    }
    if (page.length > 0) pages.push(page);
  }

  if (remaining.length > 0) {
    const last = pages[pages.length - 1];
    if (last) last.push(...remaining);
    else pages.push(remaining);
  }
  return pages;
}

function BedCard({ bed }: { bed: BedItem }) {
  const meta = bedStatusMeta[bed.status];
  const inner = (
    <div
      className={`flex h-full flex-col items-center gap-1 rounded-md border px-2 py-2.5 text-center ${meta.card}`}
    >
      <p className="text-sm font-bold text-foreground">{bed.bed_id}</p>
      <BedDouble className={`size-6 ${meta.text}`} strokeWidth={1.8} />
      {bed.status === "empty" ? (
        <p className="mt-1 text-xs text-muted-foreground">빈 병상</p>
      ) : (
        <>
          <p className="whitespace-nowrap text-[13px] font-semibold text-foreground">
            {bed.display_name}
          </p>
          <p className="tabular text-xs text-muted-foreground">
            {bed.age ?? "-"} / {bed.sex ?? "-"}
          </p>
        </>
      )}
    </div>
  );

  if (bed.stay_id) {
    return (
      <Link
        to="/monitoring/$patientId"
        params={{ patientId: bed.stay_id }}
        className="block transition-transform hover:-translate-y-0.5"
      >
        {inner}
      </Link>
    );
  }
  return inner;
}

function DashboardPage() {
  const bedsQ = useQuery({
    queryKey: dashboardKeys.beds,
    queryFn: ({ signal }) => getBeds(signal),
    refetchInterval: REFRESH_MS,
  });
  // 🔴 지금 재평가 필요 상태인 재실 환자만, **환자당 최신 알림 1건**.
  // 최신 예측이 재평가 필요가 아니면(과거에 그랬어도) 서버가 빼고 준다.
  const alertsQ = useQuery({
    queryKey: dashboardKeys.alerts("red", true),
    queryFn: ({ signal }) => getAlerts(20, "red", true, signal),
    refetchInterval: REFRESH_MS,
  });
  const reassessQ = useQuery({
    queryKey: dashboardKeys.reassess,
    queryFn: ({ signal }) => getReassessQueue(signal),
    refetchInterval: REFRESH_MS,
  });
  const incompleteRecordsQ = useQuery({
    queryKey: dashboardKeys.incompleteRecords,
    queryFn: ({ signal }) => getIncompleteRecords(5, signal),
    refetchInterval: REFRESH_MS,
  });

  const beds = bedsQ.data;

  const [bedPage, setBedPage] = useState(0);
  const bedPages = beds ? paginateZones(beds.zones) : [];
  const pageIndex = Math.min(bedPage, Math.max(bedPages.length - 1, 0));
  const currentZones = bedPages[pageIndex] ?? [];
  const currentBedCount = currentZones.reduce((n, z) => n + z.beds.length, 0);

  // 병상 요약은 현황판 카드 안이 아니라 상단 카드에서 한 번만 보여준다.
  const summaryCards = [
    {
      // 분자는 '지금 병상에 있는 환자 수' = 전체 − 빈 병상. 별도 카운트를 만들지 않는다.
      label: "전체 병상",
      value: beds ? `${beds.summary.total - beds.summary.empty} / ${beds.summary.total}` : "…",
      icon: BedDouble,
      tone: "text-primary",
    },
    {
      label: `사용 중 (${bedStatusMeta.critical.label})`,
      value: beds ? `${beds.summary.critical}` : "…",
      icon: AlertTriangle,
      tone: "text-risk-critical",
    },
    {
      label: `사용 중 (${bedStatusMeta.moderate.label})`,
      value: beds ? `${beds.summary.moderate}` : "…",
      icon: TrendingUp,
      tone: "text-risk-watch",
    },
    {
      label: `사용 중 (${bedStatusMeta.low.label})`,
      value: beds ? `${beds.summary.low}` : "…",
      icon: ShieldCheck,
      tone: "text-risk-stable",
    },
    {
      label: "기록 미완료",
      value: incompleteRecordsQ.data ? `${incompleteRecordsQ.data.count}건` : "…",
      icon: ClipboardList,
      tone: "text-risk-watch",
    },
  ];

  return (
    <div className="space-y-5">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">ER 전체 병상 현황</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            응급실 병상 배치와 환자 위험도를 실시간으로 확인합니다.
          </p>
        </div>
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <RefreshCw className="size-3.5" /> {REFRESH_MS / 1000}초마다 자동갱신
        </div>
      </div>

      <div className="grid grid-cols-5 gap-4">
        {summaryCards.map((c) => (
          <Card key={c.label}>
            <CardContent className="flex items-center justify-between px-5 py-4">
              <div>
                <p className="text-xs text-muted-foreground">{c.label}</p>
                <p className={`tabular mt-1 text-2xl font-bold ${c.tone}`}>{c.value}</p>
              </div>
              <c.icon className={`size-7 opacity-25 ${c.tone}`} />
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="grid grid-cols-[1fr_320px] gap-5">
        <div className="space-y-4">
          <Card>
            <CardHeader className="flex-row items-center justify-between border-b py-3">
              <CardTitle className="text-base">
                병상 현황판
                {bedPages.length > 0 ? (
                  <span className="ml-2 text-sm font-normal text-muted-foreground">
                    {currentBedCount}병상 · {pageIndex + 1}/{bedPages.length} 페이지
                  </span>
                ) : null}
              </CardTitle>
              <div className="flex items-center gap-3 text-xs">
                {(["critical", "moderate", "low", "pending", "empty"] as const).map((st) => (
                  <span key={st} className="flex items-center gap-1.5 text-muted-foreground">
                    <span
                      className={`h-1 w-4 rounded-full ${
                        st === "critical"
                          ? "bg-risk-critical"
                          : st === "moderate"
                            ? "bg-risk-watch"
                            : st === "low"
                              ? "bg-risk-stable"
                              : st === "pending"
                                ? "border border-border bg-card"
                                : "bg-border"
                      }`}
                    />
                    {bedStatusMeta[st].label}
                  </span>
                ))}
                {bedPages.length > 1 ? (
                  <div className="flex items-center gap-1 border-l pl-3">
                    {bedPages.map((page, i) => (
                      <Button
                        key={page[0]?.zone ?? i}
                        size="sm"
                        variant={i === pageIndex ? "secondary" : "ghost"}
                        className="h-6 px-2 text-xs"
                        onClick={() => setBedPage(i)}
                      >
                        {i + 1}
                      </Button>
                    ))}
                  </div>
                ) : null}
              </div>
            </CardHeader>
            <CardContent className="space-y-4 pt-4">
              {bedsQ.isPending ? (
                <Skeleton className="h-96 w-full" />
              ) : bedsQ.isError ? (
                <div className="flex flex-col items-center gap-2 py-16 text-center">
                  <AlertCircle className="size-7 text-risk-critical" />
                  <p className="text-sm">병상 현황을 불러오지 못했습니다</p>
                  <p className="text-xs text-muted-foreground">{bedsQ.error.message}</p>
                </div>
              ) : (
                <>
                  <div className="grid grid-cols-2 gap-4">
                    {currentZones.map((z) => (
                      <div key={z.zone} className="space-y-2">
                        <div className="rounded-md bg-navy py-1.5 text-center text-sm font-semibold text-navy-foreground">
                          {z.zone}
                        </div>
                        <div className="grid grid-cols-6 gap-1.5">
                          {z.beds.map((bed) => (
                            <BedCard key={bed.bed_id} bed={bed} />
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                </>
              )}
            </CardContent>
          </Card>
        </div>

        <div className="space-y-4">
          <Card>
            <CardHeader className="border-b py-3">
              <CardTitle className="text-sm">위험 환자 우선순위</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 pt-3">
              {reassessQ.isPending ? (
                <Skeleton className="h-24 w-full" />
              ) : reassessQ.isError ? (
                <p className="py-4 text-center text-xs text-muted-foreground">
                  불러오지 못했습니다
                </p>
              ) : reassessQ.data.items.length === 0 ? (
                <p className="py-4 text-center text-xs text-muted-foreground">
                  대상 환자가 없습니다
                </p>
              ) : (
                reassessQ.data.items.slice(0, 5).map((r, i) => {
                  const band = r.risk_band;
                  return (
                    <div key={r.stay_id} className="flex items-center justify-between text-sm">
                      <span className="flex items-center gap-2">
                        <span className="tabular w-4 text-xs text-muted-foreground">{i + 1}</span>
                        <span
                          className={`size-2 rounded-full ${band ? bandMeta[band].dot : "bg-muted-foreground"}`}
                        />
                        <Link
                          to="/monitoring/$patientId"
                          params={{ patientId: r.stay_id }}
                          className="font-medium hover:underline"
                        >
                          {r.display_name ?? `ED-${r.stay_id}`}
                          <span className="tabular ml-1 text-xs font-normal text-muted-foreground">
                            ({r.stay_id})
                          </span>
                        </Link>
                      </span>
                      <span
                        className={`tabular text-xs font-semibold ${band ? bandMeta[band].text : "text-muted-foreground"}`}
                      >
                        {r.bed_id ?? "-"}
                      </span>
                    </div>
                  );
                })
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="border-b py-3">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Bell className="size-4 text-mint" /> 실시간 AI 경고
                {/* 안내문구는 항상 노출하지 않고 이 아이콘에 hover 했을 때만 띄운다 */}
                <TooltipProvider delayDuration={100}>
                  <Tooltip>
                    <TooltipTrigger
                      type="button"
                      aria-label="실시간 AI 경고 안내"
                      className="-ml-1 rounded-full text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    >
                      <Info className="size-3.5" />
                    </TooltipTrigger>
                    <TooltipContent
                      side="top"
                      align="start"
                      collisionPadding={12}
                      className="max-w-xs text-[11px] font-normal leading-relaxed"
                    >
                      지금 🔴 재평가 필요 상태인 재실 환자만 표시합니다(등급·확률은 최신 예측 기준).
                      시각은 경보가 켜진 시점이며, 문구는 예측에 기여한 신호로 임상적 인과관계가
                      아닙니다.
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
                <Badge variant="outline" className={`ml-auto ${bandMeta.red.badge}`}>
                  <span className={`mr-1 size-1.5 rounded-full ${bandMeta.red.dot}`} />
                  {bandMeta.red.label}
                </Badge>
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              {/* 알림 수가 적을 때 빈 공간이 생기지 않도록 고정 높이 대신 max-height 를 쓴다 */}
              <div className="max-h-72 overflow-y-auto">
                {alertsQ.isPending ? (
                  <div className="space-y-2 p-4">
                    <Skeleton className="h-12 w-full" />
                    <Skeleton className="h-12 w-full" />
                  </div>
                ) : alertsQ.isError ? (
                  <p className="p-6 text-center text-xs text-muted-foreground">
                    경고를 불러오지 못했습니다
                  </p>
                ) : alertsQ.data.items.length === 0 ? (
                  <p className="px-4 py-10 text-center text-xs leading-relaxed text-muted-foreground">
                    {alertsQ.data.meta.model_connected ? (
                      <>
                        현재 🔴 재평가 필요 상태인 재실 환자가 없습니다.
                        <br />
                        병상 현황판의 빨간 병상과 같은 기준입니다.
                      </>
                    ) : (
                      <>
                        표시할 AI 경고가 없습니다.
                        <br />
                        예측 모델이 연동되면 이곳에 표시됩니다.
                      </>
                    )}
                  </p>
                ) : (
                  <ul className="divide-y">
                    {alertsQ.data.items.map((a) => (
                      <li key={a.id} className="px-4 py-2.5">
                        <div className="flex items-center justify-between">
                          <Link
                            to="/monitoring/$patientId"
                            params={{ patientId: a.stay_id }}
                            className="text-sm font-semibold hover:underline"
                          >
                            {a.display_name ?? `ED-${a.stay_id}`}
                            <span className="tabular ml-1 text-xs font-normal text-muted-foreground">
                              ({a.stay_id})
                            </span>
                          </Link>
                          <span className="tabular text-xs text-muted-foreground">
                            {formatTime(a.alert_time)}
                          </span>
                        </div>
                        {/* 모델이 만든 기여 신호 문장. 프론트에서 문구를 만들지 않는다. */}
                        <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                          {a.message}
                        </p>
                        <div className="mt-1.5 flex items-center gap-2">
                          {a.band ? (
                            <Badge variant="outline" className={bandMeta[a.band].badge}>
                              <span
                                className={`mr-1 size-1.5 rounded-full ${bandMeta[a.band].dot}`}
                              />
                              {bandMeta[a.band].label}
                            </Badge>
                          ) : null}
                          {a.risk_probability !== null ? (
                            <span className="tabular text-xs text-muted-foreground">
                              악화 확률 {(a.risk_probability * 100).toFixed(1)}%
                            </span>
                          ) : null}
                          {a.acknowledged_at ? (
                            <span
                              className="ml-auto flex items-center gap-0.5 text-xs text-risk-stable"
                              title="의료진 재검토 완료"
                            >
                              <Check className="size-3.5" /> 확인
                            </span>
                          ) : null}
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex flex-row items-center justify-between border-b py-3">
              <CardTitle className="text-sm">기록 미완료 알림</CardTitle>
              {incompleteRecordsQ.data &&
              incompleteRecordsQ.data.count > incompleteRecordsQ.data.items.length ? (
                <Link
                  to="/records"
                  aria-label="기록 미완료 전체 목록 보기"
                  className="text-xs font-medium text-muted-foreground hover:text-foreground hover:underline"
                >
                  더보기 &gt;
                </Link>
              ) : null}
            </CardHeader>
            <CardContent className="pt-3">
              {incompleteRecordsQ.isPending ? (
                <div className="space-y-2">
                  <Skeleton className="h-5 w-full" />
                  <Skeleton className="h-5 w-full" />
                  <Skeleton className="h-5 w-full" />
                </div>
              ) : incompleteRecordsQ.isError ? (
                <p className="py-6 text-center text-xs text-muted-foreground">
                  기록 미완료 정보를 불러오지 못했습니다
                </p>
              ) : incompleteRecordsQ.data.items.length === 0 ? (
                <p className="py-6 text-center text-xs text-muted-foreground">
                  기록 미완료 환자가 없습니다
                </p>
              ) : (
                <div className="max-h-48 space-y-2 overflow-y-auto">
                  {incompleteRecordsQ.data.items.map((record) => (
                    <div
                      key={record.stay_id}
                      className="flex items-start justify-between gap-2 text-sm"
                    >
                      <Link
                        to="/records/$patientId"
                        params={{ patientId: record.stay_id }}
                        className="font-medium hover:underline"
                      >
                        {record.display_name
                          ? `${record.display_name}(${record.stay_id})`
                          : `ED-${record.stay_id}`}
                      </Link>
                      <span className="text-right text-xs text-risk-rising">
                        {record.reason === "RECORD_NOT_CREATED"
                          ? "기록 미작성"
                          : formatMissingRecordFields(record.missing_fields)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
