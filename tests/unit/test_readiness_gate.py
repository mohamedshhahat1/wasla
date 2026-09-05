"""The last gate of every deployment, executed rather than read.

F-8. The check was `curl -fsS --max-time 10 http://127.0.0.1/health/ready`, and
port 80 is nginx, whose only non-ACME rule is a 301 to HTTPS. `curl -f` fails on
4xx and 5xx; a 301 without `-L` exits **zero**. So the step passed whenever
nginx was running - regardless of whether the API was up, the database was
reachable, or the migrations had applied. `up -d --wait` does not cover that
either: it waits on liveness, which is deliberately independent of PostgreSQL
and Redis, so a running API with a dead database satisfies both gates.

The test that certified this asserted `"/health/ready" in steps` - a substring
search over the workflow's YAML text. It proved the string was present. It could
not have failed for any behaviour of the command, which is why a command that
cannot fail passed it.

So these tests run **the script the deployment runs**, against a real socket
serving each shape a broken deployment actually produces. The matrix at the
bottom is the finding's own list, executed.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_readiness.sh"
SHELL = shutil.which("sh") or shutil.which("bash")

# What the application answers when every dependency it needs is reachable.
READY = {
    "status": "ok",
    "components": [{"name": "postgresql", "status": "ok", "duration_ms": 1.0, "detail": None}],
}
# What it answers when one is not - with a 503, which is the pairing that
# matters: the status and the body agree, and either alone would be enough.
DEGRADED = {
    "status": "degraded",
    "components": [
        {"name": "postgresql", "status": "error", "duration_ms": 1.0, "detail": "refused"}
    ],
}


Responder = Callable[[BaseHTTPRequestHandler], None]


def _serve(responder: Responder) -> Iterator[str]:
    """A real HTTP server on a real port, answering however the test says.

    A real socket rather than a mocked `curl`, because what is being tested is
    what `curl` does with a status code - and a double that decided that would
    be asserting the assumption the finding disproved.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            responder(self)

        def log_message(self, *args: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/health/ready"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _json(handler: BaseHTTPRequestHandler, status: int, body: object) -> None:
    encoded = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def _run(url: str) -> subprocess.CompletedProcess[str]:
    assert SHELL is not None
    return subprocess.run(  # noqa: S603 - a fixed script path, no shell string
        [SHELL, str(SCRIPT), url],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture(autouse=True)
def _needs_a_shell_and_curl() -> None:
    if SHELL is None or shutil.which("curl") is None:  # pragma: no cover - CI has both
        pytest.skip("The readiness gate is a shell script; it needs sh and curl to run.")


def _readiness_run() -> str:
    """The deploy job's readiness step, read from the parsed workflow.

    Parsed rather than sliced out of the text, so the explanatory comment above
    the step - which quotes the old, broken command on purpose - is not mistaken
    for the command itself. That is a small thing and it is exactly the class of
    mistake F-8 was: a test reading prose and reporting it as behaviour.
    """
    document = yaml.safe_load(
        (SCRIPT.parents[1] / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    )
    steps = document["jobs"]["deploy"]["steps"]
    named = [step for step in steps if step.get("name") == "Check what is now serving"]
    assert len(named) == 1, "the deploy job has no readiness step"
    command = named[0]["run"]
    assert isinstance(command, str)
    return command


def test_the_script_the_deployment_runs_exists_and_is_the_one_under_test() -> None:
    """Non-vacuity. Every test below is a subprocess, and a missing file is an
    error the runner reports as a failure that looks like a behaviour.

    Asserted first, and with the workflow's reference to it, so a script renamed
    on one side fails here rather than leaving the pipeline running something
    this file has never seen.
    """
    assert SCRIPT.is_file()
    assert "check_readiness.sh" in _readiness_run()


# ------------------------------------------------------- the failure shapes


def test_an_nginx_style_redirect_fails_the_gate() -> None:
    """The finding itself: `curl -f` exits zero on a 301.

    This is what port 80 answers on the deployed host, so before the fix this
    was the *entire* content of the final deployment gate - a proxy declining to
    answer, read as an application reporting itself ready.
    """

    def redirect(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(301)
        handler.send_header("Location", "https://example.test/health/ready")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    for url in _serve(redirect):
        result = _run(url)

    assert result.returncode != 0
    assert "301" in result.stderr


def test_a_redirect_carrying_a_ready_body_still_fails_the_gate() -> None:
    """The status and the body are two rules, and this is what separates them.

    Without it, deleting the status check entirely leaves every other test
    green: nginx's 301 has an empty body and a 503 says `degraded`, so the body
    rule alone catches both. A redirect whose body happens to look like the
    application - a proxy replaying a cached page, a captive portal, an ingress
    with a custom error document - passes the body rule and is still not the
    application answering.

    That mutation is exactly what F-8 was, one layer along: a check that looks
    like two conditions but is really one.
    """

    def redirect_with_body(handler: BaseHTTPRequestHandler) -> None:
        encoded = json.dumps(READY).encode("utf-8")
        handler.send_response(301)
        handler.send_header("Location", "https://example.test/health/ready")
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(encoded)))
        handler.end_headers()
        handler.wfile.write(encoded)

    for url in _serve(redirect_with_body):
        result = _run(url)

    assert result.returncode != 0
    assert "expected a 2xx, got 301" in result.stderr


def test_a_degraded_readiness_response_fails_the_gate() -> None:
    """The database is unreachable, so the release does not work.

    503 with a `degraded` body - both halves of what `/health/ready` really
    answers, so this fails on the status rule and would fail on the body rule
    too.
    """
    for url in _serve(lambda handler: _json(handler, 503, DEGRADED)):
        result = _run(url)

    assert result.returncode != 0
    assert "503" in result.stderr


def test_a_refused_connection_fails_the_gate() -> None:
    """The API is not listening at all.

    A port that nothing is on, obtained by binding one and closing it, so this
    is a real refusal rather than a hostname that does not resolve.
    """
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    result = _run(f"http://127.0.0.1:{port}/health/ready")

    assert result.returncode != 0
    assert "could not reach" in result.stderr


def test_a_200_that_is_not_this_endpoint_fails_the_gate() -> None:
    """A maintenance page, a stale container, a proxy serving a cached root.

    The reason the exit code alone is not the property: this satisfies every
    rule about status codes and is not the application saying it can serve.
    """

    def wrong_page(handler: BaseHTTPRequestHandler) -> None:
        body = b"<html><body>Service temporarily unavailable</body></html>"
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    for url in _serve(wrong_page):
        result = _run(url)

    assert result.returncode != 0
    assert "did not report ready" in result.stderr


def test_a_200_reporting_degraded_fails_the_gate() -> None:
    """The nastier version of the case above: the right endpoint, wrong answer.

    A body-only check keyed on the endpoint being reachable would pass this. It
    exists because a future change could return 200 with a degraded body - the
    status and the body are separate assertions on purpose.
    """
    for url in _serve(lambda handler: _json(handler, 200, DEGRADED)):
        result = _run(url)

    assert result.returncode != 0
    assert "did not report ready" in result.stderr


# ------------------------------------------------------------ the pass case


def test_a_ready_application_passes_the_gate() -> None:
    """The positive control, and the reason every refusal above means something.

    A gate that failed on everything would satisfy all five tests before this
    one, and would also stop every deployment - which is a different way of
    being useless.
    """
    for url in _serve(lambda handler: _json(handler, 200, READY)):
        result = _run(url)

    assert result.returncode == 0, result.stderr
    assert "reported ready" in result.stdout


def test_whitespace_in_the_body_does_not_decide_a_deployment() -> None:
    """Pretty-printed JSON is the same answer.

    The check normalises before matching, so a serialiser that adds spaces
    after colons - or a proxy that reformats - does not fail a release that
    works. Written down because "the gate broke when the body gained a space"
    is the kind of thing that gets a gate deleted rather than fixed.
    """

    def spaced(handler: BaseHTTPRequestHandler) -> None:
        encoded = json.dumps(READY, indent=2).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(encoded)))
        handler.end_headers()
        handler.wfile.write(encoded)

    for url in _serve(spaced):
        result = _run(url)

    assert result.returncode == 0, result.stderr


