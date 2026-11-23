"""Isolated official SDK process. Only public anonymous requests, never user secrets.

No upstream code is copied here. The SDK has process-global socket state, so it
must not share the API/worker's threads, socket defaults or output streams.
"""
import calendar
import contextlib
import json
import os
import socket
import sys
from datetime import date, datetime, timedelta


def rows(result, maximum=15000):
    if result.error_code != '0':
        raise RuntimeError('upstream_unavailable')
    values = []
    while result.next():
        row = result.get_row_data()
        if len(values) >= maximum or len(row) != len(result.fields) or any(len(str(v)) > 200 for v in row):
            raise RuntimeError('schema_changed')
        values.append(dict(zip(result.fields, row)))
    if result.error_code != '0':
        raise RuntimeError('upstream_unavailable')
    return values


def read(now):
    import baostock as bs
    import baostock.util.socketutil as transport
    calls = 0
    original = transport.send_msg

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    transport.send_msg = counted
    # The official bulk response can take more than ten seconds in Docker.
    # Keep one bounded connection; do not increase request concurrency/retries.
    socket.setdefaulttimeout(30)
    answer = {}
    try:
        if bs.login().error_code != '0':
            raise RuntimeError('upstream_unavailable')
        first = now.date().replace(day=1)
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        days = rows(bs.query_trade_dates(start_date=str(first - timedelta(days=20)), end_date=str(last)), 70)
        # A date is not an intraday timestamp. Before the publication window,
        # ask for the previous date; never label a close as a live quotation.
        cutoff = now.date() if now.hour >= 18 else now.date() - timedelta(days=1)
        candidates = sorted((r['calendar_date'] for r in days
            if r['is_trading_day'] == '1' and date.fromisoformat(r['calendar_date']) <= cutoff), reverse=True)
        market = []
        for day in candidates[:2]:
            market = rows(bs.query_daily_history_k_AStock(date=day))
            if market:
                break
        if not market:
            raise RuntimeError('daily_data_not_published')
        names = {r['code']: r['code_name'] for r in rows(bs.query_stock_basic())}
        selected_day = market[0]['date']
        for code in ('sh.000001', 'sz.399001', 'sz.399006', 'sh.000688'):
            result = bs.query_history_k_data_plus(code,
                'date,code,open,high,low,close,preclose,volume,amount',
                start_date=selected_day, end_date=selected_day, frequency='d', adjustflag='3')
            market.extend(rows(result, 1))
        answer = {'trade_day': selected_day, 'rows': market, 'names': names,
                  'calendar': [r for r in days if str(first) <= r['calendar_date'] <= str(last)]}
    except (KeyError, ValueError):
        answer = {'error': 'schema_changed'}
    except RuntimeError as error:
        answer = {'error': str(error)}
    except Exception:
        answer = {'error': 'transport_error'}
    finally:
        try:
            bs.logout()
        except Exception:
            pass
    answer['request_count'] = calls
    return answer


if __name__ == '__main__':
    # The SDK can print diagnostics; neither those nor tracebacks contain data
    # in our worker log. Memory and wall time are bounded independently.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (1_500_000_000, 1_500_000_000))
        resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
    except ImportError:
        pass  # Windows operator probes are still limited by the parent timeout.
    try:
        now = datetime.fromisoformat(sys.stdin.read(100))
        with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            answer = read(now)
        sys.stdout.write(json.dumps(answer, ensure_ascii=False))
    except Exception:
        sys.stdout.write('{"error":"transport_error","request_count":0}')
