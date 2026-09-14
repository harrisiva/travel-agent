"""HTTP transport for the Enterprise APIs.

The pricing host fingerprints the TLS handshake. Python's *default* cipher
ordering earns a deterministic 403 that arrives as an HTML block page rather
than an exception - so naive code reads it as "no cars available". Reordering
the ciphers to match Chrome fixes it with no new dependency.

Rungs are tried in this order, and the winner is remembered so a fan-out pays
discovery only once:

1. `requests` + Chrome cipher order.               (verified working)
2. The same, with ALPN pinned to HTTP/1.1.         (curl passes on 1.1)
3. `curl`, if present.                             (verified working)
4. `requests` with stock ciphers.                  (known to fail on the
   verified host, but a different OpenSSL build may negotiate acceptably, and
   it costs one request to find out)

Certificate verification is never disabled on any rung.

A response counts as success only if it is HTTP 200 *and* parses as JSON. That
rule is what stops a bot-block masquerading as an empty result.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

from .errors import BotBlocked, TransportError, UsageError

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: Chrome's cipher order. Python's stock ordering is what triggers the block.
CHROME_CIPHERS = (
    "TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256:"
    "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
    "ECDHE-RSA-AES128-SHA:ECDHE-RSA-AES256-SHA:"
    "AES128-GCM-SHA256:AES256-GCM-SHA384:AES128-SHA:AES256-SHA"
)

TIMEOUT = 45
RETRIES = 3
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: Statuses that mean "your request is wrong", not "the network is unwell".
#: 403 is deliberately absent - it is handled as a block, not a usage error.
CLIENT_ERROR_STATUS = frozenset({400, 404, 405, 409, 410, 422})

#: Rung identifiers, in attempt order.
CHROME_TLS, CHROME_TLS_H11, CURL, STOCK_TLS = (
    "chrome-tls", "chrome-tls-http1.1", "curl", "stock-tls",
)

_CA_HINT = (
    "Fix: pip install truststore (verifies against the OS trust store), or "
    "point REQUESTS_CA_BUNDLE / SSL_CERT_FILE at a current CA bundle. "
    "Certificate checking is never disabled."
)

_BLOCK_HINT = (
    "The host is refusing this client rather than failing to connect. Two "
    "causes look identical from here: sustained request volume from one IP "
    "(which clears on its own - wait and retry, and slow down), or a TLS "
    "handshake this environment cannot produce acceptably. If it started "
    "working and then stopped, it is almost certainly volume. This is NOT "
    "'no cars available'."
)


def _adapter(alpn_http11: bool):
    """An HTTPAdapter pinned to Chrome's cipher order."""
    import requests.adapters
    from urllib3.util.ssl_ import create_urllib3_context

    class ChromeTLS(requests.adapters.HTTPAdapter):
        def init_poolmanager(self, *args: Any, **kwargs: Any):
            context = create_urllib3_context(ciphers=CHROME_CIPHERS)
            if alpn_http11:
                try:
                    context.set_alpn_protocols(["http/1.1"])
                except NotImplementedError:  # pragma: no cover - old OpenSSL
                    pass
            kwargs["ssl_context"] = context
            return super().init_poolmanager(*args, **kwargs)

    return ChromeTLS()


class _Response:
    __slots__ = ("status", "body", "content_type")

    def __init__(self, status: int, body: str, content_type: str = "") -> None:
        self.status = status
        self.body = body
        self.content_type = content_type

    @property
    def looks_like_html(self) -> bool:
        return self.body.lstrip()[:1] == "<"


