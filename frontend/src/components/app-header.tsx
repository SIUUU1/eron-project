import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bell,
  Check,
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  Gauge,
  RotateCcw,
  UserRound,
} from "lucide-react";
import { Link } from "@tanstack/react-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { dashboardKeys, getAlerts } from "@/api/dashboard";
import { formatDateTime } from "@/api/display";
import {
  advanceDemoClock,
  DEMO_STEP_HOURS,
  demoClockKeys,
  elapsedLabel,
  getDemoClock,
  resetDemoClock,
  setDemoSpeed,
  speedLabel,
  SPEED_CYCLE,
  type DemoClock,
} from "@/api/demo-clock";
import { runPredictions } from "@/api/ed-stays";
import { invalidateDemoTimeQueries, invalidatePredictionQueries } from "@/api/refresh";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
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

/**
 * 시계 조작 한 건.
 * `predictAfter` 는 앞으로 갈 때만 true 다 — 되감기·배속·초기화는 재계산하지 않는다.
 */
type ClockAction = { run: () => Promise<DemoClock>; predictAfter?: boolean };

export function AppHeader() {
  const queryClient = useQueryClient();

  // 🔔 종 아이콘: 아직 재검토하지 않은 🔴 재평가 필요 환자 수.
  // 대시보드 경고 카드와 **같은 엔드포인트**를 쓰므로 두 화면의 상태가 어긋나지 않는다.
  const alertsQuery = useQuery({
    // 종 목록은 예측 시점별로 누적된 알림을 그대로 보여준다(카드와 달리 최신 1건이 아니다).
    queryKey: dashboardKeys.alerts("red", false),
    queryFn: ({ signal }) => getAlerts(20, "red", false, signal),
    refetchInterval: 30_000,
  });
  const unread = alertsQuery.data?.unread_count ?? 0;
  const alerts = alertsQuery.data?.items ?? [];

  const clockQuery = useQuery({
    queryKey: demoClockKeys.clock,
    queryFn: ({ signal }) => getDemoClock(signal),
    refetchInterval: 10_000,
  });
  const clock = clockQuery.data;
  const now = useDisplayClock(clock, clockQuery.dataUpdatedAt || undefined);

  /**
   * 예측 갱신을 백그라운드로 돌린다. **시계 이동은 이걸 기다리지 않는다.**
   *
   * 예측 동안 시계 버튼이 잠기지 않으므로 연타로 /predictions/run 이 겹칠 수 있다.
   * 실행 중이면 재실행을 1회만 예약해 두고, 끝난 뒤 이어서 한 번 더 돈다.
   * (어떤 슬롯을 계산할지는 요청 시점에 백엔드가 정하므로 마지막 실행이 최신 시각을 덮는다.)
   */
  const runningRef = useRef(false);
  const rerunQueuedRef = useRef(false);

  const startPredictionRun = useCallback(() => {
    if (runningRef.current) {
      rerunQueuedRef.current = true;
      return;
    }
    runningRef.current = true;

    const cycle = (): Promise<void> =>
      runPredictions()
        // 예측이 끝난 뒤에야 예측·경보 관련 쿼리를 받는다(활력징후는 다시 읽지 않는다).
        .then(() => invalidatePredictionQueries(queryClient))
        .catch(() => {
          // 예측 서비스가 꺼져 있어도 시계 이동은 성공한 것이다.
          // 스케줄러가 다음 주기에 같은 일을 하므로 화면만 알린다.
          toast.warning("예측 갱신은 다음 주기에 반영됩니다.");
        })
        .then(() => {
          if (!rerunQueuedRef.current) return;
          rerunQueuedRef.current = false;
          return cycle();
        });

    void cycle().finally(() => {
      runningRef.current = false;
    });
  }, [queryClient]);

  // 시계를 움직이면 화면 전체가 새 시각 기준으로 다시 그려져야 한다.
  // ⚠ 순서가 핵심이다. 시계 → 시간축 데이터 → (앞으로 갈 때만) 예측 순으로 시작하고,
  //   예측은 await 하지 않는다. 예측이 느려도 환자·활력징후·병상은 먼저 화면에 뜬다.
  //   기본 데이터 요청을 예측 POST 보다 **먼저** 발사하는 것도 의도된 것이다.
  const mutate = useMutation({
    mutationFn: ({ run }: ClockAction) => run(),
    onSuccess: async (next, { predictAfter }) => {
      // POST 응답이 곧 새 시계다. /clock 을 다시 GET 하지 않고 헤더를 즉시 바꾼다.
      queryClient.setQueryData(demoClockKeys.clock, next);
      // 갱신이 실패해도 "시계를 바꾸지 못했습니다" 로 보이면 안 된다 — 시계는 이미 바뀌었다.
      // (개별 쿼리의 실패는 각 화면이 자기 에러 상태로 보여준다.)
      await invalidateDemoTimeQueries(queryClient).catch(() => undefined);
      if (predictAfter) startPredictionRun();
    },
    onError: (e: Error) =>
      toast.error("데모 시계를 바꾸지 못했습니다.", { description: e.message }),
  });

  /**
   * 시계를 옮긴다. 앞으로 갈 때만 예측 갱신을 한 번 돌린다.
   *
   * ⚠ 되감기(hours < 0)는 재계산하지 않는다. 되감은 구간의 예측은 이미 저장돼 있고,
   *   화면은 demo_now 기준으로 보이는 범위만 줄이면 된다.
   * ⚠ 어떤 환자를 계산할지는 백엔드가 정한다(due · 15분 슬롯). 프론트는 판단하지 않는다.
   */
  const step = (hours: number) =>
    mutate.mutate({ run: () => advanceDemoClock(hours), predictAfter: hours > 0 });

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
              {clock.elapsed_seconds > 60 ? ` · ${elapsedLabel(clock.elapsed_seconds)}` : ""}
            </span>
          )}
        </div>

        {/* 시연용 시계 제어 — 1시간 단위 악화 예측을 빠르게 보여주기 위한 것 */}
        <div className="flex items-center gap-1">
          <Button
            size="sm"
            variant="outline"
            className="h-7 whitespace-nowrap px-2 text-xs"
            // 시나리오 시작점보다 이전으로는 되감지 않는다(백엔드가 시작점에서 멈춘다)
            disabled={mutate.isPending || !clock?.can_rewind}
            onClick={() => step(-DEMO_STEP_HOURS.hour)}
          >
            <ChevronsLeft className="size-3.5" /> -1시간
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7 whitespace-nowrap px-2 text-xs"
            disabled={mutate.isPending || !clock?.can_rewind}
            onClick={() => step(-DEMO_STEP_HOURS.quarter)}
          >
            <ChevronLeft className="size-3.5" /> -15분
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7 whitespace-nowrap px-2 text-xs"
            disabled={mutate.isPending}
            onClick={() => step(DEMO_STEP_HOURS.quarter)}
          >
            <ChevronRight className="size-3.5" /> +15분
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7 whitespace-nowrap px-2 text-xs"
            disabled={mutate.isPending}
            onClick={() => step(DEMO_STEP_HOURS.hour)}
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
              mutate.mutate({ run: () => setDemoSpeed(v) });
            }}
          >
            <Gauge className="size-3.5" /> {speedLabel(clock?.speed ?? 1)}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-xs"
            disabled={mutate.isPending || !clock?.is_shifted}
            onClick={() => mutate.mutate({ run: resetDemoClock })}
          >
            <RotateCcw className="size-3.5" />
          </Button>
        </div>
      </div>

      <div className="flex items-center gap-4">
        <Popover>
          <PopoverTrigger asChild>
            <button
              type="button"
              className="relative rounded-md p-2 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
              aria-label={`재검토 필요 알림 ${unread}건`}
            >
              <Bell className="size-5" />
              {unread > 0 && (
                <span className="absolute right-1.5 top-1.5 flex size-4 items-center justify-center rounded-full bg-risk-critical text-[10px] font-bold text-primary-foreground">
                  {unread}
                </span>
              )}
            </button>
          </PopoverTrigger>
          <PopoverContent align="end" className="w-96 p-0">
            <div className="flex items-center justify-between border-b px-4 py-2.5">
              <p className="text-sm font-semibold">재검토 필요 알림</p>
              <span className="text-xs text-muted-foreground">미확인 {unread}건</span>
            </div>
            {/* 알림이 많아도 헤더 팝오버가 길어지지 않게 목록에만 스크롤을 준다.
                적을 때는 스크롤바가 보이지 않는다(max-height + auto). */}
            <div className="max-h-80 overflow-y-auto">
              {alerts.length === 0 ? (
                <p className="px-4 py-8 text-center text-xs leading-relaxed text-muted-foreground">
                  현재 재검토가 필요한 환자가 없습니다.
                </p>
              ) : (
                <ul className="divide-y">
                  {alerts.map((a) => (
                    <li key={a.id} className="px-4 py-2.5">
                      <div className="flex items-center justify-between gap-2">
                        <Link
                          to="/monitoring/$patientId"
                          params={{ patientId: a.stay_id }}
                          className="text-sm font-semibold hover:underline"
                        >
                          {a.display_name ?? `ED-${a.stay_id}`}
                          <span className="tabular ml-1 text-xs font-normal text-muted-foreground">
                            ({a.stay_id})
                          </span>
                        </Link>
                        {a.acknowledged_at ? (
                          <span className="flex shrink-0 items-center gap-1 text-[11px] text-risk-stable">
                            <Check className="size-3.5" /> 확인
                          </span>
                        ) : null}
                      </div>
                      <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                        {a.message}
                      </p>
                      <div className="mt-1 flex items-center justify-between text-xs">
                        <span className="tabular font-semibold text-risk-critical">
                          악화 확률{" "}
                          {a.risk_probability === null
                            ? "-"
                            : `${(a.risk_probability * 100).toFixed(1)}%`}
                        </span>
                        {/* 발생 시각은 데모 시간축이다(서버 now() 아님) */}
                        <span className="tabular text-muted-foreground">
                          {formatDateTime(a.alert_time)}
                        </span>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </PopoverContent>
        </Popover>
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
