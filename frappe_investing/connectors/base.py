"""Shared plumbing for broker connectors (and, via import, market-data providers).

Output contracts — normalized results are plain dicts, never core Event objects:

* account:  ``{external_id, name, currency}``
* position: ``{security_key, qty, avg_cost, currency, market_price, as_of}``
* event:    ``{type, security_key, qty, price, amount, gross, fees, taxes,
  currency, date, source_ref, meta}`` where ``type`` is a core
  ``EVENT_TYPES`` member, ``date`` is YYYY-MM-DD and every numeric field is
  ``None`` or a string parseable by ``Decimal`` (floats never cross the
  boundary). Events may carry extra keys (``split_ratio``,
  ``basis_allocation``, ``child_security``, ``child_ratio``, ``notes``)
  when the event type needs them.

HTTP safety rules enforced here for every outbound call:

* TLS verification on, 30s timeout, ``trust_env=False`` sessions (no proxy
  env vars, no ~/.netrc attaching credentials implicitly).
* Responses capped at 20 MB so a hostile or broken endpoint cannot exhaust
  memory.
* Errors are sanitized to a status code plus a short message. URLs are
  stripped of query strings and response bodies are never echoed, so API
  keys, tokens and secrets cannot leak through exceptions or logs.
* HTTP 429 maps to ``BrokerError(code="rate_limited")`` so callers back off
  uniformly.
"""

import json
from decimal import Decimal
from urllib.parse import urlsplit

import requests

DEFAULT_TIMEOUT = 30
MAX_RESPONSE_BYTES = 20 * 1024 * 1024  # 20 MB

CAPABILITY_NAMES = ("accounts", "positions", "trades", "income", "corporate_actions", "history", "quotes")

_STATUS_CODES = {400: "bad_request", 401: "auth", 403: "auth", 404: "not_found", 429: "rate_limited"}


class BrokerError(Exception):
    """Connector failure. ``code`` is machine-stable; the message is sanitized."""

    def __init__(self, message, code="broker_error"):
        super().__init__(message)
        self.code = code


def new_session():
    """A requests session that ignores proxy env vars and ~/.netrc."""
    session = requests.Session()
    session.trust_env = False
    return session


def sanitize(text, secrets=()):
    """Replace every occurrence of each secret with ``***`` (belt-and-braces scrubbing)."""
    cleaned = str(text)
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(str(secret), "***")
    return cleaned


def _safe_target(url):
    # scheme://host/path only: the query string may carry tokens, never include it
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}"


def _read_capped(response, max_bytes, target):
    length = getattr(response, "headers", {}).get("Content-Length", "")
    if length.isdigit() and int(length) > max_bytes:
        raise BrokerError(f"response from {target} exceeds {max_bytes} bytes", code="too_large")
    chunks, total = [], 0
    for chunk in response.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > max_bytes:
            raise BrokerError(f"response from {target} exceeds {max_bytes} bytes", code="too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def http_request(
    session,
    method,
    url,
    *,
    headers=None,
    params=None,
    data=None,
    json_body=None,
    timeout=DEFAULT_TIMEOUT,
    max_bytes=MAX_RESPONSE_BYTES,
):
    """Perform one HTTP request with hard safety limits. Returns the body bytes.

    Never raises requests exceptions and never leaks credentials: all
    failures surface as :class:`BrokerError` with a stable ``code`` and a
    message reduced to host, path and status.
    """
    target = _safe_target(url)
    try:
        response = session.request(
            method.upper(),
            url,
            headers=headers,
            params=params,
            data=data,
            json=json_body,
            timeout=timeout,
            verify=True,
            stream=True,
        )
    except requests.Timeout as exc:
        raise BrokerError(f"timeout calling {target}", code="timeout") from exc
    except requests.RequestException as exc:
        # requests exception messages can embed the full URL (with query string)
        raise BrokerError(
            f"network error calling {target} ({type(exc).__name__})", code="network_error"
        ) from exc
    try:
        body = _read_capped(response, max_bytes, target)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if response.status_code == 429:
        raise BrokerError(f"rate limited by {target} (HTTP 429)", code="rate_limited")
    if response.status_code >= 400:
        code = _STATUS_CODES.get(
            response.status_code, "server_error" if response.status_code >= 500 else "http_error"
        )
        raise BrokerError(f"HTTP {response.status_code} from {target}", code=code)
    return body


def http_json(session, method, url, **kwargs):
    """http_request + JSON parse. Floats decode as Decimal so binary error never enters the ledger."""
    body = http_request(session, method, url, **kwargs)
    try:
        return json.loads(body.decode("utf-8"), parse_float=Decimal)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BrokerError(f"invalid JSON from {_safe_target(url)}", code="bad_response") from exc


class ConnectorBase:
    """Base class for broker connectors.

    ``supported`` names what the broker can truly deliver; ``capabilities()``
    reports it over the full capability vocabulary (False means "the API
    cannot do this" or "this connector does not implement it" — honest, not
    aspirational). The HTTP session is injectable so tests never touch the
    network.
    """

    name = "base"
    supported = frozenset()

    def __init__(self, session=None):
        self.session = session or new_session()

    def capabilities(self):
        return {name: name in self.supported for name in CAPABILITY_NAMES}


def normalized_event(
    type_,
    security_key=None,
    qty=None,
    price=None,
    amount=None,
    gross=None,
    fees="0",
    taxes="0",
    currency="",
    date="",
    source_ref="",
    meta=None,
    **extra,
):
    """Build one normalized event dict (the connector output contract).

    Numeric fields stay strings (Decimal-parseable) or None; ``meta`` is
    always a dict. Extra keys (split_ratio, child_security, ...) are merged
    in for event types that need them.
    """
    event = {
        "type": type_,
        "security_key": security_key,
        "qty": qty,
        "price": price,
        "amount": amount,
        "gross": gross,
        "fees": fees,
        "taxes": taxes,
        "currency": currency,
        "date": date,
        "source_ref": source_ref,
        "meta": dict(meta or {}),
    }
    event.update(extra)
    return event
