"""Short retries for a fully shaped KPL page with an incoherent native order.

This changes only local retry pacing. Every retry still validates the entire
page and takes the shared physical permit and real supplier cooldown.
"""
FAMILY='direction_kpl_top'
ORDER_RECHECK_SECONDS=30
SCHEMA_RECHECK_SECONDS=900


def interval_seconds(family,configured,diagnostic=None):
    short=False
    if family==FAMILY and isinstance(diagnostic,dict):
        order=diagnostic.get('top_strength_order')
        count=diagnostic.get('source_total_count')
        short=(type(diagnostic.get('version')) is int and diagnostic['version']==1
            and diagnostic.get('family')==FAMILY and diagnostic.get('checkpoint')=='kpl_strength_order'
            and diagnostic.get('root_shape')=='object' and diagnostic.get('row_widths')==[19]
            and diagnostic.get('source_error_code')=='0'
            and isinstance(count,str) and count.isascii() and count.isdigit() and len(count)<=3
            and 1<=int(count)<=400 and type(diagnostic.get('row_count')) is int
            and diagnostic['row_count']==min(30,int(count)) and isinstance(order,dict)
            and type(order.get('head_descending')) is bool and type(order.get('head_dominates_tail')) is bool
            and (not order['head_descending'] or not order['head_dominates_tail']))
    return max(configured,ORDER_RECHECK_SECONDS if short else SCHEMA_RECHECK_SECONDS)


def latest_diagnostic(db,provider,clock):
    if provider.family!=FAMILY:return None
    from sqlalchemy import select
    from app.market.models import DataFetchRun
    from app.models import Job
    # Inspect the latest physical outcome, not an older convenient failure.
    row=db.execute(select(DataFetchRun.status,DataFetchRun.reason,Job.result_ref)
        .outerjoin(Job,Job.id==DataFetchRun.job_id).where(DataFetchRun.source_id==provider.id,
            DataFetchRun.request_count>0,DataFetchRun.received_at<=clock)
        .order_by(DataFetchRun.received_at.desc(),DataFetchRun.id.desc()).limit(1)).first()
    if row is None or row.status!='error' or row.reason!='schema_changed':return None
    return (row.result_ref or {}).get('direction_diagnostic')
