"""Reference coverage metadata, derived from an already authorized payload."""


def reference_summary(payload):
    rows = payload.get('rows') or []
    first = rows[0] if rows else {}
    return dict(dataset=payload.get('dataset'),
        trade_day=first.get('trade_day') or first.get('trade_date') or payload.get('observed_day'),
        scope=payload.get('scope'), row_count=len(rows))
