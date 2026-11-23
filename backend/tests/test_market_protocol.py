"""Synthetic protocol edge cases. Default CI must never contact a real source."""
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.market.protocol import (
    InstrumentKey, SourceFailure, decimal_value, exchange_for_listed_share, fetch_https,
    parse_cninfo_catalog, parse_szse_calendar, parse_tencent_quotes, quote_url,
)

RECEIVED = datetime(2026, 10, 19, 8, tzinfo=UTC)
INDEX = InstrumentKey('SSE', '000001', 'index')
STOCK = InstrumentKey('SZSE', '000001', 'stock')


def sample(key, *, name='合成测试标的', price='12.30', previous='12', volume='123', clock='20261019150000', code='000001'):
    fields = [''] * 88
    for index, value in {1:name,2:code,3:price,4:previous,5:'12.10',6:volume,30:clock,33:'12.50',34:'12.00'}.items():
        fields[index] = value
    return f'v_{key}="'+ '~'.join(fields) +'";'


def test_same_code_keeps_exchange_asset_identity_and_units():
    body=(sample('sh000001')+'\n'+sample('sz000001')).encode('gb18030')
    batch=parse_tencent_quotes(body,[INDEX,STOCK],RECEIVED)
    assert batch.expected == 2 and not batch.rejected
    assert [quote.instrument.identity for quote in batch.records] == ['SSE:index:000001','SZSE:stock:000001']
    assert [quote.price_unit for quote in batch.records] == ['point','CNY']
    assert all(quote.price == Decimal('12.30') and quote.quote_time.utcoffset().total_seconds()==8*3600 for quote in batch.records)
    assert batch.records[0].volume_shares is None and batch.records[0].amount_cny is None
    assert batch.records[1].volume_shares == Decimal('12300') and batch.records[1].amount_cny is None
    assert batch.records[0].change_percent == Decimal('2.500')


def test_missing_time_and_previous_close_do_not_become_known_or_zero():
    batch=parse_tencent_quotes(sample('sz000001',clock='',previous='',volume='0').encode('gb18030'),[STOCK],RECEIVED)
    assert not batch.rejected
    quote=batch.records[0]
    assert quote.quote_time is None and quote.previous_close is None and quote.change_percent is None
    assert quote.received_at == RECEIVED and quote.volume_native == 0
    assert decimal_value('') is None and decimal_value('0') == 0


@pytest.mark.parametrize('change,reason', [
    ({'code':'600000'},'instrument_mismatch'),
    ({'clock':'20261020150000'},'future_source_time'),
    ({'clock':'20261319150000'},'invalid_source_time'),
    ({'price':'NaN'},'invalid_number'),
    ({'price':'999999'},'inconsistent_price_range'),
])
def test_bad_rows_are_quarantined_without_losing_other_requested_instruments(change,reason):
    body=(sample('sh000001')+'\n'+sample('sz000001',**change)).encode('gb18030')
    batch=parse_tencent_quotes(body,[INDEX,STOCK],RECEIVED)
    assert len(batch.records)==1 and batch.records[0].instrument==INDEX
    assert batch.rejected=={'sz000001':reason}


def test_missing_and_unsupported_symbols_are_distinct_from_successful_market_zero():
    batch=parse_tencent_quotes(b'v_sz000001="";',[INDEX,STOCK],RECEIVED)
    assert not batch.records and batch.rejected=={'sz000001':'instrument_unavailable','sh000001':'instrument_missing'}
    assert quote_url([INDEX,STOCK])=='https://qt.gtimg.cn/q=sh000001,sz000001'
    with pytest.raises(SourceFailure,match='invalid_instrument_batch'):
        parse_tencent_quotes(b'',[STOCK,STOCK],RECEIVED)
    for body in [b'<html>blocked</html>',b'alert("malicious");', (sample('sh000001')+'\n'+sample('sh000001')).encode('gb18030')]:
        with pytest.raises(SourceFailure):
            parse_tencent_quotes(body,[INDEX],RECEIVED)


def test_200_stock_native_batch_preserves_identity_and_rejects_overflow_or_duplicates():
    keys=[InstrumentKey('SZSE',f'{code:06}','stock') for code in range(100,300)]
    body='\n'.join(sample(key.vendor_key,code=key.symbol) for key in keys).encode('gb18030')
    batch=parse_tencent_quotes(body,keys,RECEIVED)
    assert batch.expected==200 and len(batch.records)==200 and not batch.rejected
    assert [row.instrument for row in batch.records]==keys
    assert quote_url(keys)=='https://qt.gtimg.cn/q='+','.join(key.vendor_key for key in keys)
    for invalid in [[],[*keys,STOCK],[*keys[:-1],keys[0]]]:
        with pytest.raises(SourceFailure,match='invalid_instrument_batch'):
            quote_url(invalid)
        with pytest.raises(SourceFailure,match='invalid_instrument_batch'):
            parse_tencent_quotes(body,invalid,RECEIVED)
    duplicate=body+b'\n'+sample(keys[0].vendor_key,code=keys[0].symbol).encode('gb18030')
    with pytest.raises(SourceFailure,match='unexpected_instrument'):
        parse_tencent_quotes(duplicate,keys,RECEIVED)


