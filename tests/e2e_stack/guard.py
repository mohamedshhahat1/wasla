"""Nothing in the e2e stack reaches a real provider (FE-0, E2E-03, E2E-08).

Two mechanisms, installed by the launcher in the api and worker processes and by
the fakes service in its own:

- **A socket wall.** Every DNS lookup and connect is refused unless it names
  loopback, a private address, or one of this stack's service names. It covers
  the clients that ignore `HTTP(S)_PROXY` too, which the application's guarded
  clients do by design (they carry an explicit transport).
- **A reroute.** Every request the application sends through
  `app.core.net.GuardedTransport` - the one transport every provider client is
  built with - is rewritten to the fakes service as
  ``{WASLA_E2E_FAKES_URL}/_host/<original host><path>``. The original host
  travels in the path, so the fakes service answers as Meta, OpenAI, Paymob or
  Google by name; nothing in ``app/`` changes, and nothing is exempted from the
  guard except the fakes service itself, inside this process only. A provider
  host that the application resolves *before* a request, to judge it, resolves
  to a placeholder global address that nothing ever connects to.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any

SERVICE_NAMES = frozenset({"pg", "redis", "api", "worker", "fakes", "mailbox", "migrate"})
_ALLOWED_NAMES = frozenset({"localhost", "", "host.docker.internal"}) | SERVICE_NAMES
REFUSED_LOG = os.environ.get("WASLA_E2E_REFUSED_LOG", "/mailbox/refused.log")
PROVIDER_PLACEHOLDER_ADDRESS = "192.88.99.1"


def _local_address(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text.split("%")[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_unspecified


def allowed(host: object) -> bool:
    """Whether this stack may resolve or connect to `host`."""
    if isinstance(host, bytes):
        host = host.decode()
    if host is None:
        return True
    text = str(host)
    return text in _ALLOWED_NAMES or text.startswith("wasla-fe-e2e-") or _local_address(text)


def _note(what: str) -> None:
    try:
        with open(REFUSED_LOG, "a", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()} refused {what}\n")
    except OSError:
        pass


def install_socket_wall() -> None:
    """Refuse every lookup and connect outside the stack, once per process."""
    if getattr(socket, "_wasla_e2e_wall", False):
        return
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not allowed(host):
            _note(f"dns {host}")
            raise socket.gaierror(socket.EAI_NONAME, f"e2e wall: lookup of {host} refused")
        return real_getaddrinfo(host, *args, **kwargs)

    def _check(address: Any) -> None:
        if isinstance(address, tuple) and address and not allowed(address[0]):
            _note(f"connect {address}")
            raise ConnectionRefusedError(f"e2e wall: connect to {address} refused")

    def connect(self: socket.socket, address: Any) -> None:
        _check(address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        _check(address)
        return real_connect_ex(self, address)

    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect  # type: ignore[assignment]
    socket.socket.connect_ex = connect_ex  # type: ignore[assignment]
    socket._wasla_e2e_wall = True  # type: ignore[attr-defined]


def install_reroute() -> None:
    """Send every guarded provider request to the fakes service instead."""
    import httpx

    from app.core import net

    fakes = httpx.URL(os.environ.get("WASLA_E2E_FAKES_URL", "http://fakes:8100"))
    plain = httpx.AsyncHTTPTransport()

    async def handle(self: Any, request: httpx.Request) -> httpx.Response:
        original = request.url
        target = fakes.copy_with(
            path=f"/_host/{original.host}{original.path}", query=original.query or None
        )
        headers = httpx.Headers(request.headers)
        headers["host"] = f"{fakes.host}:{fakes.port}" if fakes.port else str(fakes.host)
        headers["x-e2e-original-url"] = str(original)
        rerouted = httpx.Request(
            request.method,
            target,
            headers=headers,
            content=await request.aread(),
            extensions=request.extensions,
        )
        return await plain.handle_async_request(rerouted)

    net.GuardedTransport.handle_async_request = handle  # type: ignore[method-assign]

    real_resolver = net.system_resolver

    def resolver(host: str, port: int) -> list[str]:
        # A provider URL is judged by resolving its host before any request
        # (Meta's media URLs are). The wall refuses that lookup, so a provider
        # name resolves here to one fixed global address - 192.88.99.1, the
        # retired 6to4 relay anycast (RFC 7526). Nothing ever connects to it:
        # the request itself is rerouted above, and the wall refuses any
        # connect outside the stack.
        if allowed(host):
            return real_resolver(host, port)
        return [PROVIDER_PLACEHOLDER_ADDRESS]

    net.system_resolver = resolver
