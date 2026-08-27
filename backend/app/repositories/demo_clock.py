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
    """시나리오를 처음 상태로 되돌린다.

    매핑 기준점(epoch_virtual)까지 현재 시각으로 다시 잡으므로,
    적재 직후의 재실/퇴실 구성이 그대로 복원된다.
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
    db.commit()
    return read(db)