def test_calendar_uses_explicit_source_flags_and_requires_full_month():
    rows=[{'jyrq':f'2026-02-{day:02d}','jybz':'1' if day==1 else '0'} for day in range(1,29)]
    # Deliberately Sunday-open synthetic input: never derive the flag from weekday.
    result=parse_szse_calendar(json.dumps({'data':rows}).encode(),2026,2)
    assert len(result)==28 and result[0][1] is True
    with pytest.raises(SourceFailure,match='incomplete_calendar'):
        parse_szse_calendar(json.dumps({'data':rows[:-1]}).encode(),2026,2)
    with pytest.raises(SourceFailure,match='schema_changed'):
        parse_szse_calendar(json.dumps({'data':rows+[rows[0]]}).encode(),2026,2)


def test_endpoint_policy_rejects_arbitrary_hosts_and_credentials_before_any_request(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail('A rejected endpoint must not perform a network call')
    monkeypatch.setattr('urllib.request.build_opener',fail)
    for url in ['http://qt.gtimg.cn/q=sh000001','https://127.0.0.1/','https://qt.gtimg.cn.evil.test/','https://u:p@qt.gtimg.cn/','https://qt.gtimg.cn:8443/','https://qt.gtimg.cn/#fragment']:
        with pytest.raises(SourceFailure,match='endpoint_not_allowed'):
            fetch_https(url)


def catalog_payload(names, *, code='600123', org_id='synthetic-same-company'):
    return json.dumps({'stockList': [{'category': 'A股', 'code': code, 'orgId': org_id, 'zwjc': name} for name in names]}).encode()


def test_cninfo_identical_duplicates_collapse_to_one_claim_in_any_order():
    forward = parse_cninfo_catalog(catalog_payload(['Synthetic name A', 'Synthetic name A']))
    reverse = parse_cninfo_catalog(catalog_payload(['Synthetic name A', 'Synthetic name A'][::-1]))
    assert forward.records == reverse.records
    assert len(forward.records) == 1 and forward.records[0].name == 'Synthetic name A'
    assert sorted(forward.rejected.values()) == ['duplicate_instrument']


def test_cninfo_same_identity_with_different_names_is_quarantined_in_any_order():
    forward = parse_cninfo_catalog(catalog_payload(['Synthetic name A', 'Synthetic name B']))
    reverse = parse_cninfo_catalog(catalog_payload(['Synthetic name B', 'Synthetic name A']))
    # No record may be chosen by input order, and both claims stay auditable.
    assert forward.records == reverse.records == []
    assert sorted(forward.rejected.values()) == ['conflicting_instrument'] * 2
    assert sorted(reverse.rejected.values()) == ['conflicting_instrument'] * 2


def test_cninfo_identity_conflict_with_different_org_evidence_is_not_a_rename():
    rows = [{'category': 'A股', 'code': '600123', 'orgId': 'org-a', 'zwjc': 'Synthetic name A'},
            {'category': 'A股', 'code': '600123', 'orgId': 'org-b', 'zwjc': 'Synthetic name A'}]
    batch = parse_cninfo_catalog(json.dumps({'stockList': rows}).encode())
    assert batch.records == [] and set(batch.rejected.values()) == {'conflicting_instrument'}


def test_cninfo_single_source_backed_rename_keeps_identity():
    batch = parse_cninfo_catalog(catalog_payload(['Synthetic name B']))
    assert len(batch.records) == 1 and batch.records[0].instrument.identity == 'SSE:stock:600123'
    assert batch.records[0].name == 'Synthetic name B'


def test_cninfo_catalog_maps_exchanges_and_rejects_b_shares_offline():
    body = (Path(__file__).parent / 'fixtures' / 'cninfo_catalog_sample.json').read_bytes()
    batch = parse_cninfo_catalog(body)
    assert batch.expected == 7
    assert {(row.instrument.identity, row.name) for row in batch.records} == {
        ('SZSE:stock:002724', '海洋王'), ('SZSE:stock:000001', '平安银行'),
        ('SSE:stock:600000', '浦发银行'), ('SSE:stock:688001', '华兴源创'),
        ('BSE:stock:920001', '北交样本'), ('SSE:stock:689009', '九号公司'),
    }
    assert any(reason == 'unsupported_share_class' for reason in batch.rejected.values())
    assert exchange_for_listed_share('200002') is None
    with pytest.raises(SourceFailure, match='schema_changed'):
        parse_cninfo_catalog(b'<html>blocked</html>')
    with pytest.raises(SourceFailure, match='instrument_unavailable'):
        parse_cninfo_catalog(json.dumps({'stockList': [
            {'category': 'B股', 'code': '200002', 'orgId': 'x', 'pinyin': 'wkb', 'zwjc': '万科B'}
        ]}).encode())
