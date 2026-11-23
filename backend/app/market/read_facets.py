"""Derived metadata; always join back to rights/cutoff-checked evidence."""
import uuid

from sqlalchemy import Index, String, Text, case, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.evidence.models import EvidenceVersion
from app.market.trading_datasets import distinct_date_values


class EvidenceReadFacet(Base):
    __tablename__ = 'evidence_read_facets'
    __table_args__ = (Index('ix_evidence_read_observed', 'observed_day', 'evidence_id'),)
    # No FK: a disposable index cannot obstruct source retention or restoration.
    evidence_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    trade_dates: Mapped[list[str]] = mapped_column(ARRAY(Text()))
    observed_day: Mapped[str | None] = mapped_column(Text())
    dataset: Mapped[str | None] = mapped_column(Text())
    row_dataset: Mapped[str | None] = mapped_column(Text())
    requested_day: Mapped[str | None] = mapped_column(Text())
    batch_hash: Mapped[str | None] = mapped_column(Text())
    ths_member_capture_id: Mapped[str | None] = mapped_column(Text())


def with_facets(query):
    return query.outerjoin(EvidenceReadFacet,
        (EvidenceReadFacet.evidence_id == EvidenceVersion.id) &
        (EvidenceReadFacet.content_hash == EvidenceVersion.content_hash))


def trade_dates():
    # Missing/stale derived rows fall back to the authoritative original.
    return func.coalesce(EvidenceReadFacet.trade_dates, distinct_date_values('trade_date'))


def observed_day():
    return value('observed_day')


def value(name):
    original = {
        'dataset': EvidenceVersion.payload['dataset'].astext,
        'observed_day': EvidenceVersion.payload['observed_day'].astext,
        'row_dataset': EvidenceVersion.payload['rows'][0]['dataset'].astext,
        'requested_day': EvidenceVersion.payload['rows'][0]['requested_day'].astext,
        'batch_hash': EvidenceVersion.payload['rows'][0]['batch_hash'].astext,
        # This facet is deliberately scoped: adding it does not require
        # decoding every other source's historical large reference table.
        'ths_member_capture_id': case(
            (EvidenceVersion.payload['dataset'].astext == 'tushare.ths_member',
             EvidenceVersion.payload['rows'][0]['capture_id'].astext), else_=None),
    }[name]
    # A known-null facet must stay null. COALESCE would decode the large raw
    # table again whenever an optional first-row field is legitimately absent.
    return case((EvidenceReadFacet.evidence_id.is_not(None), getattr(EvidenceReadFacet, name)),
                else_=original)
