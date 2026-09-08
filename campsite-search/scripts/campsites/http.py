"""HTTP transport for the Camis reservation API.

TLS note: reservation.pc.gc.ca fails verification against older `certifi`
bundles even though the certificate is fine at the OS level. The fix order is:

1. `truststore` (uses the OS trust store — Keychain / Windows CA store). This is
   the correct fix and works on every platform. `pip install truststore`.
2. `curl`, which uses the OS trust store directly. Present on macOS, Linux, and
   Windows 10+ (as curl.exe), but *not* in a minimal container.
3. With no curl to shell out to, the system CA bundle at the usual distribution
   paths is retried once — a container's `ca-certificates` is normally newer
   than a pinned certifi, and needs no extra package. If that fails too, the
   error names the OpenSSL verdict and how to fix it, rather than a traceback.

If neither works the error is raised rather than downgrading to an unverified
connection — we never disable certificate checking.

Every Transport is per-host, so a provider whose TLS or DNS fails takes down
only its own commands; the other providers are untouched.
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import time
import urllib.parse
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

TIMEOUT = 60
RETRIES = 3

RETRYABLE_STATUS = ("429", "500", "502", "503", "504")

#: What curl's -w %{http_code} prints when there was never an HTTP response.
NO_STATUS = ("", "000")

#: curl exit codes worth another attempt: timeout, empty reply, recv/send
#: error, truncated transfer.
CURL_TRANSIENT = frozenset({0, 18, 28, 52, 55, 56})

TLS_HINT = (
    "Fix: pip install truststore (uses the OS trust store), or point "
    "REQUESTS_CA_BUNDLE / SSL_CERT_FILE at a current CA bundle"
)

#: curl exit codes that no amount of retrying will fix, and what they mean.
CURL_FATAL: dict[int, str] = {
    6: "could not resolve {host} — no DNS, or no network egress",
    7: "could not connect to {host} — network egress appears to be blocked",
    35: "TLS handshake with {host} failed. " + TLS_HINT,
    60: "{host}'s certificate could not be verified by curl either. " + TLS_HINT,
    77: "curl cannot read its CA certificate bundle. " + TLS_HINT,
}


#: Where distributions keep the system CA bundle. Tried when certifi's bundle
#: rejects a host, before falling back to curl.
OS_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Alpine
    "/etc/pki/tls/certs/ca-bundle.crt",    # RHEL, Fedora, Amazon Linux
    "/etc/ssl/ca-bundle.pem",              # SUSE
    "/etc/ssl/cert.pem",                   # Alpine, BSD, Homebrew OpenSSL
)


class CamisHTTPError(RuntimeError):
    pass


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


def _describe(exc: requests.exceptions.RequestException, host: str, timeout: int) -> str:
    """A one-line, actionable rendering of a requests failure.

    The raw text is a nest of urllib3 exceptions; sandboxes fail here often
    enough (no DNS, no egress, a MITM proxy) that the cause is worth naming.
    """
    # Match on the whole string but print only the head: the useful cause is
    # the innermost exception, which sits at the far end of a long message.
    text = str(exc)
    detail = text[:200]
    if isinstance(exc, requests.exceptions.ProxyError):
        return f"proxy error reaching {host} — check HTTPS_PROXY/NO_PROXY ({detail})"
    if isinstance(exc, requests.exceptions.Timeout):
        return f"{host} did not respond within {timeout}s"
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
    """A per-host JSON GET client with retries and a curl fallback."""

    def __init__(self, host: str, timeout: int = TIMEOUT):
        self.host = host
        self.timeout = timeout
        self._use_curl = False
        self._curl_bin: str | None = None  # resolved only if TLS actually fails
        self._tried_os_bundle = False
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "en-CA,en;q=0.9",
                "Referer": f"https://{host}/",
            }
        )
        retry = Retry(
            total=RETRIES,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        context = _ssl_context()
        adapter = (
            _TruststoreAdapter(context, max_retries=retry)
            if context
            else HTTPAdapter(max_retries=retry)
        )
        self._session.mount("https://", adapter)

    def _next_ca_bundle(self) -> str | None:
        """The OS CA bundle to retry with, once, or None if there is nothing new.

        A container's `ca-certificates` bundle is usually newer than a pinned
        `certifi`, so this rescues the very case where curl is also missing.
        Verification stays on — this swaps the trust anchors, it does not
        weaken them.
        """
        if self._tried_os_bundle:
            return None
        self._tried_os_bundle = True
        if os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE"):
            return None  # the caller already chose a bundle, and it failed
        return _os_ca_bundle()

    def get(self, path: str, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        while not self._use_curl:
            try:
                r = self._session.get(
                    f"https://{self.host}{path}", params=params, timeout=self.timeout
                )
                if r.status_code != 200:
                    raise CamisHTTPError(f"{path} -> HTTP {r.status_code}")
                return r.json()
            except ValueError as e:
                # Ahead of RequestException: a JSON decode failure is both.
                raise CamisHTTPError(f"{path} -> response was not JSON") from e
            except requests.exceptions.SSLError as e:
                self._curl_bin = self._curl_bin or shutil.which("curl")
                if self._curl_bin:
                    self._use_curl = True  # ends the loop; retry via curl
                    continue
                bundle = self._next_ca_bundle()
                if bundle:
                    self._session.verify = bundle
                    continue  # same request, system trust anchors this time
                tried = self._session.verify
                exhausted = (
                    f"the system CA bundle at {tried} did not verify it either"
                    if isinstance(tried, str)
                    else "no system CA bundle was found to try"
                )
                raise CamisHTTPError(
                    f"TLS verification failed for {self.host}: {_tls_reason(e)}. "
                    f"curl is not installed to fall back on and {exhausted}. "
                    f"{TLS_HINT}"
                ) from e
            except requests.exceptions.RequestException as e:
                raise CamisHTTPError(
                    f"{path} -> {_describe(e, self.host, self.timeout)}"
                ) from e
        return self._curl(path, params)

    def _curl(self, path: str, params: dict) -> Any:
        url = f"https://{self.host}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        last = ""
        for attempt in range(RETRIES):
            try:
                proc = subprocess.run(
                    [
                        self._curl_bin or "curl", "-sS", "--max-time", str(self.timeout),
                        "-A", USER_AGENT,
                        "-H", "Accept: application/json",
                        "-H", f"Referer: https://{self.host}/",
                        "-w", "\n%{http_code}",
                        url,
                    ],
                    capture_output=True,
                )
            except OSError as e:
                # curl vanished between the which() probe and now, or the
                # sandbox forbids spawning processes at all.
                raise CamisHTTPError(
                    f"TLS verification failed for {self.host} and the curl fallback "
                    f"could not be run ({type(e).__name__}: {e}). {TLS_HINT}"
                ) from e
            if proc.returncode in CURL_FATAL:
                reason = CURL_FATAL[proc.returncode].format(host=self.host)
                raise CamisHTTPError(f"{path} -> {reason}")
            body, _, status = proc.stdout.decode("utf-8", "replace").rpartition("\n")
            if status == "200":
                try:
                    return json.loads(body)
                except json.JSONDecodeError as e:
                    raise CamisHTTPError(
                        f"{path} -> non-JSON response: {body[:120]!r}"
                    ) from e
            if status in NO_STATUS:  # curl never got an HTTP response
                last = _curl_failure(proc)
                if proc.returncode not in CURL_TRANSIENT:
                    break
            else:
                last = f"HTTP {status}"
                if status not in RETRYABLE_STATUS:
                    break
            time.sleep(2**attempt)
        raise CamisHTTPError(f"{path} -> {last}")


def _curl_failure(proc: subprocess.CompletedProcess) -> str:
    """Describe a curl run that produced no HTTP status line."""
    err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
    detail = err[-1][:160] if err else "no response"
    return f"{detail} (curl exit {proc.returncode})"
