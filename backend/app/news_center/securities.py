"""Read source-declared announcement identities without rewriting old evidence.

An official announcement ID can recover an explicit code from an already saved
announcement archive. Headlines, current instrument names and model output are
never identity evidence. Fixed editions can use only archives received by their
original cutoff; later receipts cannot enrich their historical identity.
"""
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError
from sqlalchemy import column, func, or_, select, true
from sqlalchemy.dialects.postgresql import JSONB

from app.evidence.models import EvidenceVersion
from app.evidence.schemas import NewsSecurity
from app.models import Provider


def _subject(value):
    if not isinstance(value, dict):
        return None
    value = dict(value)
    for key in ('code', 'name', 'exchange'):
        if isinstance(value.get(key), str):
            value[key] = value[key].strip() or None
    try:
        return NewsSecurity.model_validate(value).model_dump()
    except (ValidationError, ValueError):
        return None


def _view(value, evidence, provider, fields, *, basis='source_reported', source_url=None):
    return {**value, 'basis': basis, 'evidence_id': evidence.id, 'source_id': evidence.source_id,
        'source_name': provider.name, 'source_url': source_url or evidence.payload['source_url'],
        'received_at': evidence.received_at, 'content_hash': evidence.content_hash, 'source_fields': fields}


def _direct(evidence, provider):
    values = evidence.payload.get('securities')
    declared = 'securities' in evidence.payload or provider.family == 'cninfo_announcements' and any(
        key in evidence.payload for key in ('secCode', 'secName'))
    if declared and getattr(evidence, 'provenance', None) != 'source_reported':
        return [], 'unverified'
    if 'securities' in evidence.payload and (not isinstance(values, list) or len(values) > 20):
        return [], 'unverified'
    if isinstance(values, list) and len(values) <= 20:
        subjects = []
        for index, raw in enumerate(values):
            value = _subject(raw)
            if value is None:
                return [], 'unverified'
            fields = [f'payload.securities[{index}].{key}' for key in ('code', 'name', 'exchange') if value[key]]
            subjects.append(_view(value, evidence, provider, fields))
        if subjects:
            return subjects, 'source_provided'
    # Compatibility for already stored publisher-shaped metadata, not generic
    # "code" or company-looking words from an unrelated news source.
    if provider.family == 'cninfo_announcements' and any(key in evidence.payload for key in ('secCode', 'secName')):
        value = _subject({'code': evidence.payload.get('secCode'), 'name': evidence.payload.get('secName')})
        if value:
            fields = ['payload.' + key for key, present in [('secCode', value['code']), ('secName', value['name'])] if present]
            return [_view(value, evidence, provider, fields)], 'source_provided'
        return [], 'unverified'
    return [], 'missing'


def _announcement_id(evidence, provider):
    if provider.family != 'cninfo_announcements' or evidence.provenance != 'source_reported':
        return None
    parts = urlsplit(evidence.payload.get('source_url') or '')
    values = parse_qs(parts.query).get('announcementId', [])
    if (parts.scheme != 'https' or parts.hostname != 'www.cninfo.com.cn' or parts.username or parts.password
            or parts.path != '/new/disclosure/detail' or len(values) != 1
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', values[0])
            or evidence.source_key != 'news:' + values[0]):
        return None
    return values[0]


def news_securities(db, rows, *, cutoff=None):
    """Batch a bounded page of identities; every additional proof stays visible."""
    result = {evidence.id: _direct(evidence, provider) for evidence, provider in rows}
    missing = {}
    for evidence, provider in rows:
        if result[evidence.id][0]:
            continue
        identity = _announcement_id(evidence, provider)
        if identity:
            missing[evidence.id] = identity
    if not missing:
        return result
    historical = cutoff is not None
    cutoff = cutoff or datetime.now(UTC)
    cell = func.jsonb_array_elements(EvidenceVersion.payload['rows']).table_valued(column('value', JSONB)).lateral()
    # Select identity metadata only: archived PDF text is not loaded for a list.
    stmt = select(EvidenceVersion.id, EvidenceVersion.source_id, EvidenceVersion.content_hash,
        EvidenceVersion.received_at, EvidenceVersion.availability, EvidenceVersion.available_at, Provider.name.label('source_name'),
        cell.c.value['id'].astext.label('announcement_id'), cell.c.value['ts_code'].astext.label('code'),
        cell.c.value['secName'].astext.label('name'), cell.c.value['source_url'].astext.label('source_url')).select_from(EvidenceVersion).join(
            Provider, Provider.id == EvidenceVersion.source_id).join(cell, true()).where(
        EvidenceVersion.kind == 'reference', EvidenceVersion.visibility == 'shared',
        EvidenceVersion.provenance == 'source_reported',
        EvidenceVersion.payload['dataset'].astext == 'tomorrow.announcement_text',
        EvidenceVersion.received_at <= cutoff,
        or_(EvidenceVersion.available_at.is_(None), EvidenceVersion.available_at <= cutoff),
        Provider.family == 'cninfo_announcements',
        Provider.enabled.is_(True), Provider.configured.is_(True), Provider.entitlement_status == 'verified',
        Provider.rights['display'].astext == 'allowed', Provider.rights['cache'].astext == 'allowed',
        cell.c.value['id'].astext.in_(set(missing.values()))).order_by(
            EvidenceVersion.received_at.desc(), EvidenceVersion.id).limit(1001)
    if historical:
        stmt = stmt.where(EvidenceVersion.availability == 'known', EvidenceVersion.available_at <= cutoff,
            EvidenceVersion.historical_eligible.is_(True))
    saved = list(db.execute(stmt))
    if len(saved) > 1000:
        # Do not silently pick an identity from a truncated conflict set.
        return {key: ([], 'unverified') if key in missing else value for key, value in result.items()}
    candidates = {}
    for row in saved:
        code = row.code or ''
        parts = urlsplit(row.source_url or '')
        if (not re.fullmatch(r'[0-9]{6}\.(SH|SZ|BJ)', code) or parts.scheme != 'https'
                or parts.hostname != 'static.cninfo.com.cn' or parts.username or parts.password
                or not re.fullmatch(r'/finalpage/\d{4}-\d{2}-\d{2}/' + re.escape(row.announcement_id) + r'\.pdf', parts.path)):
            continue
        value = _subject({'code': code[:6], 'name': row.name,
            'exchange': {'SH': 'SSE', 'SZ': 'SZSE', 'BJ': 'BSE'}[code[-2:]]})
        if value:
            candidates.setdefault(row.announcement_id, []).append((row, value))
    for evidence_id, identity in missing.items():
        matches = candidates.get(identity, [])
        identities = {(value['code'], value['name'], value['exchange']) for _, value in matches}
        if len(identities) > 1:
            result[evidence_id] = [], 'unverified'
        elif matches:
            row, value = next(((row, value) for row, value in matches if row.availability == 'known' and row.available_at is not None), matches[0])
            fields = ['rows[id=' + identity + '].ts_code'] + (['rows[id=' + identity + '].secName'] if value['name'] else [])
            result[evidence_id] = [{**value, 'basis': 'archived_announcement', 'evidence_id': row.id,
                'source_id': row.source_id, 'source_name': row.source_name, 'source_url': row.source_url,
                'received_at': row.received_at, 'content_hash': row.content_hash, 'source_fields': fields}], (
                    'source_provided' if row.availability == 'known' and row.available_at is not None else 'unverified')
    return result
