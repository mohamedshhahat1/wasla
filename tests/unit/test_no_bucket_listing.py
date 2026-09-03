"""Nothing can enumerate the bucket, so nothing can delete by absence of evidence.

The obvious way to find an object no row references is to list the store,
subtract the keys the database knows, and remove the rest. It is also unsafe in
a way that only shows up after it has destroyed something: a PostgreSQL failure,
a lagging replica, a query that timed out, a row this process could not read for
any reason at all - each one makes a live attachment look like an orphan, and
the rule says delete it. Deletion by *absence of evidence* cannot be made safe
(ADR-087).

So the guarantee is structural rather than a promise in a docstring: the storage
adapter has no operation that returns more than one object, and every key it can
be handed comes from a row that committed it.

These tests are what stops that being quietly reintroduced. A future
`list_objects` would fail here before it could grow a caller, and a sweep
written against it could not be built at all without first changing this file -
which is the point where somebody has to argue for it.
"""

from __future__ import annotations

import inspect
import io
import logging
import re
import tokenize
from pathlib import Path
from typing import Final, get_type_hints

from app.core.config import Settings
from app.core.logging import configure_logging
from app.core.object_store import S3MediaStorage
from app.core.storage import LocalMediaStorage, MediaStorage

APP = Path(__file__).parents[2] / "app"

# What a store can be asked to do, and there is no fifth thing. Reconciliation
# and retention both work from an exact key a row committed; neither has, or
# can have, a way to ask what else is in there.
EXPECTED_OPERATIONS: Final = frozenset({"put_at", "get", "delete", "exists"})

# The S3 spellings of "tell me what is in this bucket". `list-type=2` is
# ListObjectsV2; the bare `GET /{bucket}` with no key is V1; the others are the
# multipart and versioning enumerations.
LISTING_MARKERS: Final = (
    "list-type",
    "ListObjects",
    "list_objects",
    "ListBucket",
    "list_buckets",
    "ListMultipartUploads",
    "ListObjectVersions",
    "continuation-token",
)


def _code_only(path: Path) -> str:
    """The module with its comments and string literals removed.

    Because the honest way to explain why a 403 is ambiguous is to name
    `s3:ListBucket` in a comment, and a scanner that could not tell prose from
    an API call would force the code to stop explaining itself.
    """
    source = path.read_text(encoding="utf-8")
    kept: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        kept.append(token.string)
    return " ".join(kept)


def _protocol_operations(protocol: type) -> set[str]:
    return {
        name
        for name, value in vars(protocol).items()
        if not name.startswith("_") and callable(value)
    }


def test_the_storage_protocol_offers_no_way_to_enumerate() -> None:
    """Four operations, each about one object named by the caller."""
    assert _protocol_operations(MediaStorage) == EXPECTED_OPERATIONS


def test_every_storage_operation_takes_one_key() -> None:
    """A key, singular, and never a prefix or a pattern.

    This is what makes "the only objects Wasla can act on are the ones a row
    names" true by construction rather than by review: an operation taking a
    prefix would be a bucket scan whatever it was called.
    """
    for implementation in (LocalMediaStorage, S3MediaStorage):
        for operation in sorted(EXPECTED_OPERATIONS):
            hints = get_type_hints(getattr(implementation, operation))
            hints.pop("return", None)
            assert hints.get("key") is str, f"{implementation.__name__}.{operation}"
            assert "prefix" not in hints
            assert "pattern" not in hints


def test_neither_implementation_grew_a_listing_method() -> None:
    for implementation in (LocalMediaStorage, S3MediaStorage):
        public = {
            name
            for name, _ in inspect.getmembers(implementation, inspect.isfunction)
            if not name.startswith("_")
        }
        # `from_settings` is a constructor, not an operation on objects.
        public.discard("from_settings")
        assert public <= EXPECTED_OPERATIONS | {"root"}, implementation.__name__


def test_no_application_module_speaks_the_listing_api() -> None:
    """A grep, deliberately, and over the whole application rather than the adapter.

    The adapter is where a listing *should* live if one ever existed, which is
    exactly why checking only there would miss the version of this mistake that
    matters: a service reaching past the abstraction with its own signed
    request. `app/` is small enough that reading all of it costs nothing.
    """
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        source = _code_only(path)
        for marker in LISTING_MARKERS:
            if re.search(rf"\b{re.escape(marker)}\b", source):
                offenders.append(f"{path.relative_to(APP)}: {marker}")
    assert offenders == []


def test_the_object_store_sends_only_four_http_methods() -> None:
    """PUT, GET, DELETE, HEAD - and every one of them addresses a single key.

    A listing is a `GET` on the bucket rather than on an object, so the shape
    that would express one is a request built without a key. `_address` cannot
    produce that: it takes a key, and the key is refused unless it matches the
    pattern `build_key` produces.
    """
    source = inspect.getsource(S3MediaStorage)
    methods = set(re.findall(r'self\._request\(\s*"([A-Z]+)"', source))
    assert methods == {"PUT", "GET", "DELETE", "HEAD"}
    # No query string is ever built, and a listing needs one.
    assert "params=" not in _code_only(Path(inspect.getfile(S3MediaStorage)))


def test_an_object_key_cannot_reach_the_logs_through_the_http_client() -> None:
    """httpx logs every request URL at INFO, and an object URL *is* a key.

    Bucket, tenant identifier and object key, in one line, for every media
    read, write, HEAD and delete - which after ADR-087 includes the two
    requests reconciliation makes per interrupted upload. Wasla records its own
    outbound calls, so quieting this loses a duplicate and keeps a log that
    cannot tell a reader which workspace received which file.
    """
    configure_logging(
        Settings(
            _env_file=None,
            environment="test",
            log_format="console",
            log_level="INFO",
            cors_origins=[],
        )
    )

    assert logging.getLogger("httpx").level == logging.WARNING
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
