"""Bounded pagination of the existing funds endpoint; never a new data source.

Each physical request uses the caller's original permission and pacing permit.
Individual pages retain their receipt clocks. A partial scan may retain actual
points, but cannot claim a complete ranking or replace a complete page set.
"""
from datetime import UTC, datetime
import hashlib
import json
import math
import re
import time

from app.market.protocol import Response, SourceFailure, SourceRedirect
from app.market.reference import ReferenceBatch
from app.evidence.schemas import ReferenceColumn

FORMAT = 'eastmoney-funds-pages-v1'
MAX_TOTAL = 5000
MAX_PAGES = 50
MAX_STOCKS = 8000
MAX_STOCK_PAGES = 80
# This is a local envelope of separately bounded physical responses, not a
# larger upstream response allowance. ReferencePayload still caps each table.
MAX_CAPTURE_BODY = 16_000_000
MAX_PAGE_BYTES = 8_000_000
META = ('capture_id', 'capture_total', 'capture_pages', 'capture_complete', 'capture_page', 'capture_received_at')


def limits(table):
    return (MAX_STOCKS, MAX_STOCK_PAGES) if table == 'stock_funds' else (MAX_TOTAL, MAX_PAGES)


def fetch(reserve, *, include_catalog=False):
    from app.market import eastmoney_trading as m
    from app.worker_watchdog import report_progress
    started = time.monotonic()
    count = 0
    tables = []
    metadata = {}
    page_bytes = 0

    def request(url, attempt):
        nonlocal count
        reserve()
        count += 1
        attempt['requests'] += 1
        try:
            return m.fetch_https(url)
        except SourceRedirect as redirect:
            target = m.eastmoney_allocation_url(url, redirect)
            reserve()
            count += 1
            attempt['requests'] += 1
            return m.fetch_eastmoney_allocation(target, original_url=url)

    try:
        for table, category, title in m.TABLES['eastmoney_funds']:
            piece = dict(table=table, title=title, pages=[], attempts=[], total=None, page_count=1, ordering='code_descending')
            tables.append(piece)
            page = 1
            while page <= piece['page_count']:
                attempt = dict(page=page, requests=0, reason=None)
                piece['attempts'].append(attempt)
                try:
                    # Rank by immutable code while scanning, then compute the
                    # funds ranking from the actually saved values. A changing
                    # funds sort would move identities across page boundaries.
                    response = request(m.ranking_url(category, funds=True, page=page, stable_codes=True), attempt)
                    payload = m.parse_ranking(response.body, response.received_at, 'market.eastmoney.' + table,
                                              title, industry_level=2 if category == 'industry_l2' else None, capture_page=page)
                    total = json.loads(response.body)['data']['total']
                    maximum, max_pages = limits(table)
                    if not 0 <= total <= maximum:
                        raise SourceFailure('funds_universe_out_of_bounds')
                    if page == 1:
                        piece['total'] = total
                        if total:
                            if not payload.rows:
                                raise SourceFailure('schema_changed')
                            piece['page_count'] = math.ceil(total / len(payload.rows))
                            if piece['page_count'] > max_pages:
                                raise SourceFailure('funds_universe_out_of_bounds')
                    page_bytes += len(response.body)
                    if page_bytes > MAX_PAGE_BYTES:
                        raise SourceFailure('response_too_large')
                    piece['pages'].append(dict(page=page, received_at=response.received_at.isoformat(),
                        body=response.body.decode('utf-8')))
                except SourceFailure as error:
                    if error.reason not in m.RECOVERABLE_TABLE_FAILURES:
                        raise
                    attempt['reason'] = error.reason
                page += 1
                # A finished, bounded page step is real progress even when its
                # transport result is a recoverable failure. Starting HTTP,
                # waiting for a permit and renewing a lease are not progress.
                report_progress(dict(table=table, page=page-1, page_count=piece['page_count'],
                    status='failed' if attempt['reason'] else 'accepted', request_count=count))
        if not any(table['pages'] for table in tables):
            raise SourceFailure(next((a['reason'] for t in reversed(tables) for a in reversed(t['attempts']) if a['reason']), 'upstream_unavailable'))
        if include_catalog:
            from app.market.industry_catalog import fetch_catalog
            try:
                reserve()
            except SourceFailure as error:
                if error.reason not in m.CATALOG_DEFERRALS:
                    raise
                metadata['catalog_deferred'] = dict(reason=error.reason, retry_after=error.retry_after)
            else:
                count += 1
                try:
                    response = fetch_catalog()
                    metadata['catalog'] = dict(received_at=response.received_at.isoformat(), body=response.body.decode('utf-8'))
                except SourceFailure as error:
                    if error.reason not in m.CATALOG_FAILURES:
                        raise
                    metadata['catalog_failure'] = dict(reason=error.reason, retry_after=error.retry_after)
        value = dict(format=FORMAT, family='eastmoney_funds', tables=tables, request_count=count, **metadata)
        body = json.dumps(value, ensure_ascii=False).encode()
        if len(body) > MAX_CAPTURE_BODY:
            raise SourceFailure('response_too_large')
        return Response(body, m.datetime.now(UTC), round((time.monotonic() - started) * 1000), 200, count)
    except SourceFailure as error:
        from app.market.fund_time_diagnostic import safe_diagnostic
        failure = m.EastmoneyFailure(error.reason, count, retry_after=error.retry_after)
        if diagnostic := safe_diagnostic(error):
            failure.funds_time_rejection = diagnostic
        raise failure from None


