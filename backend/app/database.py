from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.base import Base

DATABASE_URL = settings.database_url


engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    # 세션 타임존을 명시한다. 서버 기본값은 Etc/UTC 라, 지정하지 않으면
    # now() 기반 데모 시간축이 컨테이너 시계(Asia/Seoul)와 어긋난다.
    connect_args={"options": f"-c timezone={settings.db_timezone}"},
)


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)