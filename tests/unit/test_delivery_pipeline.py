"""The delivery pipeline's invariants, read from the workflows themselves.

Workflow YAML is the one part of this project nothing else checks: it is not
imported, not typed, and a mistake in it is found by a broken release rather
than by a failing build. These tests are cheap and they pin the handful of
properties that would be expensive to get wrong.

They deliberately assert *rules*, not contents. "The deploy workflow refuses a
failed CI run" survives renaming a step; "step 4 is called Build and push" does
not, and a test that breaks on every edit is one somebody deletes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _load(name: str) -> dict:
    with (WORKFLOWS / name).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _steps(workflow: dict, job: str) -> list[dict]:
    return workflow["jobs"][job]["steps"]


def _text(value: object) -> str:
    return yaml.safe_dump(value)


@pytest.mark.parametrize("name", ["ci.yml", "security.yml", "deploy.yml"])
def test_every_workflow_parses(name: str):
    """A workflow that does not parse does not run, and GitHub reports that
    quietly enough to miss."""
    workflow = _load(name)

    assert workflow["jobs"], f"{name} declares no jobs"


@pytest.mark.parametrize("name", ["ci.yml", "security.yml", "deploy.yml"])
def test_every_workflow_declares_its_permissions(name: str):
    """The default token is broad. Every workflow narrows it, so a compromised
    action cannot do more than the workflow needs."""
    workflow = _load(name)

    assert "permissions" in workflow, f"{name} does not narrow its token"


def test_deployment_waits_for_ci_rather_than_repeating_it():
    """ "Do not deploy if tests fail" as a dependency, not a second copy of the
    test job that could drift from the first."""
    deploy = _load("deploy.yml")
    # `on` is parsed by PyYAML as the boolean True.
    triggers = deploy[True]

    assert "workflow_run" in triggers
    assert triggers["workflow_run"]["workflows"] == ["CI"]
    assert triggers["workflow_run"]["branches"] == ["main"]


def test_a_failed_ci_run_publishes_nothing():
    condition = _load("deploy.yml")["jobs"]["publish"]["if"]

    assert "conclusion == 'success'" in condition


def test_publishing_checks_out_the_commit_ci_verified():
    """Between CI finishing and this starting, main may have moved. Publishing
    the branch head would ship a commit no test ever saw."""
    checkout = next(
        step
        for step in _steps(_load("deploy.yml"), "publish")
        if str(step.get("uses", "")).startswith("actions/checkout")
    )

    assert "workflow_run.head_sha" in checkout["with"]["ref"]


def test_the_deployed_image_is_named_by_digest():
    """A tag can be moved; a digest is the image that was built and scanned."""
    publish = _load("deploy.yml")["jobs"]["publish"]

    assert "@${{ steps.build.outputs.digest }}" in _text(publish) or "DIGEST" in _text(publish)
    assert "digest" in publish["outputs"]["image"] or "reference" in _text(publish["outputs"])


def test_the_published_image_is_scanned():
    steps = _text(_steps(_load("deploy.yml"), "publish"))

    assert "trivy-action" in steps


def test_a_deployment_without_a_target_fails_rather_than_pretending():
    """A workflow that "succeeds" without touching a server is worse than one
    that fails: the green tick is read as "it shipped"."""
    steps = _text(_steps(_load("deploy.yml"), "deploy"))

    assert "No deployment target configured" in steps
    assert "exit 1" in steps


def test_the_deploy_host_key_is_pinned():
    """Trusting an unknown host on first connection is how a deploy ends up
    talking to somebody else's server."""
    steps = _text(_steps(_load("deploy.yml"), "deploy"))

    assert "known_hosts" in steps
    assert "StrictHostKeyChecking=no" not in steps


def test_migrations_run_before_the_new_version_serves():
    steps = _text(_steps(_load("deploy.yml"), "deploy"))
    migrate = steps.index("run --rm migrate")
    start = steps.index("up -d --wait")

    assert migrate < start, "the new version would serve against an unmigrated schema"


def test_the_deployment_is_verified_rather_than_assumed():
    """A deployment is finished when the thing it started answers, not when the
    command that started it exits zero."""
    steps = _text(_steps(_load("deploy.yml"), "deploy"))

    assert "/health/ready" in steps


# A literal that is unmistakably a throwaway. CI needs *a* JWT secret to start
# the application with, and generating one per run would make a failing build
# harder to reproduce. It is tolerable only because it says what it is.
CI_ONLY_VALUES = ("continuous-integration-secret-value-not-for-deployment",)


def test_the_deployment_workflows_write_no_credential_literally():
    """Every credential on the path to a server is a reference to a secret. A
    literal would be in the git history for good.

    Scoped to the workflows that reach outside the runner rather than applied to
    all three: CI legitimately hardcodes a throwaway to boot the application
    with, and a rule that forbade that outright would be worked around instead
    of obeyed. The value it uses is pinned by the test below.
    """
    for name in ("deploy.yml", "security.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            assigned, _, value = stripped.partition("=")
            # A bare NAME=value, optionally introduced by `export` or docker's
            # `-e`. Anything else on the line is shell logic rather than an
            # assignment, and reading it as one produces noise.
            assigned = re.sub(r"^(export|-e)\s+", "", assigned.strip())
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", assigned):
                continue
            hints = ("secret", "token", "password", "_key")
            if not any(hint in assigned.lower() for hint in hints):
                continue
            assert (
                "${{" in value or not value.strip()
            ), f"{name} may assign a literal credential: {stripped}"


def test_the_only_literal_secret_in_ci_says_it_is_not_for_deployment():
    """The value CI boots with announces itself, which is what stops it being
    copied somewhere that matters."""
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    literals = [
        line.strip()
        for line in text.splitlines()
        if "JWT_SECRET=" in line and "${{" not in line and not line.strip().startswith("#")
    ]

    assert literals, "CI no longer sets a JWT secret; this guard needs updating"
    for line in literals:
        assert any(
            value in line for value in CI_ONLY_VALUES
        ), f"an unrecognised literal secret appeared in ci.yml: {line}"


def test_the_security_workflow_scans_dependencies_secrets_and_the_image():
    """All three, because they fail differently: a vulnerable dependency, a
    leaked credential, and a base image nobody has rebuilt."""
    jobs = _load("security.yml")["jobs"]

    assert set(jobs) == {"dependencies", "secrets", "image"}
    assert "pip-audit" in _text(jobs["dependencies"])
    assert "gitleaks" in _text(jobs["secrets"])
    assert "trivy-action" in _text(jobs["image"])


def test_a_pull_request_is_not_failed_by_an_unfixable_finding():
    """A gate nobody can pass is a gate people learn to bypass. Unfixable
    findings are reported by the second scan, which never fails."""
    image_job = _load("security.yml")["jobs"]["image"]
    scans = [step for step in image_job["steps"] if "trivy" in str(step.get("uses", ""))]

    blocking = [step for step in scans if step["with"]["exit-code"] == "1"]
    reporting = [step for step in scans if step["with"]["exit-code"] == "0"]

    assert len(blocking) == 1
    assert blocking[0]["with"]["ignore-unfixed"] is True
    assert len(reporting) == 1
    assert reporting[0]["with"]["ignore-unfixed"] is False