class Transport:
    """Per-process HTTP client. Sessions are per-thread; a fan-out is safe."""

    def __init__(self, timeout: int = TIMEOUT, verbose: bool = False) -> None:
        self.timeout = timeout
        self.verbose = verbose
        self._rung: str | None = None
        self._attempted = False
        self._local = threading.local()
        self._lock = threading.Lock()

    # -- rung plumbing ----------------------------------------------------

    def _rungs(self) -> list[str]:
        order = [CHROME_TLS, CHROME_TLS_H11]
        if shutil.which("curl"):
            order.append(CURL)
        order.append(STOCK_TLS)
        return order

    def _session(self, rung: str):
        """One requests.Session per thread per rung - Sessions aren't thread-safe."""
        cache = getattr(self._local, "sessions", None)
        if cache is None:
            cache = self._local.sessions = {}
        if rung not in cache:
            import requests

            session = requests.Session()
            if rung in (CHROME_TLS, CHROME_TLS_H11):
                session.mount("https://", _adapter(rung == CHROME_TLS_H11))
            cache[rung] = session
        return cache[rung]

    def _log(self, message: str) -> None:
        # stderr, never stdout: `doctor` runs verbose unconditionally, so a
        # single line on stdout made `doctor --json` unparseable - during
        # exactly the incident SKILL.md tells the agent to run it for.
        if self.verbose:
            print(f"  [http] {message}", file=sys.stderr, flush=True)

    # -- single attempts --------------------------------------------------

    def _via_requests(
        self, rung: str, method: str, url: str, headers: dict, body: str | None
    ) -> _Response:
        import requests

        session = self._session(rung)
        try:
            response = session.request(
                method, url, headers=headers, data=body, timeout=self.timeout
            )
        except requests.exceptions.SSLError as exc:
            # Usually a stale CA bundle rather than a bad certificate. Raised
            # as a TransportError so the ladder keeps going: curl uses the OS
            # trust store and often succeeds where a pinned certifi fails.
            raise TransportError(
                f"TLS verification against {_host(url)} failed: {exc}. {_CA_HINT}"
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise TransportError(
                f"{_host(url)} timed out after {self.timeout}s"
            ) from exc
        except requests.exceptions.ProxyError as exc:
            raise TransportError(
                f"proxy rejected the connection to {_host(url)}: {exc}"
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise TransportError(
                f"could not connect to {_host(url)}: {exc}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise TransportError(f"{_host(url)} request failed: {exc}") from exc

        return _Response(
            response.status_code,
            response.text,
            response.headers.get("content-type", ""),
        )

    def _via_curl(
        self, method: str, url: str, headers: dict, body: str | None
    ) -> _Response:
        command = [
            "curl", "-sS", "--http1.1", "--max-time", str(self.timeout),
            "-w", "\n%{http_code}",
        ]
        for key, value in headers.items():
            command += ["-H", f"{key}: {value}"]
        if body is not None:
            command += ["-X", method, "--data-binary", "@-"]
        command.append(url)
        try:
            proc = subprocess.run(
                command, input=body, capture_output=True, text=True,
                timeout=self.timeout + 15,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"curl timed out talking to {_host(url)}") from exc
        except OSError as exc:
            # curl vanished between the which() probe and here, or could not
            # be executed at all.
            raise TransportError(f"could not run curl: {exc}") from exc
        if proc.returncode != 0:
            raise TransportError(
                f"curl exited {proc.returncode}: {proc.stderr.strip()[:200]}"
            )
        text, _, status = proc.stdout.rpartition("\n")
        return _Response(int(status or 0), text)

    def _attempt(
        self, rung: str, method: str, url: str, headers: dict, body: str | None
    ) -> _Response:
        if rung == CURL:
            return self._via_curl(method, url, headers, body)
        return self._via_requests(rung, method, url, headers, body)

    # -- public API -------------------------------------------------------

    def request_json(
        self, method: str, url: str, headers: dict, body: Any = None
    ) -> Any:
        payload = json.dumps(body) if body is not None else None
        # Try the known-good rung first, but keep the rest behind it: a rung
        # that worked a minute ago can start being blocked, and giving up
        # without trying the alternatives would strand the whole run.
        self._attempted = True
        available = self._rungs()
        pinned = self._rung
        rungs = (
            [pinned] + [r for r in available if r != pinned]
            if pinned in available
            else available
        )
        blocked: list[str] = []
        last_error: Exception | None = None

        for rung in rungs:
            try:
                data = self._with_retries(rung, method, url, headers, payload)
            except UsageError:
                raise
            except BotBlocked as exc:
                blocked.append(rung)
                last_error = exc
                self._log(f"{rung}: blocked, trying next transport")
                continue
            except TransportError as exc:
                last_error = exc
                self._log(f"{rung}: {exc}")
                continue
            with self._lock:
                if self._rung != rung:
                    self._log(f"using transport: {rung}")
                    self._rung = rung
            return data

        # Every rung failed. If a pinned rung stopped working, unpin and let
        # the next call rediscover rather than failing forever on one rung.
        with self._lock:
            self._rung = None
        if blocked and len(blocked) == len(rungs):
            raise BotBlocked(
                f"{_host(url)} rejected every available HTTP client "
                f"({', '.join(blocked)}). {_BLOCK_HINT}"
            )
        raise TransportError(str(last_error) if last_error else f"{url} failed")

    def _with_retries(
        self, rung: str, method: str, url: str, headers: dict, body: str | None
    ) -> Any:
        for attempt in range(1, RETRIES + 1):
            try:
                response = self._attempt(rung, method, url, headers, body)
            except (BotBlocked, UsageError):
                raise
            except TransportError:
                # Connection resets and timeouts are worth one more go on this
                # rung before handing over to the next transport.
                if attempt >= RETRIES:
                    raise
                delay = 2 ** attempt
                self._log(f"{rung}: connection failed, retrying in {delay}s")
                time.sleep(delay)
                continue

            if response.status == 403 or (
                response.status != 200 and response.looks_like_html
            ):
                # Never retried: a fingerprint rejection only accumulates
                # strikes, and the fix is a different transport.
                raise BotBlocked(f"{_host(url)} returned {response.status}")

            if response.status in RETRYABLE_STATUS and attempt < RETRIES:
                delay = 2 ** attempt
                self._log(f"{rung}: HTTP {response.status}, retrying in {delay}s")
                time.sleep(delay)
                continue

            if response.status in CLIENT_ERROR_STATUS:
                # The request itself is wrong. Surfacing this as a network
                # failure would tell a caller to retry something that can
                # never succeed.
                raise UsageError(
                    f"{_host(url)} rejected the request (HTTP "
                    f"{response.status}): {response.body.strip()[:200]}"
                )

            if response.status != 200:
                raise TransportError(
                    f"{_host(url)} returned HTTP {response.status}: "
                    f"{response.body.strip()[:200]}"
                )

            if response.looks_like_html:
                raise BotBlocked(
                    f"{_host(url)} returned an HTML page where JSON was "
                    f"expected. {_BLOCK_HINT}"
                )

            try:
                return json.loads(response.body)
            except ValueError as exc:
                raise TransportError(
                    f"{_host(url)} returned unparseable JSON: {exc}"
                ) from exc

        raise TransportError(f"{_host(url)} kept failing after {RETRIES} attempts")

    def get_json(self, url: str, headers: dict) -> Any:
        return self.request_json("GET", url, headers)

    def post_json(self, url: str, headers: dict, body: Any) -> Any:
        return self.request_json("POST", url, headers, body)

    @property
    def transport_used(self) -> str:
        """Which rung is working, or why none is.

        Distinguishes "nothing attempted yet" from "everything was tried and
        rejected" - the log shows four attempts, so reporting "none yet" after
        a block would contradict it.
        """
        if self._rung:
            return self._rung
        return "(all transports rejected)" if self._attempted else "(none yet)"


def _host(url: str) -> str:
    parts = url.split("/", 3)
    return parts[2] if len(parts) > 2 else url
