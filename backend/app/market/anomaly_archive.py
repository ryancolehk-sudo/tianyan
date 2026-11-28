"""Serve immutable receipts from the shared artifact volume; never fetch on GET."""
import json
import logging
import os
from time import monotonic
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqlalchemy.orm import Session
from app.auth import require_instance
from app.config import settings
from app.db import engine
from app.evidence.common import lock, now
from app.maintenance_control import paused
from app.market.anomaly_feed import fetch_snapshot
from app.market.protocol import SHANGHAI
from app.market.review_store import available_trade_days


def directory():
    return Path(settings().artifact_directory) / "review-anomaly"


def read_archive(day=None, cutoff=None):
    if settings().app_mode != "real" or settings().deployment_mode != "local":
        return None
    root = directory()
    for path in sorted(root.glob(f"{day or '*'}_*.json"), reverse=True):
        if cutoff and path.stem.split("_", 1)[1] > cutoff.astimezone(UTC).strftime("%Y%m%dT%H%M%S%f"):
            continue
        return json.loads(path.read_bytes())
    return None


def save(root, name, data):
    root.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=root, delete=False, mode="w", encoding="utf8") as tmp:
        json.dump(data, tmp, ensure_ascii=False, allow_nan=False)
    os.replace(tmp.name, root / name)


_next_check = 0.0

def schedule_anomaly():
    global _next_check
    if monotonic() < _next_check:
        return
    _next_check = monotonic() + 30
    config = settings()
    if not (config.data_collection_enabled and config.anomaly_collection_enabled):
        return
    if config.app_mode != "real" or config.deployment_mode != "local":
        return
    with Session(engine()) as db, db.begin():
        require_instance(db)
        if paused(db):
            return
        clock = now(db)
        local = clock.astimezone(SHANGHAI)
        root = directory()
        lock(db, "review.anomaly.1630")
        # Cheap fast path between archive ticks. Retry source or calendar gaps.
        marker = root / "attempt.json"
        if marker.exists() and clock.timestamp() - marker.stat().st_mtime < 75:
            return
        end = local.date() if local.time() >= time(16,30) else local.date()-timedelta(days=1)
        if any(root.glob(f"{end}_*.json")):
            return
        days = available_trade_days(db, limit=1, end_day=end, as_of_time=clock)
        if not days:
            return
        day = days[-1]
        if any(root.glob(f"{day}_*.json")):
            return
        if marker.exists():
            previous = json.loads(marker.read_bytes())
            if previous.get("day") == str(day) and clock.timestamp()-marker.stat().st_mtime < 300:
                return
        save(root, "attempt.json", {"day": str(day)})
        try:
            snapshot = fetch_snapshot()
            if snapshot["day"] != str(day):
                raise ValueError("来源交易日与归档日不符")
            received = datetime.now(UTC)
            snapshot["received_at"] = received.isoformat()
            save(root, f"{day}_{received:%Y%m%dT%H%M%S%f}.json", snapshot)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logging.getLogger(__name__).warning("Anomaly archive pending for %s (%s)", day, type(exc).__name__)
