import { Info } from "lucide-react";

import type { MetricTargets, OperationalMetrics, ReferenceMetrics } from "@/api/types";
import { ReferenceBadge } from "@/components/model-monitoring/badges";
import { metric } from "@/components/model-monitoring/shared";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

/**
 * 최신 운영 성능 KPI.
 *
 * 🔑 타일 아래의 기준선은 **목표값**이다. 개발 단계 holdout 성능이 아니다.
 *    holdout 을 여기 붙이면 "운영 성능이 참조보다 좋다/나쁘다" 로 읽히는데, 두 값은
 *    모집단이 달라 비교 대상이 아니다. 참조 성능은 아래 별도 Section 에만 둔다.
 * 🔑 표본이 부족하면 값이 null 로 오고 화면에는 N/A 가 나온다. 0 을 쓰지 않는다.
 */

const KPIS = [
  { key: "pr_auc", label: "PR-AUC", hint: "보정 확률 원값 기준. threshold 와 무관하다." },
  { key: "auroc", label: "AUROC", hint: "보정 확률 원값 기준. threshold 와 무관하다." },
  { key: "recall", label: "Recall", hint: "실제 악화 중 경보를 낸 비율. threshold 를 적용한 값." },
  { key: "precision", label: "Precision", hint: "경보 중 실제 악화였던 비율." },
  { key: "f1", label: "F1", hint: "Recall 과 Precision 의 조화평균." },
] as const;

export function ModelPerformanceSummary({
  operational,
  targets,
  isLoading,
}: {
  operational?: OperationalMetrics | undefined;
  targets?: MetricTargets | undefined;
  isLoading: boolean;
}) {
  if (isLoading || !operational || !targets) {
    return (
      <div className="grid grid-cols-5 gap-3">
        {KPIS.map((kpi) => (
          <Card key={kpi.key}>
            <CardHeader className="border-b py-2.5">
              <Skeleton className="mx-auto h-3.5 w-16" />
            </CardHeader>
            <CardContent className="px-4 py-5">
              <Skeleton className="mx-auto h-8 w-24" />
              <Skeleton className="mx-auto mt-2.5 h-3 w-28" />
            </CardContent>
          </Card>
        ))}
      </div>
    );
  }

  return (
    <TooltipProvider>
      <div className="grid grid-cols-5 gap-3">
        {KPIS.map((kpi) => {
          const value = operational[kpi.key];
          const target = targets[kpi.key];
          // 목표 대비 판정은 값이 있을 때만 한다. N/A 를 미달로 칠하지 않는다.
          const delta = value === null ? null : value - target;
          const met = delta !== null && delta >= 0;
          return (
            <Card key={kpi.key} className="overflow-hidden">
              <CardHeader className="flex-row items-center justify-center gap-1 border-b py-2.5">
                <CardTitle className="text-sm font-bold tracking-wide">{kpi.label}</CardTitle>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Info className="size-3 text-muted-foreground opacity-60" />
                  </TooltipTrigger>
                  <TooltipContent className="max-w-64 text-xs">{kpi.hint}</TooltipContent>
                </Tooltip>
              </CardHeader>
              <CardContent className="px-4 py-5 text-center">
                <p
                  className={`tabular text-3xl font-bold leading-none ${
                    value === null ? "text-muted-foreground" : "text-foreground"
                  }`}
                >
                  {metric(value)}
                </p>
                <p className="mt-2.5 flex items-center justify-center gap-1.5 text-[11px]">
                  {delta === null ? (
                    <span className="text-muted-foreground">목표 대비 산출 불가</span>
                  ) : (
                    <span
                      className={`tabular font-semibold ${
                        met ? "text-risk-stable" : "text-risk-watch"
                      }`}
                    >
                      {met ? "↑" : "↓"}
                      {Math.abs(delta).toFixed(3)}
                    </span>
                  )}
                  <span className="text-muted-foreground">목표 {target.toFixed(2)}</span>
                </p>
              </CardContent>
            </Card>
          );
        })}
      </div>
    </TooltipProvider>
  );
}

/** 개발 단계 temporal holdout 성능 — 참조용. 운영 성능과 반드시 구분한다. */
export function ReferencePerformance({ reference }: { reference?: ReferenceMetrics | undefined }) {
  if (!reference) return null;
  const rows = [
    { label: "PR-AUC", value: reference.pr_auc },
    { label: "AUROC", value: reference.auroc },
    { label: "Recall", value: reference.recall },
    { label: "Precision", value: reference.precision },
    { label: "F1", value: reference.f1 },
  ];

  return (
    <Card className="border-dashed">
      <CardHeader className="flex-row items-center justify-between gap-2 border-b py-3">
        <CardTitle className="text-base">개발 단계 Temporal Holdout 참고 성능</CardTitle>
        <ReferenceBadge />
      </CardHeader>
      <CardContent className="px-5 py-4">
        <div className="grid grid-cols-5 gap-4">
          {rows.map((row) => (
            <div key={row.label} className="text-center">
              <p className="text-xs text-muted-foreground">{row.label}</p>
              <p className="tabular mt-1 text-lg font-semibold text-muted-foreground">
                {metric(row.value)}
              </p>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
