import type { Query, QueryClient } from "@tanstack/react-query";

import { dashboardKeys } from "@/api/dashboard";
import { edStayKeys } from "@/api/ed-stays";

/**
 * 데모 시계 이동 후의 화면 갱신을 두 단계로 나눈다.
 *
 * 1단계(`invalidateDemoTimeQueries`) — 데모 시각만 바뀌어도 값이 달라지는 것들.
 *   예측 실행을 기다리지 않고 바로 발사한다.
 * 2단계(`invalidatePredictionQueries`) — 새 예측 행이 저장돼야 값이 달라지는 것들.
 *   예측이 끝난 뒤에만 돌린다.
 *
 * ⚠ 병상 색상·환자 목록 위험도·상세의 alert_unread 는 예측 결과이면서 동시에
 *   demo_now() 로 잘린 값이다(v_latest_prediction 조인). 그래서 두 단계 모두에 들어간다.
 *   활력징후와 시계는 예측과 무관하므로 2단계에서 제외한다.
 * ⚠ clinical-record·kcd 처럼 데모 시각과 무관한 캐시는 어느 단계에서도 건드리지 않는다.
 */

/** ["ed","stay",id,leaf] 의 leaf 만 본다. detail 은 leaf 가 없다(undefined). */
function stayLeaf(query: Query): unknown {
  return query.queryKey[3];
}

/** 데모 시각이 바뀌면 값이 달라지는 쿼리 — 예측 실행을 기다리지 않는다. */
export function invalidateDemoTimeQueries(queryClient: QueryClient): Promise<void> {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: dashboardKeys.beds }),
    queryClient.invalidateQueries({ queryKey: dashboardKeys.alertsRoot }),
    queryClient.invalidateQueries({ queryKey: dashboardKeys.reassess }),
    queryClient.invalidateQueries({ queryKey: edStayKeys.lists }),
    // 상세·활력징후·예측 추이 모두 demo_now() 로 잘려 나오므로 여기서 한 번에 받는다.
    queryClient.invalidateQueries({ queryKey: edStayKeys.stays }),
  ]).then(() => undefined);
}

/** 새 예측이 저장돼야 값이 달라지는 쿼리 — 활력징후·시계는 다시 읽지 않는다. */
export function invalidatePredictionQueries(queryClient: QueryClient): Promise<void> {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: dashboardKeys.beds }),
    queryClient.invalidateQueries({ queryKey: dashboardKeys.alertsRoot }),
    queryClient.invalidateQueries({ queryKey: dashboardKeys.reassess }),
    queryClient.invalidateQueries({ queryKey: edStayKeys.lists }),
    // ["ed","stay"] 접두사에는 활력징후도 걸린다. 이미 화면에 뜬 값이므로 빼고 받는다.
    queryClient.invalidateQueries({
      queryKey: edStayKeys.stays,
      predicate: (query) => stayLeaf(query) !== "vitals",
    }),
  ]).then(() => undefined);
}
