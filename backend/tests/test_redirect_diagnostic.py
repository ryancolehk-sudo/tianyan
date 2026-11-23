"""Only rejected synthetic URLs; never connect to a supplier."""
import json
import io
from email.message import Message
from urllib.response import addinfourl

import pytest

from app.market.protocol import SourceFailure, SourceRedirect, eastmoney_minute_allocation_url
from app.market.redirect_diagnostic import redirect_diagnostic


ORIGINAL = 'https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?secid=90.BK0001&opaque=must_not_be_exported'
ALLOCATED = ORIGINAL.replace('push2.eastmoney.com', 'push2delay.eastmoney.com')


@pytest.mark.parametrize('target,changes', [
    (ALLOCATED, {}),
    (ALLOCATED.replace('must_not_be_exported', 'different_private_value'), {'same_query': False}),
    (ALLOCATED.replace('/stock/fflow/kline/get', '/clist/get'), {'target_path': 'ranking', 'same_path': False}),
    (ALLOCATED.replace('/stock/fflow/kline/get', '/unknown/private/path'), {'target_path': 'other_path', 'same_path': False}),
    (ALLOCATED.replace('push2delay.eastmoney.com', 'untrusted.example'), {'target_host': 'other_host'}),
    (ALLOCATED.replace('https:', 'http:'), {'secure_target': False}),
    (ALLOCATED.replace('push2delay.eastmoney.com', 'private_user:private_password@push2delay.eastmoney.com'), {'secure_target': False}),
    (ALLOCATED + '#private_fragment', {'secure_target': False}),
    ('/api/qt/stock/fflow/kline/get?' + ORIGINAL.split('?', 1)[1], {'target_host': 'push2.eastmoney.com'}),
])
def test_only_bounded_metadata_survives_a_rejected_redirect(target, changes):
    error = SourceRedirect(302, target)
    expected = dict(version=1, http_status=302, request_path='funds_minute', target_valid=True,
        target_host='push2delay.eastmoney.com', target_path='funds_minute',
        same_path=True, same_query=True, secure_target=True)
    expected.update(changes)
    result = redirect_diagnostic(ORIGINAL, error)
    assert result == expected
    rendered = json.dumps(result) + str(error) + repr(error)
    for private in ['BK0001', 'must_not_be_exported', 'different_private_value',
                    'private_user', 'private_password', 'private_fragment', 'untrusted.example', '/unknown/private/path']:
        assert private not in rendered


@pytest.mark.parametrize('target', [None, '', ALLOCATED + '\n',
    ALLOCATED.replace('.com', '.com:invalid'), 'x' * 4097])
def test_invalid_or_oversized_targets_do_not_export_partial_urls(target):
    assert redirect_diagnostic(ORIGINAL, SourceRedirect(302, target)) == {
        'version': 1, 'http_status': 302, 'request_path': 'funds_minute', 'target_valid': False}


def test_non_redirect_failures_and_unrelated_requests_have_no_redirect_metadata():
    assert redirect_diagnostic(ORIGINAL, SourceFailure('timeout')) is None
    assert redirect_diagnostic('https://unrelated.example/private', SourceRedirect(302, ALLOCATED)) is None


def test_minute_allocation_requires_an_earlier_observed_rejection():
    current = SourceRedirect(302, ALLOCATED)
    with pytest.raises(SourceFailure, match='upstream_redirect'):
        eastmoney_minute_allocation_url(ORIGINAL, current, observed_rejection=None)
    previous = redirect_diagnostic(ORIGINAL, current)
    assert eastmoney_minute_allocation_url(ORIGINAL, current, observed_rejection=previous) == ALLOCATED
    invalid = dict(previous, same_query=1)
    with pytest.raises(SourceFailure, match='upstream_redirect'):
        eastmoney_minute_allocation_url(ORIGINAL, current, observed_rejection=invalid)


@pytest.mark.parametrize('status,target', [
    (301, ALLOCATED), (307, ALLOCATED),
    (302, ALLOCATED.replace('push2delay.eastmoney.com', 'untrusted.example')),
    (302, ALLOCATED.replace('https:', 'http:')),
    (302, ALLOCATED.replace('must_not_be_exported', 'different_private_value')),
    (302, ALLOCATED.replace('/stock/fflow/kline/get', '/clist/get')),
    (302, ALLOCATED + '#'),
    (302, ALLOCATED.replace('.com', '.com:444')),
    (302, ALLOCATED.replace('.com', '.com:invalid')),
    (302, ALLOCATED.replace('push2delay.eastmoney.com', 'private_user@push2delay.eastmoney.com')),
])
def test_prior_receipt_never_authorizes_a_different_current_redirect(status, target):
    previous = redirect_diagnostic(ORIGINAL, SourceRedirect(302, ALLOCATED))
    with pytest.raises(SourceFailure, match='upstream_redirect'):
        eastmoney_minute_allocation_url(ORIGINAL, SourceRedirect(status, target), observed_rejection=previous)


def install_stdlib_redirect_wire(monkeypatch, *, status=302, location=None):
    """Use urllib's actual error and redirect chains, replacing only HTTPS I/O."""
    from app.market import protocol
    build_opener = protocol.urllib.request.build_opener
    calls = []
    class MemoryHTTPS(protocol.urllib.request.HTTPSHandler):
        def https_open(self, request):
            calls.append(request.full_url)
            headers = Message()
            if location is not None:
                headers['Location'] = location
            reply = addinfourl(io.BytesIO(b''), headers, request.full_url, status)
            reply.msg = 'synthetic redirect'
            return reply
    monkeypatch.setattr(protocol.urllib.request, 'build_opener',
        lambda *handlers: build_opener(*handlers, MemoryHTTPS()))
    return calls


@pytest.mark.parametrize('status,location', [
    (302, None), (302, 'file:///must_not_be_exported'),
    (302, 'gopher://untrusted.example/must_not_be_exported'),
    (301, None), (303, None), (307, None), (308, None),
])
def test_real_stdlib_3xx_rejections_keep_bounded_receipts_before_any_follow(monkeypatch, status, location):
    from app.market import protocol
    calls = install_stdlib_redirect_wire(monkeypatch, status=status, location=location)
    with pytest.raises(SourceRedirect) as failure:
        protocol.fetch_https(ORIGINAL)
    assert calls == [ORIGINAL] and failure.value.status == status
    result = redirect_diagnostic(ORIGINAL, failure.value)
    assert result['http_status'] == status
    assert result['target_valid'] is False or result['secure_target'] is False
    assert 'must_not_be_exported' not in json.dumps(result) + repr(failure.value)
