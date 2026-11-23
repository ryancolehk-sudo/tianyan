"""Bounded operational THS failure shapes; no HTML, names or market values."""
from datetime import datetime
from hashlib import sha256
import re

FAMILIES = {'ths_industry_funds': 'hyzjl', 'ths_concept_funds': 'gnzjl'}
CHECKPOINTS = {'decode', 'pagination_count', 'pagination_format', 'page_mismatch',
    'page_total', 'row_count', 'headings', 'row_width', 'numeric_field'}
INTEGER_FIELDS = {'version': (1, 1), 'requested_page': (1, 16), 'actual_page': (0, 999),
    'total_pages': (0, 999), 'body_bytes': (0, 2_000_001), 'pager_count': (0, 10000),
    'row_count': (0, 10000), 'heading_count': (0, 10000), 'attempt': (1, 2)}


def shape(body, table, category, requested_page, checkpoint):
    result = dict(version=1, checkpoint=checkpoint, category=category,
        requested_page=requested_page, body_bytes=min(len(body), 2_000_001),
        body_sha256=sha256(body).hexdigest() if len(body) <= 2_000_000 else None,
        pager_count=min(len(table.pages), 10000), row_count=min(len(table.rows), 10000),
        heading_count=min(len(table.headings), 10000),
        money_headings_match=table.headings[4:7] == ['流入资金(亿)', '流出资金(亿)', '净额(亿)'])
    if len(table.pages) == 1 and re.fullmatch(r'\d{1,3}/\d{1,3}', table.pages[0]):
        result['actual_page'], result['total_pages'] = map(int, table.pages[0].split('/'))
    return result


def safe(value, family):
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value.get('version') != 1
            or not isinstance(family, str) or family not in FAMILIES
            or value.get('family') != family or not isinstance(value.get('events'), list)
            or not 1 <= len(value['events']) <= 18):
        return None
    events = []
    for event in value['events']:
        if not isinstance(event, dict):
            return None
        if (event.get('category') != FAMILIES[family] or not isinstance(event.get('checkpoint'), str)
                or event['checkpoint'] not in CHECKPOINTS or not isinstance(event.get('reason'), str)
                or event['reason'] not in {'incomplete_page', 'schema_changed'}
                or type(event.get('money_headings_match')) is not bool):
            return None
        clean = {key: event[key] for key in ('category', 'checkpoint', 'reason', 'money_headings_match')}
        for key, (low, high) in INTEGER_FIELDS.items():
            if key not in event and key in {'actual_page', 'total_pages'}:
                continue
            number = event.get(key)
            if type(number) is not int or not low <= number <= high:
                return None
            clean[key] = number
        digest = event.get('body_sha256')
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)):
            return None
        clean['body_sha256'] = digest
        try:
            stamp = datetime.fromisoformat(event.get('received_at'))
            if stamp.utcoffset() is None:
                return None
        except (ValueError, TypeError):
            return None
        clean['received_at'] = stamp.isoformat()
        events.append(clean)
    return dict(version=1, family=family, events=events)


def record(current, run, family, diagnostic):
    clean = safe(diagnostic, family)
    if clean is None:
        return
    existing = current.result_ref or {}
    previous = existing.get('ths_page_failures')
    history = previous.get('runs', []) if isinstance(previous, dict) else []
    if not isinstance(history, list):
        history = []
    identity = str(run.id)
    history = [item for item in history[-5:] if isinstance(item, dict) and item.get('run_id') != identity]
    history.append(dict(run_id=identity, request_count=run.request_count, status=run.status, diagnostic=clean))
    current.result_ref = {**existing, 'ths_page_failures': dict(version=1, runs=history[-5:])}
