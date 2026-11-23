"""Immutable, permission-checked review tables and complete pagination manifests."""
import uuid
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import load_only

from app.evidence.common import lock
from app.evidence.models import EvidenceVersion, SourceVersion
from app.evidence.schemas import ReferencePayload
from app.market.protocol import SHANGHAI
from app.market.review_protocol import DATASET_FAMILIES, PUBLIC_FAMILIES, UNITS
from app.models import Provider


def family_id(family):
    return uuid.uuid5(uuid.NAMESPACE_URL, 'jiulong-pro-max/source/' + family)


def manifest_key(dataset, trade_day, scope='all'):
    return f'review.manifest:{dataset}:{trade_day}:{scope}'


def permitted_provider(db, family, as_of_time=None):
    from app.provider_access import cloud_share_allowed
    provider = db.get(Provider, family_id(family))
    if provider is None or not provider.configured or not provider.enabled or provider.entitlement_status != 'verified':
        return None
    if any(provider.rights.get(key) != 'allowed' for key in ('display', 'cache')):
        return None
    if not cloud_share_allowed(provider):
        return None
    if family in PUBLIC_FAMILIES and any(provider.rights.get(key) == 'allowed' for key in ('export', 'redistribute')):
        return None
    if as_of_time is not None:
        version = db.scalar(select(SourceVersion).where(
            SourceVersion.source_id == provider.id, SourceVersion.recorded_at <= as_of_time,
        ).order_by(SourceVersion.recorded_at.desc(), SourceVersion.id.desc()).limit(1))
        desc = version.descriptor if version else {}
        if (not desc.get('configured') or not desc.get('enabled') or
            desc.get('entitlement_status') != 'verified' or
            any(desc.get('rights', {}).get(key) != 'allowed' for key in ('display', 'cache'))):
            return None
    return provider


def available_trade_days(db, limit=63, end_day=None, as_of_time=None):
    from sqlalchemy import or_
    from app.market.calendar_selection import FAMILIES,common_open_days,selected_days
    cutoff = as_of_time or datetime.now(UTC)
    local = cutoff.astimezone(SHANGHAI)
    last = end_day or local.date()
    if last > local.date():
        return []
    if last == local.date() and local.time().replace(tzinfo=None) < time(15, 5):
        last -= timedelta(days=1)
    start = last - timedelta(days=365)
    providers={p.id:p.family for family in FAMILIES
        if (p:=permitted_provider(db,family,as_of_time)) is not None}
    rows=db.scalars(select(EvidenceVersion).where(
        EvidenceVersion.source_id.in_(providers),EvidenceVersion.kind=='calendar',
        EvidenceVersion.visibility=='shared',EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.received_at<=cutoff,
        or_(*(EvidenceVersion.source_key.between('calendar:'+exchange+':'+start.isoformat(),
            'calendar:'+exchange+':'+last.isoformat()) for exchange in ('SSE','SZSE')))
    ).order_by(EvidenceVersion.version_number.desc(),EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()))
    calendars,_=selected_days(rows,providers)
    return common_open_days(calendars)[-limit:]


def _manifest(db, dataset, trade_day, scope, cutoff):
    prefix = 'review.manifest:' + dataset + ':'
    stmt = select(EvidenceVersion).where(
        EvidenceVersion.kind == 'reference', EvidenceVersion.visibility == 'shared',
        EvidenceVersion.owner_id.is_(None), EvidenceVersion.received_at <= cutoff,
        EvidenceVersion.source_id == family_id(DATASET_FAMILIES[dataset]),
        EvidenceVersion.payload['dataset'].astext == 'review.manifest',
        EvidenceVersion.source_key.startswith(prefix),
        EvidenceVersion.payload['rows'][0]['scope'].astext == scope,
    )
    if dataset in {'indices', 'emotion_history', 'new_high_history'}:
        stmt = stmt.where(
            EvidenceVersion.payload['rows'][0]['requested_day'].astext >= trade_day.isoformat(),
            EvidenceVersion.payload['rows'][0]['start_day'].astext <= trade_day.isoformat())
    elif dataset in {'tencent_minutes', 'sina_minutes'}:
        # Rolling vendor windows must not hide a previously saved older day.
        stmt = stmt.where(
            EvidenceVersion.payload['rows'][0]['actual_start_day'].astext <= trade_day.isoformat(),
            EvidenceVersion.payload['rows'][0]['actual_end_day'].astext >= trade_day.isoformat())
    else:
        stmt = stmt.where(EvidenceVersion.source_key == manifest_key(dataset, trade_day, scope))
    return db.scalar(stmt.order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(1))


