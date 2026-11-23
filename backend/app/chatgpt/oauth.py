"""Fixed-destination public OAuth client and verified OIDC identity.

All remote diagnostics are deliberately bounded and machine-readable. Neither response
bodies nor exception messages containing authorization material leave this module.
"""
import http.client
import json
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlencode, urlsplit

import jwt

from app.auth import digest

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
SHARING_SCOPES = {"resource.invoke", "chatgpt.tokens.use.direct"}
TERMINAL_REFRESH_ERRORS = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused"}
_cache_lock = threading.RLock()
_discovery_cache = None
_jwks_cache = None


class OAuthFailure(Exception):
    def __init__(self, code, *, status=None, uncertain=False):
        self.code, self.status, self.uncertain = code, status, uncertain
        super().__init__(code)


def auth_url_allowed(url):
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.hostname == "auth.openai.com"
                and parts.port in {None, 443} and not parts.username and not parts.password
                and not parts.fragment and not parts.query and parts.path.startswith("/")
                and "\\" not in url and all(ord(c) >= 33 for c in url))
    except (ValueError, TypeError):
        return False


def request_json(url, *, form=None, empty_success=False):
    if not auth_url_allowed(url):
        raise OAuthFailure("chatgpt_destination_rejected")
    parts = urlsplit(url)
    connection = http.client.HTTPSConnection("auth.openai.com", timeout=15, context=ssl.create_default_context())
    sent = False
    response = None
    tls_socket = None
    deadline = time.monotonic() + 30
    expired = threading.Event()

    def abort():
        expired.set()
        # HTTPResponse can retain a socket file after Connection: close has set
        # connection.sock=None. Shutdown the original TLS socket as well.
        for active in (tls_socket, getattr(connection, "sock", None)):
            if active is not None:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    active.close()
                except OSError:
                    pass
        try:
            connection.close()
        except OSError:
            pass

    timer = threading.Timer(30, abort)
    timer.daemon = True
    timer.start()

    def check_deadline():
        if expired.is_set() or time.monotonic() >= deadline:
            raise TimeoutError()

    try:
        headers = {"Accept": "application/json", "User-Agent": "Tianyan/1.0"}
        body = None
        if form is not None:
            body = urlencode(form).encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        # No redirect handler and no proxy/environment credentials are consulted.
        connection.connect()
        tls_socket = getattr(connection, "sock", None)
        check_deadline()
        sent = True
        connection.request("POST" if form is not None else "GET", parts.path, body=body, headers=headers)
        check_deadline()
        response = connection.getresponse()
        check_deadline()
        raw = response.read(262145)
        check_deadline()
        if len(raw) > 262144:
            raise OAuthFailure("chatgpt_response_invalid", status=response.status, uncertain=form is not None)
        if empty_success and response.status == 200:
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise OAuthFailure("chatgpt_response_invalid", status=response.status, uncertain=form is not None and response.status < 400) from None
        if not isinstance(data, dict):
            raise OAuthFailure("chatgpt_response_invalid", status=response.status, uncertain=form is not None)
        if response.status != 200:
            upstream_code = data.get("error")
            if isinstance(upstream_code, dict):
                upstream_code = upstream_code.get("code")
            if not isinstance(upstream_code, str) or not re.fullmatch(r"[a-zA-Z0-9_]{1,80}", upstream_code):
                upstream_code = "remote_error"
            raise OAuthFailure(upstream_code, status=response.status,
                uncertain=form is not None and response.status >= 500)
        return data
    except OAuthFailure:
        raise
    except (OSError, http.client.HTTPException, ValueError):
        raise OAuthFailure("chatgpt_network_unknown" if sent and form is not None else "chatgpt_network_unavailable", uncertain=sent and form is not None) from None
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        connection.close()


def discovery(*, force=False):
    global _discovery_cache
    with _cache_lock:
        if not force and _discovery_cache and _discovery_cache[0] > time.monotonic():
            return _discovery_cache[1]
        value = request_json(DISCOVERY_URL)
        if (value.get("issuer") != ISSUER or not auth_url_allowed(value.get("jwks_uri", ""))
                or value.get("authorization_endpoint", AUTHORIZE_URL) != AUTHORIZE_URL
                or value.get("token_endpoint", TOKEN_URL) != TOKEN_URL):
            raise OAuthFailure("chatgpt_discovery_invalid")
        _discovery_cache = (time.monotonic() + 300, value)
        return value


