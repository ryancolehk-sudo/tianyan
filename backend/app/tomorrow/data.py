"""Read-only, point-in-time research dossier over existing immutable caches.

This module never collects, changes source settings or asks a model to fill a
gap. Permission checks apply both now and at the requested cutoff. Provider
tables retain row/field lineage; manual judgments remain manual judgments.
"""

import hashlib
import re
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, or_, select

from app.evidence.common import normalized
from app.evidence.models import EvidenceVersion, SourceVersion
from app.market.models import Watchlist
from app.market.protocol import SHANGHAI
from app.market.review_notes import MANUAL_SOURCE_ID, _decode, _head
from app.market.review_protocol import DATASET_FAMILIES, INDEX_CODES, UNITS
from app.market.review_store import available_trade_days, family_id, load_dataset
from app.models import Instrument, Provider
from app.research.models import DocumentRevision, ResearchDocument
from app.tomorrow.calculations import (
    FORMULAS,
    RULE_VERSION,
    STOCK_PATTERN,
    index_rows,
    market_metrics,
    number,
    references,
    relative_strength,
    sector_record,
    stock_record,
)

SUPPLEMENTARY = frozenset({
    "tushare.daily_basic", "tushare.adj_factor", "tushare.moneyflow", "tushare.moneyflow_dc",
    "tushare.income", "tushare.balancesheet", "tushare.cashflow", "tushare.fina_indicator",
    "tushare.dividend", "tushare.block_trade", "tushare.margin_detail",
    "tushare.forecast", "tushare.express", "tushare.share_float", "tushare.stk_holdertrade",
    "tushare.fina_mainbz", "tushare.disclosure_date", "tushare.stk_managers",
    "tushare.shibor", "tushare.cn_cpi",
    "tomorrow.news_window", "tomorrow.announcement_text", "tomorrow.preparation_manifest",
    "financial.profit", "financial.operation", "financial.growth", "financial.balance",
    "financial.cash_flow", "financial.dupont",
})
FINANCIAL = frozenset({"tushare.income", "tushare.balancesheet", "tushare.cashflow", "tushare.fina_indicator",
                       "tushare.forecast", "tushare.express", "tushare.fina_mainbz"})
NEWS_TERMS = ("人工智能", "算力", "机器人", "半导体", "芯片", "电力", "光伏", "储能", "锂电", "新能源",
              "量子", "脑机接口", "光通信", "航运", "军工", "消费", "医药", "地产", "银行", "证券", "黄金", "关税", "降息", "通胀")


def _date(value):
    if isinstance(value, datetime):
        return value.astimezone(SHANGHAI).date() if value.utcoffset() is not None else value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _stamp(value):
    if not value:
        return None
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return result if result.utcoffset() is not None else result.replace(tzinfo=SHANGHAI)
    except (ValueError, TypeError):
        return None


def _ids(refs):
    return sorted({str(ref["id"]) for ref in refs if ref and ref.get("id")})


def _compact(refs):
    """Keep row lineage without repeating the entire source descriptor per row."""
    selected = {}
    for ref in refs:
        if not ref or not ref.get("id"):
            continue
        item = selected.setdefault(ref["id"], {"id": ref["id"], "source_row_ranges": [], "fields": []})
        if type(ref.get("source_row")) is int:
            item["source_row_ranges"].append([ref["source_row"], ref["source_row"]])
        # A segment group or an already deduplicated event can arrive with
        # ranges rather than a single row. Recompaction must preserve them.
        item["source_row_ranges"].extend([list(value) for value in ref.get("source_row_ranges", [])
                                          if isinstance(value, (list, tuple)) and len(value) == 2
                                          and all(type(position) is int for position in value)
                                          and 0 <= value[0] <= value[1]])
        item["fields"] = sorted(set(item["fields"]) | set(ref.get("fields", [])))
    for item in selected.values():
        ranges = []
        for start, end in sorted(item["source_row_ranges"]):
            if ranges and start <= ranges[-1][1] + 1:
                ranges[-1][1] = max(end, ranges[-1][1])
            else:
                ranges.append([start, end])
        item["source_row_ranges"] = ranges
        if ranges:
            item["row_index_base"] = 0
    return list(selected.values())


def _permission(provider, descriptor):
    return (provider.configured and provider.enabled and provider.entitlement_status == "verified"
            and all(provider.rights.get(key) == "allowed" for key in ("display", "cache"))
            and descriptor.get("configured") and descriptor.get("enabled")
            and descriptor.get("entitlement_status") == "verified"
            and all(descriptor.get("rights", {}).get(key) == "allowed" for key in ("display", "cache")))


def _permitted_providers(db, cutoff):
    providers = {provider.id: provider for provider in db.scalars(select(Provider))}
    versions = {}
    for version in db.scalars(select(SourceVersion).where(SourceVersion.recorded_at <= cutoff)
                             .order_by(SourceVersion.recorded_at.desc(), SourceVersion.id.desc())):
        versions.setdefault(version.source_id, version.descriptor)
    return {key: provider for key, provider in providers.items() if _permission(provider, versions.get(key, {}))}


def _ref(row, provider):
    return {"id": str(row.id), "content_hash": row.content_hash, "source_id": str(row.source_id),
            "source_name": provider.name, "received_at": row.received_at.isoformat(),
            "published_at": row.published_at.isoformat() if row.published_at else None,
            "available_at": row.available_at.isoformat() if row.available_at else None,
            "source_key": row.source_key, "source_revision": row.source_revision,
            "provenance": row.provenance, "kind": row.kind,
            "source_url": row.payload.get("source_url"), "title": row.payload.get("title") or provider.name}


