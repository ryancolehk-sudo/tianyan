from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from app.config import settings


class Base(DeclarativeBase):
    pass


@lru_cache
def engine():
    return create_engine(
        settings().database_url, pool_pre_ping=True, pool_size=5, max_overflow=5,
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=10000"},
    )


def db_session():
    with Session(engine(), expire_on_commit=False) as db:
        yield db
