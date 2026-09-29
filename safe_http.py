"""Outbound HTTP for URLs that come from users (company websites, a custom AI
endpoint), safe to run on a shared server.

On a server, "fetch this company's website" with a website a user typed is a
server-side request forgery risk: http://169.254.169.254/ returns the cloud
host's credentials, http://127.0.0.1:5432 probes the database. So:

  * only http/https, no user:password@ in the URL;
  * every address the name resolves to must be public — and the check runs
    again on the socket actually connected (a DNS answer that changes between
    the check and the connection, "DNS rebinding", is caught there);
  * redirects are followed by hand, each hop re-validated;
  * the body is capped, so a huge or endless response can't exhaust memory;
  * environment proxies are ignored (they would hide the real peer).

Local development can allow private addresses (ALLOW_PRIVATE_URLS, e.g. a
local Ollama); production never does.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

import config

MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 2 * 1024 * 1024


class BlockedURL(requests.RequestException):
    """The URL points somewhere the server must not connect to."""


def ip_allowed(ip: str) -> bool:
    if config.allow_private_urls():
        return True
    try:
        address = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def check_url(url: str) -> str:
    """Raise BlockedURL unless `url` is an http(s) URL whose host resolves
    only to public addresses. Returns the URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise BlockedURL(f"Only http(s) addresses can be fetched: {url[:80]}")
    if not parsed.hostname:
        raise BlockedURL("The address has no host name.")
    if parsed.username or parsed.password:
        raise BlockedURL("Addresses with a user name or password aren't allowed.")
    if config.allow_private_urls():
        return url
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise BlockedURL("The address has an invalid port.") from exc
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise requests.ConnectionError(f"Could not resolve {parsed.hostname}") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses or not all(ip_allowed(ip) for ip in addresses):
        raise BlockedURL(f"{parsed.hostname} points to a private or reserved network address.")
    return url


def check_host(host: str, port: int) -> None:
    """The same guard for a mail server a user typed (SMTP/IMAP host)."""
    if not host or len(host) > 253 or any(c in host for c in "/@: \t"):
        raise BlockedURL("Enter a server name like smtp.example.com.")
    if config.allow_private_urls():
        return
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise BlockedURL(f"Could not resolve {host}.") from exc
    if not all(ip_allowed(info[4][0]) for info in infos):
        raise BlockedURL(f"{host} points to a private or reserved network address.")


class _PeerCheck:
    def connect(self):
        super().connect()
        try:
            peer = self.sock.getpeername()[0]
        except OSError as exc:
            self.close()
            raise BlockedURL("Could not verify the connected address.") from exc
        if not ip_allowed(peer):
            self.close()
            raise BlockedURL("The server resolved to a private or reserved address.")


class _GuardedHTTPConnection(_PeerCheck, HTTPConnection):
    pass


class _GuardedHTTPSConnection(_PeerCheck, HTTPSConnection):
    pass


class _GuardedHTTPPool(HTTPConnectionPool):
    ConnectionCls = _GuardedHTTPConnection


class _GuardedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPSConnection


class _GuardedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _GuardedHTTPPool,
                                                   "https": _GuardedHTTPSPool}


def session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    adapter = _GuardedAdapter(pool_connections=20, pool_maxsize=50)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


_shared = None


def _session() -> requests.Session:
    global _shared
    if _shared is None:
        _shared = session()
    return _shared


def get(url: str, *, headers: dict | None = None, timeout: float = 10,
        max_bytes: int = DEFAULT_MAX_BYTES) -> requests.Response:
    """GET with the protections above. Raises BlockedURL or a requests error."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        check_url(current)
        response = _session().get(current, headers=headers, timeout=timeout,
                                  allow_redirects=False, stream=True)
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("location", "")
            response.close()
            if not location:
                raise requests.HTTPError("Redirect without a location.")
            current = urljoin(current, location)
            continue
        chunks, size = [], 0
        try:
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > max_bytes:
                    chunks.append(chunk[: max(0, max_bytes - (size - len(chunk)))])
                    break
                chunks.append(chunk)
        finally:
            response.close()
        response._content = b"".join(chunks)
        response._content_consumed = True
        return response
    raise requests.TooManyRedirects(f"More than {MAX_REDIRECTS} redirects.")
