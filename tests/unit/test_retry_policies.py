"""Every external client bounds its attempts, and the send path bounds itself at one.

The retry policies were built with the clients themselves rather than as a
hardening pass, so this file is a guard rather than new behaviour: an
integration added later must not ship without one, and — more importantly — must
not quietly acquire one where a retry would duplicate something a customer can
see.

What is *not* asserted here is the send path's asymmetry - that a connect error
is retried because nothing reached Meta, while a timeout or a 5xx is not because
the message may already have been delivered. That is behaviour, it is covered by
counting HTTP calls in `test_whatsapp_client.py`, and asserting it a second time
by reading the source would break on a refactor without a defect behind it.
"""

from __future__ import annotations

import pytest

from app.integrations.openai import client as responses_client
from app.integrations.openai import embeddings as embeddings_client
from app.integrations.openai import transcription as transcription_client
from app.integrations.whatsapp import client as whatsapp_client

# Every module that talks to somebody else's server.
EXTERNAL_CLIENTS = (
    ("whatsapp", whatsapp_client),
    ("responses", responses_client),
    ("transcription", transcription_client),
    ("embeddings", embeddings_client),
)


@pytest.mark.parametrize(("name", "module"), EXTERNAL_CLIENTS)
def test_every_external_client_bounds_its_attempts(name: str, module: object):
    """An unbounded retry against a provider having an outage is a way of
    turning their bad day into a queue that never drains."""
    attempts = getattr(module, "MAX_ATTEMPTS", None)
    assert isinstance(attempts, int), f"{name} declares no MAX_ATTEMPTS"
    assert 1 <= attempts <= 5, f"{name} retries {attempts} times, which is not a policy"


@pytest.mark.parametrize(("name", "module"), EXTERNAL_CLIENTS)
def test_every_external_client_waits_between_attempts(name: str, module: object):
    """Retrying immediately is not a retry policy, it is the same failure
    three times in a row and three times the load on something struggling."""
    backoff = getattr(module, "BACKOFF_SECONDS", None)
    assert isinstance(backoff, int | float), f"{name} declares no BACKOFF_SECONDS"
    assert backoff > 0


# The two modules that actually build an HTTP client. The transcription and
# embeddings clients take one that is handed to them - the media worker and the
# agent worker pass the OpenAI client's - so the timeout is declared once, where
# the client is constructed, rather than repeated on every class that borrows it.
CLIENT_FACTORIES = (
    ("whatsapp", whatsapp_client),
    ("openai", responses_client),
)


@pytest.mark.parametrize(("name", "module"), CLIENT_FACTORIES)
def test_every_client_factory_bounds_its_timeout(name: str, module: object):
    """A request with no timeout can hold a worker forever, which is the same
    outage as a crash and much harder to see."""
    timeout = getattr(module, "REQUEST_TIMEOUT_SECONDS", None) or getattr(
        module, "DEFAULT_TIMEOUT_SECONDS", None
    )
    assert isinstance(timeout, int | float), f"{name} declares no request timeout"
    assert timeout > 0
