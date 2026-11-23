"""Self-authored offline feed/page fixtures, runnable without a database via unittest."""
import hashlib
import gzip
import json
import unittest
from datetime import UTC, datetime
from unittest.mock import patch
from types import SimpleNamespace
from urllib.parse import parse_qs
from xml.sax.saxutils import escape

from app.market.news_feeds import NEWS_FEEDS, NEWS_FAMILIES
from app.market.protocol import ALLOWED_HOSTS, MAX_BODY, Response, SourceFailure, fetch_https
from app.market.public import (CNINFO_MARKETS, CNINFO_MAX_PAGES, CNINFO_PAGE_SIZE,
                               URLS, fetch_cninfo_batch, fetch_public, parse_public)
from app.market.registry import SOURCES


NOW = datetime(2026, 10, 25, 2, tzinfo=UTC)
MIDNIGHT = int(datetime(2026, 10, 24, 16, tzinfo=UTC).timestamp() * 1000)


def announcement(identity, **changes):
    return {'announcementId': identity, 'announcementTitle': '合成公告 ' + identity,
            'orgId': 'synthetic-org', 'announcementTime': MIDNIGHT, **changes}


def page_body(market, page, total):
    start = (page - 1) * CNINFO_PAGE_SIZE
    return {'totalAnnouncement': total, 'hasMore': page * CNINFO_PAGE_SIZE < total,
            'announcements': [announcement(f'{market}-{index}')
                              for index in range(start, min(start + CNINFO_PAGE_SIZE, total))]}


