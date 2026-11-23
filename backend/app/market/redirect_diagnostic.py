"""Describe a rejected existing funds request without storing its URL or query."""
from urllib.parse import urljoin, urlsplit

from app.market.protocol import SourceRedirect


PATHS = {'/api/qt/clist/get': 'ranking',
         '/api/qt/stock/fflow/kline/get': 'funds_minute'}
HOSTS = frozenset({'push2.eastmoney.com', 'push2delay.eastmoney.com'})


def redirect_diagnostic(request_url, error):
    """No follow-up request or permission change: only bounded enum/boolean data."""
    if not isinstance(error, SourceRedirect) or type(error.status) is not int or not 300 <= error.status <= 399:
        return None
    result = {'version': 1, 'http_status': error.status, 'target_valid': False}
    try:
        if not isinstance(request_url, str) or len(request_url) > 4096:
            return None
        source = urlsplit(request_url)
        if source.scheme != 'https' or source.hostname != 'push2.eastmoney.com' or source.path not in PATHS:
            return None
        result['request_path'] = PATHS[source.path]
        target_url = error.target
        if not isinstance(target_url, str) or not target_url or len(target_url) > 4096 or any(
                ord(char) < 33 or ord(char) == 127 for char in target_url):
            return result
        target = urlsplit(urljoin(request_url, target_url))
        port = target.port  # Validate malformed ports before exporting any target fields.
        result.update(target_valid=True,
            target_host=target.hostname if target.hostname in HOSTS else 'other_host',
            target_path=PATHS.get(target.path, 'other_path'),
            same_path=target.path == source.path,
            same_query=bool(source.query) and target.query == source.query,
            secure_target=target.scheme == 'https' and port in {None, 443}
                and target.username is None and target.password is None and '#' not in target_url)
    except (ValueError, TypeError, AttributeError):
        return result
    return result
