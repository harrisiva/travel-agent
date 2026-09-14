# copied from google-flights/scripts/gflights/http.py @ c9d46c1 — divergences: HOST (same), RESULTS_MARKER (ds:2 presence), THROTTLE_SECONDS (2.5), no-redirect policy (3xx raises with the Location, on all three paths: requests allow_redirects=False, curl without -L reading %{redirect_url}, urllib with a redirect handler that refuses), two attributes added for doctor: transport_used, tls_path (set when a page is returned), and wording (flights→hotels in docstrings and in the 429/403 messages, which said "no flights were checked").
# Lane A owns this file. Everything else is the flights transport verbatim; keep the header current when diverging further.
"""HTTP transport for the Google Hotels entity page.

Unusual for this repo: the response is HTML, not JSON. Google server-renders
the whole result set into the page as `AF_initDataCallback` blocks, so one GET
returns everything and there is no JSON endpoint to call. See ``parse.py``.

Four things make this transport different from a plain ``requests.get``:

1. **Size.** An entity page is 1.6-4.3 MB. That is fine once, and a problem if
   an agent sweeps thirty dates, so the client counts requests and refuses to
   exceed its budget rather than discovering the ceiling the hard way.
2. **The budget has to be honest.** Retries are driven here, in ``get``, and
   every attempt is metered — including the two TLS-recovery paths (retrying
   with the OS CA bundle, and handing off to curl), which re-issue the same
   GET. urllib3's own ``Retry`` is switched off precisely because it replays
   *inside* one call: with it on, a budget of 5 permitted up to 20 real fetches
   of a 2.5 MB page. On claude.ai the traffic leaves from a shared datacenter
   IP, so an over-count is not a rounding error — it is everyone else's
   problem.
3. **Blocking looks like success.** When Google decides you are a bot it
   answers 200 with a consent interstitial or a captcha, not 4xx. Reported
   naively that parses as "no rates" — the exact false negative this repo
   forbids. ``looks_blocked`` classifies it so the CLI can exit 3, not 1.
   A 429 is the same hazard in a different coat: it is never retried, because
   retrying a rate limit is what turns a slow day into a blocked address.
   A 3xx is a third: the hotel *list* answers 302 into the disallowed
   ``/travel/search``, so a redirect is never followed on any path — it is
   raised, naming the Location, so a wrong URL cannot be reported as a page.
4. **TLS in a sandbox.** Same hazard as campsite-search: a pinned `certifi`
   older than the server chain. Fix order is truststore -> system CA bundle ->
   curl. Verification is never disabled.

`requests` is optional here. If it is missing entirely — a real possibility in
a locked-down sandbox where pip is also blocked — the transport falls back to
`curl` and then to `urllib`, so the skill still answers. All three paths go
through the same budget and throttle.
"""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

try:  # optional: the CLI works without it, just with nicer error text
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:  # pragma: no cover - exercised only in bare sandboxes
    requests = None  # type: ignore[assignment]

HOST = "www.google.com"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

TIMEOUT = 60

#: Attempts at one page before giving up, counting the first. Each one is a
#: real request and is metered against the budget.
RETRIES = 3

#: The TLS-recovery steps (OS CA bundle, then curl) re-issue the same GET, so
#: a page can legitimately need more attempts than RETRIES. They are still
#: metered; this only bounds the loop.
MAX_ATTEMPTS = RETRIES + 2

#: First retry waits this long, then it doubles. Only 5xx and genuinely
#: transient connection failures ever get here — never a 429.
BACKOFF_SECONDS = 1.0

#: Deliberately low. Google blocks rather than throttles, and on claude.ai the
#: request leaves from a shared datacenter IP, so an over-eager sweep does not
#: just fail — it degrades the address for everyone using it. Five covers every
#: single-search command; a date sweep must raise it explicitly, which is the
#: point at which someone decides the traffic is worth it.
DEFAULT_MAX_REQUESTS = 5

#: The most --max-requests may be raised to. A 40-request run is already a lot
#: of 2.5MB fetches; beyond it, blocking is close to certain.
MAX_MAX_REQUESTS = 40

#: Pause between consecutive requests. The probe's pace: 44 entity fetches at
#: this spacing drew no captcha (04 §7). Entity pages are heavier than flights
#: pages, so this is slower than flights' 0.7 s on purpose.
THROTTLE_SECONDS = 2.5

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
#: a genuinely truncated transfer — so it would be retried for 16s before
#: failing, and the message named neither the host nor the proxy. An egress
#: allowlist that excludes Google is the single likeliest way this skill fails
#: on claude.ai, so it is worth telling apart from a flaky connection.
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