class _Reader:
    def __init__(self, db, cutoff, providers):
        self.db, self.cutoff, self.providers = db, cutoff, providers
        self.tables, self.coverage, self.refs = {}, [], {}

    def table(self, dataset, day, scope="all", *, symbols=None):
        selection = frozenset(symbols) if symbols is not None else None
        key = (dataset, day, scope, selection)
        if key in self.tables:
            return self.tables[key]
        result = dict(load_dataset(self.db, dataset, day, scope=scope, as_of_time=self.cutoff))
        source_rows = result.get("rows", []) if result.get("state") == "complete" else []
        source_row_count = len(source_rows)
        if dataset not in {"news", "calendar_sse", "calendar_szse"} and any(
            not isinstance(row, dict) or _date(row.get("trade_date")) != day for row in source_rows
        ):
            result.update(state="broken_lineage", rows=[], reason="wrong_or_missing_trade_date")
            source_rows = []
        if result.get("state") == "complete" and dataset in {"daily", "limits", "pools", "themes", "theme_funds", "indices"}:
            seen, duplicates = set(), set()
            for row in source_rows:
                code = row.get("ts_code")
                if code in seen:
                    duplicates.add(code)
                seen.add(code)
            if duplicates:
                result.update(state="broken_lineage", rows=[], reason="duplicate_identity", duplicate_codes=sorted(str(code) for code in duplicates))
                source_rows = []
        for ref in result.get("evidence_refs", []) if result.get("state") == "complete" else []:
            source_id = ref.get("source_id")
            provider = self.providers.get(uuid.UUID(source_id)) if source_id else None
            descriptor = {**ref, "source_name": provider.name if provider else DATASET_FAMILIES[dataset],
                          "dataset": "review." + dataset, "scope": scope, "trade_day": str(day)}
            self.refs[ref["id"]] = {**self.refs.get(ref["id"], {}), **descriptor}
        enriched = []
        for index, row in enumerate(source_rows):
            # Filter the original table BEFORE allocating referenced row copies.
            # enumerate still addresses the immutable part's original row, not
            # the selected subset. Only one older all-market day is transient.
            if selection is not None and row.get("ts_code") not in selection:
                continue
            ref = None
            for source in result.get("evidence_refs", []):
                start, count = source.get("start_row", 0), source.get("row_count", 0)
                if start <= index < start + count:
                    offset = index - start
                    selected = source.get("selected_rows")
                    if selected is not None:
                        if offset >= len(selected):
                            continue
                        offset = selected[offset]
                    # Full provenance lives once in the bundle's immutable
                    # evidence index. Repeating the descriptor for 98k member
                    # rows needlessly multiplies both RAM and serialized size.
                    ref = {"id": source["id"], "source_row": offset, "row_index_base": 0}
                    break
            enriched.append({**row, "row_reference": ref})
        result["rows"] = enriched
        result["source_row_count"] = source_row_count
        result["retained_row_count"] = len(enriched)
        result["selection_scope"] = "full_stored_table" if selection is None else "candidate_symbols"
        self.coverage.append({"dataset": "review." + dataset, "trade_day": str(day), "scope": scope,
                              "state": result.get("state"), "row_count": source_row_count,
                              "source_row_count": source_row_count, "retained_row_count": len(enriched),
                              "selection_scope": result["selection_scope"],
                              "selected_symbol_count": len(selection) if selection is not None else None,
                              "reason": result.get("reason"),
                              "coverage_scope": result.get("coverage_scope"),
                              "received_at": result.get("received_at"),
                              "source_id": result.get("source_id"),
                              "full_market_coverage_verified": False,
                              "evidence_refs": _ids(result.get("evidence_refs", [])),
                              "units": UNITS.get(dataset, {})})
        self.tables[key] = result
        return result


def _target_day(db, source_day, requested, cutoff, providers):
    calendars = []
    for family in ("tushare_calendar_sse", "tushare_calendar_szse"):
        identity = family_id(family)
        if identity not in providers:
            return requested, False
        latest = {}
        for row in db.scalars(select(EvidenceVersion).where(
            EvidenceVersion.source_id == identity, EvidenceVersion.kind == "calendar",
            EvidenceVersion.visibility == "shared", EvidenceVersion.owner_id.is_(None),
            EvidenceVersion.received_at <= cutoff,
            or_(EvidenceVersion.available_at.is_(None), EvidenceVersion.available_at <= cutoff),
            or_(EvidenceVersion.published_at.is_(None), EvidenceVersion.published_at <= cutoff),
            EvidenceVersion.payload["trade_day"].astext > str(source_day),
            EvidenceVersion.payload["trade_day"].astext <= str(source_day + timedelta(days=45)),
        ).order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc())):
            latest.setdefault(_date(row.payload.get("trade_day")), row.payload.get("is_open") is True)
        calendars.append({day for day, opened in latest.items() if day and opened})
    common = sorted(calendars[0] & calendars[1])
    target = requested or (common[0] if common else None)
    return target, bool(target and target in common and target > source_day)


