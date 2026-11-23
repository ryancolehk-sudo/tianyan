"""Bounded, read-only evidence probe for the Eastmoney v2 adapter.

This module is deliberately not imported by the application. It exists so a
human can run a *small* sample in a real trading session, keep the untouched
response bytes in a private directory, and publish only a redacted summary.

It never rotates proxies, never retries a hard rate limit, and refuses to write
inside the repository. No credential is read or printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.market.protocol import SHANGHAI

from . import adapter
from .client import EastmoneyClient, JsonResponse, TransportFailure
from .mapping import MappingError, iso, money, parse_sector_codes, parse_ts_codes

REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_OUT = Path(tempfile.gettempdir()) / "eastmoney-v2-live"
QUOTE_PRESENCE_FIELDS = ("f43", "f19", "f20", "f39", "f40", "f47", "f48", "f51", "f52", "f124")


def private_dir(value: str | None) -> Path:
    target = (Path(value).expanduser() if value else DEFAULT_OUT).resolve()
    if (target == REPO_ROOT or REPO_ROOT in target.parents
            or any((parent / ".git").exists() for parent in (target, *target.parents))):
        raise SystemExit("refusing to write raw market responses inside the repository")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    return target


class RecordingClient:
    """Keep decoded HTTP entity bytes and both successful and failed outcomes.

    Re-encoded payloads from injected clients are explicitly marked, never
    presented as untouched response evidence. Network errors have no raw body.
    """

    def __init__(self, inner, raw_dir: Path):
        self.inner = inner
        self.raw_dir = raw_dir
        self.calls: list[dict] = []
        self._stopped: TransportFailure | None = None
        raw_dir.mkdir(parents=True, exist_ok=True)

    def request_json(self, path: str, params: dict) -> JsonResponse:
        if self._stopped is not None:
            raise self._stopped
        sequence = len(self.calls) + 1
        call = {
            "sequence": sequence,
            "endpoint": path,
            "params": {key: str(value) for key, value in params.items()},
            "requested_at": iso(datetime.now(SHANGHAI)),
            "received_at": None,
            "bytes": 0,
            "sha256": None,
            "raw_file": None,
            "body_basis": "not_received",
        }
        self.calls.append(call)
        try:
            response = self.inner.request_json(path, params)
        except TransportFailure as error:
            self._stopped = error
            call.update(status=error.status, reason=error.reason, requests=error.requests)
            if error.received_at is not None:
                call["received_at"] = iso(error.received_at)
            if error.raw_body is not None:
                self._save_body(call, path, error.raw_body, "decoded_http_entity")
            raise
        call.update(status="ok", reason=None, requests=response.requests, received_at=iso(response.received_at))
        body = response.raw_body
        basis = "decoded_http_entity"
        if body is None:
            body = json.dumps(response.payload, ensure_ascii=False, separators=(",", ":")).encode()
            basis = "reencoded_payload_not_raw_response"
        self._save_body(call, path, body, basis)
        return response

    def _save_body(self, call: dict, path: str, body: bytes, basis: str):
        stamp = datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S%f")
        name = f"{call['sequence']:02d}_{stamp}_{path.rsplit('/', 1)[-1]}.json"
        (self.raw_dir / name).write_bytes(body)
        call.update(
            {
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "raw_file": name,
                "body_basis": basis,
            }
        )


def _presence(record: dict) -> dict:
    raw = record.get("raw_fields") or {}
    return {field: field in raw for field in QUOTE_PRESENCE_FIELDS}


def auction_summary(envelope: dict, trade_date: str, stage: str, codes: list[str]) -> dict:
    return {
        "mode": "auction",
        "trade_date": trade_date,
        "stage": stage,
        "codes": codes,
        "status": envelope["status"],
        "reason": envelope["reason"],
        "quality_flags": envelope.get("quality_flags", []),
        "records": [
            {
                "ts_code": record.get("ts_code"),
                "source_time": record.get("source_time"),
                "received_at": record.get("received_at"),
                "stage": record.get("stage"),
                "stage_time_matched": record.get("stage_time_matched"),
                "stage_verified": record.get("stage_verified"),
                "observed_phase": record.get("observed_phase"),
                "checkpoint_offset_seconds": record.get("checkpoint_offset_seconds"),
                "field_presence": _presence(record),
                "candidate_seal_present": record.get("candidate_seal_amount_cny") is not None,
                "candidate_seal_side": record.get("candidate_seal_side"),
                "seal_amount_present": record.get("seal_amount_cny") is not None,
                "match_state": record.get("match_state"),
                "quality_flags": record.get("quality_flags", []),
            }
            for record in envelope["records"]
        ],
    }


def order_book_summary(envelope: dict, trade_date: str, codes: list[str]) -> dict:
    return {
        "mode": "orderbook",
        "trade_date": trade_date,
        "codes": codes,
        "status": envelope["status"],
        "reason": envelope["reason"],
        "quality_flags": envelope.get("quality_flags", []),
        "records": [
            {
                "ts_code": record.get("ts_code"),
                "source_time": record.get("source_time"),
                "received_at": record.get("received_at"),
                "field_presence": _presence(record),
                "seal_side": record.get("seal_side"),
                "seal_amount_present": record.get("seal_amount_cny") is not None,
                "match_state": record.get("match_state"),
                "observation_basis": record.get("observation_basis"),
                "quality_flags": record.get("quality_flags", []),
            }
            for record in envelope["records"]
        ],
    }


def fund_summary(envelope: dict, trade_date: str, scope: str, request: dict) -> dict:
    return {
        "mode": "funds",
        "trade_date": trade_date,
        "scope": scope,
        "request": request,
        "status": envelope["status"],
        "reason": envelope["reason"],
        "quality_flags": envelope.get("quality_flags", []),
        "records": [
            {
                "code": record.get("sector_code") or record.get("ts_code"),
                "ts_code": record.get("ts_code"),
                "sector_code": record.get("sector_code"),
                "sector_type": record.get("sector_type"),
                "trade_date": record.get("trade_date"),
                "received_at": record.get("received_at"),
                "metric_scope": record.get("metric_scope"),
                "rank": record.get("rank"),
                "rank_scope": record.get("rank_scope"),
                "rank_total": record.get("rank_total"),
                "source_time": record.get("source_time"),
                "source_time_kind": record.get("source_time_kind"),
                "market_time": record.get("market_time"),
                "main_net_consistency": record.get("main_net_consistency"),
                "change_status": record.get("change_status"),
                "change_present": record.get("main_net_inflow_5m_change_cny") is not None,
                "quality_flags": record.get("quality_flags", []),
            }
            for record in envelope["records"]
        ],
    }


def analyze_full_flow(payload: dict, trade_date: date, snapshot_record: dict | None) -> dict:
    """Describe observed labels, without promoting a tail sample to a full day.

    Coverage refers to returned minute labels, not verified exchange semantics
    or a guarantee of a provider's complete market data.
    """
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("klines"), list):
        return {"status": "error", "reason": "schema_changed"}
    points = []
    for line in data["klines"]:
        if not isinstance(line, str):
            return {"status": "error", "reason": "schema_changed"}
        parts = line.split(",")
        if len(parts) != 6:
            return {"status": "error", "reason": "schema_changed"}
        try:
            moment = datetime.strptime(parts[0], "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)
            main = money(parts[1], field="main_net_inflow")
            large = money(parts[4], field="large_net_inflow")
            super_large = money(parts[5], field="super_large_net_inflow")
        except (ValueError, MappingError):
            return {"status": "error", "reason": "schema_changed"}
        points.append((moment, main, large, super_large))
    if not points:
        return {"status": "unavailable", "reason": "no_records"}
    gaps = [
        int((later[0] - earlier[0]).total_seconds())
        for earlier, later in zip(points, points[1:], strict=False)
    ]
    consistency = [
        main is not None and large is not None and super_large is not None
        and abs(main - (large + super_large)) <= Decimal("1")
        for _, main, large, super_large in points
    ]
    dates = sorted({point[0].date().isoformat() for point in points})
    increasing = all(later[0] > earlier[0] for earlier, later in zip(points, points[1:], strict=False))
    in_session = all(adapter._same_continuous_session(point[0], point[0]) for point in points)
    lunch_pairs = [
        (earlier[0].strftime("%H:%M"), later[0].strftime("%H:%M"))
        for earlier, later in zip(points, points[1:], strict=False)
        if earlier[0].date() == later[0].date()
        and earlier[0].strftime("%H:%M") == "11:30"
        and later[0].strftime("%H:%M") in {"13:00", "13:01"}
    ]
    unexpected_gaps = sum(
        1 for earlier, later in zip(points, points[1:], strict=False)
        if (later[0] - earlier[0]).total_seconds() != 60
        and (earlier[0].strftime("%H:%M"), later[0].strftime("%H:%M")) not in lunch_pairs
    )
    covers_day_bounds = (
        points[0][0].strftime("%H:%M") in {"09:30", "09:31"}
        and points[-1][0].strftime("%H:%M") == "15:00"
        and len(lunch_pairs) == 1
    )
    last_time, last_main, _, _ = points[-1]
    snapshot_time = None
    tail_same_minute = False
    tail_within_one_yuan = False
    if snapshot_record and snapshot_record.get("source_time"):
        try:
            parsed_time = datetime.fromisoformat(snapshot_record["source_time"])
            if parsed_time.utcoffset() is not None:
                snapshot_time = parsed_time.astimezone(SHANGHAI).replace(second=0, microsecond=0)
        except (ValueError, TypeError):
            snapshot_time = None
    if snapshot_time is not None and snapshot_record.get("main_net_inflow_cny") is not None:
        tail_same_minute = snapshot_time == last_time
        try:
            snapshot_main = money(snapshot_record["main_net_inflow_cny"], field="main_net_inflow")
        except MappingError:
            snapshot_main = None
        tail_within_one_yuan = (
            snapshot_main is not None and last_main is not None
            and abs(snapshot_main - last_main) <= Decimal("1")
        )
    point_times = {point[0] for point in points}
    pair_present = any(
        moment + timedelta(seconds=300) in point_times
        and adapter._same_continuous_session(moment, moment + timedelta(seconds=300))
        for moment in point_times
    )
    reason = None
    if dates != [trade_date.isoformat()]:
        reason = "cross_day_series"
    elif not increasing:
        reason = "timestamps_not_increasing"
    elif not in_session:
        reason = "outside_trading_session"
    elif not all(consistency):
        reason = "scope_consistency_mismatch"
    elif unexpected_gaps:
        reason = "intraday_sampling_gap"
    elif not covers_day_bounds:
        reason = "partial_day_coverage"
    sample_valid = dates == [trade_date.isoformat()] and increasing and in_session and all(consistency)
    return {
        "status": "ok" if reason is None else "partial",
        "reason": reason,
        "sample_valid": sample_valid,
        "observed_label_coverage": "full_session_bounds" if reason is None else "incomplete_or_invalid",
        "market_time_semantics_verified": False,
        "line_count": len(points),
        "first_label": points[0][0].strftime("%H:%M"),
        "last_label": points[-1][0].strftime("%H:%M"),
        "distinct_dates": dates,
        "gaps_60s": sum(1 for gap in gaps if gap == 60),
        "gaps_non_60s": sum(1 for gap in gaps if gap != 60),
        "lunch_gap_present": bool(lunch_pairs),
        "unexpected_gap_count": unexpected_gaps,
        "timestamps_strictly_increasing": increasing,
        "scope_consistency_rows": sum(1 for value in consistency if value),
        "scope_consistency_failures": sum(1 for value in consistency if not value),
        "tail_label": last_time.strftime("%H:%M"),
        "snapshot_minute": snapshot_time.strftime("%H:%M") if snapshot_time else None,
        "tail_same_minute_as_snapshot": tail_same_minute,
        "tail_within_one_yuan_of_snapshot": tail_within_one_yuan,
        "tail_snapshot_comparable": sample_valid and tail_same_minute and tail_within_one_yuan,
        "five_minute_pair_present": pair_present,
    }


def _write_summary(out: Path, mode: str, summary: dict, calls: list[dict]) -> Path:
    stamp = datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S%f")
    summary_dir = out / "summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    path = summary_dir / f"{mode}_{stamp}.json"
    payload = {"generated_at": iso(datetime.now(SHANGHAI)), "calls": calls, **summary}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _collect(args, day: date, codes: list[str], client: RecordingClient) -> dict:
    if args.mode == "auction":
        if not codes or not args.stage:
            raise SystemExit("auction mode requires --codes and --stage")
        envelope = adapter.fetch_auction_order_snapshots(
            trade_date=day, ts_codes=codes, stage=args.stage, client=client
        )
        summary = auction_summary(envelope, day.isoformat(), args.stage, codes)
    elif args.mode == "orderbook":
        if not codes:
            raise SystemExit("orderbook mode requires --codes")
        envelope = adapter.fetch_order_book_snapshots(trade_date=day, ts_codes=codes, client=client)
        summary = order_book_summary(envelope, day.isoformat(), codes)
    elif args.mode == "funds":
        if args.sector or args.sector_type:
            envelope = adapter.fetch_sector_fund_snapshots(
                trade_date=day,
                sector_codes=[args.sector] if args.sector else None,
                sector_type=args.sector_type,
                include_intraday_change=args.include_change,
                limit=args.limit,
                client=client,
            )
            summary = fund_summary(
                envelope,
                day.isoformat(),
                "sector",
                {
                    "sector": args.sector, "sector_type": args.sector_type,
                    "ranking": not args.sector, "limit": args.limit,
                    "include_change": args.include_change,
                },
            )
        else:
            envelope = adapter.fetch_stock_fund_snapshots(
                trade_date=day,
                ts_codes=codes or None,
                include_intraday_change=args.include_change,
                limit=args.limit,
                client=client,
            )
            summary = fund_summary(
                envelope,
                day.isoformat(),
                "stock",
                {"codes": codes, "include_change": args.include_change, "limit": args.limit},
            )
    elif args.mode == "full-flow":
        if bool(args.sector) == bool(codes) or (codes and len(codes) != 1):
            raise SystemExit("full-flow mode requires exactly one --sector or one --code")
        if args.sector:
            sector = parse_sector_codes([args.sector], args.sector_type)[0]
            secid = sector["secid"]
            envelope = adapter.fetch_sector_fund_snapshots(
                trade_date=day,
                sector_codes=[args.sector],
                sector_type=args.sector_type,
                include_intraday_change=False,
                client=client,
            )
        else:
            stock = parse_ts_codes([codes[0]])[0]
            secid = stock["secid"]
            envelope = adapter.fetch_stock_fund_snapshots(
                trade_date=day,
                ts_codes=[codes[0]],
                include_intraday_change=False,
                client=client,
            )
        snapshot = envelope["records"][0] if envelope["records"] else None
        if snapshot is None:
            flow = {"status": "unavailable", "reason": "snapshot_unavailable_no_followup_request"}
        else:
            response = client.request_json(
                "/api/qt/stock/fflow/kline/get",
                {
                    "lmt": 400,
                    "klt": 1,
                    "secid": secid,
                    "fields1": "f1,f2,f3,f7",
                    "fields2": "f51,f52,f53,f54,f55,f56",
                },
            )
            flow = analyze_full_flow(response.payload, day, snapshot)
        summary = {
            "mode": "full-flow",
            "trade_date": day.isoformat(),
            "target": args.sector or (codes[0] if codes else None),
            "status": flow["status"] if snapshot else envelope["status"],
            "reason": flow["reason"] if snapshot else envelope["reason"],
            "snapshot_status": envelope["status"],
            "snapshot_reason": envelope["reason"],
            "flow": flow,
        }
    else:
        raise SystemExit("unsupported mode")

    return summary


def run(args) -> dict:
    day = date.fromisoformat(args.date) if args.date else datetime.now(SHANGHAI).date()
    codes = [item.strip() for item in (args.codes or "").split(",") if item.strip()]
    if len(codes) > 3 or ((args.sector or args.sector_type) and codes):
        raise ValueError("probe requires at most three codes or one sector")
    if (type(args.limit) is not int or not 1 <= args.limit <= 3
            or type(args.max_attempts) is not int or args.max_attempts != 1
            or not math.isfinite(args.min_interval) or args.min_interval < 5):
        raise ValueError("probe bounds require limit 1..3, one attempt, interval >= 5s")
    out = private_dir(args.out)
    inner = EastmoneyClient(min_interval=args.min_interval, max_attempts=args.max_attempts)
    client = RecordingClient(inner, out / "raw")
    parsed_dir = out / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    try:
        summary = _collect(args, day, codes, client)
    except TransportFailure as error:
        summary = {
            "mode": args.mode, "trade_date": day.isoformat(),
            "status": error.status, "reason": error.reason,
        }
    stamp = datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S%f")
    (parsed_dir / f"{args.mode}_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    path = _write_summary(out, args.mode, summary, client.calls)
    summary["summary_path"] = str(path)
    summary["raw_dir"] = str(out / "raw")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Bounded Eastmoney v2 evidence probe")
    parser.add_argument("--mode", required=True, choices=("auction", "orderbook", "funds", "full-flow"))
    parser.add_argument("--date", help="trade date YYYY-MM-DD; default today in Asia/Shanghai")
    parser.add_argument("--codes", help="comma-separated ts_codes, at most three")
    parser.add_argument("--sector", help="one BKxxxx sector code")
    parser.add_argument("--sector-type", choices=("industry", "concept", "region"))
    parser.add_argument("--stage", choices=("09:15", "09:20", "09:25"))
    parser.add_argument("--include-change", action="store_true")
    parser.add_argument("--limit", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--out", help="private output directory; must be outside the repository")
    parser.add_argument("--min-interval", type=float, default=5.0)
    parser.add_argument("--max-attempts", type=int, choices=(1,), default=1)
    args = parser.parse_args(argv)
    try:
        summary = run(args)
    except ValueError:
        print(json.dumps({"status": "error", "reason": "invalid_probe_arguments"}, ensure_ascii=False))
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("status") in {"ok", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
