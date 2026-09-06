import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { useState } from "react";

import { formatDateTime, formatTime } from "@/api/display";
import {
  getModelConfusionMatrix,
  getModelStatus,
  getModelSummary,
  getModelThresholds,
  getModelTrends,
  modelMonitoringKeys,
} from "@/api/model-monitoring";
import { ConfusionMatrix } from "@/components/model-monitoring/ConfusionMatrix";
import { EvaluationBreakdownChart } from "@/components/model-monitoring/DistributionCharts";
import { EvaluationStatusCard } from "@/components/model-monitoring/EvaluationStatusCard";
import {
  ModelPerformanceSummary,
  ReferencePerformance,
} from "@/components/model-monitoring/ModelPerformanceSummary";
import { PerformanceTrendChart } from "@/components/model-monitoring/PerformanceTrendChart";
import { TREND_RANGES, trendWindow, type TrendRange } from "@/components/model-monitoring/shared";
import { ThresholdTable } from "@/components/model-monitoring/ThresholdTable";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

export const Route = createFileRoute("/model-monitoring")({
  head: () => ({
    meta: [
      {
        name: "description",
        content:
          "응급실 환자 악화 예측 모델의 최신 운영 성능과 평가 진행 상태를 확인하는 모니터링 화면.",
      },
      { property: "og:title", content: "AI 모델 성능 모니터링 · ER:ON(이로운)" },
      {
        property: "og:description",
        content: "악화 예측 모델의 운영 성능과 평가 상태를 확인합니다.",
      },
    ],
  }),
  component: ModelMonitoringPage,
});

/**
 * 화면에 표시할 모델 버전.
 *
 * ⚠ API(`model_info.model_version`)가 돌려주는 실제 배포 버전과 다르다. 표시 전용 값이며
 *   백엔드·모델 파일·DB(app.prediction.model_version)는 그대로다. 값을 두 곳(상단 타일 ·
 *   모델 정보)에 쓰므로 상수 하나로 묶어 둔다.
 */
const DISPLAY_MODEL_VERSION = "1.0.0";

/** 자동 갱신 주기. 응급실 현황과 같은 값을 쓴다. */
const REFRESH_MS = 30_000;
const TRENDS_REFRESH_MS = 60_000;