def _calendar_refs(db, days, cutoff, providers):
    identities = [family_id(family) for family in ("tushare_calendar_sse", "tushare_calendar_szse")]
    identities = [identity for identity in identities if identity in providers]
    if not identities or not days:
        return []
    selected, result = set(), []
    for row in db.scalars(select(EvidenceVersion).where(
        EvidenceVersion.source_id.in_(identities), EvidenceVersion.kind == "calendar",
        EvidenceVersion.visibility == "shared", EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.received_at <= cutoff,
        or_(EvidenceVersion.available_at.is_(None), EvidenceVersion.available_at <= cutoff),
        or_(EvidenceVersion.published_at.is_(None), EvidenceVersion.published_at <= cutoff),
        EvidenceVersion.payload["trade_day"].astext.in_([str(day) for day in days]),
    ).order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc())):
        key = (row.source_id, row.payload.get("trade_day"))
        if key not in selected:
            result.append(_ref(row, providers[row.source_id]))
            selected.add(key)
    return result


def _ordinary(db, owner_id, cutoff, providers, start_day):
    """Read permitted reference history and news; choose versions by business key later."""
    manual_scope = and_(EvidenceVersion.owner_id == owner_id, EvidenceVersion.visibility == "private",
                        EvidenceVersion.source_id == MANUAL_SOURCE_ID, EvidenceVersion.kind == "manual")
    external_scope = and_(EvidenceVersion.source_id.in_(list(providers)),
                          or_(and_(EvidenceVersion.visibility == "private", EvidenceVersion.owner_id == owner_id),
                              and_(EvidenceVersion.visibility == "shared", EvidenceVersion.owner_id.is_(None))))
    stmt = select(EvidenceVersion).where(
        or_(manual_scope, external_scope), EvidenceVersion.received_at <= cutoff,
        or_(EvidenceVersion.published_at.is_(None), EvidenceVersion.published_at <= cutoff),
        or_(EvidenceVersion.available_at.is_(None), EvidenceVersion.available_at <= cutoff),
        or_(EvidenceVersion.event_at.is_(None), EvidenceVersion.event_at <= cutoff),
        or_(and_(EvidenceVersion.kind == "reference", EvidenceVersion.payload["dataset"].astext.in_(SUPPLEMENTARY)),
            and_(EvidenceVersion.kind == "news", EvidenceVersion.received_at >= datetime.combine(start_day, time(), SHANGHAI))),
    ).order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.version_number.desc(), EvidenceVersion.id.desc())
    return list(db.scalars(stmt))


def _row_code(row):
    raw = row.get("ts_code") or row.get("stock_code") or row.get("code")
    if not isinstance(raw, str):
        return None
    if STOCK_PATTERN.fullmatch(raw):
        return raw
    if re.fullmatch(r"(?:sh|sz)\.\d{6}", raw):
        return raw[3:] + (".SH" if raw.startswith("sh.") else ".SZ")
    return None


def _reference_rows(records, providers, cutoff, source_day, refs):
    result, seen = defaultdict(list), set()
    for record in records:
        if record.kind != "reference":
            continue
        dataset = record.payload.get("dataset")
        units = {column["key"]: column.get("unit") for column in record.payload.get("columns", [])}
        for index, row in enumerate(record.payload.get("rows", [])):
            if not isinstance(row, dict):
                continue
            code = _row_code(row)
            business_day = _date(row.get("trade_date") or row.get("date"))
            dates = [_date(row.get(field)) for field in ("f_ann_date", "ann_date", "pubDate")]
            announced = max((value for value in dates if value), default=None)
            if business_day and business_day > source_day:
                continue
            if dataset in FINANCIAL or str(dataset).startswith("financial."):
                # Date-only announcements can become usable at the actual receipt,
                # never merely at the earlier reporting-period end.
                if announced is None or announced > cutoff.astimezone(SHANGHAI).date():
                    continue
            elif announced and announced > cutoff.astimezone(SHANGHAI).date():
                continue
            # Preserve separate report types, events and segmented article rows.
            discriminator = (row.get("end_date") or row.get("statDate") or str(business_day),
                             row.get("report_type"), row.get("id") or row.get("article_id") or row.get("announcement_id"),
                             row.get("part_number") or row.get("part") or row.get("segment") or row.get("segment_index") or row.get("chunk_index"),
                             row.get("document_sha256"), row.get("kind"), row.get("window_end"), row.get("job_id"),
                             row.get("month"), row.get("float_date"), row.get("ex_date"), row.get("pay_date"))
            if dataset in {"tomorrow.news_window", "tomorrow.announcement_text"}:
                # All segments from one immutable observation belong together.
                # Keeping an older observation's trailing segment would create a
                # text version which never existed at the source.
                discriminator += (record.id,)
            key = (record.source_id, dataset, code, discriminator)
            if key in seen:
                continue
            seen.add(key)
            provider = providers.get(record.source_id)
            if not provider:
                continue
            refs[str(record.id)] = _ref(record, provider)
            row_ref = {"id": str(record.id), "source_row": index, "row_index_base": 0}
            result[dataset].append({**row, "business_day": str(business_day) if business_day else None,
                                    "announced_day": str(announced) if announced else None,
                                    "row_reference": row_ref, "units": units})
    return dict(result)


