"""Rule-only news scheduling and safe retirement of legacy edition AI jobs."""
from uuid import UUID

from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session

from app.auth import require_instance
from app.db import engine
from app.evidence.common import lock, now
from app.models import Job
from app.news_center.editions import schedule_editions
from app.news_center.models import NewsAiVersion


def schedule_news():
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        require_instance(db)
        from app.maintenance_control import admit, paused
        if paused(db):
            return 0
        admit(db)
        _close_terminated_attempts(db)
        return schedule_editions(db)


def _close_terminated_attempts(db):
    candidates = list(db.scalars(select(NewsAiVersion.edition_id).join(Job,
        Job.payload['edition_id'].astext == cast(NewsAiVersion.edition_id, String)).where(
            NewsAiVersion.state == 'running', Job.kind == 'news.ai',
            Job.state.in_(['failed', 'cancelled'])).distinct().order_by(NewsAiVersion.edition_id).limit(100)))
    for edition_id in candidates:
        lock(db, f'news-ai:{edition_id}')
        run = db.scalar(select(NewsAiVersion).where(NewsAiVersion.edition_id == edition_id).with_for_update())
        if run and run.state == 'running':
            run.state, run.error_code, run.completed_at = 'failed', 'news_ai_outcome_unknown', now(db)


def process_ai(job):
    """Cancel old news.ai work without resolving credentials or invoking any model.

    Only the independent AI worker claims these jobs. The lease fence also
    protects direct compatibility calls from reviving expired work. A previous
    complete NewsAiVersion remains immutable; a running legacy receipt has an
    unknown outcome and is closed without a second request.
    """
    with Session(engine()) as db, db.begin():
        require_instance(db)
        active = db.scalar(select(Job).where(Job.id == job.id, Job.kind == 'news.ai', Job.state == 'running',
            Job.lease_token == job.lease_token, Job.lease_token.is_not(None),
            Job.lease_until > func.clock_timestamp()).with_for_update())
        if not active:
            return
        try:
            edition_id = UUID(active.payload.get('edition_id', ''))
        except (ValueError, TypeError, AttributeError):
            edition_id = None
        if edition_id:
            lock(db, f'news-ai:{edition_id}')
            run = db.scalar(select(NewsAiVersion).where(NewsAiVersion.edition_id == edition_id).with_for_update())
            if run and run.state == 'running':
                run.state, run.error_code, run.completed_at = 'failed', 'news_ai_outcome_unknown', now(db)
        active.state, active.error_code, active.completed_at = 'cancelled', 'feature_not_implemented', now(db)
        active.lease_token = active.lease_until = None
