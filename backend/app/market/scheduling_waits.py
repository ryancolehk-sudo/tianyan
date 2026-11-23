"""A local scheduling guard is neither a supplier failure nor a new observation."""

SCHEDULING_WAITS = frozenset({'intraday_window_wait', 'source_budget_wait', 'local_request_wait'})


def is_scheduling_wait(run):
    # A bulk guard may stop after real requests but before a complete set exists.
    # Preserve those requests and the immutable error receipt; do not invent data.
    return bool(run and run.status == 'error' and run.reason in SCHEDULING_WAITS
        and not (run.accepted or run.duplicates))