def _event(raw, ref, *, kind="news", cutoff):
    published = _stamp(raw.get("published_at") or raw.get("publication_time") or raw.get("pub_time") or raw.get("published"))
    published_day = _date(raw.get("published_day"))
    modified = _stamp(raw.get("modified_at"))
    if ((published is None and (kind != "announcement" or published_day is None or published_day > cutoff.astimezone(SHANGHAI).date()))
            or (published and published > cutoff) or (modified and modified > cutoff)):
        return None
    title = str(raw.get("title") or raw.get("name") or "").strip()
    body = next((str(raw.get(key)).strip() for key in ("body", "text", "content", "summary") if raw.get(key)), "")
    sufficient = len(body) >= 80 and body != title and raw.get("text_status") not in {"no_text", "parse_failed"}
    identity = raw.get("id") or raw.get("article_id") or raw.get("announcement_id") or hashlib.sha256((title + body).encode()).hexdigest()
    codes = raw.get("stock_codes") or raw.get("ts_code") or raw.get("stock_code") or ""
    if isinstance(codes, str):
        codes = re.split(r"[,;\s]+", codes)
    return {"id": f"{kind}:{identity}", "kind": kind, "title": title, "content": body,
            "content_sufficient": sufficient, "published_at": published.isoformat() if published else None,
            "published_day": str(published_day) if published_day else None,
            "time_precision": "instant" if published else "date_only",
            "text_status": raw.get("text_status"), "text_truncated": str(raw.get("text_truncated", "false")).lower() == "true" or raw.get("text_status") == "truncated",
            "modified_at": modified.isoformat() if modified else None,
            "source_url": raw.get("source_url") or raw.get("url"),
            "stock_codes": [code for code in codes if STOCK_PATTERN.fullmatch(str(code))],
            "evidence": [{**ref, "fields": [key for key in ("title", "body", "text", "content", "summary", "published_at", "modified_at") if key in raw]}] if ref else [],
            "evidence_refs": [ref["id"]] if ref else [],
            "limitations": ([] if sufficient else ["只有标题或正文不足，仅作为待补证事件，不能据此展开影响判断。"])
                            + (["原文已截断，只能依据保存部分判断。"] if str(raw.get("text_truncated", "false")).lower() == "true" or raw.get("text_status") == "truncated" else [])
                            + (["来源只给出发布日期，没有已核验的精确发布时间；以本机实际接收作为可用时点。"] if not published else [])}


def _events(records, providers, review_news, supplemental, cutoff, refs):
    events, seen = [], {}
    candidates = []
    for table in review_news:
        candidates += [(row, row.get("row_reference"), "news") for row in table.get("rows", [])]
    for record in records:
        if record.kind == "news" and record.source_id in providers:
            descriptor = _ref(record, providers[record.source_id])
            raw = {**record.payload, "published_at": normalized(record.published_at or record.event_at)}
            candidates.append((raw, descriptor, "announcement" if "announcement" in record.source_key or record.source_id == family_id("cninfo_announcements") else "news"))
    segmented = []
    for dataset in ("tomorrow.news_window", "tomorrow.announcement_text"):
        grouped = defaultdict(list)
        for row in supplemental.get(dataset, []):
            grouped[(row.get("id"), row.get("document_sha256"), row.get("row_reference", {}).get("id"))].append(row)
        article_versions = defaultdict(list)
        for group in grouped.values():
            def part_number(row):
                try:
                    return int(row.get("part_number", "0"))
                except (TypeError, ValueError):
                    return -1
            group.sort(key=part_number)
            positions = [part_number(row) for row in group]
            # A missing/duplicate part cannot be interpreted as the full article.
            contiguous = bool(positions) and positions == list(range(positions[0], positions[0] + len(positions))) and positions[0] in {0, 1}
            evidence = references(group, ["id", "text", "part_number", "published_at", "published_day"])
            raw = {**group[0], "text": "".join(str(row.get("text") or "") for row in group), "_segment_evidence": evidence}
            if not contiguous:
                raw["text_status"] = "no_text"
            ref = group[0].get("row_reference")
            article_versions[raw.get("id")].append((raw, ref, "announcement" if "announcement" in dataset else "news", evidence))
        for versions in article_versions.values():
            def priority(item):
                raw, ref, _, _ = item
                usable = bool(raw.get("text") and raw.get("text_status") not in {"no_text", "not_fetched", "parse_failed"})
                descriptor = refs.get((ref or {}).get("id"), ref or {})
                return usable, str(descriptor.get("received_at") or ""), bool(raw.get("document_sha256"))
            # Preserve the immutable versions underneath; expose one coherent
            # usable article, preferring actual PDF text to empty metadata.
            segmented.append(max(versions, key=priority))
    candidates += [(raw, ref, kind) for raw, ref, kind, _ in segmented]
    for raw, ref, kind in candidates:
        event = _event(raw, ref, kind=kind, cutoff=cutoff)
        if not event or not event["title"]:
            continue
        if raw.get("_segment_evidence"):
            event["evidence"] = _compact(raw["_segment_evidence"])
            event["evidence_refs"] = _ids(raw["_segment_evidence"])
        if ref and ref.get("id") not in refs:
            refs[ref["id"]] = ref
        key = (event["kind"], re.sub(r"\s+", "", event["title"]), event["content"])
        if key in seen:
            existing = seen[key]
            existing["evidence"] = _compact(existing["evidence"] + event["evidence"])
            existing["evidence_refs"] = _ids(existing["evidence"])
            continue
        events.append(event)
        seen[key] = event
    for event in events:
        # Full article/source descriptors are indexed exactly once at the root.
        # Each event keeps only the immutable row/range and field pointers.
        event["evidence"] = [{key: value for key, value in ref.items()
                              if key in {"id", "source_row", "source_row_ranges", "row_index_base", "fields"}}
                             for ref in event["evidence"]]
    return sorted(events, key=lambda event: (event["published_at"] or event.get("published_day") or "", event["id"]), reverse=True)


