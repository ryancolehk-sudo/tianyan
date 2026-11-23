"""Reuse immutable row counts, then select again through the caller's full scope.

The lookup bound limits cached SQL parameters, never the reference candidates.
Rows absent from the memo, including concurrent inserts, use their original JSON.
"""
from sqlalchemy import Integer, String, case, cast, column, func, tuple_, values

from app import read_cache_payloads
from app.evidence.models import EvidenceVersion

MAX_LOOKUP = 4096
COUNT_CHUNK = 512


def _key(ident, digest):
    return ('market-row-count-v1', str(ident), digest)


def largest_reference(db, query):
    """Preserve descending JSON array length/receipt order, including nulls."""
    evidence = EvidenceVersion
    metadata = list(db.execute(query.with_only_columns(evidence.id, evidence.content_hash,
        maintain_column_froms=True).order_by(evidence.received_at.desc(), evidence.id.desc()).limit(MAX_LOOKUP)))
    counts, missing = {}, []
    for ident, digest in metadata:
        cached = read_cache_payloads.get(_key(ident, digest))
        if cached is None:
            missing.append((ident, digest))
        else:
            counts[ident, digest] = cached['row_count']
    for start in range(0, len(missing), COUNT_CHUNK):
        part = missing[start:start+COUNT_CHUNK]
        counted = db.execute(query.with_only_columns(evidence.id, evidence.content_hash,
            func.jsonb_array_length(evidence.payload['rows']), maintain_column_froms=True)
            .where(tuple_(evidence.id, evidence.content_hash).in_(part)))
        for ident, digest, count in counted:
            counts[ident, digest] = count
            # Charge the id/hash as well as the scalar to the existing memory budget.
            payload = dict(evidence_id=str(ident), content_hash=digest, row_count=count)
            if getattr(read_cache_payloads, 'MAX_PROTECTED_BYTES', 0):
                read_cache_payloads.put(_key(ident, digest), payload, protected=True)
            else:
                read_cache_payloads.put(_key(ident, digest), payload)
    size = func.jsonb_array_length(evidence.payload['rows'])
    if counts:
        memo = values(column('evidence_id', evidence.id.type), column('content_hash', String(64)),
            column('row_count', Integer), name='cached_market_row_counts').data(
                [(ident, digest, count) for (ident, digest), count in counts.items()])
        query = query.outerjoin(memo, (memo.c.evidence_id == evidence.id) &
            (memo.c.content_hash == evidence.content_hash))
        size = case((memo.c.evidence_id.is_not(None), cast(memo.c.row_count, Integer)), else_=size)
    # Never choose an id from the memo alone. Original visibility/date filters
    # and a fallback for every new/uncached row remain in this final SQL query.
    return db.scalar(query.order_by(size.desc(), evidence.received_at.desc()).limit(1))
