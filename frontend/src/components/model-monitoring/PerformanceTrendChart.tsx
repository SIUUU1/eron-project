import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatDateTime, formatTime } from "@/api/display";
import type { TrendsResponse } from "@/api/types";
import { TREND_RANGES, type TrendRange } from "@/components/model-monitoring/shared";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

/**
 * 성능 추이 — 지표별 면적 그래프.
 *
 * 🔑 평가가 끝난 예측만 들어간다. 표본이 모자란 구간의 판별력 지표는 서버가 null 로 주고,
 *    `connectNulls` 를 켜지 않아 그 구간은 선이 끊긴다 — 없는 값을 이어 붙이면
 *    데이터가 있는 것처럼 보인다.
 * 🔑 면적은 얇게 깐다(0.14). 다섯 계열이 겹치므로 진하게 채우면 아래 계열이 묻힌다.
 */

const SERIES = [
  { key: "pr_auc", name: "PR-AUC", color: "var(--chart-1)" },
  { key: "auroc", name: "AUROC", color: "var(--chart-2)" },
  { key: "recall", name: "Recall", color: "var(--chart-3)" },
  { key: "precision", name: "Precision", color: "var(--chart-4)" },
  { key: "f1", name: "F1", color: "var(--chart-5)" },
] as const;

export function PerformanceTrendChart({
  data,
  range,
  onRangeChange,
  isLoading,
  isError,
}: {
  data?: TrendsResponse | undefined;
  range: TrendRange;
  onRangeChange: (range: TrendRange) => void;
  isLoading: boolean;
  isError: boolean;
}) {
  const points = (data?.points ?? []).map((point) => ({
    ...point,
    time:
      data?.interval === "day"
        ? formatDateTime(point.bucket).slice(5, 10)
        : formatTime(point.bucket),
  }));

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-2 border-b py-3">
        <CardTitle className="text-base">일자별 성능 지표</CardTitle>
        <Tabs value={range} onValueChange={(value) => onRangeChange(value as TrendRange)}>
          <TabsList>
            {(Object.keys(TREND_RANGES) as TrendRange[]).map((key) => (
              <TabsTrigger key={key} value={key} className="text-xs">
                {TREND_RANGES[key].label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      </CardHeader>
      <CardContent className="px-5 py-4">
        {isLoading ? (
          <Skeleton className="h-80 w-full" />
        ) : isError ? (
          <p className="py-20 text-center text-sm text-muted-foreground">
            성능 추이를 불러오지 못했습니다.
          </p>
        ) : points.length === 0 ? (
          <p className="py-20 text-center text-sm text-muted-foreground">
            이 구간에는 평가가 완료된 예측이 없습니다.
          </p>
        ) : (
          <div className="h-80">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={points} margin={{ top: 8, right: 12, bottom: 0, left: -12 }}>
                <defs>
                  {SERIES.map((series) => (
                    <linearGradient
                      key={series.key}
                      id={`trend-${series.key}`}
                      x1="0"
                      y1="0"
                      x2="0"
                      y2="1"
                    >
                      <stop offset="0%" stopColor={series.color} stopOpacity={0.28} />
                      <stop offset="100%" stopColor={series.color} stopOpacity={0.02} />
                    </linearGradient>
                  ))}
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                <XAxis
                  dataKey="time"
                  tick={{ fontSize: 11 }}
                  stroke="var(--muted-foreground)"
                  tickLine={false}
                />
                <YAxis
                  domain={[0, 1]}
                  ticks={[0, 0.2, 0.4, 0.6, 0.8, 1]}
                  tick={{ fontSize: 11 }}
                  stroke="var(--muted-foreground)"
                  tickLine={false}
                  width={44}
                  label={{
                    value: "Metrics",
                    angle: -90,
                    position: "insideLeft",
                    offset: 22,
                    style: { fontSize: 11, fill: "var(--muted-foreground)" },
                  }}
                />
                <Tooltip
                  contentStyle={{
                    borderRadius: 8,
                    border: "1px solid var(--border)",
                    fontSize: 12,
                  }}
                  /* 값이 없는 구간은 0 이 아니라 N/A 로 보여준다. */
                  formatter={(value) => (typeof value === "number" ? value.toFixed(3) : "N/A")}
                />
                <Legend
                  iconType="circle"
                  iconSize={8}
                  wrapperStyle={{ fontSize: 11, paddingTop: 6 }}
                />
                {SERIES.map((series) => (
                  <Area
                    key={series.key}
                    type="monotone"
                    dataKey={series.key}
                    name={series.name}
                    stroke={series.color}
                    strokeWidth={2}
                    fill={`url(#trend-${series.key})`}
                    dot={{ r: 2, strokeWidth: 0, fill: series.color }}
                    activeDot={{ r: 4 }}
                    /* 값이 없는 구간은 잇지 않는다 — 없는 데이터를 있는 것처럼 보이게 한다. */
                    connectNulls={false}
                  />
                ))}
              </AreaChart>
            </ResponsiveContainer>
          </div>
        )}
        <p className="mt-3 text-[11px] text-muted-foreground">
          평가가 완료된 예측만 집계합니다. 표본이 부족한 구간의 PR-AUC·AUROC 는 N/A 로 두고 선을
          잇지 않습니다.
        </p>
      </CardContent>
    </Card>
  );
}