#: A real hotels page always carries this block — the 2150-byte currency
#: table, present on every capture including the unknown-entity page (01 §H4;
#: `ds:0` is `[]` there and `ds:1` is an error block, so neither would do).
#: Detecting its *presence* is far safer than matching block markers: a
#: healthy page contains the string "recaptcha" (a preloaded JS reference)
#: and would trip a marker list. Absence of this block is the reliable
#: signal, and it stays correct if Google rewords its interstitials.
RESULTS_MARKER = "AF_initDataCallback({key: 'ds:2'"

#: Used only to explain *which* kind of block happened, never to decide that
#: one did. Searched over the whole page, since a 2.8MB document can put these
#: anywhere.
BLOCK_HINTS = (
    ("consent.google.com", "a cookie-consent interstitial"),
    ("/sorry/index", "Google's \"unusual traffic\" block page"),
    ("detected unusual traffic", "an unusual-traffic warning"),
    ("unusual traffic from your computer network", "an unusual-traffic warning"),
)


class HotelsHTTPError(RuntimeError):
    """Network, TLS, or bot-blocking failure. Always exit code 3."""


class RequestBudgetError(RuntimeError):
    """The planned request count exceeds --max-requests. Always exit code 2."""


class _Again(Exception):
    """Internal: this attempt failed in a way another attempt might fix.

    Carries the HotelsHTTPError to raise if there is no attempt left, so the
    caller sees the real cause rather than "retries exhausted".
    """

    def __init__(self, error: HotelsHTTPError, backoff: bool = True):
        super().__init__(str(error))
        self.error = error
        self.backoff = backoff


def looks_blocked(html: str) -> bool:
    """True if Google served something other than a hotels page.

    Positive detection: a genuine hotels page — priced, unpriced or unknown
    entity alike — always contains the ds:2 currency block. Anything else — a
    consent wall, a captcha, an error shell — does not.
    """
    return RESULTS_MARKER not in html


def block_reason(html: str) -> str:
    """A human explanation of a blocked response, for the error message."""
    lowered = html.lower()
    for marker, description in BLOCK_HINTS:
        if marker in lowered:
            return description
    return "a page with no search results in it"


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
    """The 429 message. Deliberately says "wait", never "retry".

    SKILL.md promises the user that retrying does not fix this and waiting
    does. It also has to say, out loud, that this is not an empty route —
    an agent that reads "no results" here will report a false negative.
    """
    wait = ""
    if retry_after:
        detail = retry_after.strip()[:64]
        wait = (
            f" It asked for {detail} seconds before the next request."
            if detail.isdigit()
            else f" It asked to be left alone until {detail}."
        )
    return (
        "Google returned HTTP 429 — it is rate-limiting this address, which on "
        "claude.ai is shared with other users." + wait + " No hotel was "
        "priced, so this is NOT \"no rates\". Retrying now does not help "
        "and makes the block worse; wait (minutes to hours) and ask again."
    )


