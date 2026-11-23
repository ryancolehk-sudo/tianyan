"""Verified current KPL theme summary; no name joins or inferred zeroes."""
from decimal import Decimal

from app.market import direction_sources as source
from app.market.protocol import SourceFailure

DATASET = 'market.direction.kpl_limit_counts_v1'


def latest(db, provider_id, day, clock):
    """One last observation per exact code, never last-N events across boards."""
    from sqlalchemy import select
    from app.evidence.models import EvidenceVersion
    return list(db.scalars(select(EvidenceVersion).where(
        EvidenceVersion.source_id == provider_id, EvidenceVersion.kind == 'reference',
        EvidenceVersion.visibility == 'shared', EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.provenance == 'source_reported', EvidenceVersion.received_at <= clock,
        EvidenceVersion.payload['dataset'].astext == DATASET,
        EvidenceVersion.payload['observed_day'].astext == day.isoformat(),
    ).distinct(EvidenceVersion.source_key).order_by(EvidenceVersion.source_key,
        EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(400)))


def batch(response, day, code, name):
    import hashlib
    from app.evidence.schemas import ReferencePayload
    from app.market.reference import ReferenceBatch
    count = parse(response, day)
    row = dict(code=code, name=name, source_day=day.isoformat(), limit_up_count=count,
        limit_up_received_at=response.received_at.isoformat(), body_sha256=hashlib.sha256(response.body).hexdigest())
    payload = ReferencePayload(dataset=DATASET, title='开盘啦题材涨停家数', observed_day=day,
        source_url=source.KPL_URL, attribution='开盘啦同代码题材摘要；本人本机研究',
        scope='GetPlate_Info_QJ原始List[5]；每板块独立接收采样，不按其他软件成分推算，不补零。',
        columns=[dict(key=key,label=key) for key in row], rows=[row])
    return ReferenceBatch(records=[(DATASET+':'+day.isoformat()+':'+code,payload,None)],expected=1,requests=1)


def parse(response, day):
    try:
        data = source._unpack('direction_kpl_strength', response.body)
        if not isinstance(data, dict) or str(data.get('errcode')) != '0':
            raise SourceFailure('upstream_unavailable')
        if data.get('Date') != day.isoformat():
            raise SourceFailure('source_date_mismatch')
        row = data.get('List')
        if not isinstance(row, list) or len(row) != 8:
            raise SourceFailure('schema_changed')
        # Theme summaries begin with rank and strength. Other index families
        # have a different layout and must not turn their reserved zero into a count.
        rank = int(source._number(row[0], integer=True, signed=False, maximum=Decimal(400)))
        if rank < 1:
            raise SourceFailure('schema_changed')
        source._number(row[1])
        count = source._number(row[5], integer=True, signed=False, maximum=Decimal(10000))
        if count is None:
            raise SourceFailure('schema_changed')
        return count
    except (ValueError, TypeError, ArithmeticError):
        raise SourceFailure('schema_changed') from None
