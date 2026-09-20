"""Release inputs must stay pinned and verified when the image is built."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_release_builder_installs_only_hashed_dependencies() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert (
        "pip install --require-hashes -r requirements.lock -r requirements-build.lock" in dockerfile
    )
    assert "pip install --no-deps --no-build-isolation ." in dockerfile
    for name in ("requirements.lock", "requirements-build.lock"):
        lock = (ROOT / name).read_text(encoding="utf-8")
        assert re.search(r"(?m)^[a-z][a-z0-9-]*==[^\s]+ \\\n+    --hash=sha256:[0-9a-f]{64}", lock)


def test_production_infrastructure_images_are_digest_pinned() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
    for name in ("postgres", "redis", "nginx", "prometheus", "alertmanager"):
        image = compose["services"][name]["image"]
        assert re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", image), name
    for name in ("Dockerfile", "Dockerfile.backup"):
        dockerfile = (ROOT / name).read_text(encoding="utf-8")
        assert re.search(r"(?m)^FROM [^\s]+@sha256:[0-9a-f]{64}", dockerfile), name


def test_vulnerability_scanners_still_gate_release() -> None:
    security = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
    )
    dependency_steps = security["jobs"]["dependencies"]["steps"]
    assert any("pip-audit" in step.get("run", "") for step in dependency_steps)
    image_steps = security["jobs"]["image"]["steps"]
    assert any("trivy-action" in step.get("uses", "") for step in image_steps)
