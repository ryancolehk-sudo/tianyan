from datetime import UTC, datetime

from app.config import settings


def envelope(request, data, status="complete", reasons=None, *, as_of_time=None):
    return {"data": data, "meta": {"request_id": request.state.request_id,
            "app_mode": settings().app_mode, "generated_at": datetime.now(UTC),
            "as_of_time": as_of_time, "quality": {"status": status, "reasons": reasons or []}}}
