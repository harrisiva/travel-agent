"""HTTP transport for the KAYAK affiliate APIs.

Lifted from `campsite-search/scripts/campsites/http.py` and adapted in four
ways, each because this API differs from Camis5:

1. **POST with a JSON body**, since the car search is start-then-poll rather
   than a plain GET.
2. **Thread-safe.** `sweep` fans out across pickup dates with a thread pool.
   `requests.Session` is not documented thread-safe, so each thread gets its
   own via `threading.local()`. Sessions are never shared across threads.
3. **Non-2xx is returned, not raised.** The client has to distinguish 401
   (expired key -> abort everything), 429 (honour Retry-After once) and 5xx
   (retry the same searchId, the search is still alive server-side) from a
   dead network. Only a genuine transport failure raises here.
4. **The apiKey never appears in an error message.** It travels as a query
   parameter, so any message built from a URL would leak it into stderr, into
   `--json` output, and into whatever log the agent writes. Messages are built
   from `METHOD /path` only, and the curl fallback passes its URL through a
   config file on stdin rather than argv, so the key never shows up in `ps`.

The TLS ladder (truststore -> system CA bundle -> curl) is unchanged, and so
is the rule behind it: certificate verification is never disabled.
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import threading
import urllib.parse
from dataclasses import dataclass
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from .errors import TransportError

#: KAYAK requires a User-Agent that uniquely identifies client and version —
#: it is used for click attribution and it selects platform-specific rates, so
#: a browser-impersonating string would be both dishonest and wrong.
USER_AGENT = "kayak-browse/1.0 (+claude-skill; python-requests)"

#: (connect, read). Generous on read because a poll can sit on the server for
#: a while; tight on connect so a black-holed network fails fast in a sweep.
TIMEOUT = (5, 15)

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

#: curl exit codes that no amount of retrying will fix, and what they mean.
CURL_FATAL: dict[int, str] = {
    6: "could not resolve {host} — no DNS, or no network egress",
    7: "could not connect to {host} — network egress appears to be blocked",
    35: "TLS handshake with {host} failed. " + TLS_HINT,
    60: "{host}'s certificate could not be verified by curl either. " + TLS_HINT,
    77: "curl cannot read its CA certificate bundle. " + TLS_HINT,
}


@dataclass(frozen=True)
class Response:
    """One HTTP exchange, decoded as far as it safely can be.

    `body` is the parsed JSON when the response carried any, else None — an
    error body that is not JSON (an HTML proxy page, say) must not turn a 401
    into a crash.
    """

    status: int
    body: Any
    text: str
    headers: dict

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def error_message(self) -> str:
        """The most specific server-supplied reason, or a bare status line.

        Two error envelopes exist in these specs: `PreSearchErrorResponse`
        ({status, errorCode, errorMessage}) for auth and header problems, and
        `SearchErrorResponse` ({url, errors[]}) once a search has started.
        """
        body = self.body
        if isinstance(body, dict):
            if body.get("errorMessage") or body.get("errorCode"):
                code = body.get("errorCode") or ""
                msg = body.get("errorMessage") or ""
                return f"HTTP {self.status} {code}: {msg}".strip()
            errors = body.get("errors")
            if isinstance(errors, list) and errors:
                first = errors[0] if isinstance(errors[0], dict) else {}
                code = first.get("code") or ""
                msg = first.get("localizedDescription") or first.get("description") or ""
                return f"HTTP {self.status} {code}: {msg}".strip()
        return f"HTTP {self.status}"

    def error_code(self) -> str:
        """`errorCode` / `errors[0].code`, uppercased, or "" when absent."""
        body = self.body
        if not isinstance(body, dict):
            return ""
        code = body.get("errorCode")
        if not code:
            errors = body.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                code = errors[0].get("code")
        return str(code).upper() if code else ""

    def retry_after(self) -> float | None:
        """`Retry-After` in seconds, if the server sent a parsable one."""
        raw = self.headers.get("Retry-After") or self.headers.get("retry-after")
        try:
            return float(raw)  # only the delta-seconds form; a date is ignored
        except (TypeError, ValueError):
            return None


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


def _describe(exc: requests.exceptions.RequestException, host: str) -> str:
    """A one-line, actionable rendering of a requests failure.

    The raw text is a nest of urllib3 exceptions; sandboxes fail here often
    enough (no DNS, no egress, a MITM proxy) that the cause is worth naming.
    """
    text = str(exc)
    detail = text[:200]
    if isinstance(exc, requests.exceptions.ProxyError):
        return f"proxy error reaching {host} — check HTTPS_PROXY/NO_PROXY ({detail})"
    if isinstance(exc, requests.exceptions.Timeout):
        return f"{host} did not respond in time"
    if isinstance(exc, requests.exceptions.ConnectionError):
        dns = (
            "NameResolutionError",
            "Name or service not known",
            "nodename nor servname",
            "getaddrinfo failed",
            "Failed to resolve",
            "Temporary failure in name resolution",
        )
        if any(marker in text for marker in dns):
            return f"cannot resolve {host} — no DNS, or no network egress"
        return f"cannot connect to {host} — no network egress? ({detail})"
    return f"{type(exc).__name__}: {detail}"


def _ssl_context() -> ssl.SSLContext | None:
    """An OS-trust-store SSL context if `truststore` is installed."""
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return None


class _TruststoreAdapter(HTTPAdapter):
    def __init__(self, context: ssl.SSLContext, **kwargs):
        self._context = context
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._context
        return super().init_poolmanager(*args, **kwargs)


class Transport:
    """A per-host JSON client, safe to call from several threads at once.

    One attempt per call. Retry *policy* — how many times, how long to wait,
    whether a 5xx is worth repeating — lives in `client.py`, because it
    depends on which call failed: a poll can be repeated against the same
    searchId, a start call cannot be repeated for free.
    """

    def __init__(self, host: str, timeout: tuple[int, int] = TIMEOUT,
                 client_ip: str = "127.0.0.1"):
        self.host = host
        self.timeout = timeout
        self.client_ip = client_ip
        self.requests_made = 0
        self._lock = threading.Lock()
        self._local = threading.local()
        self._verify: Any = True     # shared TLS decision, applied per session
        self._use_curl = False
        self._curl_bin: str | None = None
        self._tried_os_bundle = False
        self._context = _ssl_context()

    # ---------------- sessions ----------------

    def _session(self) -> requests.Session:
        """This thread's Session, created on first use.

        `headers.update` rather than assignment: replacing the dict wholesale
        drops the defaults requests sets for you, including
        `Accept-Encoding: gzip`, which quietly triples the bytes on a 500-row
        car response.
        """
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    # The end user's IP. KAYAK uses it for market selection and
                    # bot detection; a CLI genuinely cannot know it, and the
                    # docs allow a waiver, so the loopback address is sent
                    # rather than a fabricated public one.
                    "x-original-client-ip": self.client_ip,
                }
            )
            adapter = (
                _TruststoreAdapter(self._context, max_retries=0)
                if self._context
                else HTTPAdapter(max_retries=0)
            )
            session.mount("https://", adapter)
            self._local.session = session
        session.verify = self._verify
        return session

    # ---------------- requests ----------------

    def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        body: Any = None,
        headers: dict | None = None,
    ) -> Response:
        """One HTTP call. Raises TransportError only for transport failures.

        `params` values that are None are dropped, so callers can pass optional
        query parameters unconditionally.
        """
        params = {k: v for k, v in (params or {}).items() if v is not None}
        with self._lock:
            self.requests_made += 1
        while not self._use_curl:
            session = self._session()
            try:
                r = session.request(
                    method,
                    f"https://{self.host}{path}",
                    params=params,
                    json=body,
                    headers=headers,
                    timeout=self.timeout,
                )
                return _decode(r, method, path)
            except requests.exceptions.SSLError as e:
                if not self._degrade_tls(e):
                    continue  # retry the same request with new trust anchors
                break  # curl is available; fall through to it
            except requests.exceptions.RequestException as e:
                raise TransportError(
                    f"{method} {path} -> {_describe(e, self.host)}"
                ) from e
        return self._curl(method, path, params, body, headers)

    def _degrade_tls(self, exc: Exception) -> bool:
        """Advance one rung down the TLS ladder. True once curl should be used.

        Raises when nothing is left to try — we never fall back to an
        unverified connection.
        """
        with self._lock:
            self._curl_bin = self._curl_bin or shutil.which("curl")
            if self._curl_bin:
                self._use_curl = True
                return True
            if not self._tried_os_bundle:
                self._tried_os_bundle = True
                chosen = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get(
                    "CURL_CA_BUNDLE"
                )
                bundle = None if chosen else _os_ca_bundle()
                if bundle:
                    self._verify = bundle
                    return False
            tried = self._verify
            exhausted = (
                f"the system CA bundle at {tried} did not verify it either"
                if isinstance(tried, str)
                else "no system CA bundle was found to try"
            )
        raise TransportError(
            f"TLS verification failed for {self.host}: {_tls_reason(exc)}. "
            f"curl is not installed to fall back on and {exhausted}. {TLS_HINT}"
        ) from exc

    def _curl(
        self,
        method: str,
        path: str,
        params: dict,
        body: Any,
        headers: dict | None,
    ) -> Response:
        """Last-resort transport: curl, which uses the OS trust store.

        The request is handed to curl through a config file on **stdin**
        (`-K -`) rather than as argv. That is not a style choice: the apiKey
        rides in the query string, and argv is world-readable through `ps`.
        """
        url = f"https://{self.host}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        config = [f'url = "{url}"', f'user-agent = "{USER_AGENT}"',
                  'header = "Accept: application/json"',
                  f'header = "x-original-client-ip: {self.client_ip}"',
                  f'request = "{method}"',
                  "silent", "show-error", "include",
                  f"max-time = {self.timeout[1] + self.timeout[0]}"]
        for key, value in (headers or {}).items():
            config.append(f'header = "{key}: {value}"')
        if body is not None:
            config.append('header = "Content-Type: application/json"')
            config.append(f"data-binary = {json.dumps(json.dumps(body))}")
        try:
            proc = subprocess.run(
                [self._curl_bin or "curl", "-K", "-"],
                input="\n".join(config).encode("utf-8"),
                capture_output=True,
            )
        except OSError as e:
            raise TransportError(
                f"TLS verification failed for {self.host} and the curl fallback "
                f"could not be run ({type(e).__name__}: {e}). {TLS_HINT}"
            ) from e
        if proc.returncode in CURL_FATAL:
            raise TransportError(
                f"{method} {path} -> "
                + CURL_FATAL[proc.returncode].format(host=self.host)
            )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            detail = err[-1][:160] if err else "no response"
            raise TransportError(
                f"{method} {path} -> {detail} (curl exit {proc.returncode})"
            )
        return _parse_curl(proc.stdout.decode("utf-8", "replace"), method, path)


def _decode(r: requests.Response, method: str, path: str) -> Response:
    """Turn a requests Response into ours, tolerating a non-JSON body."""
    body: Any = None
    text = r.text
    if text.strip():
        try:
            body = r.json()
        except ValueError:
            if r.ok:
                # A 200 that is not JSON means we are talking to something
                # other than the API — a captive portal or a proxy error page.
                raise TransportError(
                    f"{method} {path} -> HTTP {r.status_code} but the body was "
                    f"not JSON ({text[:120]!r})"
                ) from None
    return Response(r.status_code, body, text, dict(r.headers))


def _parse_curl(raw: str, method: str, path: str) -> Response:
    """Split curl's `--include` output into status, headers and body.

    Redirects and `100 Continue` mean several header blocks can arrive; the
    last one is the response that matters.
    """
    head, _, body_text = raw.partition("\r\n\r\n")
    while body_text.startswith("HTTP/"):
        head, _, body_text = body_text.partition("\r\n\r\n")
    lines = head.splitlines()
    status = 0
    if lines and lines[0].startswith("HTTP/"):
        parts = lines[0].split()
        if len(parts) > 1 and parts[1].isdigit():
            status = int(parts[1])
    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip()] = value.strip()
    body: Any = None
    if body_text.strip():
        try:
            body = json.loads(body_text)
        except json.JSONDecodeError:
            if 200 <= status < 300:
                raise TransportError(
                    f"{method} {path} -> HTTP {status} but the body was not JSON "
                    f"({body_text[:120]!r})"
                ) from None
    if not status:
        raise TransportError(f"{method} {path} -> curl returned no HTTP status")
    return Response(status, body, body_text, headers)
