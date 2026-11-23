"""Bounded keep-alive for the existing pinned Fuyao origin; no redirects/replay."""
from datetime import UTC, datetime
import http.client
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from app.market.connected_specs import FUYAO_ENDPOINT
from app.market.protocol import Response, SourceFailure
from app.market.http_retry import retry_after as parse_retry_after

_local=threading.local()
IDLE_SECONDS=20
LIFETIME_SECONDS=180


def retry_after(value,clock):
    return parse_retry_after(value,clock)


def diagnostic(status,location=None):
    value={'version':1,'http_status':int(status)}
    if location is not None:
        try:
            target=urllib.parse.urlsplit(urllib.parse.urljoin(FUYAO_ENDPOINT,location))
            value['redirect_origin']='same_origin' if (target.scheme=='https' and target.hostname=='fuyao.aicubes.cn'
                and target.port in {None,443} and not target.username and not target.password) else 'other_origin'
        except ValueError:
            value['redirect_origin']='invalid_origin'
    return value


def safe_diagnostic(error):
    raw=getattr(error,'transport_diagnostic',None)
    if not isinstance(raw,dict) or raw.get('version')!=1:
        return None
    result={'version':1}
    if type(raw.get('http_status')) is int and 100<=raw['http_status']<=599:
        result['http_status']=raw['http_status']
    if isinstance(raw.get('redirect_origin'),str) and raw['redirect_origin'] in {'same_origin','other_origin','invalid_origin'}:
        result['redirect_origin']=raw['redirect_origin']
    code=raw.get('upstream_code')
    if isinstance(code,str) and (code=='unknown' or code.isascii() and code.isdigit() and 1<=len(code)<=9):
        result['upstream_code']=code
    return result if len(result)>1 else None


def status_failure(status,headers):
    headers=headers or {}
    try:
        delay=retry_after(headers.get('Retry-After',''),datetime.now(UTC)) if status in {429,503} else None
    except SourceFailure as error:
        error.http_status=status
        error.transport_diagnostic=diagnostic(status)
        return error
    error=SourceFailure('permission_missing' if status in {401,403} else 'rate_limited' if status==429
        else 'upstream_redirect' if 300<=status<400 else 'upstream_unavailable',
        retry_after=delay,http_status=status)
    error.transport_diagnostic=diagnostic(status,headers.get('Location') if 300<=status<400 else None)
    return error


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        try:fp.close()
        except OSError:pass
        raise status_failure(code,headers)


def close():
    state=getattr(_local,'connection',None)
    if state is not None:
        _local.connection=None
        try:state[0].close()
        except (OSError,http.client.HTTPException):pass


def request(path,params,headers,*,max_body,timeout=15):
    if not path.startswith('/api/a-share') or any(character in path for character in ('?','#','\r','\n')):
        raise SourceFailure('endpoint_not_allowed')
    target=path+'?'+urllib.parse.urlencode(params)
    started=time.monotonic()
    try:
        # Preserve an explicitly configured system proxy's existing TLS path.
        # Direct connections reuse one bounded, thread-owned TLS connection.
        proxy=urllib.request.getproxies().get('https')
        if proxy and not urllib.request.proxy_bypass('fuyao.aicubes.cn'):
            req=urllib.request.Request(FUYAO_ENDPOINT+target,headers=headers)
            with urllib.request.build_opener(NoRedirect()).open(req,timeout=timeout) as response:
                body=response.read(max_body+1)
                encoding=response.headers.get('Content-Encoding','').lower()
                status=response.status
        else:
            state=getattr(_local,'connection',None)
            if state and (started-state[1]>LIFETIME_SECONDS or started-state[2]>IDLE_SECONDS):
                close();state=None
            if state is None:
                state=(http.client.HTTPSConnection('fuyao.aicubes.cn',timeout=timeout),started,started)
                _local.connection=state
            connection=state[0]
            connection.request('GET',target,headers=headers)
            response=connection.getresponse()
            body=response.read(max_body+1)
            encoding=response.headers.get('Content-Encoding','').lower()
            status=response.status
            if not 200<=status<300:
                raise status_failure(status,response.headers)
            if response.will_close:
                close()
            else:
                _local.connection=(connection,state[1],time.monotonic())
        if len(body)>max_body:
            raise SourceFailure('response_too_large')
        if encoding not in {'','identity'}:
            raise SourceFailure('unsafe_upstream_response')
        return Response(body,datetime.now(UTC),round((time.monotonic()-started)*1000),status)
    except urllib.error.HTTPError as exc:
        close()
        try:error=status_failure(exc.code,exc.headers)
        finally:exc.close()
        raise error from None
    except (TimeoutError,socket.timeout):
        close()
        raise SourceFailure('timeout') from None
    except (http.client.HTTPException,urllib.error.URLError,OSError):
        close()
        raise SourceFailure('transport_error') from None
    except SourceFailure:
        close()
        raise
