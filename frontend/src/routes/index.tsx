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

import { getAlerts, getBeds, getReassessQueue, dashboardKeys } from "@/api/dashboard";
import { bandMeta, formatTime, sexLabel } from "@/api/display";
import type { BedItem, BedsResponse } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { bedStatusMeta, incompleteRecords } from "@/lib/mock-data";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "응급실 현황 · ER-GUARD AI" },
      {
        name: "description",
        content: "전체 병상 현황판과 실시간 AI 경고를 한 화면에서 확인하는 응급실 현황 대시보드.",
      },
      { property: "og:title", content: "응급실 현황 · ER-GUARD AI" },
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
      value: `${incompleteRecords.length}건`,
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

      <p className="flex items-center gap-1.5 rounded-md border bg-secondary/50 px-3 py-2 text-xs text-muted-foreground">
        <Info className="size-3.5 shrink-0" />
        환자·활력징후는 MIMIC-IV 실데이터입니다. 병상 배치는 데모 값이며,
        {/* 로딩 중(beds === undefined)에는 근거를 단정하지 않는다 */}
        {beds
          ? beds.summary.pending > 0
            ? ` 병상 색상은 AI 예측 기준이며, 첫 예측 전인 ${beds.summary.pending}병상은 흰색(예측 대기)입니다.`
            : " 병상 색상은 AI 예측 기준입니다."
          : ""}
        &nbsp;기록 미완료 항목은 아직 mock 데이터입니다.
      </p>

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
              <p className="border-t px-4 py-2 text-[11px] leading-relaxed text-muted-foreground">
                지금 🔴 재평가 필요 상태인 재실 환자만 표시합니다(등급·확률은 최신 예측 기준).
                시각은 경보가 켜진 시점이며, 문구는 예측에 기여한 신호로 임상적 인과관계가 아닙니다.
              </p>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="border-b py-3">
              <CardTitle className="text-sm">
                기록 미완료 알림
                <span className="ml-1.5 text-[11px] font-normal text-muted-foreground">(mock)</span>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 pt-3">
              {incompleteRecords.map((r) => (
                <div key={r.patient} className="flex items-start justify-between gap-2 text-sm">
                  {r.patientId ? (
                    <Link
                      to="/records/$patientId"
                      params={{ patientId: r.patientId }}
                      className="font-medium hover:underline"
                    >
                      {r.patient}
                    </Link>
                  ) : (
                    <span className="font-medium">{r.patient}</span>
                  )}
                  <span className="text-right text-xs text-risk-rising">{r.missing}</span>
                </div>
              ))}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