def _redirected(status: int, location: str | None) -> str:
    """The 3xx message. Redirects are never followed; the Location is named."""
    where = location.strip() if location and location.strip() else "an unnamed Location"
    hint = ""
    if "/travel/search" in where:
        hint = (
            " — that is the hotel list page, which this skill never fetches; "
            "the URL was not an entity page (check the id)"
        )
    return (
        f"Google answered HTTP {status} redirecting to {where}; redirects are "
        f"never followed{hint}. No hotel was priced, so this is NOT \"no rates\"."
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


class Transport:
    """Fetches Google Hotels entity pages, with a hard request budget.

    One instance per CLI invocation. The budget is shared across every command,
    so a sweep that would issue 300 requests is refused up front rather than
    discovered halfway through — and every request that actually leaves the
    machine, retries and TLS recoveries included, is counted.
    """

    def __init__(
        self,
        timeout: int = TIMEOUT,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        throttle: float = THROTTLE_SECONDS,
    ):
        self.timeout = timeout
        self.max_requests = max_requests
        self.throttle = throttle
        self.requests_made = 0
        self._last_request_at = 0.0
        self._tried_os_bundle = False
        self._curl_bin: str | None = None
        #: For `doctor` (04 §3.6): which stack returned the last page, and
        #: which trust anchors it verified with. None until a page comes back.
        self.transport_used: str | None = None
        self.tls_path: str | None = None
        self._truststore = False
        self._session = self._build_session()

    # -- budget ----------------------------------------------------------

    def plan(self, count: int, what: str) -> None:
        """Refuse a plan that would exceed the budget, before issuing any of it.

        An agent will cheerfully ask for a 400-day sweep. Failing at request 1
        with the arithmetic spelled out is far kinder than failing at request
        41 with a partial answer that looks complete.

        This is a floor, not a forecast: a retried page spends more than one,
        so a sweep that passes here can still run out. That later shortfall
        raises ``RequestBudgetError`` too (exit 2, "raise --max-requests"),
        which is what SKILL.md documents — not a network error. Do not
        "fix" one to match the other without changing both.
        """
        total = self.requests_made + count
        if total > self.max_requests:
            raise RequestBudgetError(
                f"{what} needs {count} requests ({total} this run) but the "
                f"ceiling is {self.max_requests}. Narrow the range, raise "
                f"--max-requests (up to {MAX_MAX_REQUESTS}), or use --step to "
                f"sample fewer dates across the same window."
            )

    def _budget_left(self) -> bool:
        return self.requests_made < self.max_requests

    def _spend(self) -> None:
        """Charge one request and wait out the throttle, before issuing it.

        Charged up front on purpose: a request that then fails still travelled,
        and one that failed before leaving (a DNS miss) still cost time and
        will cost it again. Counting only successes would let a flapping
        network issue an unbounded number of real fetches.
        """
        if not self._budget_left():
            raise RequestBudgetError(
                f"request ceiling of {self.max_requests} reached. Narrow the "
                f"range, or raise --max-requests deliberately."
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
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-CA,en;q=0.9",
                # An EU-geolocated egress IP is served a consent interstitial
                # instead of results. This is a "no thanks" marker, not an
                # identity cookie — no jar is kept and nothing is stored.
                "Cookie": "CONSENT=YES+cb",
            }
        )
        context = _ssl_context()
        self._truststore = context is not None
        # max_retries=0: urllib3 must not replay inside one call, or the budget
        # counts a quarter of the traffic. Retries are driven by get(), metered.
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

    def get(self, path: str, params: dict[str, str]) -> str:
        """GET a page and return its HTML, or raise HotelsHTTPError.

        Every attempt — first try, 5xx retry, CA-bundle retry, curl handoff —
        passes through ``_spend`` first, so ``requests_made`` is the true count
        of requests that left the machine and the throttle applies on all three
        transport paths.
        """
        url = f"https://{HOST}{path}"
        attempts = 0
        while True:
            self._spend()
            attempts += 1
            try:
                if self._session is not None:
                    page = self._get_requests(url, params)
                    self.transport_used, self.tls_path = "requests", self._requests_tls_path()
                    return page
                return self._get_fallback(url, params)
            except _Again as again:
                if attempts >= MAX_ATTEMPTS or not self._budget_left():
                    raise again.error from None
                if again.backoff:
                    time.sleep(BACKOFF_SECONDS * 2 ** (attempts - 1))

    def _get_requests(self, url: str, params: dict[str, str]) -> str:
        try:
            # allow_redirects=False: a 3xx is an answer in itself (the list
            # page's 302 into /travel/search), never something to follow.
            r = self._session.get(url, params=params, timeout=self.timeout, allow_redirects=False)
        except requests.exceptions.SSLError as e:
            return self._recover_tls(e)
        except requests.exceptions.RequestException as e:
            error = HotelsHTTPError(_describe(e, self.timeout))
            if _transient(e):
                raise _Again(error) from e
            raise error from e
        except Exception as e:
            # `requests` does not wrap everything it raises. The one that bites
            # here is an unreadable REQUESTS_CA_BUNDLE: it raises a bare OSError
            # from cert_verify, before any socket is opened, so it is neither a
            # RequestException nor retryable. Uncaught it reached the CLI as a
            # traceback and exit 1 — the two things this transport promises not
            # to do. Deterministic, so it is never retried.
            raise HotelsHTTPError(_describe(e, self.timeout)) from e
        if 300 <= r.status_code < 400:
            raise HotelsHTTPError(_redirected(r.status_code, r.headers.get("Location")))
        if r.status_code == 429:
            raise HotelsHTTPError(_rate_limited(r.headers.get("Retry-After")))
        if r.status_code == 403:
            raise HotelsHTTPError(
                f"Google returned HTTP 403 — it is refusing this address "
                f"outright. No hotel was priced, so this is NOT \"no rates\". "
                f"Wait, and ask again."
            )
        if r.status_code >= 500:
            raise _Again(
                HotelsHTTPError(
                    f"Google returned HTTP {r.status_code} — the search page is "
                    f"failing on Google's side"
                )
            )
        if r.status_code != 200:
            raise HotelsHTTPError(f"Google returned HTTP {r.status_code} — unexpected")
        return r.text

    def _recover_tls(self, exc: Exception) -> str:
        """Handle an SSL failure by changing trust anchors, never by disabling.

        Both cures re-issue the same GET, so neither returns a page here: it
        raises ``_Again`` and lets ``get`` charge the retry to the budget. The
        error each ``_Again`` carries is only ever surfaced when that retry
        never happens, so it must describe the cure as *untried* — saying "the
        system CA bundle did not fix it" would send someone to replace a bundle
        that was never loaded, when the real fault was an exhausted budget.
        """
        bundle = self._next_ca_bundle()
        if bundle:
            self._session.verify = bundle  # system trust anchors, next attempt
            raise _Again(
                HotelsHTTPError(
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
                HotelsHTTPError(
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
        raise HotelsHTTPError(
            f"TLS verification failed for {HOST}: {_tls_reason(exc)}. curl is "
            f"not installed to fall back on and {exhausted}. {TLS_HINT}"
        ) from exc

    def _next_ca_bundle(self) -> str | None:
        """The OS CA bundle to retry with, once, or None if nothing new to try.

        A container's `ca-certificates` is usually newer than a pinned
        `certifi`. This swaps the trust anchors; it does not weaken them.
        """
        if self._tried_os_bundle:
            return None
        self._tried_os_bundle = True
        if os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE"):
            return None  # the caller already chose a bundle, and it failed
        return _os_ca_bundle()

    def _get_fallback(self, url: str, params: dict[str, str]) -> str:
        """curl, then urllib. Reached when `requests` is absent or TLS failed.

        Both use the OS trust store, which is the usual cure for the pinned-
        certifi problem, and curl exists nearly everywhere a shell does.
        """
        # urlencode percent-escapes every value, so the query cannot smuggle a
        # `&`, a space, or a curl glob character into the URL. curl also gets
        # -g so it never interprets [] or {} even if that ever changed.
        full = url + "?" + urllib.parse.urlencode(params)
        self._curl_bin = self._curl_bin or shutil.which("curl")
        if self._curl_bin:
            page = self._get_curl(full)
            if page is not None:
                return page
            # curl could not be spawned at all; nothing left the machine, so
            # urllib gets this same attempt rather than a fresh one.
        return self._get_urllib(full)

    def _get_curl(self, full: str) -> str | None:
        """The page, or None if curl could not be run (no request was made)."""
        try:
            proc = subprocess.run(
                [
                    self._curl_bin, "-sS", "-g", "--compressed",
                    # No -L: a 3xx is raised with its Location, on this path
                    # exactly as on the other two. %{redirect_url} is what curl
                    # would have followed, printed only because it did not.
                    "--max-redirs", "0",
                    "--max-time", str(self.timeout),
                    "-A", USER_AGENT,
                    "-H", "Accept: text/html,application/xhtml+xml",
                    "-H", "Accept-Language: en-CA,en;q=0.9",
                    "-H", "Cookie: CONSENT=YES+cb",
                    "-w", "\n%{http_code}\t%{redirect_url}",
                    *_curl_env_args(),
                    full,
                ],
                capture_output=True,  # never let 2.5MB of HTML reach our stdout
            )
        except OSError:
            self._curl_bin = None
            return None
        # The page arrives as bytes, is decoded, then split: three copies of
        # ~2.5MB if we keep them all. Drop each as soon as it is superseded.
        stdout = proc.stdout.decode("utf-8", "replace")
        proc.stdout = b""
        body, _, trailer = stdout.rpartition("\n")
        del stdout
        status, _, location = trailer.partition("\t")
        if status == "200":
            self.transport_used = "curl"
            bundle = _env("curl_ca_bundle") or _env("requests_ca_bundle") or _env("ssl_cert_file")
            self.tls_path = f"curl:{bundle}" if bundle else "curl"
            return body
        del body
        if status.isdigit() and 300 <= int(status) < 400:
            raise HotelsHTTPError(_redirected(int(status), location))
        if status == "429":
            raise HotelsHTTPError(_rate_limited(None))
        detail = proc.stderr.decode("utf-8", "replace").strip()[:160]
        if status in NO_STATUS:  # curl never got an HTTP response
            if CURL_TUNNEL_DENIED in detail.lower():
                # Permanent: the proxy is configured not to allow this host.
                raise HotelsHTTPError(
                    f"the egress proxy refused a tunnel to {HOST} ({detail}). "
                    f"This host is not on the proxy's allowlist — retrying will "
                    f"not help. On claude.ai this is the network-egress setting; "
                    f"'package managers only' does not include {HOST}."
                )
            if proc.returncode in CURL_FATAL:
                raise HotelsHTTPError(f"curl: {CURL_FATAL[proc.returncode]}")
            error = HotelsHTTPError(
                f"curl could not fetch {HOST} (exit {proc.returncode}) {detail}"
            )
            if proc.returncode in CURL_TRANSIENT:
                raise _Again(error)
            raise error
        error = HotelsHTTPError(
            f"curl could not fetch {HOST} (HTTP {status}) {detail}"
        )
        if status.isdigit() and int(status) >= 500:
            raise _Again(error)
        raise error

    def _get_urllib(self, full: str) -> str:
        try:
            req = urllib.request.Request(
                full,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-CA,en;q=0.9",
                    "Cookie": "CONSENT=YES+cb",
                },
            )
            # build_opener keeps urllib's default handlers (they already read
            # https_proxy/HTTPS_PROXY and no_proxy from the environment) and
            # adds the one that refuses redirects; nothing is installed globally.
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(req, timeout=self.timeout) as resp:
                page = resp.read().decode("utf-8", "replace")
            self.transport_used, self.tls_path = "urllib", "urllib"
            return page
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                raise HotelsHTTPError(_redirected(e.code, e.headers.get("Location"))) from e
            if e.code == 429:
                raise HotelsHTTPError(
                    _rate_limited(e.headers.get("Retry-After"))
                ) from e
            error = HotelsHTTPError(f"Google returned HTTP {e.code}")
            if e.code >= 500:
                raise _Again(error) from e
            raise error from e
        except Exception as e:
            text = str(e)
            error = HotelsHTTPError(_describe_urllib(e, text, self.timeout))
            if isinstance(e, ssl.SSLError) or any(m in text for m in DNS_MARKERS):
                raise error from e  # no retry will fix trust or DNS
            if "SSL" in type(e).__name__ or "certificate verify failed" in text:
                raise error from e
            raise _Again(error) from e


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: urllib then raises the 3xx as an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


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


def _describe_urllib(exc: Exception, text: str, timeout: int) -> str:
    """The urllib equivalent of ``_describe``, with no `requests` to lean on.

    This path is reached two different ways — `requests` absent, or `requests`
    present but TLS pushed us off it — so it must not assert which. The old
    text claimed "No `requests`, and the curl/urllib fallbacks failed too",
    which was wrong in the second case and unhelpful in both: it named no cause
    and offered no fix, where the same DNS failure on the `requests` path said
    exactly what was wrong.
    """
    if any(marker in text for marker in DNS_MARKERS):
        return (
            f"cannot resolve {HOST} — no DNS, or no network egress. "
            f"This skill needs outbound HTTPS to {HOST}."
        )
    if isinstance(exc, ssl.SSLError) or "certificate verify failed" in text:
        return f"TLS verification failed for {HOST}: {_tls_reason(exc)}. {TLS_HINT}"
    if isinstance(exc, TimeoutError) or "timed out" in text:
        return f"{HOST} did not respond within {timeout}s"
    if "proxy" in text.lower():
        return f"proxy error reaching {HOST} — check HTTPS_PROXY/NO_PROXY ({text[:160]})"
    if "Connection refused" in text or "Network is unreachable" in text:
        return f"cannot connect to {HOST} — no network egress? ({text[:160]})"
    return f"could not reach {HOST}: {type(exc).__name__}: {text[:160]}"


def _describe(exc: Any, timeout: int) -> str:
    """A one-line, actionable rendering of a requests failure.

    Sandboxes fail here often enough (no DNS, no egress, a MITM proxy) that
    naming the cause is worth the branching.
    """
    text = str(exc)
    if "CA certificate bundle" in text or "CA bundle" in text:
        # REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE / SSL_CERT_FILE points at
        # something requests cannot read. Naming the variable matters: the
        # path in the message is often a leftover from another container.
        return f"the CA bundle configured for this environment is unusable: {text[:160]}"
    if isinstance(exc, requests.exceptions.ProxyError):
        return f"proxy error reaching {HOST} — check HTTPS_PROXY/NO_PROXY ({text[:160]})"
    if isinstance(exc, requests.exceptions.Timeout):
        return f"{HOST} did not respond within {timeout}s"
    if isinstance(exc, requests.exceptions.ConnectionError):
        if any(marker in text for marker in DNS_MARKERS):
            return (
                f"cannot resolve {HOST} — no DNS, or no network egress. "
                f"This skill needs outbound HTTPS to {HOST}."
            )
        return f"cannot connect to {HOST} — no network egress? ({text[:160]})"
    return f"{type(exc).__name__}: {text[:160]}"