class CninfoHarness:
    def __init__(self, totals=None, changes=None, failures=None, duration=0.01):
        self.totals = totals or dict.fromkeys(CNINFO_MARKETS, 1)
        self.changes = changes or {}
        self.failures = failures or {}
        self.calls = []
        self.clock = 0
        self.duration = duration

    def monotonic(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds

    def fetch(self, url, *, timeout, data, max_body=MAX_BODY):
        assert url == URLS['cninfo_announcements'] and 0 < timeout <= 15
        assert 0 < max_body <= MAX_BODY
        values = parse_qs(data.decode(), keep_blank_values=True)
        assert values['column'] == ['szse'] and values['pageSize'] == ['30']
        assert values['seDate'] == ['2026-10-18~2026-10-25']
        market, page = values['plate'][0], int(values['pageNum'][0])
        assert market in CNINFO_MARKETS and 1 <= page <= CNINFO_MAX_PAGES
        self.calls.append((market, page))
        self.clock += self.duration
        failure = self.failures.get((market, page))
        if failure:
            if isinstance(failure, SourceFailure):
                raise failure
            raise SourceFailure(failure)
        body = page_body(market, page, self.totals[market])
        change = self.changes.get((market, page))
        if change:
            body = change(body)
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        if len(raw) > max_body:
            raise SourceFailure('response_too_large')
        return Response(raw, NOW, 10, 200)

    def run(self, *, reserve=None):
        with patch('app.market.public.time.monotonic', self.monotonic), \
                patch('app.market.public.time.sleep', self.sleep), \
                patch('app.market.public.fetch_https', self.fetch):
            return fetch_cninfo_batch(clock=NOW, reserve=reserve)


class NewsSourcesV2Tests(unittest.TestCase):
    def test_transport_custom_budget_bounds_both_wire_bytes_and_gzip_decode(self):
        for wire, encoding, expected in [(b'x' * 101, '', 'response_too_large'),
                (gzip.compress(b'x' * 101), 'gzip', 'response_too_large'),
                (gzip.compress(b'x' * 80), 'gzip', None), (b'x' * 80, '', None)]:
            with self.subTest(encoding=encoding, expected=expected):
                reads = []
                class HttpResponse:
                    status = 200
                    headers = {'Content-Encoding': encoding}

                    def read(self, maximum):
                        reads.append(maximum)
                        return wire[:maximum]

                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        pass
                with patch('urllib.request.build_opener', return_value=SimpleNamespace(open=lambda *args, **kwargs: HttpResponse())):
                    if expected:
                        with self.assertRaises(SourceFailure) as raised:
                            fetch_https(URLS['chinanews_finance'], max_body=100)
                        self.assertEqual(raised.exception.reason, expected)
                    else:
                        response = fetch_https(URLS['chinanews_finance'], max_body=100)
                        self.assertEqual(response.body, b'x' * 80)
                self.assertEqual(reads, [101])

    def test_transport_rejects_invalid_custom_budget_before_network(self):
        with patch('urllib.request.build_opener') as open_request:
            for maximum in [True, 0, -1, 2.5, MAX_BODY + 1]:
                with self.subTest(maximum=maximum), self.assertRaises(SourceFailure) as raised:
                    fetch_https(URLS['chinanews_finance'], max_body=maximum)
                self.assertEqual(raised.exception.reason, 'invalid_response_budget')
            open_request.assert_not_called()

    def test_chinanews_registers_only_the_confirmed_publisher_host_and_900_second_interval(self):
        family = 'chinanews_finance'
        self.assertIn(family, NEWS_FAMILIES)
        self.assertEqual(URLS[family], 'https://www.chinanews.com.cn/rss/finance.xml')
        self.assertEqual(NEWS_FEEDS[family].article_hosts, ('www.chinanews.com.cn',))
        self.assertIn('www.chinanews.com.cn', ALLOWED_HOSTS)
        self.assertEqual(SOURCES[family].minimum_interval, 900)

    def cns_feed(self, *, title='合成财经原题', summary='原生短摘要', link='https://www.chinanews.com.cn/cj/synthetic.shtml', stamp='Sat, 24 Oct 2026 21:18:02 +0800'):
        return (f'<rss version="2.0"><channel><item><title>{escape(title)}</title>'
                f'<link>{escape(link)}</link><description>{escape(summary)}</description>'
                f'<pubDate>{escape(stamp)}</pubDate></item></channel></rss>').encode()

    def test_chinanews_keeps_native_title_summary_source_and_real_clock(self):
        batch = parse_public('chinanews_finance', self.cns_feed(), NOW)
        key, payload, published = batch.records[0]
        self.assertEqual(payload.title, '合成财经原题')
        self.assertEqual(payload.summary, '原生短摘要')
        self.assertEqual(payload.source_name, '中国新闻网')
        self.assertEqual(published, datetime(2026, 10, 24, 13, 18, 2, tzinfo=UTC))
        self.assertEqual(key, hashlib.sha256(payload.source_url.encode()).hexdigest())
        self.assertEqual(batch.coverage['scope'], 'current_feed')

    def test_chinanews_does_not_make_up_summary_or_date_precision(self):
        batch = parse_public('chinanews_finance', self.cns_feed(summary='', stamp='2026-10-24'), NOW)
        self.assertIsNone(batch.records[0][1].summary)
        self.assertIsNone(batch.records[0][2])
        self.assertEqual(str(batch.records[0][1].published_day), '2026-10-24')

    def test_chinanews_rejects_other_hosts_and_title_truncation(self):
        for change in [{'link': 'https://www.chinanews.com.cn.evil.invalid/story'},
                       {'link': 'https://www.chinanews.com/story'}, {'title': '字' * 201}]:
            with self.subTest(change=change):
                batch = parse_public('chinanews_finance', self.cns_feed(**change), NOW)
                self.assertFalse(batch.records)
                self.assertEqual(batch.rejected, 1)
                self.assertTrue(batch.partial)

    def test_three_market_plates_use_independent_pages_in_one_batch(self):
        fixture = CninfoHarness({'sh': 31, 'sz': 1, 'bj': 0})
        response, batch = fixture.run()
        self.assertEqual(fixture.calls, [('sh', 1), ('sh', 2), ('sz', 1), ('bj', 1)])
        self.assertEqual((response.request_count, batch.requests), (4, 4))
        self.assertEqual((len(batch.records), batch.expected, batch.rejected), (32, 32, 0))
        self.assertFalse(batch.partial)
        self.assertEqual([row['status'] for row in batch.coverage['markets']], ['complete'] * 3)
        self.assertEqual([row['total'] for row in batch.coverage['markets']], [31, 1, 0])

    def test_page_cap_is_partial_even_when_the_final_page_claims_no_more(self):
        for total in [150, 151, 1000]:
            with self.subTest(total=total):
                fixture = CninfoHarness(dict.fromkeys(CNINFO_MARKETS, total))
                _, batch = fixture.run()
                self.assertEqual(len(fixture.calls), 15)
                self.assertEqual((batch.expected, len(batch.records)), (450, 450))
                self.assertTrue(batch.partial)
                self.assertEqual([row['reason'] for row in batch.coverage['markets']], ['page_limit_reached'] * 3)
                self.assertEqual([row['has_more'] for row in batch.coverage['markets']], [total > 150] * 3)

    def test_transport_failure_keeps_verified_market_pages_and_honest_coverage(self):
        fixture = CninfoHarness({'sh': 31, 'sz': 1, 'bj': 1}, failures={('sh', 2): 'transport_error'})
        response, batch = fixture.run()
        self.assertEqual((response.request_count, batch.requests), (4, 4))
        self.assertEqual((batch.expected, len(batch.records)), (32, 32))
        self.assertTrue(batch.partial)
        self.assertEqual(batch.coverage['markets'][0]['reason'], 'transport_error')
        self.assertEqual(batch.coverage['markets'][0]['status'], 'partial')
        self.assertEqual(batch.coverage['markets'][1]['status'], 'complete')

    def test_all_transport_failures_are_failed_not_successful_empty(self):
        fixture = CninfoHarness(failures={(market, 1): 'transport_error' for market in CNINFO_MARKETS})
        with self.assertRaises(SourceFailure) as raised:
            fixture.run()
        self.assertEqual(raised.exception.reason, 'transport_error')
        self.assertEqual(raised.exception.request_count, 3)
        self.assertEqual([row['status'] for row in raised.exception.coverage['markets']], ['error'] * 3)

    def test_first_permission_or_rate_limit_stops_the_source_without_retry_bypass(self):
        for reason in ['permission_missing', 'rate_limited', 'upstream_redirect']:
            with self.subTest(reason=reason):
                fixture = CninfoHarness(failures={('sh', 1): reason})
                with self.assertRaises(SourceFailure) as raised:
                    fixture.run()
                self.assertEqual(raised.exception.reason, reason)
                self.assertEqual(raised.exception.request_count, 1)
                self.assertEqual(fixture.calls, [('sh', 1)])

    def test_reservation_runs_before_every_page_and_denial_does_not_count_as_http(self):
        fixture = CninfoHarness({'sh': 31, 'sz': 1, 'bj': 1})
        permits = []
        def reserve():
            if len(permits) == 2:
                raise SourceFailure('rate_limited', retry_after=3600)
            permits.append(len(fixture.calls))
        _, batch = fixture.run(reserve=reserve)
        self.assertEqual(permits, [0, 1])
        self.assertEqual(fixture.calls, [('sh', 1), ('sh', 2)])
        self.assertEqual((batch.requests, batch.expected), (2, 31))
        self.assertTrue(batch.partial)
        self.assertEqual(batch.coverage['markets'][1]['requests'], 0)
        self.assertEqual(batch.coverage['retry_after'], 3600)

    def test_first_refused_reservation_has_zero_requests_and_no_fabricated_empty_batch(self):
        for reason in ['rate_limited', 'collection_disabled', 'configuration_changed', 'lease_expired']:
            with self.subTest(reason=reason):
                fixture = CninfoHarness()
                def denied():
                    raise SourceFailure(reason)
                with self.assertRaises(SourceFailure) as raised:
                    fixture.run(reserve=denied)
                self.assertEqual(raised.exception.reason, reason)
                self.assertEqual(raised.exception.request_count, 0)
                self.assertEqual(fixture.calls, [])

    def test_legacy_false_reservation_also_sends_no_http(self):
        fixture = CninfoHarness()
        with self.assertRaises(SourceFailure) as raised:
            fixture.run(reserve=lambda: False)
        self.assertEqual(raised.exception.reason, 'rate_limited')
        self.assertEqual(raised.exception.request_count, 0)

    def test_reservation_time_is_included_in_the_whole_run_deadline(self):
        fixture = CninfoHarness()
        def slow_reserve():
            fixture.clock += 16
        with self.assertRaises(SourceFailure) as raised:
            fixture.run(reserve=slow_reserve)
        self.assertEqual(raised.exception.reason, 'timeout')
        self.assertEqual(raised.exception.request_count, 0)
        self.assertFalse(fixture.calls)

    def test_partial_rate_limit_keeps_retry_after_for_the_worker_schedule(self):
        fixture = CninfoHarness({'sh': 31, 'sz': 1, 'bj': 1},
            failures={('sh', 2): SourceFailure('rate_limited', retry_after=3600)})
        _, batch = fixture.run()
        self.assertEqual((batch.requests, batch.expected), (2, 30))
        self.assertTrue(batch.partial)
        self.assertEqual(batch.coverage['retry_after'], 3600)
        self.assertEqual(batch.coverage['markets'][0]['reason'], 'rate_limited')

    def test_rejected_schema_is_never_published_as_complete(self):
        cases = [lambda raw: {}, lambda raw: {**raw, 'totalAnnouncement': True},
                 lambda raw: {**raw, 'hasMore': 'false'}, lambda raw: {**raw, 'announcements': None},
                 lambda raw: {**raw, 'totalAnnouncement': 0}, lambda raw: {**raw, 'hasMore': True}]
        for change in cases:
            with self.subTest(change=change):
                fixture = CninfoHarness(changes={('sh', 1): change})
                with self.assertRaises(SourceFailure) as raised:
                    fixture.run()
                self.assertEqual(raised.exception.reason, 'schema_changed')
                self.assertEqual(raised.exception.request_count, 1)
                self.assertEqual(raised.exception.coverage['markets'][0]['status'], 'error')

    def test_schema_failure_after_a_valid_page_keeps_only_verified_rows(self):
        fixture = CninfoHarness({'sh': 31, 'sz': 1, 'bj': 1}, changes={('sh', 2): lambda _: b'<html>blocked</html>'})
        _, batch = fixture.run()
        self.assertEqual((batch.requests, batch.expected, len(batch.records)), (2, 30, 30))
        self.assertTrue(batch.partial)
        self.assertEqual(batch.coverage['markets'][0]['reason'], 'schema_changed')
        self.assertEqual(batch.coverage['markets'][1]['requests'], 0)

    def test_explicit_empty_pages_are_valid_but_do_not_create_news(self):
        fixture = CninfoHarness(dict.fromkeys(CNINFO_MARKETS, 0),
            changes={('bj', 1): lambda raw: {**raw, 'announcements': None}})
        _, batch = fixture.run()
        self.assertEqual((batch.requests, batch.expected, len(batch.records)), (3, 0, 0))
        self.assertFalse(batch.partial)

    def test_dates_encoded_at_midnight_keep_only_the_source_date(self):
        _, batch = CninfoHarness().run()
        self.assertTrue(all(published is None for _, _, published in batch.records))
        self.assertTrue(all(str(payload.published_day) == '2026-10-25' for _, payload, _ in batch.records))
        self.assertTrue(all(payload.summary is None for _, payload, _ in batch.records))

    def test_same_identity_content_and_clock_is_a_duplicate_across_markets(self):
        fixture = CninfoHarness(changes={('sz', 1): lambda raw: {**raw, 'announcements': [announcement('sh-0')]}})
        _, batch = fixture.run()
        self.assertEqual((batch.expected, len(batch.records), batch.duplicates, batch.rejected), (3, 2, 1, 0))
        self.assertEqual(batch.coverage['markets'][1]['duplicates'], 1)
        self.assertFalse(batch.partial)

    def test_same_identity_with_a_real_correction_preserves_both_observations(self):
        fixture = CninfoHarness(changes={('sz', 1): lambda raw: {**raw,
            'announcements': [announcement('sh-0', announcementTitle='合成更正原题')]}})
        _, batch = fixture.run()
        self.assertEqual((batch.expected, len(batch.records), batch.duplicates), (3, 3, 0))
        self.assertEqual(batch.records[0][0], batch.records[1][0])
        self.assertNotEqual(batch.records[0][1].title, batch.records[1][1].title)

    def test_invalid_rows_count_as_rejected_and_do_not_claim_complete(self):
        for change in [{'announcementTitle': '字' * 201}, {'announcementId': True},
                       {'orgId': {'unsafe': 'identity'}}, {'announcementTime': 'invented-clock'},
                       {'announcementTime': int(datetime(2026, 10, 26, tzinfo=UTC).timestamp() * 1000)}]:
            with self.subTest(change=change):
                fixture = CninfoHarness(changes={('sh', 1): lambda raw: {**raw,
                    'announcements': [announcement('sh-0', **change)]}})
                _, batch = fixture.run()
                self.assertEqual((batch.expected, len(batch.records), batch.rejected), (3, 2, 1))
                self.assertTrue(batch.partial)
                self.assertEqual(batch.coverage['markets'][0]['reason'], 'rows_rejected')

    def test_total_changes_during_paging_are_visible_as_partial(self):
        fixture = CninfoHarness({'sh': 31, 'sz': 1, 'bj': 1}, changes={('sh', 2): lambda _: page_body('sh', 2, 32)})
        _, batch = fixture.run()
        self.assertTrue(batch.partial)
        self.assertEqual(batch.coverage['markets'][0]['reason'], 'snapshot_changed')
        self.assertEqual(batch.coverage['markets'][0]['total'], 32)

    def test_total_byte_budget_covers_all_pages_and_stops_before_accepting_overflow(self):
        def oversized(raw):
            return {**raw, 'padding': 'x' * (MAX_BODY // 2)}
        fixture = CninfoHarness(changes={(market, 1): oversized for market in CNINFO_MARKETS})
        response, batch = fixture.run()
        self.assertEqual((batch.requests, batch.expected, len(batch.records)), (2, 1, 1))
        self.assertTrue(batch.partial)
        self.assertEqual(batch.coverage['markets'][1]['reason'], 'response_too_large')
        self.assertLessEqual(len(response.body), MAX_BODY)
        self.assertEqual(batch.coverage['received_bytes'], len(response.body))

    def test_whole_run_timeout_is_not_reset_for_the_next_market(self):
        fixture = CninfoHarness(duration=8)
        _, batch = fixture.run()
        self.assertEqual((batch.requests, batch.expected), (2, 1))
        self.assertTrue(batch.partial)
        self.assertEqual(batch.coverage['markets'][1]['reason'], 'timeout')
        self.assertEqual(batch.coverage['markets'][2]['requests'], 0)

    def test_legacy_single_page_parser_and_fetch_signature_stay_available(self):
        raw = json.dumps({'announcements': [announcement('legacy-fixture')]}).encode()
        batch = parse_public('cninfo_announcements', raw, NOW)
        self.assertEqual((batch.expected, len(batch.records)), (1, 1))
        with patch('app.market.public.fetch_https', return_value=Response(raw, NOW, 1, 200)) as fetch:
            self.assertIsInstance(fetch_public('cninfo_announcements'), Response)
        self.assertEqual(fetch.call_args.args[0], URLS['cninfo_announcements'])


def test_cninfo_worker_persists_true_pagination_duplicate_counts_and_coverage(client, monkeypatch):
    """Disposable PostgreSQL only; never requests a real upstream endpoint."""
    import uuid

    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from app.config import settings
    from app.db import engine
    from app.evidence.models import EvidenceVersion
    from app.market.budget import CNINFO_NEWS_ACTION
    from app.market.models import DataFetchRun
    from app.models import AuditEvent
    from app.news_center.models import NewsArticle, NewsSourceCoverage
    from app.worker import claim, process
    from test_connected_data import source_config
    from test_data_sources import configure, login

    fixture = CninfoHarness({'sh': 151, 'sz': 1, 'bj': 1}, changes={('sz', 1): lambda raw:
        {**raw, 'announcements': [announcement('sh-0')]}})
    monkeypatch.setenv('JL_DATA_COLLECTION_ENABLED', 'true')
    settings.cache_clear()
    try:
        headers = login(client)
        configured = configure(client, headers, source_config('cninfo_announcements'), key='synthetic-cninfo-v2')
        assert configured.status_code == 201
        source = uuid.UUID(configured.json()['data']['provider']['id'])
        def paged_batch(*, reserve):
            response, batch = fixture.run(reserve=reserve)
            return Response(response.body, datetime.now(UTC), response.elapsed_ms, 200, response.request_count), batch
        monkeypatch.setattr('app.market.collect.fetch_cninfo_batch', paged_batch)
        queued = client.post(f'/api/v1/data-sources/{source}/refresh', json={},
            headers={**headers, 'Idempotency-Key': 'synthetic-cninfo-paged-refresh'})
        assert queued.status_code == 202
        job = claim()
        assert job and str(job.id) == queued.json()['data']['job_id']
        process(job)
        with Session(engine()) as db:
            run = db.scalar(select(DataFetchRun).where(DataFetchRun.job_id == job.id))
            assert (run.status, run.reason) == ('partial', 'news_coverage_incomplete')
            assert (run.expected, run.accepted, run.rejected, run.duplicates, run.request_count) == (152, 151, 0, 1, 7)
            assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == CNINFO_NEWS_ACTION)) == 7
            coverage = db.get(NewsSourceCoverage, source)
            assert coverage.fetch_run_id == run.id and coverage.coverage['partial'] is True
            assert coverage.coverage['markets'][0]['reason'] == 'page_limit_reached'
            assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(EvidenceVersion.source_id == source)) == 151
            assert db.scalar(select(func.count()).select_from(NewsArticle).where(NewsArticle.source_id == source)) == 151
    finally:
        settings.cache_clear()


