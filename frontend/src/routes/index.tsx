import { createFileRoute, Link } from "@tanstack/react-router";
import { AlertTriangle, BedDouble, Bell, ClipboardList, RefreshCw, TrendingUp } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  aiAlerts,
  bedStatusMeta,
  bedSummary,
  bedZones,
  incompleteRecords,
  reassessQueue,
  riskMeta,
  summary,
  type Bed,
} from "@/lib/mock-data";

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

const summaryCards = [
  { label: "현재 응급실 환자", value: `${summary.total}명`, icon: BedDouble, tone: "text-primary" },
  {
    label: "즉시 재평가 필요",
    value: `${summary.critical}명`,
    icon: AlertTriangle,
    tone: "text-risk-critical",
  },
  { label: "위험 증가", value: `${summary.rising}명`, icon: TrendingUp, tone: "text-risk-rising" },
  {
    label: "기록 미완료",
    value: `${summary.incompleteRecords}건`,
    icon: ClipboardList,
    tone: "text-risk-watch",
  },
  { label: "오늘 AI 경고", value: `${summary.aiAlertsToday}건`, icon: Bell, tone: "text-mint" },
];

function BedCard({ bed }: { bed: Bed }) {
  const meta = bedStatusMeta[bed.status];
  const inner = (
    <div
      className={`flex h-full flex-col items-center gap-1 rounded-md border px-2 py-2.5 text-center ${meta.card}`}
    >
      <p className="text-sm font-bold text-foreground">{bed.id}</p>
      <BedDouble className={`size-6 ${meta.text}`} strokeWidth={1.8} />
      {bed.status === "empty" ? (
        <p className="mt-1 text-xs text-muted-foreground">빈 병상</p>
      ) : (
        <>
          <p className="whitespace-nowrap text-[13px] font-semibold text-foreground">{bed.name}</p>
          <p className="tabular text-xs text-muted-foreground">
            {bed.age} / {bed.sex}
          </p>
          <div className="mt-0.5 flex min-h-5 items-center justify-center gap-1">
            {bed.devices && bed.devices.length > 0 ? (
              bed.devices.map((d) => (
                <span
                  key={d}
                  className={`inline-flex size-5 items-center justify-center rounded border bg-card text-[10px] font-bold ${meta.text}`}
                >
                  {d}
                </span>
              ))
            ) : (
              <span className="text-xs text-muted-foreground">-</span>
            )}
          </div>
        </>
      )}
    </div>
  );

  if (bed.patientId) {
    return (
      <Link
        to="/monitoring/$patientId"
        params={{ patientId: bed.patientId }}
        className="block transition-transform hover:-translate-y-0.5"
      >
        {inner}
      </Link>
    );
  }
  return inner;
}

function DashboardPage() {
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
                {(["critical", "moderate", "low", "empty"] as const).map((s) => (
                  <span key={s} className="flex items-center gap-1.5 text-muted-foreground">
                    <span
                      className={`h-1 w-4 rounded-full ${
                        s === "critical"
                          ? "bg-risk-critical"
                          : s === "moderate"
                            ? "bg-risk-watch"
                            : s === "low"
                              ? "bg-risk-stable"
                              : "bg-border"
                      }`}
                    />
                    {bedStatusMeta[s].label}
                  </span>
                ))}
              </div>
            </CardHeader>
            <CardContent className="space-y-4 pt-4">
              <div className="grid grid-cols-4 gap-3">
                <div className="rounded-md bg-navy px-4 py-3 text-navy-foreground">
                  <p className="text-xs opacity-75">전체 병상</p>
                  <p className="tabular text-2xl font-bold">{bedSummary.total}</p>
                </div>
                {(
                  [
                    ["critical", bedSummary.critical, "text-risk-critical"],
                    ["moderate", bedSummary.moderate, "text-risk-watch"],
                    ["low", bedSummary.low, "text-risk-stable"],
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
                {bedZones.map((z) => (
                  <div key={z.zone} className="space-y-2">
                    <div className="rounded-md bg-navy py-1.5 text-center text-sm font-semibold text-navy-foreground">
                      {z.zone}
                    </div>
                    <div className="grid grid-cols-6 gap-1.5">
                      {z.beds.map((bed) => (
                        <BedCard key={bed.id} bed={bed} />
                      ))}
                    </div>
                  </div>
                ))}
              </div>

              <div className="flex items-center gap-6 rounded-md border bg-secondary/50 px-4 py-2.5 text-xs">
                <span className="font-semibold">의료기기 표기</span>
                <span className="flex items-center gap-1.5">
                  <span className="inline-flex size-5 items-center justify-center rounded border bg-card font-bold text-risk-critical">
                    E
                  </span>
                  ECMO
                </span>
                <span className="flex items-center gap-1.5">
                  <span className="inline-flex size-5 items-center justify-center rounded border bg-card font-bold text-primary">
                    V
                  </span>
                  인공호흡기
                </span>
                <span className="flex items-center gap-1.5">
                  <span className="inline-flex size-5 items-center justify-center rounded border bg-card font-bold text-risk-stable">
                    C
                  </span>
                  CRRT
                </span>
              </div>
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
                <ul className="divide-y">
                  {aiAlerts.map((a, i) => (
                    <li key={i} className="px-4 py-2.5">
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-semibold">{a.patient}</span>
                        <span className="tabular text-xs text-muted-foreground">{a.time}</span>
                      </div>
                      <p className="mt-0.5 text-xs text-muted-foreground">{a.message}</p>
                      <Badge variant="outline" className={`mt-1.5 ${riskMeta[a.level].badge}`}>
                        {riskMeta[a.level].label}
                      </Badge>
                    </li>
                  ))}
                </ul>
              </ScrollArea>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="border-b py-3">
              <CardTitle className="text-sm">위험 환자 우선순위</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 pt-3">
              {reassessQueue.map((r, i) => (
                <div key={r.patient} className="flex items-center justify-between text-sm">
                  <span className="flex items-center gap-2">
                    <span className="tabular w-4 text-xs text-muted-foreground">{i + 1}</span>
                    <span className={`size-2 rounded-full ${riskMeta[r.risk].dot}`} />
                    {r.patientId ? (
                      <Link
                        to="/monitoring/$patientId"
                        params={{ patientId: r.patientId }}
                        className="font-medium hover:underline"
                      >
                        {r.patient}
                      </Link>
                    ) : (
                      <span className="font-medium">{r.patient}</span>
                    )}
                  </span>
                  <span className={`text-xs font-semibold ${riskMeta[r.risk].text}`}>{r.due}</span>
                </div>
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="border-b py-3">
              <CardTitle className="text-sm">기록 미완료 알림</CardTitle>
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
