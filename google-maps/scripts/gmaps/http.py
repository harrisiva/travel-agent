"""HTTP transport, standard library only.

No `requests`. Every command here is one GET with a browser User-Agent, which
`urllib.request` does perfectly well, and dropping the dependency removes the
one install step between downloading this skill and asking it a question — on
claude.ai there may be no PyPI to reach at all.

The environment is assumed hostile: no HOME, a read-only filesystem, no curl,
and a stale CA bundle are all survivable. Certificate verification is never
disabled; if every path fails, the error names the host and the fix.
"""

from __future__ import annotations

import gzip
import json
import shutil
import ssl
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any

from gmaps.errors import NetworkError

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 25

OS_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Alpine
    "/etc/pki/tls/certs/ca-bundle.crt",    # RHEL, Fedora, Amazon Linux
    "/etc/ssl/ca-bundle.pem",              # SUSE
    "/etc/ssl/cert.pem",                   # Alpine, BSD, Homebrew OpenSSL
)

TLS_HINT = ("Fix: point SSL_CERT_FILE at a current CA bundle, "
            "or install certifi")

#: On claude.ai the code-execution sandbox blocks every domain except package
#: managers by default, so this skill's very first request fails. Left as a
#: bare connection error that reads as "Google is down", which sends the user
#: nowhere. Naming the actual cause turns a dead skill into a two-click fix.
BLOCKED_EGRESS_HINT = (
    "If you are on claude.ai: the code-execution sandbox blocks all domains "
    "except package managers by default. Enable Settings -> Capabilities -> "
    "Code execution -> network access for all domains (or allowlist "
    "www.google.com), then start a NEW chat — the setting does not apply to "
    "the conversation you are already in."
)


class Session:
    """A tiny stand-in for `requests.Session`.

    Holds the shared TLS context and default headers. `urllib` opens a fresh
    connection per request, so there is no pool to make thread-unsafe — the
    only shared state is the immutable context, which is why this is safe to
    hand to several worker threads at once.
    """

    def __init__(self, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout
        self._context: ssl.SSLContext | None = None
        self._lock = threading.Lock()

    def _ssl_context(self) -> ssl.SSLContext:
        with self._lock:
            if self._context is None:
                self._context = ssl.create_default_context()
            return self._context

    def _retry_context(self) -> ssl.SSLContext | None:
        """A context built from the OS bundle, for when the default is stale.

        A container's `ca-certificates` is usually newer than a pinned certifi,
        and needs nothing installed.
        """
        for path in OS_CA_BUNDLES:
            try:
                return ssl.create_default_context(cafile=path)
            except (OSError, ssl.SSLError):
                continue
        return None

    def get_text(self, url: str, params: dict | None = None,
                 raw_suffix: str = "") -> str:
        """GET and decode to text.

        `raw_suffix` is appended after any encoded params **verbatim**. Google's
        `pb` arguments must keep their literal `!` delimiters: percent-encoding
        them to `%21` makes Google answer with an empty result rather than an
        error, which reads as "nothing found" — a silent wrong answer, not a
        crash. So the pb never goes through urlencode.
        """
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        if raw_suffix:
            url = url + ("&" if "?" in url else "?") + raw_suffix

        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        })

        try:
            return self._open(request, self._ssl_context())
        except ssl.SSLError as exc:
            retry = self._retry_context()
            if retry is not None:
                try:
                    return self._open(request, retry)
                except (ssl.SSLError, urllib.error.URLError):
                    pass
            return self._curl(url, exc)
        except urllib.error.HTTPError as exc:
            raise NetworkError(
                f"{urllib.parse.urlparse(url).netloc} returned HTTP {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            host = urllib.parse.urlparse(url).netloc
            if isinstance(exc.reason, ssl.SSLError):
                return self._curl(url, exc)
            raise NetworkError(
                f"could not reach {host}: {exc.reason}. {BLOCKED_EGRESS_HINT}"
            ) from exc
        except OSError as exc:
            host = urllib.parse.urlparse(url).netloc
            raise NetworkError(
                f"could not reach {host}: {exc}. {BLOCKED_EGRESS_HINT}") from exc
        except Exception as exc:  # noqa: BLE001
            # Everything else the transport can raise, converted here so callers
            # have ONE exception type to handle. Not paranoia: a truncated body
            # raises http.client.IncompleteRead, which subclasses HTTPException
            # and ValueError but NOT OSError, so it sailed past every net above
            # and out through workers that catch only NetworkError — the failure
            # then vanished and the place was reported as having no hours.
            host = urllib.parse.urlparse(url).netloc
            raise NetworkError(f"{host} failed mid-transfer: "
                               f"{type(exc).__name__}: {exc}") from exc

    def _open(self, request: urllib.request.Request,
              context: ssl.SSLContext) -> str:
        with urllib.request.urlopen(request, timeout=self.timeout,
                                    context=context) as resp:
            body = resp.read()
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
        if encoding == "gzip":
            body = gzip.decompress(body)
        elif encoding == "deflate":
            body = zlib.decompress(body, -zlib.MAX_WBITS)
        return body.decode("utf-8", "replace")

    def _curl(self, url: str, cause: Exception) -> str:
        """Last resort: curl uses the OS trust store directly.

        Present on macOS, Linux and Windows 10+, but not in a minimal
        container — hence the explicit error rather than a traceback.
        """
        exe = shutil.which("curl")
        host = urllib.parse.urlparse(url).netloc
        if not exe:
            raise NetworkError(
                f"TLS verification failed for {host} and no curl is available "
                f"to fall back on. {TLS_HINT}"
            ) from cause
        proc = subprocess.run(
            [exe, "-sS", "--compressed", "-A", USER_AGENT,
             "-m", str(self.timeout), url],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise NetworkError(
                f"curl could not reach {host}: {proc.stderr.strip()}") from cause
        return proc.stdout

    def get_json(self, url: str, params: dict | None = None,
                 raw_suffix: str = "") -> Any:
        body = self.get_text(url, params, raw_suffix)
        try:
            return json.loads(body)
        except ValueError as exc:
            host = urllib.parse.urlparse(url).netloc
            raise NetworkError(
                f"{host} returned a non-JSON body ({len(body)} bytes)") from exc


def new_session(timeout: int = DEFAULT_TIMEOUT) -> Session:
    return Session(timeout)
