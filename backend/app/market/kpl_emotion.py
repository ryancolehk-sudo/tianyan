"""KPL's reported 0–100 scalar, sampled at receipt time; never reconstructed."""
from datetime import date, time, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.evidence.common import now
from app.evidence.models import EvidenceVersion
from app.evidence.schemas import ReferencePayload
from app.market.protocol import SHANGHAI, SourceFailure
from app.market.reference import ReferenceBatch

FAMILY = 'direction_kpl_emotion'
DATASET = 'market.direction.kpl_emotion'
MAX_SAMPLES = 600
CHART_WINDOWS = ((time(9, 15), time(11, 30)), (time(13), time(15)))


def parse(body, received_at, *, expected_day=None):
    from app.market.direction_sources import _unpack, _number, _day, KPL_URL
    try:
        data = _unpack(FAMILY, body)
        if not isinstance(data, dict) or str(data.get('errcode')) != '0':
            raise SourceFailure('upstream_unavailable')
        rows = data.get('info')
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError()
        row = rows[0]
        day = _day(row['Day'], received_at)
        if expected_day is not None and day != expected_day:
            raise SourceFailure('source_day_mismatch')
        value = _number(row['strong'], signed=False, maximum=Decimal(100))
        if value is None:
            raise ValueError()
        # Receipt included even for unchanged values: equal readings are real
        # observations too, and must survive evidence content deduplication.
        payload = ReferencePayload(dataset=DATASET, title='开盘啦市场情绪',
            source_url=KPL_URL, attribution='开盘啦公开情绪指标；本人本机研究',
            observed_day=day, scope='ChangeStatistics/HomeDingPan strong 原始0–100值；来源只有日期，横轴为接收采样时间。盘后、跨日回值不补为盘中点；缺测不插值。',
            columns=[dict(key=k,label=k) for k in ('value','source_day','sample_received_at')],
            rows=[dict(value=value, source_day=day.isoformat(), sample_received_at=received_at.isoformat())])
        return ReferenceBatch(records=[(DATASET,payload,None)], expected=1, requests=1)
    except SourceFailure:
        raise
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError):
        raise SourceFailure('schema_changed') from None


def project(records, day, clock):
    latest = None
    points = []
    for record in records[:MAX_SAMPLES]:
        if record.received_at > clock or record.payload.get('observed_day') != day.isoformat():
            continue
        rows = record.payload.get('rows', [])
        if len(rows) != 1 or rows[0].get('source_day') != day.isoformat():
            continue
        try:
            value = Decimal(str(rows[0]['value']))
            if not value.is_finite() or not 0 <= value <= 100:
                continue
        except (KeyError, ValueError, ArithmeticError):
            continue
        point = dict(received_at=record.received_at.isoformat(), value=str(value))
        if latest is None:
            latest = point
        local = record.received_at.astimezone(SHANGHAI)
        t = local.time().replace(tzinfo=None)
        if local.date() == day and any(start <= t <= end for start, end in CHART_WINDOWS):
            points.append(point)
    return dict(trade_day=day.isoformat(), latest=latest, points=list(reversed(points)),
        truncated=len(records)>MAX_SAMPLES)


def read(db, day=None, cutoff=None):
    from app.market.review_store import permitted_provider, available_trade_days
    from app.market.session import market_session
    clock = cutoff or now(db)
    day = day or market_session(db, clock).trade_day or clock.astimezone(SHANGHAI).date()
    previous = available_trade_days(db, limit=1, end_day=day-timedelta(days=1), as_of_time=cutoff)
    prior = previous[-1] if previous else None
    provider = permitted_provider(db, FAMILY, cutoff)
    def series(target):
        if target is None:
            return None
        records = [] if provider is None else list(db.scalars(select(EvidenceVersion).where(
            EvidenceVersion.source_id==provider.id, EvidenceVersion.kind=='reference',
            EvidenceVersion.owner_id.is_(None), EvidenceVersion.visibility=='shared',
            EvidenceVersion.payload['dataset'].astext==DATASET,
            EvidenceVersion.payload['observed_day'].astext==target.isoformat(),
            EvidenceVersion.received_at<=clock,
        ).order_by(EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(MAX_SAMPLES+1)))
        return project(records,target,clock)
    return dict(source='开盘啦', time_basis='received_at', interval_seconds=60,
        state='enabled' if provider else 'disabled', today=series(day), yesterday=series(prior))