function ModelMonitoringPage() {
  const [range, setRange] = useState<TrendRange>("24h");

  const summaryQ = useQuery({
    queryKey: modelMonitoringKeys.summary,
    queryFn: ({ signal }) => getModelSummary(signal),
    refetchInterval: REFRESH_MS,
  });
  const statusQ = useQuery({
    queryKey: modelMonitoringKeys.status,
    queryFn: ({ signal }) => getModelStatus(signal),
    refetchInterval: REFRESH_MS,
  });
  // 조회 범위는 데모 시각 기준이다. demo_now 는 /status 응답에 이미 들어 있으므로
  // 시계를 얻으려고 API 를 더 부르지 않는다. 아직 못 받았으면 서버 기본값에 맡긴다.
  const trendRange = trendWindow(statusQ.data?.demo_now, range);
  const trendsQ = useQuery({
    queryKey: modelMonitoringKeys.trends(TREND_RANGES[range].interval, range, trendRange.start),
    queryFn: ({ signal }) =>
      getModelTrends(TREND_RANGES[range].interval, trendRange.start, trendRange.end, signal),
    refetchInterval: TRENDS_REFRESH_MS,
  });
  const confusionQ = useQuery({
    queryKey: modelMonitoringKeys.confusion(),
    queryFn: ({ signal }) => getModelConfusionMatrix(undefined, signal),
    refetchInterval: REFRESH_MS,
  });
  const thresholdsQ = useQuery({
    queryKey: modelMonitoringKeys.thresholds,
    queryFn: ({ signal }) => getModelThresholds(signal),
    refetchInterval: REFRESH_MS,
  });
  const summary = summaryQ.data;
  const coverage = summary?.label_coverage;
  const info = summary?.model_info;

  // 평가 완료분이 하나도 없으면 KPI 를 보여줄 근거가 없다.
  const hasEvaluated = (coverage?.evaluated ?? 0) > 0;
  const insufficient = summary?.operational.status === "insufficient_data";

  if (summaryQ.isError) {
    return (
      <div className="space-y-5">
        <PageHeader />
        <Card>
          <CardContent className="flex flex-col items-center gap-3 py-16">
            <AlertTriangle className="size-8 text-risk-critical opacity-60" />
            <p className="text-sm text-muted-foreground">모델 성능 데이터를 불러오지 못했습니다.</p>
            <Button variant="outline" size="sm" onClick={() => void summaryQ.refetch()}>
              재시도
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex items-end justify-between">
        <PageHeader />
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <RefreshCw className="size-3.5" />
          마지막 업데이트:{" "}
          {formatTime(
            summaryQ.dataUpdatedAt ? new Date(summaryQ.dataUpdatedAt).toISOString() : null,
          )}
        </div>
      </div>

      {/* 상단 — 버전 · 마지막 평가 · 평가 데이터 */}
      <div className="grid grid-cols-3 gap-4">
        <SummaryTile
          label="모델 버전"
          value={info ? `v${DISPLAY_MODEL_VERSION}` : undefined}
          sub={info?.algorithm}
        />
        <SummaryTile
          label="마지막 평가"
          value={summary ? formatDateTime(summary.last_evaluated_at) : undefined}
          sub={statusQ.data ? `데모 시각 ${formatDateTime(statusQ.data.demo_now)}` : undefined}
        />
        <SummaryTile
          label="평가 데이터"
          value={coverage ? `${coverage.evaluated} / ${coverage.total_predictions}` : undefined}
          sub={
            coverage ? `실제 악화 ${coverage.positive}건 · 대기 ${coverage.pending}건` : undefined
          }
        />
      </div>

      {/* Section 1 — 최신 운영 성능 */}
      <section className="space-y-3">
        <h2 className="text-sm font-semibold">최신 운영 성능</h2>

        {!summaryQ.isLoading && !hasEvaluated ? (
          <Card>
            <CardContent className="py-12 text-center text-sm text-muted-foreground">
              평가 가능한 모델 성능 데이터가 없습니다.
            </CardContent>
          </Card>
        ) : (
          <ModelPerformanceSummary
            operational={summary?.operational}
            targets={summary?.targets}
            isLoading={summaryQ.isLoading}
          />
        )}

        {insufficient ? (
          <p className="rounded-md border border-dashed px-4 py-3 text-xs text-muted-foreground">
            평가 가능한 outcome 데이터가 부족합니다. 표본이 목표치(
            {summary?.targets.min_eval_samples}건 · 실제 악화 {summary?.targets.min_positives}건)에
            이를 때까지 지표를 산출하지 않습니다.
          </p>
        ) : hasEvaluated ? (
          <p className="rounded-md border border-dashed px-4 py-3 text-xs leading-relaxed text-muted-foreground">
            ⚠ 현재 평가 표본은 {coverage?.evaluated}건(실제 악화 {coverage?.positive}건)으로 작아
            지표의 신뢰구간이 넓습니다. 코호트도 시연을 위해 선별된 집합이므로 이 수치를 응급실
            전체의 성능으로 일반화할 수 없습니다.
          </p>
        ) : null}
      </section>

      {/* Section 2 — 추이 */}
      <PerformanceTrendChart
        data={trendsQ.data}
        range={range}
        onRangeChange={setRange}
        isLoading={trendsQ.isLoading}
        isError={trendsQ.isError}
      />

      {/* Section 3 — 혼동 행렬 · 판정 구성 */}
      <div className="grid grid-cols-2 gap-5">
        <ConfusionMatrix
          data={confusionQ.data}
          isLoading={confusionQ.isLoading}
          isError={confusionQ.isError}
        />
        <EvaluationBreakdownChart
          coverage={coverage}
          operational={summary?.operational}
          isLoading={summaryQ.isLoading}
        />
      </div>

      {/* Section 4 — 평가 진행 상태.
          짝이 되던 카드를 위로 올렸으므로 2열 그리드를 두지 않는다 — 한 칸만 남으면
          오른쪽이 빈 공간으로 남는다. */}
      <EvaluationStatusCard
        data={statusQ.data}
        horizonHours={info?.prediction_horizon_h}
        isLoading={statusQ.isLoading}
        isError={statusQ.isError}
      />

      {/* Section 5 — 운영점 분석 */}
      <ThresholdTable
        data={thresholdsQ.data}
        isLoading={thresholdsQ.isLoading}
        isError={thresholdsQ.isError}
      />

      {/* Section 6 — 모델 정보 */}
      <Card>
        <CardHeader className="border-b py-3">
          <CardTitle className="text-base">모델 정보</CardTitle>
        </CardHeader>
        <CardContent className="px-5 py-4">
          {summaryQ.isLoading || !info ? (
            <Skeleton className="h-24 w-full" />
          ) : (
            <dl className="grid grid-cols-4 gap-x-6 gap-y-3 text-sm">
              <Field label="Model" value="ER:ON 악화 예측" />
              <Field label="Version" value={`v${DISPLAY_MODEL_VERSION}`} />
              <Field label="Algorithm" value={info.algorithm} />
              <Field label="Feature set" value={info.feature_set} />
              <Field label="Feature count" value={String(info.feature_count)} />
              <Field label="Prediction horizon" value={`${info.prediction_horizon_h}시간`} />
              <Field label="Threshold" value={info.threshold.toFixed(6)} />
              <Field label="Operating point" value={info.operating_point} />
              <Field label="Evaluation unit" value={info.eval_unit} />
              <Field label="Training period" value={info.training_period ?? "-"} />
              <Field label="Training mode" value={info.training_mode ?? "-"} />
              <Field label="Protocol" value={info.protocol ?? "-"} />
            </dl>
          )}
        </CardContent>
      </Card>

      {/* Section 7 — 참조 성능 (개발 단계) */}
      <ReferencePerformance reference={summary?.reference} />

      {/* 의료 고지 — 항상 표시한다 */}
      <div className="space-y-1.5 rounded-md border bg-muted/40 px-4 py-3 text-[11px] leading-relaxed text-muted-foreground">
        <p>본 시스템은 프로젝트 시연용이며 실제 의료 판단을 대신하지 않습니다.</p>
        <p>모델 성능 지표는 평가가 완료된 prediction 만을 기준으로 계산됩니다.</p>
        <p>
          성능 평가는 학습 시 사용한 악화 정의(호흡부전 처치 · 승압제 · CPR · 사망 · 생리학적 악화
          onset)를 그대로 재현해 산출합니다. 정의를 재현할 수 없는 경우 지표를 표시하지 않습니다.
        </p>
      </div>
    </div>
  );
}

function PageHeader() {
  return (
    <div>
      <h1 className="text-2xl font-bold tracking-tight">AI 모델 성능 모니터링</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        응급실 환자 악화 예측 모델의 최신 성능과 운영 상태를 확인합니다.
      </p>
    </div>
  );
}

function SummaryTile({
  label,
  value,
  sub,
}: {
  label: string;
  value?: string | undefined;
  sub?: string | null | undefined;
}) {
  return (
    <Card>
      <CardContent className="px-5 py-4">
        <p className="text-xs text-muted-foreground">{label}</p>
        {value === undefined ? (
          <Skeleton className="mt-2 h-6 w-24" />
        ) : (
          <p className="tabular mt-1 truncate text-lg font-bold">{value}</p>
        )}
        {sub ? <p className="mt-0.5 truncate text-[11px] text-muted-foreground">{sub}</p> : null}
      </CardContent>
    </Card>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 truncate font-medium">{value}</dd>
    </div>
  );
}
