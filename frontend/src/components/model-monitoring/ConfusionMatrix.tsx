import type { ConfusionMatrixResponse } from "@/api/types";
import { metric } from "@/components/model-monitoring/shared";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * 혼동 행렬.
 *
 * 🔑 `risk_probability >= threshold` 가 양성 예측이다. 실제 운영 threshold 를 적용한 결과다.
 */
export function ConfusionMatrix({
  data,
  isLoading,
  isError,
}: {
  data?: ConfusionMatrixResponse | undefined;
  isLoading: boolean;
  isError: boolean;
}) {
  return (
    <Card>
      <CardHeader className="border-b py-3">
        <CardTitle className="text-base">혼동 행렬</CardTitle>
      </CardHeader>
      <CardContent className="px-5 py-4">
        {isLoading ? (
          <Skeleton className="h-48 w-full" />
        ) : isError || !data ? (
          <p className="py-12 text-center text-sm text-muted-foreground">
            혼동 행렬을 불러오지 못했습니다.
          </p>
        ) : data.evaluated_count === 0 ? (
          <p className="py-12 text-center text-sm text-muted-foreground">
            평가 가능한 모델 성능 데이터가 없습니다.
          </p>
        ) : (
          <>
            <div className="grid grid-cols-[auto_1fr_1fr] gap-px overflow-hidden rounded-md bg-border text-sm">
              <div className="bg-muted px-3 py-2" />
              <div className="bg-muted px-3 py-2 text-center text-xs font-medium">
                실제 악화 없음
              </div>
              <div className="bg-muted px-3 py-2 text-center text-xs font-medium">실제 악화</div>

              <div className="bg-muted px-3 py-3 text-xs font-medium">예측 음성</div>
              <div className="bg-card px-3 py-3 text-center">
                <p className="tabular text-xl font-bold">{data.true_negative}</p>
                <p className="text-[11px] text-muted-foreground">TN</p>
              </div>
              <div className="bg-card px-3 py-3 text-center">
                {/* 놓친 악화. 임상적으로 가장 무거운 칸이라 색으로 구분한다. */}
                <p className="tabular text-xl font-bold text-risk-critical">
                  {data.false_negative}
                </p>
                <p className="text-[11px] text-muted-foreground">FN · 놓친 악화</p>
              </div>

              <div className="bg-muted px-3 py-3 text-xs font-medium">예측 양성</div>
              <div className="bg-card px-3 py-3 text-center">
                <p className="tabular text-xl font-bold text-risk-watch">{data.false_positive}</p>
                <p className="text-[11px] text-muted-foreground">FP</p>
              </div>
              <div className="bg-card px-3 py-3 text-center">
                <p className="tabular text-xl font-bold text-risk-stable">{data.true_positive}</p>
                <p className="text-[11px] text-muted-foreground">TP</p>
              </div>
            </div>

            <div className="mt-4 grid grid-cols-4 gap-3 text-sm">
              {[
                { label: "Recall", value: metric(data.recall) },
                { label: "Precision", value: metric(data.precision) },
                { label: "F1", value: metric(data.f1) },
                { label: "Threshold", value: data.threshold.toFixed(6) },
              ].map((item) => (
                <div key={item.label}>
                  <p className="text-xs text-muted-foreground">{item.label}</p>
                  <p className="tabular mt-0.5 font-semibold">{item.value}</p>
                </div>
              ))}
            </div>

            <p className="mt-3 text-[11px] text-muted-foreground">
              평가 완료 {data.evaluated_count}건 중 실제 악화 {data.positive_count}건 기준입니다.
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