def load_dataset(db, dataset, trade_day, scope='all', as_of_time=None, include_rows=True):
    """Only return tables belonging to one fully committed manifest.

    None is not encoded as an empty source table. Missing/revoked/broken lineage
    returns a non-complete state. Reconstructed history is never an old receipt.
    """
    if dataset not in DATASET_FAMILIES:
        raise ValueError('unsupported review dataset')
    if isinstance(trade_day, str):
        trade_day = date.fromisoformat(trade_day)
    cutoff = as_of_time or datetime.now(UTC)
    base = dict(state='missing', dataset=dataset, requested_day=trade_day.isoformat(),
                scope=scope, rows=[], row_count=0, evidence_refs=[], source_id=None, received_at=None,
                knowledge_mode='received_by_cutoff' if as_of_time else 'historical_reference')
    provider = permitted_provider(db, DATASET_FAMILIES[dataset], as_of_time)
    if provider is None:
        return {**base, 'state': 'permission_denied'}
    base['source_id'] = str(provider.id)
    manifest = _manifest(db, dataset, trade_day, scope, cutoff)
    if manifest is None:
        return base
    records = manifest.payload.get('rows', [])
    if not records:
        return {**base, 'state': 'broken_lineage'}
    refs, result, count, part_ids = [], [], 0, set()
    read_rows = include_rows or dataset in {'indices', 'emotion_history', 'new_high_history', 'tencent_minutes', 'sina_minutes'}
    for number, entry in enumerate(records):
        try:
            part_id = uuid.UUID(entry['part_id'])
            expected_count = int(entry['row_count'])
            if (entry['dataset'] != dataset or entry['scope'] != scope or int(entry['part_number']) != number
                or expected_count < 0 or part_id in part_ids):
                raise ValueError()
            part_ids.add(part_id)
        except (KeyError, ValueError, TypeError):
            return {**base, 'state': 'broken_lineage'}
        part = db.scalar(select(EvidenceVersion).options(load_only(
            EvidenceVersion.id, EvidenceVersion.source_id, EvidenceVersion.content_hash,
            EvidenceVersion.kind, EvidenceVersion.visibility, EvidenceVersion.owner_id, EvidenceVersion.received_at,
        )).where(EvidenceVersion.id == part_id))
        if (part is None or part.source_id != manifest.source_id or part.content_hash != entry.get('part_hash')
            or part.kind != 'reference' or part.visibility != 'shared' or part.owner_id is not None
            or part.received_at > manifest.received_at or part.received_at > cutoff):
            return {**base, 'state': 'broken_lineage'}
        ref = dict(id=str(part.id), content_hash=part.content_hash, source_id=str(part.source_id),
                   received_at=part.received_at.isoformat(), start_row=count, row_count=expected_count)
        refs.append(ref)
        if read_rows:
            payload = part.payload
            rows = payload.get('rows', [])
            if payload.get('dataset') != 'review.' + dataset or len(rows) != expected_count:
                return {**base, 'state': 'broken_lineage'}
            if dataset in {'indices', 'emotion_history', 'new_high_history'}:
                ref['selected_rows'] = [i for i, row in enumerate(rows) if row.get('trade_date') == trade_day.strftime('%Y%m%d')]
                rows = [row for row in rows if row.get('trade_date') == trade_day.strftime('%Y%m%d')]
            elif dataset in {'tencent_minutes', 'sina_minutes'}:
                ref['selected_rows'] = [i for i, row in enumerate(rows) if str(row.get('trade_time', ''))[:10] == trade_day.isoformat()]
                rows = [row for row in rows if str(row.get('trade_time', ''))[:10] == trade_day.isoformat()]
            ref['row_count'] = len(rows)
            count += len(rows)
            if include_rows:
                result.extend(rows)
        else:
            count += expected_count
    state = 'missing' if read_rows and not count and dataset in {'indices', 'emotion_history', 'new_high_history', 'tencent_minutes', 'sina_minutes'} else 'complete'
    return {**base, 'state': state, 'rows': result, 'row_count': count, 'evidence_refs': refs,
            'manifest_ref': dict(id=str(manifest.id), content_hash=manifest.content_hash),
            'received_at': manifest.received_at.isoformat(),
            'requested_start_day': records[0].get('start_day'),
            'requested_end_day': records[0].get('requested_day'),
            'coverage_scope': records[0].get('coverage_scope', 'date_scope_table'),
            'configuration_sha256': records[0].get('configuration_sha256')}


