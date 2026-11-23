"""Bounded official iFinD MCP transport; no arbitrary hosts, retries or purchases."""
import http.client
import json
import ssl
import time

ENDPOINT = 'https://api-mcp.51ifind.com:8643/ds-mcp-servers'
DOMAINS = {
    'stock': ('A股数据', 'stock', ('get_stock_summary', 'search_stocks', 'get_stock_performance', 'get_stock_info', 'get_stock_shareholders', 'get_stock_financials', 'get_risk_indicators', 'get_stock_events', 'get_esg_data', 'stock_highfreq_quotes')),
    'fund': ('基金数据', 'fund', ('search_funds', 'get_fund_profile', 'get_fund_market_performance', 'get_fund_ownership', 'get_fund_portfolio', 'get_fund_financials', 'get_fund_company_info', 'fund_highfreq_quotes')),
    'edb': ('经济数据库', 'edb', ('get_edb_data', 'search_edb')),
    'news': ('新闻公告', 'news', ('search_notice', 'search_news', 'search_trending_news')),
    'bond': ('债券数据', 'bond', ('bond_basic_info', 'bond_market_data', 'bond_financial_data', 'bond_special_data', 'bond_highfreq_quotes')),
    'global_stock': ('港美股数据', 'global-stock', ('search_global_stocks', 'global_stock_profile', 'global_stock_quotes', 'global_stock_financial', 'global_stock_events')),
    'index': ('指数板块数据', 'index', ('index_data', 'sector_data', 'index_highfreq_quotes')),
    'future': ('期货数据', 'futures', ('future_profile', 'future_quotes')),
}
MAX_BYTES = 2_000_000


class IfindFailure(Exception):
    def __init__(self, code, sent=False):
        self.code, self.sent = code, sent
        super().__init__(code)


def decode(body, content_type, request_id):
    try:
        text = body.decode('utf-8-sig').replace('\r\n', '\n')
        if 'text/event-stream' in content_type:
            values = [json.loads('\n'.join(line[5:].lstrip() for line in block.split('\n') if line.startswith('data:')))
                      for block in text.split('\n\n') if any(line.startswith('data:') for line in block.split('\n'))]
        else:
            values = [json.loads(text)]
        value = next(item for item in values if isinstance(item, dict) and item.get('id') == request_id)
        if value.get('error'):
            raise IfindFailure('ifind_rpc_error')
        if not isinstance(value.get('result'), dict):
            raise ValueError()
        return value['result']
    except (ValueError, UnicodeError, StopIteration, TypeError):
        raise IfindFailure('ifind_invalid_response') from None


class Client:
    def __init__(self, token, domain, timeout=60):
        if domain not in DOMAINS or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise IfindFailure('ifind_configuration_invalid')
        self.token, self.domain = token, domain
        self.path = '/ds-mcp-servers/hexin-ifind-ds-' + DOMAINS[domain][1] + '-mcp'
        self.session = None
        self.deadline = time.monotonic() + min(timeout, 60)
        self.request_count = 0
        self.call_sent = False

    def post(self, method, params, identity=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise IfindFailure('ifind_timeout', self.call_sent)
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'Authorization': self.token, 'User-Agent': 'tianyan-ifind/1.0'}
        if self.session:
            headers['Mcp-Session-Id'] = self.session
        payload = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if identity is not None:
            payload['id'] = identity
        conn = http.client.HTTPSConnection('api-mcp.51ifind.com', 8643, timeout=min(remaining, 30), context=ssl.create_default_context())
        try:
            self.request_count += 1
            if method == 'tools/call':
                self.call_sent = True
            conn.request('POST', self.path, json.dumps(payload, ensure_ascii=False).encode(), headers)
            response = conn.getresponse()
            if response.status not in {200, 202, 204}:
                code = 'ifind_auth_denied' if response.status in {401, 403} else 'ifind_quota_or_rate_limit' if response.status in {402, 429} else 'ifind_http_error'
                raise IfindFailure(code, self.call_sent)
            session = response.getheader('Mcp-Session-Id')
            if session:
                if len(session) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in session):
                    raise IfindFailure('ifind_invalid_session', self.call_sent)
                self.session = session
            if identity is None:
                return {}
            chunks, size = [], 0
            while True:
                if time.monotonic() > self.deadline:
                    raise IfindFailure('ifind_timeout', self.call_sent)
                chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    raise IfindFailure('ifind_response_too_large', self.call_sent)
            return decode(b''.join(chunks), response.getheader('Content-Type', ''), identity)
        except (OSError, http.client.HTTPException):
            raise IfindFailure('ifind_outcome_unknown' if self.call_sent else 'ifind_connection_failed', self.call_sent) from None
        finally:
            conn.close()

    def tools(self):
        self.post('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'tianyan', 'version': '1.0'}}, 1)
        if not self.session:
            raise IfindFailure('ifind_session_missing')
        self.post('notifications/initialized', {})
        result = self.post('tools/list', {}, 2)
        tools = result.get('tools')
        if not isinstance(tools, list) or result.get('nextCursor'):
            raise IfindFailure('ifind_catalog_incomplete')
        allowed = [item for item in tools if isinstance(item, dict) and item.get('name') in DOMAINS[self.domain][2]]
        return json.loads(json.dumps(allowed, ensure_ascii=False).replace(self.token, '[credential redacted]'))

    def call(self, tool, arguments):
        if tool not in DOMAINS[self.domain][2]:
            raise IfindFailure('ifind_tool_not_allowed')
        listed = {item['name']: item for item in self.tools()}
        if tool not in listed:
            raise IfindFailure('ifind_tool_unavailable')
        schema = listed[tool].get('inputSchema', {})
        props = schema.get('properties', {})
        if not set(schema.get('required', [])).issubset(arguments) or not set(arguments).issubset(props):
            raise IfindFailure('ifind_arguments_invalid')
        result = self.post('tools/call', {'name': tool, 'arguments': arguments}, 3)
        if result.get('isError'):
            raise IfindFailure('ifind_tool_error', True)
        content = result.get('content')
        if not isinstance(content, list) or not content:
            raise IfindFailure('ifind_empty_result', True)
        texts = [item['text'] for item in content if isinstance(item, dict) and item.get('type') == 'text' and isinstance(item.get('text'), str)]
        if not texts:
            raise IfindFailure('ifind_empty_result', True)
        for text in texts:
            try:
                inner = json.loads(text)
            except ValueError:
                continue
            if isinstance(inner, dict) and 'code' in inner and inner['code'] != 1:
                raise IfindFailure('ifind_application_error', True)
        # Provider text is untrusted. Never render HTML or interpret it as commands.
        return '\n\n'.join(texts).replace(self.token, '[credential redacted]')