def test_cninfo_worker_failed_pages_keep_unknown_expected_and_actual_requests(client, monkeypatch):
    import uuid

    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from app.config import settings
    from app.db import engine
    from app.evidence.models import EvidenceVersion
    from app.market.budget import CNINFO_NEWS_ACTION
    from app.market.models import DataFetchRun
    from app.models import AuditEvent
    from app.news_center.models import NewsSourceCoverage
    from app.worker import claim, process
    from test_connected_data import source_config
    from test_data_sources import configure, login

    fixture = CninfoHarness(failures={(market, 1): 'transport_error' for market in CNINFO_MARKETS})
    def failed_batch(*, reserve):
        return fixture.run(reserve=reserve)
    monkeypatch.setenv('JL_DATA_COLLECTION_ENABLED', 'true')
    settings.cache_clear()
    try:
        headers = login(client)
        configured = configure(client, headers, source_config('cninfo_announcements'), key='synthetic-cninfo-fail-config')
        assert configured.status_code == 201
        source = uuid.UUID(configured.json()['data']['provider']['id'])
        monkeypatch.setattr('app.market.collect.fetch_cninfo_batch', failed_batch)
        queued = client.post(f'/api/v1/data-sources/{source}/refresh', json={},
            headers={**headers, 'Idempotency-Key': 'synthetic-cninfo-failed-refresh'})
        assert queued.status_code == 202
        job = claim()
        assert job and str(job.id) == queued.json()['data']['job_id']
        process(job)
        with Session(engine()) as db:
            run = db.scalar(select(DataFetchRun).where(DataFetchRun.job_id == job.id))
            assert (run.status, run.reason, run.expected, run.accepted, run.request_count) == ('error', 'transport_error', None, None, 3)
            assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == CNINFO_NEWS_ACTION)) == 3
            assert db.get(NewsSourceCoverage, source).coverage['partial'] is True
            assert not db.scalar(select(func.count()).select_from(EvidenceVersion).where(EvidenceVersion.source_id == source))
    finally:
        settings.cache_clear()


