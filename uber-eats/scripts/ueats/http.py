# copied from google-hotels/scripts/ghotels/http.py @ 73c62d2 — divergences:
#   * method: POST JSON to https://www.ubereats.com/_p/api/<Endpoint>?localeCode=<locale>
#     (was GET with query params); the body is `json.dumps(body)` on all three paths
#   * headers: UA, `x-csrf-token: x`, `content-type: application/json`, `accept:
#     application/json`, and `Cookie: uev2.loc=<url-encoded compact cookie JSON>`
#     only when a Location is given (was Accept: text/html + CONSENT cookie)
#   * ALLOWED_ENDPOINTS checked at the top of call(), before _spend and before any
#     socket: anything else raises NotAllowed (read-only guarantee, design 01 §8)
#   * response classification is one function (_classify) shared by all three
#     paths: Cloudflare challenge → Blocked (403 + "Just a moment"/"_cf_chl_opt",
#     the header cf-mitigated: challenge, or — defensively — a 200 carrying the
#     challenge body); Uber's own botdefense — any status whose JSON body has
#     metadata.botdefense.state == "challenge" (403 seen live after ~110
#     requests/day, provider RECAPTCHA) → Blocked with a distinct message and
#     Blocked.provider = "recaptcha" ("cloudflare" for the edge challenge);
#     3xx raised with its Location, never followed; 429 and 403
#     never retried; 5xx → retry; 200 must parse as {"status", "data"}: non-JSON
#     or an unexpected shape → PayloadError, status failure with message
#     invalid_store_uuid OR data.code "404" → LookupFailure (a well-formed
#     unknown UUID answers 200 failure "We had an issue finding this store",
#     code "404", seen live 2026-09-13), any other failure → PayloadError;
#     call() returns the `data` object (was: the raw HTML page)
#   * looks_blocked(status, body, headers) replaces looks_blocked(html): the
#     hotels page had a positive results marker; here the positive signal is
#     "the body parses as JSON", so blocking is detected by Cloudflare's own
#     markers, bounded to the first 16 KB and skipped when the body is JSON
#   * curl runs with -i so the response headers (cf-mitigated, Location) are
#     seen on that path too; -H "Expect:" so a large body never produces a
#     100-continue interim block; -X POST -d @- with the body on stdin so it
#     never appears in `ps`; _split_curl_response peels 1xx blocks and the
#     proxy's "200 Connection established" CONNECT block before the response
#   * the requests session gets a reject-all cookie policy (_RejectAll): the
#     only cookie ever sent is uev2.loc, built per call from the Location, and
#     Uber's Set-Cookie is dropped so one call's cookies never leak into the
#     next (hotels sent a fixed CONSENT cookie and kept requests' default jar)
#   * THROTTLE_SECONDS 1.0 (was 2.5); TIMEOUT 30 (was 60); DEFAULT_MAX_REQUESTS
#     25 (was 5); HARD_CAP_REQUESTS 25 (was MAX_MAX_REQUESTS 40)
#   * retries: MAX_ATTEMPTS = 3 counts only retryable failures (5xx, timeouts,
#     transient connection errors); the two TLS-recovery steps (OS bundle, then
#     curl) are bounded structurally — each can happen once — and still charged
#     to the budget, so the loop issues at most MAX_ATTEMPTS + 2 requests
#   * error classes per the frozen stub: UEHTTPError (Blocked is a subclass),
#     PayloadError, LookupFailure, NotAllowed, RequestBudgetError; wording
#     hotels→Uber Eats, "no rates"→"nothing found"
#   * Transport.__init__(max_requests, throttle, locale, timeout) per the stub;
#     `locale` is new and goes into the query string
# Lane A owns this file. Everything else is the hotels transport verbatim; keep
# the header current when diverging further.
"""HTTP transport for Uber Eats' web JSON API (`/_p/api/<Endpoint>`).

Five things make this transport different from a plain ``requests.post``:

1. **It can only read.** ``ALLOWED_ENDPOINTS`` is the whole surface. The web
   app has endpoints that add to a cart, place an order or set a delivery
   address; ``call`` refuses any name outside the five read endpoints before
   a socket is opened, so no code path in this package can be talked into
   sending one (design 01 §8).
2. **The budget has to be honest.** Retries are driven here, in ``call``, and
   every attempt is metered — including the two TLS-recovery paths (retrying
   with the OS CA bundle, and handing off to curl), which re-issue the same
   POST. urllib3's own ``Retry`` is switched off precisely because it replays
   *inside* one call. On claude.ai the traffic leaves from a shared datacenter
   IP, so an over-count is everyone else's problem.
3. **Blocking looks like a page, and failure looks like success.** Cloudflare
   answers a challenged request with 403 and an 8 KB HTML page saying "Just a
   moment…" (evidence/city.html). Reported naively that is "no restaurants" —
   the false negative this repo forbids — so it is ``Blocked`` (exit 3) and
   never retried. Conversely a bad store id is HTTP **200** with
   ``{"status": "failure", "data": {"message": "invalid_store_uuid"}}``
   (evidence/api.json): status is read from the body, never the HTTP code.
4. **TLS in a sandbox.** Same hazard as campsite-search: a pinned `certifi`
   older than the server chain. Fix order is truststore -> system CA bundle ->
   curl. Verification is never disabled.
5. **Pace.** 16 store fetches at ~1 s drew no challenge (01 §1); the throttle
   is 1.0 s on every path, including curl and urllib.

`requests` is optional here. If it is missing entirely — a real possibility in
a locked-down sandbox where pip is also blocked — the transport falls back to
`curl` and then to `urllib`, so the skill still answers. All three paths go
through the same budget, throttle, headers and response classification.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .model import Location

try:  # optional: the CLI works without it, just with nicer error text
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:  # pragma: no cover - exercised only in bare sandboxes
    requests = None  # type: ignore[assignment]

HOST = "www.ubereats.com"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

TIMEOUT = 30.0

#: Attempts at one call that failed in a retryable way (5xx, timeout, transient
#: connection error), counting the first. Each one is a real request and is
#: metered against the budget. The TLS-recovery steps are not counted here —
#: they are bounded by construction (each happens once) and metered all the same.
MAX_ATTEMPTS = 3

#: First retry waits this long, then it doubles. Only 5xx and genuinely
#: transient connection failures ever get here — never a 403 or a 429.
BACKOFF_SECONDS = 1.0

#: Enough for every single-question command and a default `compare` (11).
#: A sweep must raise it explicitly, up to the hard cap.
DEFAULT_MAX_REQUESTS = 25

#: The most --max-requests may be raised to (design 02 §2).
HARD_CAP_REQUESTS = 25

#: Pause between consecutive requests, on every transport path. The probe's
#: pace: 16 store fetches at ~1 s were all served (01 §1).
THROTTLE_SECONDS = 1.0

#: The five read endpoints. Anything else is refused before a socket opens.
ALLOWED_ENDPOINTS = frozenset({
    "mapsSearchV1", "getDeliveryLocationV1", "getFeedV1", "getStoreV1", "getMenuItemV1",
})

#: Cloudflare's challenge page: title text and the JS options blob. The page
#: is ~8 KB and the title is in its first 200 bytes, so the search is bounded.
CHALLENGE_MARKERS = ("Just a moment", "_cf_chl_opt")
CHALLENGE_SCAN_BYTES = 16 * 1024

BLOCKED_MESSAGE = (
    "Uber Eats' bot protection (Cloudflare) blocked this request — this is "
    "not 'nothing found'. Try again later; `doctor` shows which step is blocked."
)

BOTDEFENSE_MESSAGE = (
    "Uber Eats' own bot defense (reCAPTCHA challenge) is refusing this client "
    "— this happens after many requests from one address in a day; it is NOT "
    "'nothing found'. Wait (hours, not seconds) and try again; `doctor` shows "
    "which step is blocked."
)

TLS_HINT = (
    "Fix: pip install truststore (uses the OS trust store), or point "
    "REQUESTS_CA_BUNDLE / SSL_CERT_FILE at a current CA bundle"
)

#: Where distributions keep the system CA bundle. Tried when certifi's bundle
#: rejects the host, before falling back to curl.
OS_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Alpine
    "/etc/pki/tls/certs/ca-bundle.crt",    # RHEL, Fedora, Amazon Linux
    "/etc/ssl/ca-bundle.pem",              # SUSE
    "/etc/ssl/cert.pem",                   # Alpine, BSD, Homebrew OpenSSL
)

#: Substrings that mean "this name will not resolve", in any of the three
#: stacks. Retrying these only burns budget.
DNS_MARKERS = (
    "NameResolutionError", "Name or service not known",
    "nodename nor servname", "getaddrinfo failed",
    "Failed to resolve", "Temporary failure in name resolution",
)

#: What curl's -w %{http_code} prints when there was never an HTTP response.
NO_STATUS = ("", "000")

#: curl exit codes worth another attempt: timeout, empty reply, recv/send
#: error, truncated transfer.
CURL_TRANSIENT = frozenset({18, 28, 52, 55, 56})

#: A proxy that refuses the destination reports curl exit 56, the same code as
#: a genuinely truncated transfer — so it would be retried before failing, and
#: the message named neither the host nor the proxy. An egress allowlist that
#: excludes Uber is the single likeliest way this skill fails on claude.ai, so
#: it is worth telling apart from a flaky connection.
CURL_TUNNEL_DENIED = "connect tunnel failed"


#: curl exit codes that no amount of retrying will fix, and what they mean.
CURL_FATAL: dict[int, str] = {
    6: f"could not resolve {HOST} — no DNS, or no network egress",
    7: f"could not connect to {HOST} — network egress appears to be blocked "
       f"(if a proxy is configured, it may be the proxy refusing the connection)",
    5: "could not resolve the proxy — check HTTPS_PROXY/NO_PROXY",
    35: f"TLS handshake with {HOST} failed. " + TLS_HINT,
    60: f"{HOST}'s certificate could not be verified by curl either. " + TLS_HINT,
    77: "curl cannot read its CA certificate bundle. " + TLS_HINT,
}


class UEHTTPError(Exception):
    """Network / HTTP failure after retries (exit 3)."""


class Blocked(UEHTTPError):
    """A bot-protection challenge was served instead of an answer (exit 3).

    ``provider`` says whose: "cloudflare" (the edge's HTML challenge page) or
    "recaptcha" (Uber's own botdefense, a JSON body with
    metadata.botdefense.state == "challenge"). `doctor` prints it.
    """

    def __init__(self, message: str, provider: str = "cloudflare") -> None:
        super().__init__(message)
        self.provider = provider


class PayloadError(Exception):
    """A 200 whose body is not the JSON we expect (exit 3)."""


class LookupFailure(Exception):
    """Uber says the id does not exist: status failure / invalid_store_uuid (exit 2)."""


class NotAllowed(Exception):
    """An endpoint outside ALLOWED_ENDPOINTS was requested (programming error; never sent)."""


class RequestBudgetError(Exception):
    """The plan needs more requests than --max-requests allows (exit 2, before any request)."""


class _Again(Exception):
    """Internal: this attempt failed in a way another attempt might fix.

    Carries the UEHTTPError to raise if there is no attempt left, so the
    caller sees the real cause rather than "retries exhausted". ``backoff``
    is False for the TLS-recovery steps: they are not counted as retries
    and there is nothing to wait for.
    """

    def __init__(self, error: UEHTTPError, backoff: bool = True):
        super().__init__(str(error))
        self.error = error
        self.backoff = backoff


def _header(headers: dict | None, name: str) -> str | None:
    """A header value by case-insensitive name, from any mapping-like object."""
    if not headers:
        return None
    try:
        items = headers.items()
    except AttributeError:
        return None
    for key, value in items:
        if str(key).lower() == name:
            return str(value)
    return None


def looks_blocked(status: int, body: str, headers: dict | None = None) -> bool:
    """True if Cloudflare served a challenge instead of an API response.

    Signals, any of which decides it (01 §1): the header
    ``cf-mitigated: challenge``; or the challenge page's own markers in the
    body — regardless of the HTTP status, so a 200 carrying the page is still
    a block, not a payload. A body that is JSON (first non-blank byte ``{`` or
    ``[``) is never a challenge, however a dish description is worded.
    """
    mitigated = _header(headers, "cf-mitigated")
    if mitigated and "challenge" in mitigated.lower():
        return True
    head = (body or "")[:CHALLENGE_SCAN_BYTES]
    stripped = head.lstrip()
    if stripped[:1] in ("{", "["):
        return False
    return any(marker in head for marker in CHALLENGE_MARKERS)


def botdefense_provider(body: str) -> str | None:
    """The provider name when the JSON body carries Uber's own challenge, else None.

    Seen live after ~110 requests from one address in a day: HTTP 403 with
    {"status":"failure","metadata":{"botdefense":{"state":"challenge",
    "provider":"RECAPTCHA", …}}} (fixtures/botdefense_challenge.json). The
    status code is not part of the test — the body is what says "challenge".
    """
    head = (body or "").lstrip()
    if not head.startswith("{") or "botdefense" not in head[:CHALLENGE_SCAN_BYTES]:
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    meta = payload.get("metadata")
    bot = meta.get("botdefense") if isinstance(meta, dict) else None
    if not isinstance(bot, dict) or str(bot.get("state") or "").lower() != "challenge":
        return None
    return str(bot.get("provider") or "unknown").lower()


def _os_ca_bundle() -> str | None:
    for path in OS_CA_BUNDLES:
        if os.path.isfile(path):
            return path
    return None


def _tls_reason(exc: Exception) -> str:
    """The OpenSSL verdict ("certificate has expired", ...) without the stack."""
    text = str(exc)
    marker = "certificate verify failed: "
    if marker in text:
        return text.split(marker, 1)[1].split(" (_ssl.c")[0]
    return text[:120]


def _ssl_context() -> ssl.SSLContext | None:
    """An OS-trust-store SSL context if `truststore` is installed."""
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return None


def _rate_limited(retry_after: str | None) -> str:
    """The 429 message. Deliberately says "wait", never "retry"."""
    wait = ""
    if retry_after:
        detail = retry_after.strip()[:64]
        wait = (
            f" It asked for {detail} seconds before the next request."
            if detail.isdigit()
            else f" It asked to be left alone until {detail}."
        )
    return (
        "Uber Eats returned HTTP 429 — it is rate-limiting this address, which "
        "on claude.ai is shared with other users." + wait + " Nothing was "
        "fetched, so this is NOT 'nothing found'. Retrying now does not help "
        "and makes the block worse; wait (minutes to hours) and ask again."
    )


def _redirected(status: int, location: str | None) -> str:
    """The 3xx message. Redirects are never followed; the Location is named."""
    where = location.strip() if location and location.strip() else "an unnamed Location"
    return (
        f"Uber Eats answered HTTP {status} redirecting to {where}; redirects are "
        f"never followed. Nothing was fetched, so this is NOT 'nothing found'."
    )


def _forbidden() -> str:
    return (
        f"Uber Eats returned HTTP 403 without a Cloudflare challenge — it is "
        f"refusing this address outright. Nothing was fetched, so this is NOT "
        f"'nothing found'. Wait, and ask again."
    )


def _env(name: str) -> str | None:
    """An environment variable, either case. Proxy vars appear as both."""
    return os.environ.get(name.lower()) or os.environ.get(name.upper()) or None


def _proxy_bypassed(no_proxy: str) -> bool:
    """True if NO_PROXY exempts HOST, using requests' suffix-match rules."""
    for entry in no_proxy.split(","):
        entry = entry.strip().lstrip(".").lower()
        if not entry:
            continue
        if entry == "*" or HOST == entry or HOST.endswith("." + entry):
            return True
    return False


def _curl_env_args() -> list[str]:
    """Make curl honour the same proxy/CA environment `requests` does.

    The two stacks disagree: curl reads `http_proxy` in lower case only, has
    its own `CURL_CA_BUNDLE`, and knows nothing of `REQUESTS_CA_BUNDLE`. A
    sandbox that routes through a proxy would otherwise work on the requests
    path and fail the moment TLS pushed us onto curl — the exact case the
    fallback exists for. Resolving it here, explicitly, removes the guessing.
    """
    args: list[str] = []
    no_proxy = _env("no_proxy")
    if no_proxy and _proxy_bypassed(no_proxy):
        args += ["--noproxy", "*"]
    else:
        proxy = _env("https_proxy") or _env("all_proxy")
        if proxy:
            args += ["--proxy", proxy]
        if no_proxy:
            args += ["--noproxy", no_proxy]
    bundle = _env("curl_ca_bundle") or _env("requests_ca_bundle") or _env("ssl_cert_file")
    if bundle:
        args += ["--cacert", bundle]  # swaps trust anchors, never disables them
    return args


def cookie_header(location: Location) -> str:
    """The exact `Cookie` header value the web app sends (01 §3.2).

    Compact JSON (no spaces), percent-encoded the way `urllib.parse.quote`
    does by default — which is what the probe sent and Uber accepted.
    """
    compact = json.dumps(location.cookie(), separators=(",", ":"), ensure_ascii=False)
    return "uev2.loc=" + urllib.parse.quote(compact)


def _headers(location: Location | None) -> dict[str, str]:
    """The request headers, identical on all three paths."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "en-CA,en;q=0.9",
        "x-csrf-token": "x",
        "content-type": "application/json",
    }
    if location is not None:
        headers["Cookie"] = cookie_header(location)
    return headers


def _classify(status: int, body: str, headers: dict | None) -> Any:
    """Turn one HTTP response into `data`, or raise. Shared by all three paths.

    Order matters: a challenge is decided first, whatever the status —
    Cloudflare's HTML page, then Uber's own botdefense JSON — so a challenged
    403 is ``Blocked`` (never retried) and a plain 403 is not; then
    the statuses that are never retried (3xx, 429, 403); then 5xx (retried);
    then the body of a 200, whose ``status`` field — not the HTTP code — says
    whether the call worked.
    """
    if looks_blocked(status, body, headers):
        raise Blocked(BLOCKED_MESSAGE, provider="cloudflare")
    provider = botdefense_provider(body)
    if provider is not None:
        raise Blocked(BOTDEFENSE_MESSAGE, provider=provider)
    if 300 <= status < 400:
        raise UEHTTPError(_redirected(status, _header(headers, "location")))
    if status == 429:
        raise UEHTTPError(_rate_limited(_header(headers, "retry-after")))
    if status == 403:
        raise UEHTTPError(_forbidden())
    if status >= 500:
        raise _Again(UEHTTPError(
            f"Uber Eats returned HTTP {status} — the API is failing on Uber's side"
        ))
    if status != 200:
        raise UEHTTPError(f"Uber Eats returned HTTP {status} — unexpected")
    try:
        payload = json.loads(body)
    except ValueError:
        raise PayloadError(
            f"Uber Eats answered 200 but the body is not JSON "
            f"({len(body)} bytes, starts {body[:60]!r}) — the API may have changed"
        ) from None
    if not isinstance(payload, dict) or "status" not in payload:
        raise PayloadError(
            "Uber Eats answered JSON without the expected {status, data} envelope "
            "— the API may have changed"
        )
    data = payload.get("data")
    if payload.get("status") == "success":
        return data
    message = code = None
    if isinstance(data, dict):
        message, code = data.get("message"), data.get("code")
    message = message if isinstance(message, str) else None
    code = str(code) if code is not None else None
    if payload.get("status") == "failure" and (message == "invalid_store_uuid" or code == "404"):
        # Two wordings seen live, both with code "404": a malformed id says
        # "invalid_store_uuid" (evidence/api.json); a well-formed id Uber does
        # not know says "We had an issue finding this store…". Both are the
        # user's id being wrong, not the API drifting.
        raise LookupFailure(
            f"Uber Eats does not know that store id ({message or 'code 404'})"
        )
    detail = message or (json.dumps(data)[:160] if data is not None else "no detail")
    if code:
        detail += f" (code {code})"
    raise PayloadError(f"Uber Eats reported status {payload.get('status')!r}: {detail}")


class Transport:
    """POSTs to Uber Eats' web API, with a hard request budget.

    One instance per CLI invocation. The budget is shared across every command,
    so a `compare` that would issue 30 requests is refused up front rather than
    discovered halfway through — and every request that actually leaves the
    machine, retries and TLS recoveries included, is counted.
    """

    def __init__(self, max_requests: int = DEFAULT_MAX_REQUESTS, throttle: float = THROTTLE_SECONDS,
                 locale: str = "ca", timeout: float = TIMEOUT) -> None:
        self.max_requests = max_requests
        self.throttle = throttle
        self.locale = locale
        self.timeout = timeout
        self.requests_made = 0
        self._last_request_at = 0.0
        self._tried_os_bundle = False
        self._curl_bin: str | None = None
        #: For `doctor` (02 §3.9): which stack returned the last response, and
        #: which trust anchors it verified with. None until one comes back.
        self.transport_used: str | None = None   # "requests" | "curl" | "urllib"
        self.tls_path: str | None = None
        self._truststore = False
        self._session = self._build_session()

    # -- budget ----------------------------------------------------------

    def plan(self, count: int, what: str) -> None:
        """Refuse a plan that would exceed the budget, before issuing any of it.

        This is a floor, not a forecast: a retried call spends more than one,
        so a plan that passes here can still run out. That later shortfall
        raises ``RequestBudgetError`` too (exit 2, "raise --max-requests").
        """
        total = self.requests_made + count
        if total > self.max_requests:
            raise RequestBudgetError(
                f"{what} needs {count} requests ({total} this run) but the "
                f"ceiling is {self.max_requests}. Narrow the plan or raise "
                f"--max-requests (hard cap {HARD_CAP_REQUESTS})."
            )

    def _budget_left(self) -> bool:
        return self.requests_made < self.max_requests

    def _spend(self) -> None:
        """Charge one request and wait out the throttle, before issuing it.

        Charged up front on purpose: a request that then fails still travelled,
        and one that failed before leaving (a DNS miss) still cost time and
        will cost it again. Counting only successes would let a flapping
        network issue an unbounded number of real requests.
        """
        if not self._budget_left():
            raise RequestBudgetError(
                f"request ceiling of {self.max_requests} reached. Narrow the "
                f"plan, or raise --max-requests deliberately (hard cap {HARD_CAP_REQUESTS})."
            )
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < self.throttle:
            time.sleep(self.throttle - elapsed)
        self.requests_made += 1
        self._last_request_at = time.monotonic()

    # -- transport -------------------------------------------------------

    def _build_session(self) -> Any:
        if requests is None:
            return None
        session = requests.Session()
        # No jar semantics wanted: the only cookie we ever send is uev2.loc,
        # set per call from the Location. Anything Uber sets back is dropped
        # so one call's Set-Cookie cannot leak into the next.
        session.cookies.set_policy(_RejectAll())
        context = _ssl_context()
        self._truststore = context is not None
        # max_retries=0: urllib3 must not replay inside one call, or the budget
        # counts a third of the traffic. Retries are driven by call(), metered.
        session.mount("https://", _adapter(context))
        return session

    def _requests_tls_path(self) -> str:
        """Which trust anchors the `requests` session verifies with, for `doctor`."""
        if self._truststore:
            return "truststore"
        verify = getattr(self._session, "verify", True)
        if isinstance(verify, str):
            return f"os-bundle:{verify}"
        env = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")
        return f"env-bundle:{env}" if env else "certifi"

    def _url(self, endpoint: str) -> str:
        return f"https://{HOST}/_p/api/{endpoint}?" + urllib.parse.urlencode(
            {"localeCode": self.locale}
        )

    def call(self, endpoint: str, body: dict, location: Location | None = None) -> Any:
        """One POST; returns the parsed `data` object of a status:success body.

        `data` is a dict for every endpoint except ``mapsSearchV1``, whose
        `data` is a list of candidates. Raises NotAllowed before anything
        else for an endpoint outside the allowlist; RequestBudgetError when
        the budget is spent; Blocked / UEHTTPError / PayloadError /
        LookupFailure as documented on each class.

        Every attempt — first try, 5xx retry, CA-bundle retry, curl handoff —
        passes through ``_spend`` first, so ``requests_made`` is the true count
        of requests that left the machine and the throttle applies on all three
        transport paths.
        """
        if endpoint not in ALLOWED_ENDPOINTS:
            raise NotAllowed(
                f"endpoint {endpoint!r} is not on the read-only allowlist "
                f"({', '.join(sorted(ALLOWED_ENDPOINTS))}); nothing was sent"
            )
        url = self._url(endpoint)
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = _headers(location)
        attempts = 0
        while True:
            self._spend()
            try:
                if self._session is not None:
                    data = self._post_requests(url, payload, headers)
                    self.transport_used, self.tls_path = "requests", self._requests_tls_path()
                    return data
                return self._post_fallback(url, payload, headers)
            except _Again as again:
                if again.backoff:
                    attempts += 1
                    if attempts >= MAX_ATTEMPTS or not self._budget_left():
                        raise again.error from None
                    time.sleep(BACKOFF_SECONDS * 2 ** (attempts - 1))
                elif not self._budget_left():
                    raise again.error from None

    def _post_requests(self, url: str, payload: bytes, headers: dict[str, str]) -> Any:
        try:
            # allow_redirects=False: a 3xx is an answer in itself, never
            # something to follow.
            r = self._session.post(url, data=payload, headers=headers,
                                   timeout=self.timeout, allow_redirects=False)
        except requests.exceptions.SSLError as e:
            return self._recover_tls(e)
        except requests.exceptions.RequestException as e:
            error = UEHTTPError(_describe(e, self.timeout))
            if _transient(e):
                raise _Again(error) from e
            raise error from e
        except Exception as e:
            # `requests` does not wrap everything it raises. The one that bites
            # here is an unreadable REQUESTS_CA_BUNDLE: it raises a bare OSError
            # from cert_verify, before any socket is opened, so it is neither a
            # RequestException nor retryable. Deterministic, so never retried.
            raise UEHTTPError(_describe(e, self.timeout)) from e
        return _classify(r.status_code, r.text, r.headers)

    def _recover_tls(self, exc: Exception) -> Any:
        """Handle an SSL failure by changing trust anchors, never by disabling.

        Both cures re-issue the same POST, so neither returns data here: it
        raises ``_Again`` and lets ``call`` charge the retry to the budget. The
        error each ``_Again`` carries is only ever surfaced when that retry
        never happens, so it must describe the cure as *untried*.
        """
        bundle = self._next_ca_bundle()
        if bundle:
            self._session.verify = bundle  # system trust anchors, next attempt
            raise _Again(
                UEHTTPError(
                    f"TLS verification failed for {HOST}: {_tls_reason(exc)}. "
                    f"The system CA bundle at {bundle} was the next thing to "
                    f"try, but there was no request left in the budget to try "
                    f"it with. {TLS_HINT}"
                ),
                backoff=False,
            ) from exc
        if shutil.which("curl"):
            self._session = None  # every later call uses the fallback
            raise _Again(
                UEHTTPError(
                    f"TLS verification failed for {HOST}: {_tls_reason(exc)}. "
                    f"curl was the next thing to try, but there was no request "
                    f"left in the budget to try it with. {TLS_HINT}"
                ),
                backoff=False,
            ) from exc
        tried = self._session.verify
        exhausted = (
            f"the system CA bundle at {tried} did not verify it either"
            if isinstance(tried, str)
            else "no system CA bundle was found to try"
        )
        raise UEHTTPError(
            f"TLS verification failed for {HOST}: {_tls_reason(exc)}. curl is "
            f"not installed to fall back on and {exhausted}. {TLS_HINT}"
        ) from exc

    def _next_ca_bundle(self) -> str | None:
        """The OS CA bundle to retry with, once, or None if nothing new to try."""
        if self._tried_os_bundle:
            return None
        self._tried_os_bundle = True
        if os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE"):
            return None  # the caller already chose a bundle, and it failed
        return _os_ca_bundle()

    def _post_fallback(self, url: str, payload: bytes, headers: dict[str, str]) -> Any:
        """curl, then urllib. Reached when `requests` is absent or TLS failed.

        Both use the OS trust store, which is the usual cure for the pinned-
        certifi problem, and curl exists nearly everywhere a shell does.
        """
        self._curl_bin = self._curl_bin or shutil.which("curl")
        if self._curl_bin:
            result = self._post_curl(url, payload, headers)
            if result is not None:
                return result
            # curl could not be spawned at all; nothing left the machine, so
            # urllib gets this same attempt rather than a fresh one.
        return self._post_urllib(url, payload, headers)

    def _post_curl(self, url: str, payload: bytes, headers: dict[str, str]) -> Any:
        """The data, or None if curl could not be run (no request was made)."""
        argv = [
            self._curl_bin, "-sS", "-g", "--compressed",
            # No -L: a 3xx is raised with its Location, on this path exactly
            # as on the other two.
            "--max-redirs", "0",
            "--max-time", str(self.timeout),
            # -i: the response headers come back with the body, so
            # cf-mitigated and Location are seen here too.
            "-i",
            "-X", "POST", "-d", "@-",
            "-H", "Expect:",  # never a 100-continue interim header block
        ]
        for name, value in headers.items():
            argv += ["-H", f"{name}: {value}"]
        argv += ["-w", "\n%{http_code}", *_curl_env_args(), url]
        try:
            proc = subprocess.run(argv, input=payload, capture_output=True)
        except OSError:
            self._curl_bin = None
            return None
        stdout = proc.stdout.decode("utf-8", "replace")
        proc.stdout = b""
        raw, _, status = stdout.rpartition("\n")
        del stdout
        status = status.strip()
        detail = proc.stderr.decode("utf-8", "replace").strip()[:160]
        if status in NO_STATUS:  # curl never got an HTTP response
            if CURL_TUNNEL_DENIED in detail.lower():
                # Permanent: the proxy is configured not to allow this host.
                raise UEHTTPError(
                    f"the egress proxy refused a tunnel to {HOST} ({detail}). "
                    f"This host is not on the proxy's allowlist — retrying will "
                    f"not help. On claude.ai this is the network-egress setting; "
                    f"'package managers only' does not include {HOST}."
                )
            if proc.returncode in CURL_FATAL:
                raise UEHTTPError(f"curl: {CURL_FATAL[proc.returncode]}")
            error = UEHTTPError(
                f"curl could not fetch {HOST} (exit {proc.returncode}) {detail}"
            )
            if proc.returncode in CURL_TRANSIENT:
                raise _Again(error)
            raise error
        resp_headers, body = _split_curl_response(raw)
        del raw
        if proc.returncode != 0 and proc.returncode in CURL_TRANSIENT:
            # A status arrived but the transfer was cut short (exit 18/28/56):
            # the body is not trustworthy, so this is a retry, not a payload.
            raise _Again(UEHTTPError(
                f"curl transfer from {HOST} was interrupted (exit {proc.returncode}) {detail}"
            ))
        self.transport_used = "curl"
        bundle = _env("curl_ca_bundle") or _env("requests_ca_bundle") or _env("ssl_cert_file")
        self.tls_path = f"curl:{bundle}" if bundle else "curl"
        return _classify(int(status), body, resp_headers)

    def _post_urllib(self, url: str, payload: bytes, headers: dict[str, str]) -> Any:
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            # build_opener keeps urllib's default handlers (they already read
            # https_proxy/HTTPS_PROXY and no_proxy from the environment) and
            # adds the one that refuses redirects; nothing is installed globally.
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                status, resp_headers = resp.status, dict(resp.headers.items())
        except urllib.error.HTTPError as e:
            # Non-2xx arrives here, body and headers intact: a challenged 403
            # still needs its body read to be told from a plain one.
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            status, resp_headers = e.code, dict(e.headers.items())
        except Exception as e:
            text = str(e)
            error = UEHTTPError(_describe_urllib(e, text, self.timeout))
            if isinstance(e, ssl.SSLError) or any(m in text for m in DNS_MARKERS):
                raise error from e  # no retry will fix trust or DNS
            if "SSL" in type(e).__name__ or "certificate verify failed" in text:
                raise error from e
            raise _Again(error) from e
        self.transport_used, self.tls_path = "urllib", "urllib"
        return _classify(status, body, resp_headers)


def _split_curl_response(raw: str) -> tuple[dict[str, str], str]:
    """Split curl -i output into (headers, body), skipping preliminary blocks.

    Two kinds of block precede the real response: a 1xx interim, and — through
    an HTTPS proxy (HTTPS_PROXY set, the claude.ai egress case) — the proxy's
    own "HTTP/1.1 200 Connection established" for the CONNECT tunnel, which
    curl -i prints before the origin's headers. Either way the remainder
    starts with another "HTTP/" block, so keep peeling while that is so.
    (--suppress-connect-headers would do it but errors on curl < 7.54.)
    """
    text = raw
    headers: dict[str, str] = {}
    while text.startswith("HTTP/"):
        block, sep, rest = text.partition("\r\n\r\n")
        if not sep:
            block, sep, rest = text.partition("\n\n")
            if not sep:
                break
        lines = block.splitlines()
        status_line = lines[0] if lines else ""
        parts = status_line.split()
        code = parts[1] if len(parts) > 1 and parts[1].isdigit() else ""
        headers = {}
        for line in lines[1:]:
            name, colon, value = line.partition(":")
            if colon:
                headers[name.strip().lower()] = value.strip()
        text = rest
        interim = code.startswith("1") and len(code) == 3
        if not interim and not text.startswith("HTTP/"):
            break
    return headers, text


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: urllib then raises the 3xx as an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class _RejectAll(http.cookiejar.DefaultCookiePolicy):
    """A cookie policy that stores nothing: Set-Cookie from Uber is dropped."""

    def set_ok(self, cookie, request):  # noqa: D401
        return False


def _adapter(context: ssl.SSLContext | None) -> Any:
    if context is None:
        return HTTPAdapter(max_retries=0)

    class _TruststoreAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs["ssl_context"] = context
            return super().init_poolmanager(*args, **kwargs)

    return _TruststoreAdapter(max_retries=0)


def _transient(exc: Any) -> bool:
    """True if another (metered) attempt could plausibly succeed.

    A proxy misconfiguration and a name that will not resolve are settled
    facts; retrying them spends the budget for nothing.
    """
    if isinstance(exc, requests.exceptions.ProxyError):
        return False
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        return not any(marker in str(exc) for marker in DNS_MARKERS)
    return False


def _describe_urllib(exc: Exception, text: str, timeout: float) -> str:
    """The urllib equivalent of ``_describe``, with no `requests` to lean on."""
    if any(marker in text for marker in DNS_MARKERS):
        return (
            f"cannot resolve {HOST} — no DNS, or no network egress. "
            f"This skill needs outbound HTTPS to {HOST}."
        )
    if isinstance(exc, ssl.SSLError) or "certificate verify failed" in text:
        return f"TLS verification failed for {HOST}: {_tls_reason(exc)}. {TLS_HINT}"
    if isinstance(exc, TimeoutError) or "timed out" in text:
        return f"{HOST} did not respond within {timeout:g}s"
    if "proxy" in text.lower():
        return f"proxy error reaching {HOST} — check HTTPS_PROXY/NO_PROXY ({text[:160]})"
    if "Connection refused" in text or "Network is unreachable" in text:
        return f"cannot connect to {HOST} — no network egress? ({text[:160]})"
    return f"could not reach {HOST}: {type(exc).__name__}: {text[:160]}"


def _describe(exc: Any, timeout: float) -> str:
    """A one-line, actionable rendering of a requests failure."""
    text = str(exc)
    if "CA certificate bundle" in text or "CA bundle" in text:
        return f"the CA bundle configured for this environment is unusable: {text[:160]}"
    if isinstance(exc, requests.exceptions.ProxyError):
        return f"proxy error reaching {HOST} — check HTTPS_PROXY/NO_PROXY ({text[:160]})"
    if isinstance(exc, requests.exceptions.Timeout):
        return f"{HOST} did not respond within {timeout:g}s"
    if isinstance(exc, requests.exceptions.ConnectionError):
        if any(marker in text for marker in DNS_MARKERS):
            return (
                f"cannot resolve {HOST} — no DNS, or no network egress. "
                f"This skill needs outbound HTTPS to {HOST}."
            )
        return f"cannot connect to {HOST} — no network egress? ({text[:160]})"
    return f"{type(exc).__name__}: {text[:160]}"
