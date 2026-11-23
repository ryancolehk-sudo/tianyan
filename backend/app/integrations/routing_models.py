"""Instance-wide source choices; source evidence and credentials remain separate."""
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DataRoutes(Base):
    __tablename__ = 'data_routes'
    __table_args__ = (CheckConstraint('id = 1'), CheckConstraint('revision > 0'))
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    revision: Mapped[int] = mapped_column(default=1)
    choices: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