def test_cninfo_persisted_quota_refuses_a_page_without_counting_or_sending_it(client, monkeypatch):
    import uuid

    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from app.config import settings
    from app.db import engine
    from app.market.budget import CNINFO_NEWS_ACTION
    from app.market.models import DataFetchRun, SourceSettings
    from app.models import AuditEvent
    from app.news_center.models import NewsSourceCoverage
    from app.worker import claim, process
    from test_connected_data import source_config
    from test_data_sources import configure, login

    fixture = CninfoHarness({'sh': 31, 'sz': 1, 'bj': 1})
    monkeypatch.setenv('JL_DATA_COLLECTION_ENABLED', 'true')
    monkeypatch.setattr('app.market.budget.CNINFO_NEWS_DAILY_LIMIT', 2)
    settings.cache_clear()
    try:
        headers = login(client)
        configured = configure(client, headers, source_config('cninfo_announcements'), key='synthetic-news-local-budget')
        assert configured.status_code == 201
        source = uuid.UUID(configured.json()['data']['provider']['id'])
        def budgeted_batch(*, reserve):
            response, batch = fixture.run(reserve=reserve)
            return Response(response.body, datetime.now(UTC), response.elapsed_ms, 200, response.request_count), batch
        monkeypatch.setattr('app.market.collect.fetch_cninfo_batch', budgeted_batch)
        queued = client.post(f'/api/v1/data-sources/{source}/refresh', json={},
            headers={**headers, 'Idempotency-Key': 'synthetic-news-local-budget-refresh'})
        assert queued.status_code == 202
        job = claim()
        assert job and str(job.id) == queued.json()['data']['job_id']
        process(job)
        assert fixture.calls == [('sh', 1), ('sh', 2)]
        with Session(engine()) as db:
            run = db.scalar(select(DataFetchRun).where(DataFetchRun.job_id == job.id))
            assert (run.status, run.reason, run.expected, run.accepted, run.request_count) == ('partial', 'news_coverage_incomplete', 31, 31, 2)
            assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == CNINFO_NEWS_ACTION)) == 2
            assert run.retry_not_before is not None
            assert db.get(SourceSettings, source).next_run_at >= run.retry_not_before
            coverage = db.get(NewsSourceCoverage, source).coverage
            assert coverage['markets'][1]['requests'] == 0
            assert coverage['markets'][1]['reason'] == 'rate_limited'
            assert coverage['retry_after'] > 0
    finally:
        settings.cache_clear()


