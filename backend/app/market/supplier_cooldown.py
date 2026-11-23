"""Read actual shared supplier deadlines independently of sampling schedules."""
from sqlalchemy import func,select

from app.market.models import DataFetchRun,SourceSettings
from app.models import Provider

THS_FAMILIES=frozenset({'ths_industry_funds','ths_concept_funds'})


def shared_deadline(db,clock,families):
    ids=select(Provider.id).where(Provider.family.in_(families))
    short=db.scalar(select(func.max(DataFetchRun.retry_not_before)).where(
        DataFetchRun.source_id.in_(ids),DataFetchRun.received_at<=clock,DataFetchRun.retry_not_before>clock))
    long=db.scalar(select(func.max(SourceSettings.legacy_cooldown_until)).where(
        SourceSettings.source_id.in_(ids),SourceSettings.legacy_cooldown_until>clock))
    return max(value for value in (short,long) if value is not None) if short or long else None
