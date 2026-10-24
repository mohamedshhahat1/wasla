"""The API process: uvicorn, with every event stream closed first on exit (FRM-03).

`scripts/entrypoint.sh api` runs `python -m app.serve`. Uvicorn's graceful
shutdown waits for open connections to finish before it runs the lifespan's
shutdown, and an event stream never finishes on its own: each must be told to
say `closed` (`server_restart`) and end the moment the exit signal arrives.

That has to be the handler uvicorn registers. `Server.serve` registers the
bound `self.handle_exit` for SIGINT and SIGTERM *before* it imports the
application, so nothing the application does at import time can change which
handler runs; the handler belongs to the server this module builds.

Plain `uvicorn app.main:app` still serves, but cuts open streams at shutdown
instead of closing them, and says so at start-up (`events.shutdown_hook
active=false`).
"""

from __future__ import annotations

import argparse
import logging
import signal
from collections.abc import Sequence
from types import FrameType
from typing import NoReturn

import uvicorn
from uvicorn.main import STARTUP_FAILURE
from uvicorn.supervisors import ChangeReload, Multiprocess

from app.services.realtime import close_every_stream

APP = "app.main:app"

logger = logging.getLogger("uvicorn.error")


class StreamClosingServer(uvicorn.Server):
    """A uvicorn server whose exit signal closes this process's event streams first."""

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        close_every_stream("server_restart")
        super().handle_exit(sig, frame)


def stream_shutdown_active() -> bool:
    """Whether SIGTERM, in this process, is handled by a `StreamClosingServer`."""
    handler = signal.getsignal(signal.SIGTERM)
    return isinstance(getattr(handler, "__self__", None), StreamClosingServer)


def _exit_cleanly(sig: int, frame: FrameType | None) -> NoReturn:
    raise SystemExit(0)


def parse(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.serve")
    # Uvicorn's own defaults; the entrypoint passes both.
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--timeout-graceful-shutdown", type=int, default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--reload", action="store_true")
    return parser.parse_args(argv)


def config_from(options: argparse.Namespace) -> uvicorn.Config:
    """The `Config` the entrypoint's uvicorn command line used to build."""
    return uvicorn.Config(
        APP,
        host=options.host,
        port=options.port,
        # Wasla owns the only forwarding-header policy. Uvicorn must not
        # rewrite request.client before `client_identity` can verify the
        # immediate peer against TRUSTED_PROXY_IPS; two trust systems with
        # different allowlists turn a forged X-Forwarded-For value into a
        # socket address.
        proxy_headers=False,
        timeout_graceful_shutdown=options.timeout_graceful_shutdown,
        workers=options.workers,
        reload=options.reload,
    )


def main(argv: Sequence[str] | None = None) -> None:
    config = config_from(parse(argv))
    server = StreamClosingServer(config)
    logger.info(
        "serve.config host=%s port=%s proxy_headers=%s timeout_graceful_shutdown=%s "
        "workers=%s reload=%s",
        config.host,
        config.port,
        config.proxy_headers,
        config.timeout_graceful_shutdown,
        config.workers,
        config.reload,
    )
    # Once it has shut down, uvicorn restores the handler it found and raises
    # the signal again. The default SIGTERM action would end the process as
    # killed by it - unless it is PID 1, as in the container: exit 0 either way.
    signal.signal(signal.SIGTERM, _exit_cleanly)
    try:
        if config.should_reload:
            ChangeReload(config, target=server.run, sockets=[config.bind_socket()]).run()
        elif config.workers > 1:
            Multiprocess(config, target=server.run, sockets=[config.bind_socket()]).run()
        else:
            server.run()
    except KeyboardInterrupt:
        pass
    if not server.started and not config.should_reload and config.workers == 1:
        raise SystemExit(STARTUP_FAILURE)


if __name__ == "__main__":
    # The server class must be `app.serve`'s, not this `__main__` copy of it:
    # `stream_shutdown_active` (and so the start-up line) asks for that one.
    from app.serve import main as serve

    serve()
