"""Self-authored RSS fixtures only; unittest is offline, PR pytest retains conftest."""
import hashlib
import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from html import escape

from app.market.hk_gov_news import FAMILY, MAX_ITEMS, SOURCE_NAME, parse_hk_gov_news
from app.market.protocol import MAX_BODY, SourceFailure
from app.market.public import NewsBatch

RECEIVED = datetime(2026, 10, 20, 4, tzinfo=UTC)
LINK = 'https://www.news.gov.hk/tc/categories/finance/html/synthetic-fixture.shtml'


def item(title='合成：財經及就業服務', link=LINK, published='Tue, 20 Oct 2026 00:00:00 +0800', extra=''):
    fields = [('title', title), ('link', link), ('pubDate', published)]
    return '<item>' + ''.join(
        f'<{tag}>{escape(value, quote=False)}</{tag}>'
        for tag, value in fields if value is not None
    ) + extra + '</item>'


def rss(*items, channel_extra=''):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0"><channel><title>自製財經測試頻道</title>'
            + channel_extra + ''.join(items) + '</channel></rss>').encode('utf-8')


class TestHkGovNews(unittest.TestCase):
    def parse(self, body, received=RECEIVED):
        batch = parse_hk_gov_news(FAMILY, body, received)
        self.assertIsInstance(batch, NewsBatch)
        self.assertEqual(batch.expected, len(batch.records) + batch.rejected)
        self.assertTrue(all(published is None for _, _, published in batch.records))
        return batch

    def failure(self, body, reason='schema_changed', *, received=RECEIVED, family=FAMILY):
        with self.assertRaises(SourceFailure) as caught:
            parse_hk_gov_news(family, body, received)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(str(caught.exception), reason)

    def test_actual_field_structure_with_self_authored_content(self):
        extra = ('<objectId>synthetic-object</objectId><guid isPermaLink="false">synthetic-guid</guid>'
                 '<description><![CDATA[<p>自製摘要，不能複製</p>'
                 '<img src="https://example.com/synthetic.png"/>]]></description>')
        batch = self.parse(rss(item(extra=extra)))
        self.assertEqual((batch.expected, batch.rejected, batch.requests), (1, 0, 1))
        key, payload, published = batch.records[0]
        self.assertEqual(key, hashlib.sha256(LINK.encode()).hexdigest())
        self.assertEqual(payload.title, '合成：財經及就業服務')
        self.assertEqual(payload.source_url, LINK)
        self.assertEqual(payload.source_name, SOURCE_NAME)
        self.assertEqual(payload.published_day, date(2026, 10, 20))
        self.assertIsNone(payload.summary)
        self.assertIsNone(payload.category)
        self.assertIsNone(published)
        self.assertNotIn('自製摘要', payload.model_dump_json())

    def test_cdata_and_markup_titles_keep_traditional_chinese(self):
        body = ('<item><title><![CDATA[ <b>合成財經</b>\t與就業\u202e\u200b\u007f ]]></title>'
                f'<link>{LINK}</link></item>')
        self.assertEqual(self.parse(rss(body)).records[0][1].title, '合成財經 與就業')
        nested = f'<item><title>自製<b>財經</b>標題</title><link>{LINK}</link></item>'
        self.assertEqual(self.parse(rss(nested)).records[0][1].title, '自製財經標題')
        self.assertEqual(len(self.parse(rss(item(title='合成' * 150))).records[0][1].title), 200)

    def test_source_and_summary_cannot_be_spoofed_by_optional_fields(self):
        extra = '<source>自製其他來源</source><author>自製作者</author><description>自製摘要</description>'
        payload = self.parse(rss(item(extra=extra))).records[0][1]
        self.assertEqual(payload.source_name, '香港政府新聞網')
        self.assertIsNone(payload.summary)

    def test_rss_midnight_is_day_only_not_a_publication_instant(self):
        payload = self.parse(rss(item())).records[0][1]
        self.assertEqual(payload.published_day, date(2026, 10, 20))
        self.assertNotIn('published_at', payload.model_dump())

    def test_utc_encoded_midnight_is_converted_to_hong_kong_day(self):
        for value in ['Mon, 19 Oct 2026 16:00:00 GMT', '2026-10-19T16:00:00Z',
                      '2026-10-19T16:00Z', '19 Oct 2026 16:00 GMT']:
            with self.subTest(value=value):
                payload = self.parse(rss(item(published=value))).records[0][1]
                self.assertEqual(payload.published_day, date(2026, 10, 20))

    def test_non_midnight_clock_cannot_promote_the_source_to_second_precision(self):
        for value in ['Tue, 20 Oct 2026 10:12:34 +0800', '2026-10-20T10:12:34.123456+08:00',
                      'Tue, 20 Oct 2026 23:59:59 +0800']:
            with self.subTest(value=value):
                self.assertEqual(self.parse(rss(item(published=value))).records[0][1].published_day,
                                 date(2026, 10, 20))

    def test_date_only_input_is_not_turned_into_midnight(self):
        self.assertEqual(self.parse(rss(item(published='2026-10-19'))).records[0][1].published_day,
                         date(2026, 10, 19))

    def test_received_time_never_replaces_missing_pubdate(self):
        for value in [None, '', ' ']:
            with self.subTest(value=value):
                batch = self.parse(rss(item(published=value, link=LINK + '?day=2099-01-01'),
                                       channel_extra='<pubDate>Tue, 20 Oct 2026 00:00:00 +0800</pubDate>'))
                self.assertIsNone(batch.records[0][1].published_day)

    def test_publisher_day_across_utc_midnight_and_receipt_offsets(self):
        for received in [datetime(2026, 10, 19, 16, 1, tzinfo=UTC),
                         datetime(2026, 10, 20, 0, 1, tzinfo=timezone(timedelta(hours=8)))]:
            with self.subTest(received=received):
                self.assertEqual(self.parse(rss(item()), received).records[0][1].published_day,
                                 date(2026, 10, 20))

    def test_future_hong_kong_dates_are_rejected_without_losing_good_rows(self):
        for value in ['2026-10-21', 'Wed, 21 Oct 2026 00:00:00 +0800', '2026-10-20T16:00:00Z']:
            with self.subTest(value=value):
                batch = self.parse(rss(item(published=value), item(link=LINK + '?good=1')))
                self.assertEqual((batch.expected, batch.rejected, len(batch.records)), (2, 1, 1))

    def test_invalid_or_unknown_timezones_are_not_assumed_to_be_local(self):
        for value in ['2026-02-30', 'unknown', '2026-10-20T00:00:00',
                      'Tue, 20 Oct 2026 00:00:00', 'Tue, 20 Oct 2026 00:00:00 HKT',
                      'Tue, 20 Oct 2026 00:00:00 CST', 'Tue, 20 Oct 2026 00:00:00 -0000',
                      '2026-10-20T00:00:00-00:00', '2026-10-20T00:00:00+08:60',
                      'Tue, 20 Oct 2026 00:00:00 +0860', '2026-10-20T00:00:00+24:00']:
            with self.subTest(value=value):
                batch = self.parse(rss(item(published=value)))
                self.assertEqual((batch.expected, batch.rejected, batch.records), (1, 1, []))

    def test_receipt_requires_aware_datetime(self):
        for received in [RECEIVED.replace(tzinfo=None), None, '2026-10-20']:
            with self.subTest(received=received):
                self.failure(rss(), 'invalid_received_time', received=received)

    def test_missing_or_empty_title_and_link_are_rejected(self):
        for title, link in [(None, LINK), ('', LINK), ('<b> </b>', LINK),
                            ('\u202e\u200b', LINK), ('合成', None), ('合成', '')]:
            with self.subTest(title=title, link=link):
                self.assertEqual(self.parse(rss(item(title=title, link=link))).rejected, 1)

    def test_duplicate_and_nested_identity_or_date_fields_are_rejected(self):
        for extra in ['<title>合成另一題</title>', f'<link>{LINK}</link>',
                      '<pubDate>2026-10-19</pubDate>']:
            with self.subTest(extra=extra):
                self.assertEqual(self.parse(rss(item(extra=extra))).rejected, 1)
        for field in ['link', 'pubDate']:
            with self.subTest(field=field):
                body = item().replace(f'<{field}>', f'<{field}><b>').replace(f'</{field}>', f'</b></{field}>')
                self.assertEqual(self.parse(rss(body)).rejected, 1)

    def test_same_link_deduplicates_even_when_object_or_guid_changes(self):
        batch = self.parse(rss(item(extra='<guid>synthetic-one</guid>'),
                               item(extra='<guid>synthetic-two</guid><objectId>changed</objectId>')))
        self.assertEqual((batch.expected, batch.rejected, len(batch.records)), (2, 1, 1))

    def test_guid_does_not_merge_different_original_urls(self):
        batch = self.parse(rss(item(extra='<guid>same-synthetic</guid>'),
                               item(link=LINK + '?other=1', extra='<guid>same-synthetic</guid>')))
        self.assertEqual((batch.expected, batch.rejected, len(batch.records)), (2, 0, 2))
        self.assertNotEqual(batch.records[0][0], batch.records[1][0])

    def test_invalid_first_row_does_not_hide_later_valid_same_url(self):
        batch = self.parse(rss(item(title=''), item()))
        self.assertEqual((batch.rejected, len(batch.records)), (1, 1))

    def test_cross_batch_corrections_return_new_payload_under_same_key(self):
        first = self.parse(rss(item())).records[0]
        changed = self.parse(rss(item(title='合成更正：財經服務', published='2026-10-19'))).records[0]
        self.assertEqual(first[0], changed[0])
        self.assertNotEqual(first[1].model_dump(), changed[1].model_dump())
        self.assertEqual(first[1].title, '合成：財經及就業服務')

    def test_original_https_url_query_and_normal_port_are_preserved(self):
        for link in [LINK, LINK + '?a=1&b=2', LINK.replace('www.news.gov.hk', 'www.news.gov.hk:443')]:
            with self.subTest(link=link):
                self.assertEqual(self.parse(rss(item(link=link))).records[0][1].source_url, link)

    def test_nonofficial_or_unsafe_urls_are_rejected(self):
        links = ['http://www.news.gov.hk/a', '//www.news.gov.hk/a', 'javascript:alert(1)',
                 'data:text/plain,synthetic', 'file:///synthetic', 'https://example.com/a',
                 'https://www.news.gov.hk.example.com/a', 'https://news.gov.hk/a',
                 'https://www.news.gov.hk@evil.example/a', 'https://user:password@www.news.gov.hk/a',
                 'https://@www.news.gov.hk/a', 'https://:password@www.news.gov.hk/a',
                 'https://www.news.gov.hk:80/a', 'https://www.news.gov.hk:444/a',
                 'https://www.news.gov.hk:65536/a', 'https://www.news.gov.hk:bad/a',
                 'https://www.news.gov.hk:/a', 'https://www.news.gov.hk:0443/a',
                 'https://www.news.gov.hk./a', 'https://www.news.gov.hk/a#fragment',
                 'https://www.news.gov.hk/a b', 'https://www.news.gov.hk/\n/a',
                 'https://www.news.gov.hk/\u202e/a', 'https://www.news.gov.hk\\evil/a',
                 'https://[invalid/a', LINK + '?' + 'x' * 2000]
        for link in links:
            with self.subTest(link=link):
                batch = self.parse(rss(item(link=link), item()))
                self.assertEqual((batch.expected, batch.rejected, len(batch.records)), (2, 1, 1))

    def test_legitimate_empty_and_all_rejected_have_different_coverage(self):
        empty = self.parse(rss())
        rejected = self.parse(rss(item(title=None)))
        self.assertEqual((empty.expected, empty.rejected, empty.records), (0, 0, []))
        self.assertEqual((rejected.expected, rejected.rejected, rejected.records), (1, 1, []))

    def test_bad_xml_or_unsupported_structure_fails_the_batch(self):
        for body in [b'', b'<rss', b'<html><body>synthetic refusal</body></html>',
                     b'<feed/>', b'<rss version="2.0"/>', b'<rss><channel/></rss>',
                     b'<rss version="2.0"><channel/><channel/></rss>',
                     b'<rss version="2.0"><channel><channel/></channel></rss>',
                     b'<rss version="2.0"><channel><items><item/></items></channel></rss>',
                     b'<rss version="2.0"><channel/><item/></rss>',
                     b'<rss version="2.0" xmlns="urn:synthetic"><channel/></rss>',
                     b'<rss version="2.0"><channel><x:item xmlns:x="urn:synthetic"/></channel></rss>']:
            with self.subTest(body=body):
                self.failure(body)

    def test_dtd_entities_and_multibyte_marker_bypasses_are_rejected(self):
        for declaration in ['<!DOCTYPE rss>', '<!DOCTYPE rss SYSTEM "https://example.com/synthetic">',
                            '<!DOCTYPE rss [<!ENTITY synthetic "synthetic text">]>']:
            for encoding in ['utf-8', 'utf-16', 'utf-16-le', 'utf-32']:
                with self.subTest(declaration=declaration, encoding=encoding):
                    self.failure((declaration + '<rss version="2.0"><channel/></rss>').encode(encoding))
        self.failure(b'<!ENTITY synthetic "text"><rss version="2.0"><channel/></rss>')

    def test_unsupported_encoding_and_body_type_are_controlled_errors(self):
        self.failure(b'<?xml version="1.0" encoding="synthetic-unknown"?><rss version="2.0"><channel/></rss>')
        self.failure(rss().decode())
        self.failure(None)

    def test_two_megabyte_boundary(self):
        base = rss(channel_extra='<description></description>')
        exact = base.replace(b'<description>', b'<description>' + b'x' * (MAX_BODY - len(base)))
        self.assertEqual(len(exact), MAX_BODY)
        self.assertEqual(self.parse(exact).expected, 0)
        self.failure(exact + b' ')

    def test_item_count_boundary(self):
        rows = [item(link=LINK + f'?synthetic={index}') for index in range(MAX_ITEMS)]
        self.assertEqual(len(self.parse(rss(*rows)).records), MAX_ITEMS)
        self.failure(rss(*rows, item(link=LINK + '?over=1')))

    def test_other_families_never_fall_back_to_this_source(self):
        for family in ['xinhua_fortune', 'hkma_public', 'unknown']:
            with self.subTest(family=family):
                self.failure(rss(item()), 'capability_not_implemented', family=family)


if __name__ == '__main__':
    unittest.main()