def persist_pages(db, provider, config, job, pages):
    """Publish all pages and their manifest in one transaction, or none."""
    from app.config import settings
    from app.market.collect import store_observation
    from app.market.protocol import SourceFailure
    from app.market.service import permission_reason
    if permission_reason(provider):
        raise SourceFailure('permission_missing')
    context = job.payload['review_context']
    dataset, scope = context['dataset'], context['scope']
    lock(db, f'source:records:{provider.id}')
    entries = []
    total = 0
    for number, (page, received_at) in enumerate(pages):
        units = UNITS.get(dataset, {})
        payload = ReferencePayload(
            dataset='review.' + dataset, title='每日复盘 · ' + dataset,
            source_url=page.source_url, attribution='原始来源：' + provider.name,
            scope=('按原数据日保存的复盘资料；本机收到时间另列。历史补采不等于当时已知；'
                   '缺失不补零，不作为逐笔事件、AI判断或交易指令。' + page.scope)[:2000],
            columns=[{'key': field, 'label': field, 'unit': units.get(field)} for field in page.fields],
            rows=page.rows, observed_day=received_at.astimezone(SHANGHAI).date(),
        )
        key = f'review.data:{dataset}:{context["day"]}:{scope}:{job.id}:{number}'
        store_observation(db, provider, config, source_key=key, kind='reference',
                          payload=payload, received_at=received_at, eligible=False)
        part = db.scalar(select(EvidenceVersion).where(
            EvidenceVersion.source_id == provider.id, EvidenceVersion.source_key == key,
        ).order_by(EvidenceVersion.version_number.desc()).limit(1))
        entries.append(dict(dataset=dataset, requested_day=context['day'],
                            start_day=context['start_day'], scope=scope, part_number=str(number),
                            part_id=str(part.id), part_hash=part.content_hash, row_count=str(len(page.rows)),
                            raw_hash=page.raw_hash, configuration_sha256=context['configuration_sha256'],
                            job_id=str(job.id), collector_revision=settings().build_revision,
                            coverage_scope='daily_latest_20_news' if dataset == 'news' else 'date_scope_table'))
        total += len(page.rows)
    if not entries:
        raise SourceFailure('review_no_pages')
    final_received = max(stamp for _, stamp in pages)
    if dataset in {'tencent_minutes', 'sina_minutes'}:
        actual_days = [row['trade_time'][:10] for page, _ in pages for row in page.rows]
        for entry in entries:
            entry.update(actual_start_day=min(actual_days), actual_end_day=max(actual_days))
    manifest = ReferencePayload(
        dataset='review.manifest', title='每日复盘 · 完整采集批次',
        source_url=pages[0][0].source_url, attribution=provider.name,
        scope='只有全部分页验证并提交后才存在此清单；每部分绑定不可变证据ID和hash。',
        columns=[{'key': key, 'label': key} for key in entries[0]], rows=entries,
        observed_day=final_received.astimezone(SHANGHAI).date(),
    )
    key = manifest_key(dataset, context['day'], scope)
    store_observation(db, provider, config, source_key=key, kind='reference',
                      payload=manifest, received_at=final_received, eligible=False, repeated_state=True)
    return total
