"""Bounded missing-leader scope, derived only from permitted saved observations."""
import re
from datetime import date, timedelta
from sqlalchemy import select
from app.evidence.models import EvidenceVersion
from app.market.kpl_atlas_source import DATASET
from app.market.kpl_leaders import DATASET as LEADERS
from app.read_cache_models import ReadCache
from app.models import User, Job

MAX_REPAIR_CODES = 10
MAX_SELECTED_CODES = 20


def daily_ranking(db, source_id, day, clock):
    saved=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id,
        EvidenceVersion.kind=='reference',EvidenceVersion.visibility=='shared',EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.received_at<=clock,EvidenceVersion.payload['dataset'].astext==DATASET,
        EvidenceVersion.payload['observed_day'].astext==str(day),
        EvidenceVersion.payload['rows'][0]['archive_kind'].astext=='daily'
        ).order_by(EvidenceVersion.received_at.desc()).limit(1))
    if saved is None:
        return None
    from app.market.sector_atlas import kpl_panel
    return saved if kpl_panel(saved,day,set())['status']=='ready' else None


def required_codes(db, provider, owner_id, day, days, clock, saved=None):
    """At most 40: daily top ten, latest top ten, 20 recent selections.

    Read-cache params are scheduling interest, never market evidence. Historical
    as-of reads cannot create present-day work. No read request calls upstream.
    """
    latest=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==provider.id,
        EvidenceVersion.kind=='reference',EvidenceVersion.visibility=='shared',EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.received_at<=clock,EvidenceVersion.payload['dataset'].astext==DATASET
        ).order_by(EvidenceVersion.payload['observed_day'].astext.desc(),EvidenceVersion.received_at.desc()).limit(1))
    codes=[r['code'] for r in (saved.payload['rows'][:10] if saved else [])]
    codes += [r['code'] for r in (latest.payload['rows'][:10] if latest else [])]
    selections=[]
    from app.read_cache import cache_scope
    owner=db.get(User,owner_id)
    scope=cache_scope(db,owner) if owner and owner.enabled else None
    for cache in db.scalars(select(ReadCache).where(ReadCache.owner_id==owner_id,ReadCache.scope==scope,
            ReadCache.operation=='review-sector-atlas',ReadCache.accessed_at>=clock-timedelta(days=7)
            ,ReadCache.accessed_at<=clock
            ).order_by(ReadCache.accessed_at.desc(),ReadCache.key).limit(100)):
        params=cache.params
        if params.get('source')!='kpl' or params.get('as_of_time'):
            continue
        try:
            end=date.fromisoformat(params['trade_day'])
            window=int(params.get('lookback',20))
            selected=params.get('selected_codes') or ''
            if not days or end>max(days) or not 1<=window<=50 or not re.fullmatch(r'\d{6}(?:,\d{6}){0,19}',selected):
                continue
            if day not in sorted(d for d in days if d<=end)[-window:]:
                continue
            selections.extend(code for code in selected.split(',') if code not in selections)
            selections=selections[:MAX_SELECTED_CODES]
        except (ValueError,TypeError,KeyError):
            continue
    # Selected codes lead the repair queue, within the same unchanged budget.
    # The most recent job carries the cumulative target, independently of
    # disposable cache eviction. Revalidate its original owner/permission scope.
    prior=db.scalar(select(Job).where(Job.kind=='direction.archive',Job.owner_id==owner_id,
        Job.payload['source_id'].astext==str(provider.id),Job.payload['day'].astext==str(day),
        Job.payload['leader_required_codes'].is_not(None),Job.created_at<=clock
        ).order_by(Job.created_at.desc(),Job.id.desc()).limit(1))
    carried=(prior.payload.get('leader_required_codes',[]) if prior and
             prior.payload.get('leader_interest_scope')==scope else [])
    ordered=list(dict.fromkeys(selections+codes+[c for c in carried if isinstance(c,str)]))[:400]
    universe={r['code'] for r in saved.payload['rows']} if saved else None
    return [c for c in ordered if re.fullmatch(r'\d{6}',c) and (universe is None or c in universe)]


def missing_codes(db, source_id, day, codes, clock):
    complete=set()
    for saved in db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id,
            EvidenceVersion.kind=='reference',EvidenceVersion.visibility=='shared',EvidenceVersion.owner_id.is_(None),
            EvidenceVersion.received_at<=clock,EvidenceVersion.payload['dataset'].astext==LEADERS,
            EvidenceVersion.payload['observed_day'].astext==str(day),
            EvidenceVersion.payload['rows'][0]['archive_kind'].astext=='daily',
            EvidenceVersion.payload['rows'][0]['coverage'].astext=='complete')):
        rows=saved.payload.get('rows',[])
        if rows:
            complete.add(rows[0].get('plate_code'))
    return [code for code in codes if code not in complete]


def repair_slice(db, source_id, owner_id, day, missing):
    """Least recently attempted first so a permanently partial code cannot starve others."""
    attempted={}
    jobs=db.scalars(select(Job).where(Job.owner_id==owner_id,Job.kind=='direction.archive',
        Job.payload['kpl_counts'].astext.is_distinct_from('true'),
        Job.payload['kpl_leaders'].astext.is_distinct_from('true'),
        Job.payload['source_id'].astext==str(source_id),Job.payload['day'].astext==str(day)
        ).order_by(Job.created_at.desc(),Job.id.desc()).limit(400))
    for job in jobs:
        for code in job.payload.get('leader_repair_codes',[]):
            attempted.setdefault(code,job.created_at.timestamp())
    return sorted(missing,key=lambda code:attempted.get(code,0))[:MAX_REPAIR_CODES]