def test_cninfo_request_permit_rechecks_controls_before_each_page(client, monkeypatch):
    import uuid
    from datetime import timedelta

    import pytest
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from app.config import settings
    from app.db import engine
    from app.market.budget import CNINFO_NEWS_ACTION, reserve_news_request
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.models import SourceSettings
    from app.models import AuditEvent, Job, Provider
    from app.worker import claim
    from test_connected_data import source_config
    from test_data_sources import configure, login

    monkeypatch.setenv('JL_DATA_COLLECTION_ENABLED', 'true')
    settings.cache_clear()
    try:
        headers = login(client)
        configured = configure(client, headers, source_config('cninfo_announcements'), key='synthetic-news-permit-controls')
        assert configured.status_code == 201
        source = uuid.UUID(configured.json()['data']['provider']['id'])
        queued = client.post(f'/api/v1/data-sources/{source}/refresh', json={},
            headers={**headers, 'Idempotency-Key': 'synthetic-news-controls-refresh'})
        assert queued.status_code == 202
        job = claim()
        assert job and str(job.id) == queued.json()['data']['job_id']
        with Session(engine()) as db:
            expected = configuration_fingerprint(db.get(Provider, source), db.get(SourceSettings, source))
        assert reserve_news_request(job, expected_configuration=expected) is True
        for control, reason in [('rights', 'permission_missing'), ('paused', 'source_paused'),
                                ('configuration', 'configuration_changed'), ('lease', 'lease_expired')]:
            with Session(engine()) as db, db.begin():
                provider, config, current = db.get(Provider, source), db.get(SourceSettings, source), db.get(Job, job.id)
                if control == 'rights':
                    provider.rights = {**provider.rights, 'cache': 'denied'}
                elif control == 'paused':
                    config.paused_reason = 'schema_changed'
                elif control == 'configuration':
                    config.interval_seconds = 1800
                else:
                    current.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            with pytest.raises(SourceFailure, match=reason):
                reserve_news_request(job, expected_configuration=expected)
            with Session(engine()) as db, db.begin():
                provider, config, current = db.get(Provider, source), db.get(SourceSettings, source), db.get(Job, job.id)
                provider.rights = {**provider.rights, 'cache': 'allowed'}
                config.paused_reason = None
                config.interval_seconds = 900
                current.lease_until = datetime.now(UTC) + timedelta(seconds=30)
        monkeypatch.setenv('JL_DATA_COLLECTION_ENABLED', 'false')
        settings.cache_clear()
        with pytest.raises(SourceFailure, match='collection_disabled'):
            reserve_news_request(job, expected_configuration=expected)
        with Session(engine()) as db:
            assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == CNINFO_NEWS_ACTION)) == 1
    finally:
        settings.cache_clear()


if __name__ == '__main__':
    unittest.main()
