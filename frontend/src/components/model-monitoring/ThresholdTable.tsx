import type { ThresholdsResponse } from "@/api/types";
import { metric } from "@/components/model-monitoring/shared";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

/**
 * 운영점(threshold)별 성능.
 *
 * 🔑 PR-AUC·AUROC 는 넣지 않는다. threshold 와 무관한 값이라 행마다 다르게 보이면 오해를 부른다.
 * 🔑 후보는 bundle 의 운영점 5종 + 현재 운영값이다. 임의의 격자를 만들지 않는다.
 */
export function ThresholdTable({
  data,
  isLoading,
  isError,
}: {
  data?: ThresholdsResponse | undefined;
  isLoading: boolean;
  isError: boolean;
}) {
  return (
    <Card>
      <CardHeader className="border-b py-3">
        <CardTitle className="text-base">Threshold 분석</CardTitle>
      </CardHeader>
      <CardContent className="px-5 py-4">
        {isLoading ? (
          <Skeleton className="h-48 w-full" />
        ) : isError || !data ? (
          <p className="py-12 text-center text-sm text-muted-foreground">
            운영점 분석을 불러오지 못했습니다.
          </p>
        ) : data.rows.length === 0 ? (
          <p className="py-12 text-center text-sm text-muted-foreground">
            평가 가능한 모델 성능 데이터가 없습니다.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Threshold</TableHead>
                <TableHead className="text-right">Recall</TableHead>
                <TableHead className="text-right">Precision</TableHead>
                <TableHead className="text-right">F1</TableHead>
                <TableHead className="text-right">경보 건수</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.rows.map((row) => (
                <TableRow key={row.threshold} className={row.is_current ? "bg-muted/50" : ""}>
                  <TableCell className="tabular">
                    <div className="flex items-center gap-2">
                      {row.threshold.toFixed(6)}
                      {row.is_current ? <Badge className="text-[10px]">현재 운영값</Badge> : null}
                      {row.operating_point ? (
                        <span className="text-[11px] text-muted-foreground">
                          {row.operating_point}
                        </span>
                      ) : null}
                    </div>
                  </TableCell>
                  <TableCell className="tabular text-right">{metric(row.recall)}</TableCell>
                  <TableCell className="tabular text-right">{metric(row.precision)}</TableCell>
                  <TableCell className="tabular text-right">{metric(row.f1)}</TableCell>
                  <TableCell className="tabular text-right">
                    {row.positive_predictions}
                    <span className="ml-1 text-[11px] text-muted-foreground">
                      (TP {row.true_positive} / FP {row.false_positive})
                    </span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        <p className="mt-3 text-[11px] text-muted-foreground">
          PR-AUC·AUROC 는 threshold 와 무관한 지표라 이 표에 넣지 않습니다.
        </p>
      </CardContent>
    </Card>
  );
}
