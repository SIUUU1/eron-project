import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import {
  AlertCircle,
  AlertTriangle,
  BedDouble,
  Bell,
  ClipboardList,
  Info,
  RefreshCw,
  TrendingUp,
} from "lucide-react";

import {
  getAlerts,
  getBeds,
  getDashboardSummary,
  getReassessQueue,
  dashboardKeys,
} from "@/api/dashboard";
import { formatTime, sexLabel } from "@/api/display";
import type { BedItem } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { bedStatusMeta, incompleteRecords, riskMeta } from "@/lib/mock-data";

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
  const summaryQ = useQuery({
    queryKey: dashboardKeys.summary,
    queryFn: ({ signal }) => getDashboardSummary(signal),
  });
  const bedsQ = useQuery({
    queryKey: dashboardKeys.beds,
    queryFn: ({ signal }) => getBeds(signal),
  });
  const alertsQ = useQuery({
    queryKey: dashboardKeys.alerts,
    queryFn: ({ signal }) => getAlerts(20, signal),
  });
  const reassessQ = useQuery({
    queryKey: dashboardKeys.reassess,
    queryFn: ({ signal }) => getReassessQueue(signal),
  });

  const s = summaryQ.data;
  const beds = bedsQ.data;

  const summaryCards = [
    {
      label: "현재 응급실 환자",
      value: s ? `${s.total}명` : "…",
      icon: BedDouble,
      tone: "text-primary",
    },
    {
      label: "즉시 재평가 필요",
      value: s ? `${s.critical}명` : "…",
      icon: AlertTriangle,
      tone: "text-risk-critical",
    },
    {
      label: "위험 증가",
      value: s ? `${s.rising}명` : "…",
      icon: TrendingUp,
      tone: "text-risk-rising",
    },
    {
      label: "기록 미완료",
      value: `${incompleteRecords.length}건`,
      icon: ClipboardList,
      tone: "text-risk-watch",
    },
    {
      label: "오늘 AI 경고",
      value: s ? `${s.ai_alerts_today}건` : "…",
      icon: Bell,
      tone: "text-mint",
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
          <RefreshCw className="size-3.5" /> 30초마다 자동갱신
        </div>
      </div>

      <p className="flex items-center gap-1.5 rounded-md border bg-secondary/50 px-3 py-2 text-xs text-muted-foreground">
        <Info className="size-3.5 shrink-0" />
        환자·활력징후는 MIMIC-IV 실데이터입니다. 병상 배치는 데모 값이며,
        {/* 로딩 중(beds === undefined)에는 근거를 단정하지 않는다 */}
        {beds
          ? beds.meta.status_source === "triage_acuity"
            ? " 예측 모델이 연동되지 않아 병상 색상은 KTAS 중증도 기준입니다."
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
              <CardTitle className="text-base">병상 현황판</CardTitle>
              <div className="flex items-center gap-3 text-xs">
                {(["critical", "moderate", "low", "empty"] as const).map((st) => (
                  <span key={st} className="flex items-center gap-1.5 text-muted-foreground">
                    <span
                      className={`h-1 w-4 rounded-full ${
                        st === "critical"
                          ? "bg-risk-critical"
                          : st === "moderate"
                            ? "bg-risk-watch"
                            : st === "low"
                              ? "bg-risk-stable"
                              : "bg-border"
                      }`}
                    />
                    {bedStatusMeta[st].label}
                  </span>
                ))}
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
                  <div className="grid grid-cols-4 gap-3">
                    <div className="rounded-md bg-navy px-4 py-3 text-navy-foreground">
                      <p className="text-xs opacity-75">전체 병상</p>
                      <p className="tabular text-2xl font-bold">{beds!.summary.total}</p>
                    </div>
                    {(
                      [
                        ["critical", beds!.summary.critical, "text-risk-critical"],
                        ["moderate", beds!.summary.moderate, "text-risk-watch"],
                        ["low", beds!.summary.low, "text-risk-stable"],
                      ] as const
                    ).map(([k, v, tone]) => (
                      <div key={k} className="rounded-md border bg-card px-4 py-3">
                        <p className="text-xs text-muted-foreground">
                          사용 중 ({bedStatusMeta[k].label})
                        </p>
                        <p className={`tabular text-2xl font-bold ${tone}`}>{v}</p>
                      </div>
                    ))}
                  </div>

                  <div className="grid grid-cols-2 gap-4">
                    {beds!.zones.map((z) => (
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
              <CardTitle className="flex items-center gap-2 text-sm">
                <Bell className="size-4 text-mint" /> 실시간 AI 경고
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              <ScrollArea className="h-56">
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
                    표시할 AI 경고가 없습니다.
                    <br />
                    예측 모델이 연동되면 이곳에 표시됩니다.
                  </p>
                ) : (
                  <ul className="divide-y">
                    {alertsQ.data.items.map((a) => (
                      <li key={a.id} className="px-4 py-2.5">
                        <div className="flex items-center justify-between">
                          <span className="text-sm font-semibold">{a.display_name}</span>
                          <span className="tabular text-xs text-muted-foreground">
                            {formatTime(a.alert_time)}
                          </span>
                        </div>
                        <p className="mt-0.5 text-xs text-muted-foreground">{a.message}</p>
                        <Badge variant="outline" className={`mt-1.5 ${riskMeta[a.level].badge}`}>
                          {riskMeta[a.level].label}
                        </Badge>
                      </li>
                    ))}
                  </ul>
                )}
              </ScrollArea>
            </CardContent>
          </Card>

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
                reassessQ.data.items.slice(0, 4).map((r, i) => {
                  const level = r.risk_level;
                  return (
                    <div key={r.stay_id} className="flex items-center justify-between text-sm">
                      <span className="flex items-center gap-2">
                        <span className="tabular w-4 text-xs text-muted-foreground">{i + 1}</span>
                        <span
                          className={`size-2 rounded-full ${level ? riskMeta[level].dot : "bg-muted-foreground"}`}
                        />
                        <Link
                          to="/monitoring/$patientId"
                          params={{ patientId: r.stay_id }}
                          className="font-medium hover:underline"
                        >
                          {r.display_name ?? `ED-${r.stay_id}`}
                        </Link>
                      </span>
                      <span
                        className={`text-xs font-semibold ${level ? riskMeta[level].text : "text-muted-foreground"}`}
                      >
                        {r.due_label}
                      </span>
                    </div>
                  );
                })
              )}
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
