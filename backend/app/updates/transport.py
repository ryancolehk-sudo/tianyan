"""Bounded public HTTPS GET with pinned DNS, no redirects or credentials."""
import http.client
import time
from urllib.parse import urlsplit

from app.ai.protocols import ModelFailure, PinnedHTTPSConnection, resolved_addresses
from app.updates.manifest import UpdateFailure


def destination(url, feed, allowed_hosts):
    try:
        parts = urlsplit(url)
        allowed = {urlsplit(feed).hostname, *allowed_hosts}
        if (parts.scheme != 'https' or parts.hostname not in allowed or parts.port not in {None, 443}
            or parts.username or parts.password or parts.fragment or parts.query
            or '\\' in url or any(ord(c) < 33 for c in url)):
            raise ValueError()
        return parts
    except (ValueError, TypeError):
        raise UpdateFailure('update_destination_rejected', '更新地址不在已配置的官方 HTTPS 范围内。') from None


def fetch(url, *, feed, allowed_hosts, limit, output=None, timeout=30):
    parts = destination(url, feed, allowed_hosts)
    connection = None
    try:
        deadline = time.monotonic() + timeout
        addresses = resolved_addresses(parts.hostname, 443, min(timeout, 5))
        connection = PinnedHTTPSConnection(parts.hostname, addresses[0], 443, min(timeout, 15))
        connection.deadline = deadline
        connection.request('GET', parts.path or '/', headers={'Accept': 'application/octet-stream, application/json', 'User-Agent': 'Tianyan-Updater/1'})
        reply = connection.getresponse()
        if reply.status != 200:
            raise UpdateFailure('update_download_failed', '更新源暂时不可用，请稍后重试。')
        length = reply.getheader('Content-Length')
        if length and (not length.isascii() or not length.isdigit() or int(length) > limit):
            raise UpdateFailure('update_size_invalid', '更新内容长度超过允许范围。')
        total, chunks = 0, []
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            if connection.sock:
                connection.sock.settimeout(min(15, max(.01, deadline - time.monotonic())))
            chunk = reply.read(min(1024 * 1024, limit - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise UpdateFailure('update_size_invalid', '更新内容长度超过允许范围。')
            if output:
                output.write(chunk)
            else:
                chunks.append(chunk)
        if length and total != int(length):
            raise UpdateFailure('update_download_incomplete', '更新下载未完成，请重新下载。')
        return total if output else b''.join(chunks)
    except (OSError, TimeoutError, ModelFailure, http.client.HTTPException):
        raise UpdateFailure('update_connection_failed', '无法连接官方更新源，请稍后重试。') from None
    finally:
        if connection:
            connection.close()
