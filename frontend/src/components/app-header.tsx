import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Bell,
  ChevronsLeft,
  ChevronsRight,
  Gauge,
  RotateCcw,
  UserRound,
  Users,
} from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { dashboardKeys, getDashboardSummary } from "@/api/dashboard";
import {
  advanceDemoClock,
  demoClockKeys,
  getDemoClock,
  resetDemoClock,
  setDemoSpeed,
  speedLabel,
  SPEED_CYCLE,
  type DemoClock,
} from "@/api/demo-clock";
import { Button } from "@/components/ui/button";
import { currentUser } from "@/lib/mock-data";

function format(d: Date) {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/**
 * 데모 시계를 로컬에서 이어서 흐르게 한다.
 * 서버는 10초마다 다시 물어보고, 그 사이는 speed 를 곱해 보간한다.
 */
function useDisplayClock(clock: DemoClock | undefined, fetchedAt: number | undefined) {
  const [now, setNow] = useState<string | null>(null);

  useEffect(() => {
    const tick = () => {
      if (!clock || !fetchedAt) {
        setNow(format(new Date()));
        return;
      }
      const elapsed = (Date.now() - fetchedAt) * clock.speed;
      setNow(format(new Date(new Date(clock.virtual_now).getTime() + elapsed)));
    };
    tick();
    const t = setInterval(tick, 1000);
    return () => clearInterval(t);
  }, [clock, fetchedAt]);

  return now;
}

export function AppHeader() {
  const queryClient = useQueryClient();

  const { data: summary } = useQuery({
    queryKey: dashboardKeys.summary,
    queryFn: ({ signal }) => getDashboardSummary(signal),
  });

  const clockQuery = useQuery({
    queryKey: demoClockKeys.clock,
    queryFn: ({ signal }) => getDemoClock(signal),
    refetchInterval: 10_000,
  });
  const clock = clockQuery.data;
  const now = useDisplayClock(clock, clockQuery.dataUpdatedAt || undefined);

  // 시계를 움직이면 화면 전체가 새 시각 기준으로 다시 그려져야 한다
  const mutate = useMutation({
    mutationFn: (run: () => Promise<DemoClock>) => run(),
    onSuccess: () => void queryClient.invalidateQueries(),
    onError: (e: Error) =>
      toast.error("데모 시계를 바꾸지 못했습니다.", { description: e.message }),
  });

  const nextSpeed = () => {
    const i = SPEED_CYCLE.indexOf((clock?.speed ?? 1) as (typeof SPEED_CYCLE)[number]);
    return SPEED_CYCLE[(i + 1) % SPEED_CYCLE.length] ?? 1;
  };

  return (
    <header className="flex h-16 shrink-0 items-center justify-between border-b border-border bg-card px-6">
      <div className="flex items-center gap-6">
        <div className="flex items-center gap-2">
          <div className="tabular text-sm font-semibold text-foreground">{now ?? "--:--:--"}</div>
          {clock?.is_shifted && (
            <span className="rounded bg-risk-rising-soft px-1.5 py-0.5 text-[10px] font-bold text-risk-rising">
              데모 {speedLabel(clock.speed)}
              {clock.elapsed_seconds > 60 ? ` · +${Math.round(clock.elapsed_seconds / 3600)}h` : ""}
            </span>
          )}
        </div>

        {/* 시연용 시계 제어 — 1시간 단위 악화 예측을 빠르게 보여주기 위한 것 */}
        <div className="flex items-center gap-1">
          <Button
            size="sm"
            variant="outline"
            className="h-7 px-2 text-xs"
            // 시나리오 시작점보다 이전으로는 되감지 않는다
            disabled={mutate.isPending || !clock?.can_rewind}
            onClick={() => mutate.mutate(() => advanceDemoClock(-1))}
          >
            <ChevronsLeft className="size-3.5" /> -1시간
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7 px-2 text-xs"
            disabled={mutate.isPending}
            onClick={() => mutate.mutate(() => advanceDemoClock(1))}
          >
            <ChevronsRight className="size-3.5" /> +1시간
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-xs"
            disabled={mutate.isPending}
            onClick={() => {
              const v = nextSpeed();
              mutate.mutate(() => setDemoSpeed(v));
            }}
          >
            <Gauge className="size-3.5" /> {speedLabel(clock?.speed ?? 1)}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-xs"
            disabled={mutate.isPending || !clock?.is_shifted}
            onClick={() => mutate.mutate(resetDemoClock)}
          >
            <RotateCcw className="size-3.5" />
          </Button>
        </div>

        <div className="flex items-center gap-2 rounded-md bg-secondary px-3 py-1.5 text-sm">
          <Users className="size-4 text-primary" />
          <span className="text-muted-foreground">현재 환자</span>
          <span className="tabular font-bold text-foreground">
            {summary ? `${summary.total}명` : "…"}
          </span>
        </div>
        <div className="flex items-center gap-2 rounded-md bg-risk-critical-soft px-3 py-1.5 text-sm">
          <AlertTriangle className="size-4 text-risk-critical" />
          <span className="text-risk-critical/80">위험 환자</span>
          <span className="tabular font-bold text-risk-critical">
            {summary ? `${summary.critical + summary.rising}명` : "…"}
          </span>
        </div>
      </div>

      <div className="flex items-center gap-4">
        <button
          type="button"
          className="relative rounded-md p-2 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
          aria-label="알림"
        >
          <Bell className="size-5" />
          <span className="absolute right-1.5 top-1.5 flex size-4 items-center justify-center rounded-full bg-risk-critical text-[10px] font-bold text-primary-foreground">
            {summary?.ai_alerts_today ?? 0}
          </span>
        </button>
        <div className="flex items-center gap-2 border-l border-border pl-4">
          <div className="flex size-8 items-center justify-center rounded-full bg-navy text-navy-foreground">
            <UserRound className="size-4" />
          </div>
          <div className="text-sm leading-tight">
            <p className="font-semibold text-foreground">
              {currentUser.dept} {currentUser.name}
            </p>
            <p className="text-xs text-muted-foreground">{currentUser.role}</p>
          </div>
        </div>
      </div>
    </header>
  );
}
