import {
  Bar,
  BarChart,
  Cell,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { LabelCoverage, OperationalMetrics } from "@/api/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * 판정 구성 분포 차트.
 *
 * 🔑 "왜 지표 표본이 작은가" 를 설명하는 그림이다. 평가 완료 옆에
 *    대기·중도절단·학습구간 밖을 같은 축에 세워야 빠진 이유가 보인다.
 */

const AXIS = {
  tick: { fontSize: 11 },
  stroke: "var(--muted-foreground)",
  tickLine: false,
} as const;

const TOOLTIP_STYLE = {
  borderRadius: 8,
  border: "1px solid var(--border)",
  fontSize: 12,
} as const;

export function EvaluationBreakdownChart({
  coverage,
  operational,
  isLoading,
}: {
  coverage?: LabelCoverage | undefined;
  operational?: OperationalMetrics | undefined;
  isLoading: boolean;
}) {
  const bars = coverage
    ? [
        { name: "평가 완료", count: coverage.evaluated, tone: "var(--chart-4)" },
        { name: "Outcome 대기", count: coverage.pending, tone: "var(--chart-5)" },
        { name: "중도절단", count: coverage.censored, tone: "var(--muted-foreground)" },
        { name: "학습구간 밖", count: coverage.truncated, tone: "var(--muted-foreground)" },
        { name: "실제 악화", count: coverage.positive, tone: "var(--chart-1)" },
      ]
    : [];

  return (
    <Card>
      <CardHeader className="border-b py-3">
        <CardTitle className="text-sm font-bold tracking-wide">PREDICTION — 판정 구성</CardTitle>
      </CardHeader>
      <CardContent className="px-5 py-4">
        {isLoading || !coverage ? (
          <Skeleton className="h-56 w-full" />
        ) : (
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={bars} margin={{ top: 8, right: 8, bottom: 0, left: -8 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                <XAxis dataKey="name" {...AXIS} interval={0} />
                <YAxis
                  {...AXIS}
                  width={48}
                  label={{
                    value: "COUNT",
                    angle: -90,
                    position: "insideLeft",
                    offset: 20,
                    style: { fontSize: 10, fill: "var(--muted-foreground)" },
                  }}
                />
                <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(value) => [value, "건"]} />
                <Bar dataKey="count" radius={[3, 3, 0, 0]}>
                  {bars.map((bar) => (
                    <Cell key={bar.name} fill={bar.tone} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
        <p className="mt-3 text-[11px] leading-relaxed text-muted-foreground">
          지표에 들어가는 것은 <span className="font-medium">평가 완료</span>뿐입니다.
          대기·중도절단· 학습구간 밖은 음성으로 세지 않고 제외합니다
          {operational ? ` (현재 표본 ${operational.evaluated_count}건).` : "."}
        </p>
      </CardContent>
    </Card>
  );
}
