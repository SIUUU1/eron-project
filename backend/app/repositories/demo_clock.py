"""데모 시계 조회·조작.

화면의 모든 시각이 app.demo_now() 에서 파생되므로, 이 시계 하나만 움직이면
목록·상세·차트·병상·퇴실 판정이 한꺼번에 따라온다.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

_READ = text("""
    SELECT app.demo_now()                              AS virtual_now,
           now()::timestamp                            AS real_now,
           c.speed,
           c.epoch_virtual,
           EXTRACT(epoch FROM app.demo_now() - now()::timestamp)::bigint  AS offset_seconds,
           -- 시나리오 시작점 이후로 얼마나 흘렀는지. 0 이면 더 되감을 수 없다.
           EXTRACT(epoch FROM app.demo_now() - c.epoch_virtual)::bigint   AS elapsed_seconds
    FROM app.demo_clock c
    WHERE c.id = 1
""")


def read(db: Session) -> Any:
    return db.execute(_READ).mappings().one()


def advance(db: Session, hours: float) -> Any:
    """가상 시각을 앞뒤로 옮긴다. anchor_real 을 now() 로 재설정해 배속과 무관하게 동작한다.

    되감기는 시나리오 시작점(epoch_virtual) 아래로 내려가지 않는다.
    그 이전으로 가면 아직 내원하지 않은 환자가 목록에 남아 앞뒤가 맞지 않는다.
    """
    db.execute(
        text("""
            UPDATE app.demo_clock
               SET anchor_virtual = greatest(
                       app.demo_now() + make_interval(secs => :secs),
                       c.epoch_virtual
                   ),
                   anchor_real    = now(),
                   updated_at     = now()
              FROM app.demo_clock c
             WHERE app.demo_clock.id = 1 AND c.id = 1
        """),
        {"secs": hours * 3600},
    )
    db.commit()
    return read(db)


def set_speed(db: Session, speed: float) -> Any:
    """배속 변경. 지금까지 흐른 가상 시각을 고정한 뒤 속도만 바꾼다."""
    db.execute(
        text("""
            UPDATE app.demo_clock
               SET anchor_virtual = app.demo_now(),
                   anchor_real    = now(),
                   speed          = :speed,
                   updated_at     = now()
             WHERE id = 1
        """),
        {"speed": speed},
    )
    db.commit()
    return read(db)


def reset(db: Session) -> Any:
    """시나리오를 처음 상태로 되돌린다. **의료진 확인 기록도 함께 지운다.**

    매핑 기준점(epoch_virtual)까지 현재 시각으로 다시 잡으므로,
    적재 직후의 재실/퇴실 구성이 그대로 복원된다.

    🔑 왜 알림 상태를 같이 지우는가
       시계만 되돌리면 지난 시연에서 누른 "의료진 재검토" 기록이 그대로 남아,
       새 시연에서 같은 알림이 처음부터 확인된 것처럼 보인다.

    🔑 지우는 것과 남기는 것
       지움 : app.prediction_ack (확인 기록) · app.alert (미사용 · 방어적으로 비움)
       남김 : app.prediction (예측 결과) · mimic.* (원천 데이터) · app.cohort 등
       예측은 알림의 원천이지 알림 자체가 아니고, 다시 계산해도 같은 값이라
       지울 이유가 없다. 시계가 처음으로 돌아가면 자연스럽게 다시 감춰진다.

    ⚠ 환자 배치(app.demo_stay.now_ref)는 건드리지 않는다. 적재가 정한 체류 지점이
      시나리오 그 자체이고, 그중 일부는 리셋 직후부터 ED 도착 +1h 를 넘겨 있어
      예측 결과를 갖는다 — 규칙대로 도래한 예측이라 감추지 않는다.

    ⚠ 시계를 '이동'(advance)할 때는 아무것도 지우지 않는다. 그때는 확인 기록의
      acknowledged_demo_at 이 현재 데모 시각보다 미래면 자동으로 무효가 된다.
    ⚠ app.prediction 은 지우지 않는다. 되돌아간 시점 기준으로 아직 도래하지 않은
      예측이 되어 화면에서 자동으로 빠지고, 시계가 다시 그 시점을 지나면 같은 값으로
      다시 보인다(모델을 다시 돌려도 같은 결과다).
    """
    db.execute(
        text("""
            UPDATE app.demo_clock
               SET epoch_virtual  = now(),
                   anchor_virtual = now(),
                   anchor_real    = now(),
                   speed = 1, updated_at = now()
             WHERE id = 1
        """)
    )
    # prediction_ack 를 참조하는 FK 가 없어 삭제 순서를 따질 필요가 없다.
    db.execute(text("DELETE FROM app.prediction_ack"))
    db.execute(text("DELETE FROM app.alert"))
    db.commit()
    return read(db)
