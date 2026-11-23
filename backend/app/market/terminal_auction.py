"""Permission-checked public seal tables for the live workbench.

Pair only the latest two daily tables from the same provider and HTTP hash.
The receipt, never today's moving clock, bounds the supplier's stage columns.
Matched trades stay in their separate Tushare fields.
"""
from datetime import datetime, time
from types import SimpleNamespace

from app.evidence.models import EvidenceVersion
from app.market.auction_source import FAMILIES as LEGACY_FAMILIES, FAMILY, SUMMARY_DATASET, STOCKS_DATASET
from app.market.fuyao_auction import FAMILY as NATIVE_FAMILY
from app.market.protocol import SHANGHAI
from app.market import read_facets
from app.models import Provider

STAGES = ('09:15', '09:20', '09:25')
FAMILIES = LEGACY_FAMILIES | {NATIVE_FAMILY}
NODE_FIELDS = ('node_amounts', 'node_received_at', 'node_source', 'auction_indicative_pct',
               'auction_tags', 'auction_board', 'auction_current_seal_cny')


def stock_code(value):
    if not isinstance(value, str) or len(value) != 6 or not value.isascii() or not value.isdigit():
        return None
    suffix = ('SH' if value.startswith(('6', '900')) else 'SZ' if value.startswith(('0', '2', '3'))
              else 'BJ' if value.startswith(('4', '8', '920')) else None)
    return value + '.' + suffix if suffix else None


class AuctionTables:
    def __init__(self, reader):
        self.reader = reader
        self.pairs = {}
        self.loaded = {}
        from app.market.fuyao_auction_read import NativeAuction
        self.native = NativeAuction(reader)
        ev = EvidenceVersion
        stmt = read_facets.with_facets(reader.query(FAMILIES, 'reference')).where(
            read_facets.value('dataset').in_((SUMMARY_DATASET, STOCKS_DATASET))).with_only_columns(
                ev.id, ev.source_id, Provider.family, ev.source_key, ev.received_at, ev.content_hash,
                read_facets.observed_day(), read_facets.value('dataset'),
                read_facets.value('batch_hash')).distinct(ev.source_id, ev.source_key).order_by(
                    ev.source_id, ev.source_key, ev.received_at.desc(), ev.version_number.desc(), ev.id.desc())
        grouped = {}
        for ident, source, family, key, received, digest, raw_day, dataset, batch_hash in reader.db.execute(stmt):
            day = reader.day(raw_day)
            if day is None or day > reader.cutoff.astimezone(SHANGHAI).date() or not batch_hash:
                continue
            if key != f'reference:auction:{family}:{day}:' + ('summary' if dataset == SUMMARY_DATASET else 'stocks'):
                continue
            meta = SimpleNamespace(id=ident, source_id=source, family=family, source_key=key,
                received_at=received, content_hash=digest, batch_hash=batch_hash)
            grouped.setdefault((source, family, day), {})[dataset] = meta
        for (_, family, day), tables in grouped.items():
            summary, stocks = tables.get(SUMMARY_DATASET), tables.get(STOCKS_DATASET)
            if summary is None or stocks is None or summary.batch_hash != stocks.batch_hash:
                continue
            pair = (summary, stocks)
            previous = self.pairs.get(day)
            def rank(candidate):
                gate = min(m.received_at for m in candidate)
                phases = sum(gate >= datetime.fromisoformat(f'{day}T{stage}:00+08:00') for stage in STAGES)
                return phases, max(m.received_at for m in candidate), candidate[0].family == FAMILY
            # A partial morning copy must not hide a later complete history
            # table. Select one whole batch; never stitch its stage amounts.
            if previous is None or rank(pair) > rank(previous):
                self.pairs[day] = pair

    def dates(self, through):
        return sorted((day for day in set(self.pairs) | set(self.native.by_day) if day <= through), reverse=True)

    def batch(self, day):
        if day in self.loaded:
            return self.loaded[day]
        self.loaded[day] = None
        native = self.native.batch(day)
        if native is not None:
            self.loaded[day] = native
            return native
        pair = self.pairs.get(day)
        if pair is None:
            return None
        summary, stocks = (self.reader.evidence(meta).payload for meta in pair)
        headers, rows = summary.get('rows') or [], stocks.get('rows') or []
        kind = 'current' if pair[0].family == FAMILY else 'history'
        if len(headers) != 1 or not rows:
            return None
        header = headers[0]
        if (header.get('trade_day') != day.isoformat() or header.get('batch_hash') != pair[0].batch_hash
                or header.get('table_kind') != kind or str(len(rows)) != header.get('row_count')):
            return None
        if any(row.get('trade_day') != day.isoformat() or row.get('batch_hash') != pair[0].batch_hash for row in rows):
            return None
        codes = [row.get('code') for row in rows]
        if len(set(codes)) != len(codes):
            return None
        gate = min(meta.received_at for meta in pair)
        result = SimpleNamespace(header=header, rows=rows, family=pair[0].family,
            received_at=max(meta.received_at for meta in pair),
            source='短线侠当前表' if kind == 'current' else '短线侠历史表',
            stages={stage for stage in STAGES if gate >= datetime.fromisoformat(f'{day}T{stage}:00+08:00')})
        self.loaded[day] = result
        return result


def attach_nodes(item, row, batch, number):
    item.name = row.get('name') or item.name
    item.node_amounts = {stage: number(row.get(f'fd_{stage.replace(":", "")}_cny'))
                         if stage in batch.stages else None for stage in STAGES}
    item.node_received_at = batch.received_at
    item.node_source = batch.source
    item.auction_indicative_pct = number(row.get('change_pct')) if batch.stages else None
    received = batch.received_at.astimezone(SHANGHAI)
    rolling = next((value for stage in reversed(STAGES)
                    if (value := number(row.get(f'fd_{stage.replace(":", "")}_cny'))) is not None), None)
    item.auction_current_seal_cny = (rolling
        if batch.family == FAMILY and received.date().isoformat() == row.get('trade_day')
        and time(9, 15) <= received.time() < time(9, 26) else None)
    if batch.family == NATIVE_FAMILY:
        item.auction_current_seal_cny = number(row.get('current_seal_cny')) if batch.live else None
    item.auction_tags = [tag for tag in (row.get('tags_raw') or '').split('|') if tag]
    item.auction_board = row.get('board_raw')


def header_total(batch, number):
    if batch is None:
        return None
    declared = number(batch.header.get('header_total_cny'))
    if declared is not None:
        return declared
    stage = next((value for value in reversed(STAGES) if value in batch.stages), None)
    return number(batch.header.get('total_fd_' + stage.replace(':', '') + '_cny')) if stage else None


def project_nodes(reader, day, items, make_stock, names, identities, number, only_codes=None):
    batch = reader.auction.batch(day)
    if batch is None:
        return None
    for row in batch.rows:
        code = stock_code(row.get('code'))
        if code is None or only_codes is not None and code not in only_codes:
            continue
        item = items.setdefault(code, make_stock(code, names, identities))
        attach_nodes(item, row, batch, number)
    return batch
