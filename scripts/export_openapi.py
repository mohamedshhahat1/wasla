"""Export the OpenAPI document the frontend generates its client from (FEB-02, ADR-191).

Production serves no document (`DOCS_ENABLED` must be false outside local and
test), so the frontend's typed client is generated from a committed file
instead. This writes it:

    python -m scripts.export_openapi              write both files
    python -m scripts.export_openapi --check      write nothing; exit 1 if either is stale
    python -m scripts.export_openapi --output-dir <dir>

into ``frontend/packages/api-client/``:

- ``openapi.json``       the document, with the error envelope declared on every
                         operation (`app/api/openapi.py`)
- ``event-types.json``   the event stream's vocabulary (`app/core/events.py`)

It builds the application in-process with the factory the server uses and reads
the document off it. No server, database, Redis, network or secret is needed or
touched: settings are built from fixed values with the process environment set
aside, so the output depends on the code alone. The route set is the one
production serves - routes do not depend on settings here, and
`tests/unit/test_openapi_export.py` proves the document's operations equal a
production-shaped application's routes, less the ones that exclude themselves
from the schema.

Output is ``json.dumps(..., sort_keys=True, indent=2, ensure_ascii=False)`` plus
a newline, UTF-8, LF: two runs are byte-identical, on Windows and on Linux.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "frontend" / "packages" / "api-client"
OPENAPI_FILE = "openapi.json"
EVENT_TYPES_FILE = "event-types.json"
REGENERATE = "run python -m scripts.export_openapi"


def _settings() -> Any:
    """Settings from fixed values only: the environment and `.env` play no part."""
    from app.core.config import Settings

    with mock.patch.dict(os.environ, {"ENVIRONMENT": "test"}, clear=True):
        return Settings(_env_file=None, environment="test", docs_enabled=True)


def build_document() -> dict[str, Any]:
    """The document, built from the application factory."""
    from app.main import create_app

    document: dict[str, Any] = create_app(_settings()).openapi()
    return document


def build_event_types() -> dict[str, Any]:
    """The stream's event types and close reasons, from the code that sends them."""
    from app.core.events import CLOSED, EVENT_TYPES, RESYNC
    from app.core.telemetry import BILLING_OUTCOMES

    return {
        "event_types": sorted(EVENT_TYPES),
        # The only closed list of close reasons is the label domain of the
        # counter every close increments (`app/core/telemetry.py`).
        "close_reasons": sorted(BILLING_OUTCOMES["wasla_events_stream_closed_total"]),
        "stream_frames": sorted({CLOSED, RESYNC}),
    }


def render(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def build_files() -> dict[str, str]:
    """Every exported file's name and exact text."""
    return {
        OPENAPI_FILE: render(build_document()),
        EVENT_TYPES_FILE: render(build_event_types()),
    }


def stale_files(directory: Path) -> list[str]:
    """The exported files whose committed bytes differ from what the code produces."""
    stale = []
    for name, text in build_files().items():
        path = directory / name
        if not path.exists() or path.read_bytes() != text.encode("utf-8"):
            stale.append(name)
    return stale


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="write nothing; fail if stale")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    options = _parse(argv)
    if not os.environ.get("ENVIRONMENT"):
        # Importing `app.main` builds its module-level application from the
        # process settings; `test` keeps that from asking for production secrets.
        os.environ["ENVIRONMENT"] = "test"
    directory: Path = options.output_dir
    if options.check:
        stale = stale_files(directory)
        if stale:
            print(f"{', '.join(stale)} out of date: {REGENERATE}", file=sys.stderr)  # noqa: T201
            return 1
        return 0
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in build_files().items():
        (directory / name).write_bytes(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