def _manual_context(db, owner_id, source_day, cutoff):
    row = _head(db, owner_id, source_day, cutoff)
    notes = None
    if row:
        notes = {"content": _decode(row).model_dump(mode="json"), "evidence_id": str(row.id),
                 "content_hash": row.content_hash, "received_at": row.received_at.isoformat(), "authorship": "manual"}
    watch = db.get(Watchlist, owner_id)
    watch_codes = []
    if watch and watch.updated_at <= cutoff:
        identities = [uuid.UUID(value) for value in watch.instrument_ids]
        for instrument in db.scalars(select(Instrument).where(Instrument.id.in_(identities))):
            if instrument.exchange not in {"SSE", "SZSE"} or instrument.first_received_at is None or instrument.first_received_at > cutoff:
                continue
            code = instrument.symbol + (".SH" if instrument.exchange == "SSE" else ".SZ")
            if STOCK_PATTERN.fullmatch(code):
                watch_codes.append(code)
    revisions = list(db.scalars(select(DocumentRevision).join(ResearchDocument, ResearchDocument.id == DocumentRevision.document_id).where(
        ResearchDocument.owner_id == owner_id, DocumentRevision.owner_id == owner_id,
        ResearchDocument.kind == "plan", ResearchDocument.source_trade_day <= source_day,
        DocumentRevision.recorded_at <= cutoff,
    ).order_by(DocumentRevision.recorded_at.desc(), DocumentRevision.id.desc()).limit(30)))
    latest, historical = set(), []
    for revision in revisions:
        if revision.document_id in latest:
            continue
        latest.add(revision.document_id)
        historical.append({"revision_id": str(revision.id), "document_id": str(revision.document_id),
                           "title": revision.title, "content": revision.content, "content_hash": revision.content_hash,
                           "recorded_at": revision.recorded_at.isoformat(), "authorship": "manual_research_revision",
                           "limitation": "历史人工判断，仅供一致性及验证参考，不能作为外部事实。"})
    return notes, watch_codes, historical


def _future_events(supplemental, cutoff):
    fields = {
        "tushare.dividend": {"record_date": "股权登记计划", "ex_date": "除权除息计划", "pay_date": "派息计划"},
        "tushare.disclosure_date": {"pre_date": "财报预约披露", "actual_date": "供应商披露日程"},
        "tushare.share_float": {"float_date": "限售股解禁日程"},
        "tushare.stk_holdertrade": {"begin_date": "股东交易计划窗口开始", "close_date": "股东交易计划窗口结束"},
    }
    events, seen = [], set()
    for dataset, mapping in fields.items():
        for row in supplemental.get(dataset, []):
            for field, title in mapping.items():
                day = _date(row.get(field))
                if day is None or day < cutoff.astimezone(SHANGHAI).date():
                    continue
                code = _row_code(row)
                key = (dataset, code, field, day)
                if key in seen:
                    continue
                seen.add(key)
                events.append({"id": ":".join(map(str, key)), "title": title, "date": str(day), "code": code,
                               "time_precision": "date_only", "status": "announced_schedule",
                               "source_status": row.get("div_proc"), "announced_day": row.get("announced_day"),
                               "evidence": references([row], [field, "ann_date", "f_ann_date"]),
                               "evidence_refs": _ids(references([row])),
                               "limitation": "来源已保存的计划日期，可能更改；不代表事件已发生，也不推断投资影响。"})
    return sorted(events, key=lambda event: (event["date"], event["code"] or "", event["title"]))


def research_order(stocks):
    """Research coverage order, not a score or a recommendation."""
    def liquid(stock):
        amount = number((stock.get('facts') or {}).get('amount', {}).get('value'))
        return (amount is None, -(amount or 0), stock['code'])
    ordered = sorted(stocks, key=liquid)
    forced = [stock for stock in ordered if any(reason in {'本次冻结持仓', '人工复盘核心观察', '用户自选'}
                                               for reason in stock.get('candidate_reasons') or [])]
    pools = [stock for stock in ordered if (stock.get('facts', {}).get('pool', {}).get('value') or {}).get('limit') == 'U']
    strong = [stock for stock in ordered if any('非涨停观察样本' in reason for reason in stock.get('candidate_reasons') or [])]
    events = [stock for stock in ordered if '已保存正文的事件关联标的' in (stock.get('candidate_reasons') or [])]
    result, seen = [], set()
    for stock in forced:
        result.append(stock)
        seen.add(stock['code'])
    for index in range(max(map(len, (pools, strong, events)), default=0)):
        for group in (pools, strong, events):
            if index < len(group) and group[index]['code'] not in seen:
                result.append(group[index])
                seen.add(group[index]['code'])
    return result + [stock for stock in ordered if stock['code'] not in seen]


