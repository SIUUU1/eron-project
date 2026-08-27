"""mimic / app 스키마용 declarative base.

public 스키마의 기존 Base 와 분리한다. 이 메타데이터는
`Base.metadata.create_all()` 대상이 아니다 — 해당 테이블은
database/init/*.sql 과 적재 스크립트가 만든다.
"""

from sqlalchemy.orm import DeclarativeBase


class EdBase(DeclarativeBase):
    pass