def test_the_gate_gives_up_rather_than_hanging_a_deployment() -> None:
    """A server that accepts and never answers must not hold the pipeline open.

    `--max-time` is what makes the failure a failure rather than a job that
    runs until GitHub cancels it, and the timeout is short here so the test is
    a second rather than the ten a deployment allows.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    try:
        assert SHELL is not None
        result = subprocess.run(  # noqa: S603 - a fixed script path, no shell string
            [SHELL, str(SCRIPT), f"http://127.0.0.1:{port}/health/ready"],
            capture_output=True,
            text=True,
            timeout=60,
            env={"READINESS_TIMEOUT_SECONDS": "1", "PATH": _path()},
        )
    finally:
        listener.close()

    assert result.returncode != 0
    assert "could not reach" in result.stderr


def _path() -> str:
    import os

    return os.environ.get("PATH", "")


# ------------------------------------------------------- and in the pipeline


def test_the_deployment_queries_the_application_and_not_the_proxy() -> None:
    """The gate has to run where the application is, not where nginx is.

    Port 80 on the host is the proxy. Reaching the API through
    `docker compose exec` puts the request inside the compose network on the
    application's own port, so nothing in the answer is a proxy's opinion - and
    it is the one form that cannot be satisfied by a redirect, because there is
    nothing in between to redirect.
    """
    step = _readiness_run()

    assert "exec -T api" in step
    assert "check_readiness.sh" in step
    assert "http://127.0.0.1/health/ready" not in step, "that is nginx, and it answers 301"