def build_bundle(db, owner_id, source_day: date, target_day: date | None, cutoff: datetime,
                 *, additional_codes=()) -> dict:
    """Freeze all available research inputs; this function performs no writes."""
    if not isinstance(cutoff, datetime) or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be timezone-aware")
    if not isinstance(source_day, date) or isinstance(source_day, datetime):
        raise ValueError("source_day must be a date")
    if source_day > cutoff.astimezone(SHANGHAI).date():
        raise ValueError("source_day cannot follow cutoff")
    if target_day is not None and (not isinstance(target_day, date) or isinstance(target_day, datetime) or target_day <= source_day):
        raise ValueError("target_day must follow source_day")
    providers = _permitted_providers(db, cutoff)
    reader = _Reader(db, cutoff, providers)
    days = sorted({day for day in available_trade_days(db, limit=63, end_day=source_day, as_of_time=cutoff) if day <= source_day})
    known_day = source_day in days
    target_day, target_verified = _target_day(db, source_day, target_day, cutoff, providers)
    for ref in _calendar_refs(db, [*days, *([target_day] if target_day else [])], cutoff, providers):
        reader.refs[ref["id"]] = ref
    tables = {}
    if known_day:
        for day in days:
            for dataset in ("themes", "theme_funds"):
                tables[(dataset, day)] = reader.table(dataset, day)
        for day in days[-3:]:
            tables[("daily", day)] = reader.table("daily", day)
            tables[("pools", day)] = reader.table("pools", day)
            tables[("limits", day)] = reader.table("limits", day)
        for dataset in ("members", "lhb", "seats"):
            tables[(dataset, source_day)] = reader.table(dataset, source_day)
    metrics = market_metrics(source_day, tables, days) if known_day else {"state": "unavailable", "reason": "verified_trading_day_unavailable", "evidence": []}
    metrics["evidence"] = _compact(metrics.get("evidence", []))
    metrics["evidence_refs"] = _ids(metrics["evidence"])
    index_tables = [reader.table("indices", source_day, code) for code in INDEX_CODES] if known_day else []
    indices = [row for table in index_tables for row in table.get("rows", [])]
    market = {**metrics, "indices": indices, "source_trade_day": str(source_day)}
    market["evidence_refs"] = sorted(set(market.get("evidence_refs", [])) | set(_ids(references(indices))))
    records = _ordinary(db, owner_id, cutoff, providers, source_day - timedelta(days=7))
    supplemental = _reference_rows(records, providers, cutoff, source_day, reader.refs)
    supplemental_coverage = []
    for dataset in sorted(SUPPLEMENTARY):
        rows = supplemental.get(dataset, [])
        supplemental_coverage.append({"dataset": dataset, "state": "available_partial_scope" if rows else "missing",
                                      "row_count": len(rows), "stock_count": len({_row_code(row) for row in rows if _row_code(row)}),
                                      "evidence_refs": _ids(references(rows)), "full_market_coverage_verified": False})
    news_tables = [reader.table("news", day) for day in days[-5:]] if known_day else []
    events = _events(records, providers, news_tables, supplemental, cutoff, reader.refs)
    # The canonical events now own the exact saved article content and its row
    # pointers. Do not retain all ORM source payloads and segmented text copies
    # throughout candidate history loading and stock calculation as well.
    del records
    for dataset in ("tomorrow.news_window", "tomorrow.announcement_text"):
        supplemental.pop(dataset, None)
    notes, watch_codes, historical_plans = _manual_context(db, owner_id, source_day, cutoff)
    if notes:
        note_row = db.get(EvidenceVersion, uuid.UUID(notes["evidence_id"]))
        manual_provider = db.get(Provider, MANUAL_SOURCE_ID)
        if manual_provider:
            reader.refs[str(note_row.id)] = _ref(note_row, manual_provider)
    daily_by_day, themes_by_day, duplicate_daily, duplicate_themes = {}, {}, [], []
    for day in days:
        daily_by_day[day], conflicts = index_rows(tables.get(("daily", day), {}).get("rows", []))
        duplicate_daily += [(str(day), code) for code in conflicts]
        themes_by_day[day], conflicts = index_rows(tables.get(("themes", day), {}).get("rows", []))
        duplicate_themes += [(str(day), code) for code in conflicts]
    current_daily = daily_by_day.get(source_day, {})
    current_themes = themes_by_day.get(source_day, {})
    caps, _ = index_rows(tables.get(("limits", source_day), {}).get("rows", []))
    pool_table = tables.get(("pools", source_day), {})
    pool_rows = pool_table.get("rows", [])
    pools, _ = index_rows(pool_rows)
    members = tables.get(("members", source_day), {}).get("rows", [])
    by_sector, by_stock = defaultdict(list), defaultdict(list)
    for row in members:
        by_sector[row.get("ts_code")].append(row)
        if STOCK_PATTERN.fullmatch(str(row.get("con_code", ""))):
            by_stock[row["con_code"]].append(row.get("ts_code"))
    funds_by_sector = defaultdict(list)
    for row in tables.get(("theme_funds", source_day), {}).get("rows", []):
        funds_by_sector[row.get("ts_code")].append(row)
    sectors = []
    for code in sorted(current_themes):
        sector = sector_record(code, {day: rows[code] for day, rows in themes_by_day.items() if code in rows}, days,
                               by_sector[code], current_daily, pool_rows if pool_table.get("state") == "complete" else None,
                               funds_by_sector[code], members_loaded=tables.get(("members", source_day), {}).get("state") == "complete")
        # Compact each sector immediately; retaining every sector's repeated
        # constituent evidence dicts until a later pass multiplies peak memory.
        sector["evidence"] = _compact(sector["evidence"] + references(sector["funds"]))
        sectors.append(sector)
    historical_funds = defaultdict(list)
    for day in days:
        for row in tables.get(("theme_funds", day), {}).get("rows", []):
            ref = row.get("row_reference") or {}
            historical_funds[row.get("ts_code")].append([str(day), row.get("net_amount"), ref.get("id"), ref.get("source_row")])
    for sector in sectors:
        sector["evidence_refs"] = _ids(sector["evidence"])
        series = historical_funds[sector["code"]]
        sector["calculations"]["net_amount_series"] = {
            "format": "tianyan.referenced-column-series.v1",
            "columns": ["trade_day", "net_amount", "evidence_id", "source_row"],
            "rows": series, "unit": "元", "row_index_base": 0,
        }
        sector["evidence_refs"] = _ids(sector["evidence"] +
                                        [ref for metric in sector["calculations"]["returns_pct"].values() for ref in metric["evidence"]])
        sector["evidence_refs"] = sorted(set(sector["evidence_refs"]) | {row[2] for row in series if row[2]})
    # Ranking is a disclosed single factor, never an invented composite score.
    sectors.sort(key=lambda row: (number(row["facts"]["pct_change"]["value"]) is not None,
                                 number(row["facts"]["pct_change"]["value"]) or 0, row["code"]), reverse=True)
    reasons = defaultdict(list)
    for code, row in pools.items():
        if STOCK_PATTERN.fullmatch(code):
            reasons[code].append({"U": "供应商涨停池", "D": "供应商跌停池风险观察", "Z": "供应商炸板池"}.get(row.get("limit"), "供应商池"))
    for code in watch_codes:
        reasons[code].append("用户自选")
    for code in additional_codes:
        if STOCK_PATTERN.fullmatch(str(code)):
            reasons[code].append("本次冻结持仓")
    note_content = notes["content"] if notes else {}
    for row in note_content.get("core_stocks", []):
        reasons[row["code"]].append("人工复盘核心观察")
    selected_sectors = {row["sector_code"] for row in note_content.get("directions", [])}
    selected_sectors.update(row["code"] for row in sectors[:10])
    for sector_code in sorted(selected_sectors):
        member_codes = {member.get("con_code") for member in by_sector[sector_code]}
        eligible = []
        for code in sorted(member_codes & current_daily.keys()):
            row, cap = current_daily[code], caps.get(code, {})
            close, up, down = number(row.get("close")), number(cap.get("up_limit")), number(cap.get("down_limit"))
            # Absence from a non-ST vendor pool does not prove a stock is not
            # sealed. Require the actual valid same-day limit and price.
            if (pools.get(code, {}).get("limit") != "U" and number(row.get("pct_chg")) is not None
                    and close is not None and up is not None and down is not None and 0 < down <= close < up):
                eligible.append(row)
        eligible.sort(key=lambda row: (number(row["pct_chg"]), number(row.get("amount")) or 0, row["ts_code"]), reverse=True)
        for row in eligible[:3]:
            reasons[row["ts_code"]].append(f"板块{sector_code}当日涨幅排序前三的非涨停观察样本")
    for event in events:
        if event["content_sufficient"]:
            for code in event["stock_codes"]:
                reasons[code].append("已保存正文的事件关联标的")
    # Candidate recall uses the complete current market, all U/D/Z rows, manual
    # cores/watchlists and events. Only after that set is fixed do we read older
    # all-market tables, retaining referenced candidate rows before caching.
    candidate_codes = frozenset(reasons)
    if known_day and candidate_codes:
        for day in days[:-3]:
            table = reader.table("daily", day, symbols=candidate_codes)
            tables[("daily", day)] = table
            daily_by_day[day], conflicts = index_rows(table.get("rows", []))
            duplicate_daily += [(str(day), code) for code in conflicts]
    supplemental_by_stock = defaultdict(lambda: defaultdict(list))
    for dataset, rows in supplemental.items():
        if dataset.startswith("tomorrow."):
            continue
        for row in rows:
            code = _row_code(row)
            if code:
                supplemental_by_stock[code][dataset].append(row)
    stocks = [stock_record(code, pools.get(code, {}).get("name") or current_daily.get(code, {}).get("name") or
                           next((row.get("name") for row in members if row.get("con_code") == code), code),
                           {day: rows[code] for day, rows in daily_by_day.items() if code in rows}, days,
                           cap=caps.get(code), pool=pools.get(code), sectors=sorted(set(by_stock[code])),
                           reasons=reasons[code], references_by_dataset=dict(supplemental_by_stock[code])) for code in sorted(reasons)]
    sector_index = {sector["code"]: sector for sector in sectors}
    sector_return_refs = {sector["code"]: _ids([ref for metric in sector["calculations"]["returns_pct"].values()
                                                for ref in metric["evidence"]]) for sector in sectors}
    for stock in stocks:
        stock["lhb"] = [row for row in tables.get(("lhb", source_day), {}).get("rows", []) if row.get("ts_code") == stock["code"]]
        stock["lhb_seats"] = [row for row in tables.get(("seats", source_day), {}).get("rows", []) if row.get("ts_code") == stock["code"]]
        stock["calculations"]["relative_to_sectors"] = [
            {"sector_code": code, "returns_difference": relative_strength(stock["calculations"]["returns_pct"], sector_index[code]["calculations"]["returns_pct"])}
            for code in stock["sectors"] if code in sector_index]
        additional = [row for rows in stock["cached_references"].values() for row in rows]
        dependency_refs = references([row for row in members if row.get("con_code") == stock["code"]] + stock["lhb"] + stock["lhb_seats"])
        dependency_refs += [ref for event in events if stock["code"] in event["stock_codes"] for ref in event["evidence"]]
        if notes and any(row["code"] == stock["code"] for row in note_content.get("core_stocks", [])):
            dependency_refs.append(reader.refs.get(notes["evidence_id"]))
        stock["evidence_refs"] = _ids(stock["evidence"] + references(additional) + dependency_refs +
                                      [ref for metric in stock["calculations"]["returns_pct"].values() for ref in metric["evidence"]] +
                                      stock["calculations"]["support_resistance"]["evidence"])
        stock["evidence_refs"] = sorted(set(stock["evidence_refs"]) | {
            identity for code in stock["sectors"] for identity in sector_return_refs.get(code, [])})
    limitations = ["统计基于截止前已收到的授权缓存；历史补采不等于历史当天已知。",
                   "完整采集清单与完整市场覆盖不同，未用当前上市目录回填历史。",
                   "资金流是供应商分类统计，不据此推断真实主力意图。",
                   "社会热词语料、市场全网热度、已确认综合评分与仓位价位规则尚不足，保持不可判断。"]
    if not known_day:
        limitations.append("缺少截止前已核验的沪深共同交易日历，核心研究暂停。")
    if not target_verified:
        limitations.append("目标交易日未获沪深共同日历确认，条件预案不能确认为可执行。")
    if duplicate_daily or duplicate_themes:
        limitations.append("存在重复行情或板块身份，冲突对象已剔除，不能视为完整覆盖。")
    reader.coverage.extend(supplemental_coverage)
    prepared = supplemental.get("tomorrow.preparation_manifest", [])
    news_windows = [row for row in prepared if row.get("kind") in {"news", "news_window"}]
    full_news = any(str(row.get("complete", "")).lower() == "true" and _stamp(row.get("window_start")) is not None
                    and _stamp(row.get("window_end")) is not None
                    and _stamp(row["window_start"]) <= datetime.combine(source_day, time(), SHANGHAI)
                    and _stamp(row["window_end"]) >= cutoff for row in news_windows)
    reader.coverage += [{"dataset": "calendar", "state": "complete" if known_day else "unavailable", "source_trade_day_verified": known_day,
                         "target_trade_day_verified": target_verified, "history_sessions": len(days)},
                        {"dataset": "social_hotwords", "state": "missing", "row_count": 0},
                        {"dataset": "user_portfolio", "state": "missing", "row_count": 0},
                        {"dataset": "news_corpus", "state": "partial", "row_count": len(events),
                         "content_sufficient_count": sum(event["content_sufficient"] for event in events),
                         "full_day_coverage_verified": full_news, "preparation_windows": news_windows}]
    reader.coverage.append({"dataset": "manual_review_notes", "state": "available" if notes else "missing",
                            "evidence_refs": [notes["evidence_id"]] if notes else [], "authorship": "manual"})
    reader.coverage.append({"dataset": "historical_manual_plans", "state": "available" if historical_plans else "missing",
                            "document_count": len(historical_plans), "scope": "最近30次截止前修订中的各文档最新版本"})
    terms = []
    for term in NEWS_TERMS:
        matched = [event for event in events if event["content_sufficient"] and term in event["title"] + event["content"]]
        if matched:
            terms.append({"term": term, "document_count": len(matched), "event_ids": [event["id"] for event in matched],
                          "evidence_refs": sorted({identity for event in matched for identity in event["evidence_refs"]}),
                          "heat_score": None, "scope": "固定词表在本机已保存充分正文中的文档频次；不是全网热度"})
    terms.sort(key=lambda row: (-row["document_count"], row["term"]))
    core_complete = known_day and all(tables.get((dataset, source_day), {}).get("state") == "complete" for dataset in ("daily", "limits", "pools"))
    verified = metrics.get("limits", {}).get("price_verified", {})
    ready = bool(core_complete and current_daily and metrics.get("state") not in {"invalid", "unavailable"}
                 and verified.get("unverified_n") == 0 and metrics.get("breadth", {}).get("missing_n") == 0
                 and not duplicate_daily)
    # Every source payload is already JSON, all arithmetic returns decimal
    # strings, and manual DTOs were dumped in JSON mode. Normalize the sole
    # timestamp here rather than cloning the entire graph while all source
    # tables remain alive. This matters under the worker's memory limit.
    stocks = research_order(stocks)
    return {"format": "tianyan.tomorrow-bundle.v1", "rule_version": RULE_VERSION,
                       "source_trade_day": str(source_day), "target_trade_day": str(target_day) if target_day else None,
                       "as_of_time": normalized(cutoff), "knowledge_mode": "received_by_cutoff", "ready": ready,
                       "target_trade_day_verified": target_verified, "market": market, "sectors": sectors,
                       "stocks": stocks, "events": events, "scenarios": [],
                       "future_events": _future_events(supplemental, cutoff),
                       "macro_references": {key: rows for key, rows in supplemental.items() if key in {"tushare.shibor", "tushare.cn_cpi"}},
                       "coverage": reader.coverage, "limitations": limitations,
                       "evidence_refs": list(reader.refs.values()), "metrics": {"rule_version": RULE_VERSION, "formulas": FORMULAS,
                           "history_trade_days": [str(day) for day in days], "candidate_count": len(stocks),
                           "history_daily_scope": "最近3日全市场；更早交易日仅保留完整召回候选，原始表逐日验证后过滤",
                           "evidence_encoding": "行/范围指针在顶层evidence_refs还原来源；相对强弱指向同一底稿的个股/板块窗口；资金历史为带列定义的行式数组",
                           "retained_daily_rows": sum(len(table.get("rows", [])) for (dataset, _), table in tables.items() if dataset == "daily"),
                           "full_market_daily_sessions_retained": min(3, len(days)) if known_day else 0,
                           "candidate_selection": "全U/D/Z池+自选+人工核心+重点板块非涨停分项前三+正文事件关联；无综合评分",
                           "candidate_research_order": "持仓/人工核心/自选优先，其后涨停、非涨停强势、正文事件交替；同组按成交额，缺失排末。仅决定有界深研覆盖，不代表推荐。",
                           "sector_ranking": "当日pct_change降序，未知排末；前十用于候选检索，不代表推荐",
                           "duplicate_daily": [list(item) for item in duplicate_daily], "duplicate_themes": [list(item) for item in duplicate_themes]},
                       "manual_notes": notes, "historical_plans": historical_plans,
                       "hotwords": {"financial": terms, "social": [], "scope": "本机固定语料词频，分数缺省"}}