def parse(value, received_at):
    from app.market import eastmoney_trading as m
    try:
        if value.get('format') != FORMAT or value.get('family') != 'eastmoney_funds':
            raise ValueError()
        tables = value['tables']
        expected = {name for name, _, _ in m.TABLES['eastmoney_funds']}
        if not isinstance(tables, list) or len(tables) != 3 or {t['table'] for t in tables} != expected:
            raise ValueError()
        records, rejected, requests = [], 0, 0
        for piece in tables:
            name, pages, attempts = piece['table'], piece['pages'], piece['attempts']
            ordering=piece.get('ordering')
            if ordering not in {None,'code_descending'}:
                raise ValueError()
            total, count = piece['total'], piece['page_count']
            maximum, max_pages = limits(name)
            if type(count) is not int or not 1 <= count <= max_pages or (total is not None and (type(total) is not int or not 0 <= total <= maximum)):
                raise ValueError()
            if not isinstance(pages, list) or not isinstance(attempts, list) or len(attempts) != count:
                raise ValueError()
            if [a['page'] for a in attempts] != list(range(1, count + 1)):
                raise ValueError()
            successful = set()
            for attempt in attempts:
                if type(attempt['requests']) is not int or attempt['requests'] not in {1, 2}:
                    raise ValueError()
                if attempt['reason'] is not None and attempt['reason'] not in m.RECOVERABLE_TABLE_FAILURES:
                    raise ValueError()
                requests += attempt['requests']
                if attempt['reason'] is None:
                    successful.add(attempt['page'])
            if len(pages) != len(successful) or {p['page'] for p in pages} != successful:
                raise ValueError()
            rows, seen, duplicates, totals, original = [], set(), set(), set(), None
            capture = hashlib.sha256(json.dumps(piece, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            for page in sorted(pages, key=lambda p: p['page']):
                stamp = datetime.fromisoformat(page['received_at'])
                if stamp.utcoffset() is None or stamp > received_at:
                    raise ValueError()
                original = m.parse_ranking(page['body'].encode(), stamp, 'market.eastmoney.' + name,
                    piece['title'], industry_level=2 if name == 'industry_funds' else None, capture_page=page['page'])
                page_total = json.loads(page['body'])['data']['total']
                totals.add(page_total)
                if not 0 <= page_total <= maximum:
                    raise ValueError()
                for row_index, row in enumerate(original.rows):
                    if row['source_time'] is not None and datetime.fromisoformat(row['source_time']) > stamp:
                        from app.market.fund_time_diagnostic import rejection
                        raise rejection(stage='capture_validation', table=name, page=page['page'],
                            row_index=row_index, code=row['code'], source_time=datetime.fromisoformat(row['source_time']),
                            page_received_at=stamp, body=page['body'].encode(), capture_received_at=received_at)
                    identity = row['exchange'], row['code']
                    if identity in seen:
                        duplicates.add(identity)
                    seen.add(identity)
                    rows.append(dict(row, capture_id=capture, capture_total=str(total if total is not None else page_total),
                        capture_pages=str(count), capture_page=str(page['page']), capture_received_at=stamp.isoformat()))
            rows = [r for r in rows if (r['exchange'], r['code']) not in duplicates]
            allocation_note = (' 本表有实际成功页经供应商同接口分配节点接收，时效仍以实际来源时点判断。'
                               if any(a['page'] in successful and a['requests'] == 2 for a in attempts) else '')
            ordered = ordering is None or [r['code'] for r in rows] == sorted((r['code'] for r in rows),reverse=True)
            complete = total is not None and totals == {total} and len(pages) == count and not duplicates and len(rows) == total and ordered
            stock = name == 'stock_funds'
            if not original:
                rejected += 1
                continue
            for row in rows:
                row['capture_complete'] = '1' if complete else '0'
            payload = original.model_copy(update=dict(rows=rows, columns=[*original.columns,
                *[ReferenceColumn(key=key, label=key, unit=None) for key in META]],
                scope=('原同接口逐页真实采集；每页不超过200条。' +
                    ('个股' if stock else '板块') + f'本轮覆盖{len(rows)}/{total}条；' + ('范围完整。' if complete else '范围未齐，不替换完整排行。')
                    + '各页保留真实来源及接收时点，缺失不补零、不插值。' + allocation_note)))
            records.append((payload.dataset, payload, None))
            rejected += int(not complete)
        kinds = [k for k in ('catalog', 'catalog_failure', 'catalog_deferred') if k in value]
        if len(kinds) > 1:
            raise ValueError()
        if kinds:
            kind = kinds[0]
            item = value[kind]
            if kind == 'catalog':
                from app.market.industry_catalog import parse_catalog
                stamp = datetime.fromisoformat(item['received_at'])
                if stamp.utcoffset() is None or stamp > received_at:
                    raise ValueError()
                payload = parse_catalog(item['body'].encode(), stamp)
                records.append((payload.dataset, payload, None))
                requests += 1
            else:
                allowed = m.CATALOG_FAILURES if kind == 'catalog_failure' else m.CATALOG_DEFERRALS
                if item['reason'] not in allowed or item['retry_after'] is not None and (type(item['retry_after']) is not int or not 0 <= item['retry_after'] <= 86400):
                    raise ValueError()
                requests += int(kind == 'catalog_failure')
                rejected += 1
        if type(value['request_count']) is not int or value['request_count'] != requests:
            raise ValueError()
        return ReferenceBatch(records=records, expected=len(records) + rejected, rejected=rejected, requests=requests)
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise SourceFailure('schema_changed') from None


def saved_capture(payload, received_at):
    """Revalidate the normalized page-set fence before reading a larger table."""
    rows = payload.get('rows')
    try:
        maximum, max_pages = limits('stock_funds' if payload.get('dataset') == 'market.eastmoney.stock_funds' else '')
        if not isinstance(rows, list) or not 1 <= len(rows) <= maximum:
            return None
        first = rows[0]
        digest, total, pages, complete = first['capture_id'], int(first['capture_total']), int(first['capture_pages']), first['capture_complete']
        if not re.fullmatch('[a-f0-9]{64}', digest) or not 1 <= total <= maximum or not 1 <= pages <= max_pages or complete not in {'0', '1'}:
            return None
        observed, identities, counts = set(), set(), {}
        for row in rows:
            if any(row[key] != first[key] for key in ('capture_id', 'capture_total', 'capture_pages', 'capture_complete')):
                return None
            page = int(row['capture_page'])
            stamp = datetime.fromisoformat(row['capture_received_at'])
            if not 1 <= page <= pages or stamp.utcoffset() is None or stamp > received_at:
                return None
            if row.get('source_time') and datetime.fromisoformat(row['source_time']) > stamp:
                return None
            identity = row.get('exchange'), row.get('code')
            if identity in identities:
                return None
            identities.add(identity)
            observed.add(page)
            counts[page]=counts.get(page,0)+1
            if counts[page]>200:
                return None
        if len(rows) > total or complete == '1' and (len(rows) != total or observed != set(range(1, pages + 1))):
            return None
        return dict(total=total, complete=complete == '1', capture_id=digest)
    except (ValueError, KeyError, TypeError):
        return None
