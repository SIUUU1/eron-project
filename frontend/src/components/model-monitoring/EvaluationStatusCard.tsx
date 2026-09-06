import type { EvaluationStatusResponse } from "@/api/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * 평가 진행 상태.
 *
 * 🔑 pending / censored / truncated 를 각각 보여준다. 셋 다 지표에서 빠지지만 이유가 다르다.
 *    합쳐 버리면 "왜 평가가 안 됐는지" 를 화면에서 알 수 없다.
 */
export function EvaluationStatusCard({
  data,
  horizonHours,
  isLoading,
  isError,
}: {
  data?: EvaluationStatusResponse | undefined;
  horizonHours?: number | undefined;
  isLoading: boolean;
  isError: boolean;
}) {
  const rows = data
    ? [
        { label: "전체 Prediction", value: data.total_predictions, tone: "" },
        { label: "평가 완료", value: data.evaluated_predictions, tone: "text-risk-stable" },
        { label: "Outcome 대기", value: data.pending_predictions, tone: "text-risk-watch" },
        { label: "중도절단", value: data.censored_predictions, tone: "text-muted-foreground" },
        {
          label: "학습 구간 밖",
          value: data.truncated_predictions,
          tone: "text-muted-foreground",
        },
      ]
    : [];

  const ratio =
    data && data.total_predictions > 0
      ? (100 * data.evaluated_predictions) / data.total_predictions
      : null;

  return (
    <Card>
      <CardHeader className="border-b py-3">
        <CardTitle className="text-base">평가 진행 상태</CardTitle>
      </CardHeader>
      <CardContent className="px-5 py-4">
        {isLoading ? (
          <Skeleton className="h-28 w-full" />
        ) : isError || !data ? (
          <p className="py-10 text-center text-sm text-muted-foreground">
            평가 진행 상태를 불러오지 못했습니다.
          </p>
        ) : (
          <>
            <div className="grid grid-cols-5 gap-3">
              {rows.map((row) => (
                <div key={row.label}>
                  <p className="text-xs text-muted-foreground">{row.label}</p>
                  <p className={`tabular mt-1 text-xl font-bold ${row.tone}`}>{row.value}</p>
                </div>
              ))}
            </div>
            <p className="mt-3 text-sm">
              평가 가능 비율{" "}
              <span className="tabular font-semibold">
                {ratio === null ? "N/A" : `${ratio.toFixed(1)}%`}
              </span>
            </p>
          </>
        )}
        <p className="mt-3 text-[11px] leading-relaxed text-muted-foreground">
          Prediction 발생 후 {horizonHours ?? 3}시간의 관찰 시간이 지나야 최종 성능 평가가
          가능합니다. 중도절단(관측 경로 없음)과 학습 구간 밖(첫 악화 이후) 시점은 음성으로 세지
          않고 지표에서 제외합니다.
        </p>
      </CardContent>
    </Card>
  );
}