def signing_keys(*, force=False):
    global _jwks_cache
    with _cache_lock:
        if not force and _jwks_cache and _jwks_cache[0] > time.monotonic():
            return _jwks_cache[1]
        value = request_json(discovery()["jwks_uri"])
        if not isinstance(value.get("keys"), list) or not 1 <= len(value["keys"]) <= 50:
            raise OAuthFailure("chatgpt_jwks_invalid")
        _jwks_cache = (time.monotonic() + 300, value["keys"])
        return value["keys"]


def verify_identity(token, client_id, nonce_hash=None):
    if not isinstance(token, str) or not 1 <= len(token) <= 32768:
        raise OAuthFailure("chatgpt_identity_invalid")
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str) or not 1 <= len(kid) <= 200:
            raise ValueError()
        keys = signing_keys()
        matches = [key for key in keys if isinstance(key, dict) and key.get("kid") == kid]
        if not matches:
            matches = [key for key in signing_keys(force=True) if isinstance(key, dict) and key.get("kid") == kid]
        if len(matches) != 1:
            raise ValueError()
        key = matches[0]
        if key.get("kty") != "RSA" or key.get("alg", "RS256") != "RS256" or key.get("use", "sig") != "sig":
            raise ValueError()
        claims = jwt.decode(token, jwt.PyJWK.from_dict(key, algorithm="RS256").key,
            algorithms=["RS256"], issuer=ISSUER, audience=client_id, leeway=5,
            options={"require": ["iss", "aud", "sub", "exp", "iat"]})
        if (not isinstance(claims.get("sub"), str) or not 1 <= len(claims["sub"]) <= 512
                or type(claims.get("iat")) is not int or type(claims.get("exp")) is not int
                or claims["exp"] <= claims["iat"]):
            raise ValueError()
        if claims.get("azp") is not None and claims["azp"] != client_id:
            raise ValueError()
        if isinstance(claims["aud"], list) and (len(claims["aud"]) > 1 and claims.get("azp") != client_id):
            raise ValueError()
        if nonce_hash is not None:
            nonce = claims.get("nonce")
            import secrets
            if not isinstance(nonce, str) or not secrets.compare_digest(digest(nonce), nonce_hash):
                raise ValueError()
        return claims
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise OAuthFailure("chatgpt_identity_invalid") from None


def token_bundle(response, *, previous=None):
    """Trust granted scopes only from the server token response, never callback query."""
    previous = previous or {}
    scope = response.get("scope")
    if scope is None and previous:
        scopes = previous.get("scopes", [])
    elif scope is None:
        scopes = []
    elif isinstance(scope, str) and len(scope) <= 4000:
        scopes = sorted(set(scope.split()))
    else:
        raise OAuthFailure("chatgpt_token_invalid")
    expires = response.get("expires_in")
    access = response.get("access_token")
    refresh = response.get("refresh_token", previous.get("refresh_token"))
    identity = response.get("id_token", previous.get("id_token"))
    if access is None and not SHARING_SCOPES.issubset(set(scopes)) and not previous:
        if not isinstance(identity, str) or not 1 <= len(identity) <= 32768:
            raise OAuthFailure("chatgpt_token_invalid")
        return {"access_token": None, "refresh_token": None, "id_token": identity,
            "token_type": None, "scopes": scopes, "expires_at": 0}
    if not isinstance(response.get("token_type"), str) or response["token_type"].lower() != "bearer":
        raise OAuthFailure("chatgpt_token_invalid")
    if (type(expires) is not int or not 1 <= expires <= 604800 or not isinstance(access, str)
            or not 1 <= len(access) <= 32768 or any(ord(char) < 33 or ord(char) > 126 for char in access)):
        raise OAuthFailure("chatgpt_token_invalid")
    for value in (refresh, identity):
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 32768 or any(ord(char) < 33 or ord(char) > 126 for char in value)):
            raise OAuthFailure("chatgpt_token_invalid")
    return {"access_token": access, "refresh_token": refresh, "id_token": identity,
        "token_type": "Bearer", "scopes": scopes, "expires_at": time.time() + expires}
