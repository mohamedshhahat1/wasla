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
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _load(name: str) -> dict[Any, Any]:
    """One workflow document.

    The key type is `Any` rather than `str` for one specific reason: YAML's
    `on:` is parsed by PyYAML as the boolean `True`, and this file reads it.
    """
    with (WORKFLOWS / name).open(encoding="utf-8") as handle:
        document: dict[Any, Any] = yaml.safe_load(handle)
        return document


def _steps(workflow: dict[Any, Any], job: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = workflow["jobs"][job]["steps"]
    return steps


def _text(value: object) -> str:
    rendered: str = yaml.safe_dump(value)
    return rendered


@pytest.mark.parametrize("name", ["ci.yml", "security.yml", "deploy.yml"])
def test_every_workflow_parses(name: str) -> None:
    """A workflow that does not parse does not run, and GitHub reports that
    quietly enough to miss."""
    workflow = _load(name)

    assert workflow["jobs"], f"{name} declares no jobs"


@pytest.mark.parametrize("name", ["ci.yml", "security.yml", "deploy.yml"])
def test_every_workflow_declares_its_permissions(name: str) -> None:
    """The default token is broad. Every workflow narrows it, so a compromised
    action cannot do more than the workflow needs."""
    workflow = _load(name)

    assert "permissions" in workflow, f"{name} does not narrow its token"


def test_deployment_waits_for_ci_rather_than_repeating_it() -> None:
    """ "Do not deploy if tests fail" as a dependency, not a second copy of the
    test job that could drift from the first."""
    deploy = _load("deploy.yml")
    # `on` is parsed by PyYAML as the boolean True.
    triggers = deploy[True]

    assert "workflow_run" in triggers
    assert triggers["workflow_run"]["workflows"] == ["CI"]
    assert triggers["workflow_run"]["branches"] == ["main"]


def test_a_failed_ci_run_publishes_nothing() -> None:
    condition = _load("deploy.yml")["jobs"]["publish"]["if"]

    assert "conclusion == 'success'" in condition


def test_publishing_checks_out_the_commit_ci_verified() -> None:
    """Between CI finishing and this starting, main may have moved. Publishing
    the branch head would ship a commit no test ever saw."""
    checkout = next(
        step
        for step in _steps(_load("deploy.yml"), "publish")
        if str(step.get("uses", "")).startswith("actions/checkout")
    )

    assert "workflow_run.head_sha" in checkout["with"]["ref"]


def test_the_deployed_image_is_named_by_digest() -> None:
    """A tag can be moved; a digest is the image that was built and scanned."""
    publish = _load("deploy.yml")["jobs"]["publish"]

    assert "@${{ steps.build.outputs.digest }}" in _text(publish) or "DIGEST" in _text(publish)
    assert "digest" in publish["outputs"]["image"] or "reference" in _text(publish["outputs"])


def test_the_published_image_is_scanned() -> None:
    steps = _text(_steps(_load("deploy.yml"), "publish"))

    assert "trivy-action" in steps


def test_a_deployment_without_a_target_fails_rather_than_pretending() -> None:
    """A workflow that "succeeds" without touching a server is worse than one
    that fails: the green tick is read as "it shipped"."""
    steps = _text(_steps(_load("deploy.yml"), "deploy"))

    assert "No deployment target configured" in steps
    assert "exit 1" in steps


def test_the_deploy_host_key_is_pinned() -> None:
    """Trusting an unknown host on first connection is how a deploy ends up
    talking to somebody else's server."""
    steps = _text(_steps(_load("deploy.yml"), "deploy"))

    assert "known_hosts" in steps
    assert "StrictHostKeyChecking=no" not in steps


def test_migrations_run_before_the_new_version_serves() -> None:
    steps = _text(_steps(_load("deploy.yml"), "deploy"))
    migrate = steps.index("run --rm migrate")
    start = steps.index("up -d --wait")

    assert migrate < start, "the new version would serve against an unmigrated schema"


def test_the_deployment_is_verified_rather_than_assumed() -> None:
    """A deployment is finished when the thing it started answers, not when the
    command that started it exits zero.

    This used to assert `"/health/ready" in steps`, which certified a string
    and not a property - and the command containing that string could not fail,
    because it asked nginx on port 80 and a 301 exits zero under `curl -f`
    (F-8). What the gate actually does is now proven by executing it:
    `tests/unit/test_readiness_gate.py` runs the script against a stub serving
    a redirect, a 503, a refused connection, a 200 that is not this endpoint,
    and a real ready response.

    What is left here is the part that belongs to the *pipeline* rather than to
    the script: that the gate exists, that it asks the application rather than
    the proxy, and that it runs after the new version is up.
    """
    steps = _text(_steps(_load("deploy.yml"), "deploy"))

    assert "check_readiness.sh" in steps
    assert "exec -T api" in steps
    assert "http://127.0.0.1/health/ready" not in steps, "that is nginx, and it answers 301"
    assert steps.index("up -d --wait") < steps.index("check_readiness.sh")


# A literal that is unmistakably a throwaway. CI needs *a* JWT secret to start
# the application with, and generating one per run would make a failing build
# harder to reproduce. It is tolerable only because it says what it is.
CI_ONLY_VALUES = ("continuous-integration-secret-value-not-for-deployment",)


def test_the_deployment_workflows_write_no_credential_literally() -> None:
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


def test_every_ci_jwt_secret_is_generated_or_announces_itself() -> None:
    """Two assignment forms, and they now answer to different rules.

    The pytest job sets `JWT_SECRET:` as YAML env and runs as `ENVIRONMENT=test`,
    the one environment exempt from the signing-key rule; its literal is allowed
    because it says out loud that it is not for deployment, which is what stops
    it being copied somewhere that matters.

    The container smoke test passes `JWT_SECRET=` as a docker argument and boots
    as `staging`, which the settings validator now holds to the same rule as
    production. That one must be generated per run - a written-down value would
    fail the check and teach the wrong habit at once.
    """
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")

    assignments = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "JWT_SECRET" not in stripped:
            continue
        # Trailing line-continuation backslashes belong to the shell, not to
        # the value, so they are trimmed before the value is judged.
        match = re.search(r"JWT_SECRET\s*[:=]\s*(.+?)\s*\\?$", stripped)
        if match:
            assignments.append((stripped, match.group(1).strip().strip('"')))

    assert assignments, "CI no longer sets a JWT secret; this guard needs updating"

    generated = [value for _, value in assignments if value.startswith("$")]
    assert generated, (
        "no CI job generates its JWT secret any more. The container smoke test "
        "runs as staging, which must not boot on a value from the repository."
    )

    for line, value in assignments:
        if value.startswith("$") or "${{" in value:
            continue
        assert any(
            allowed in value for allowed in CI_ONLY_VALUES
        ), f"an unrecognised literal secret appeared in ci.yml: {line}"


def test_the_security_workflow_scans_dependencies_secrets_and_the_image() -> None:
    """All three, because they fail differently: a vulnerable dependency, a
    leaked credential, and a base image nobody has rebuilt."""
    jobs = _load("security.yml")["jobs"]

    assert set(jobs) == {"dependencies", "secrets", "image"}
    assert "pip-audit" in _text(jobs["dependencies"])
    assert "gitleaks" in _text(jobs["secrets"])
    assert "trivy-action" in _text(jobs["image"])


def test_the_secret_allowlist_suppresses_values_and_never_a_class() -> None:
    """F-9. Nineteen standing false positives, and the branch red on its own gate.

    The cost of that is not the red tick. A scanner with standing false
    positives is a scanner a team learns to skip, and the next real finding is
    skipped with it - which is why the config's own header already says "A real
    secret must be rotated, never allowlisted."

    The fix had to close nineteen findings without closing anything else, and
    the two easy ways to do it are the two that must not happen: allowlisting
    `tests/` as a path, or allowlisting a rule. Either would have made a real
    key committed into a fixture invisible, and a fixture is exactly where one
    gets committed.

    So this pins the *shape* of the allowlist rather than its contents. Every
    entry must be a value, and every path entry must name a single file. A
    directory, a wildcard over a tree, or a rule id would all be caught here.

    The proof that the values are narrow enough is a control run rather than an
    assertion: a live-shaped Paymob key with no fake marker, committed into
    `tests/` under the very rule the fixtures trip, is still reported.
    """
    config = tomllib.loads((WORKFLOWS.parents[1] / ".gitleaks.toml").read_text(encoding="utf-8"))
    allowlist = config["allowlist"]

    assert allowlist["regexes"], "an empty allowlist would pass every check below"

    # No rule is switched off, anywhere.
    assert "rules" not in config
    assert "disabledRules" not in config.get("extend", {})
    assert config["extend"]["useDefault"] is True

    # Every path is one file, anchored at both ends. `^tests/` or `.*` would
    # close the finding and open the hole.
    for path in allowlist.get("paths", []):
        assert path.startswith("^") and path.endswith("$"), f"{path} is not anchored"
        assert not path.strip("^$").endswith("/"), f"{path} is a tree, not a file"

    # And every regex is a value rather than a wildcard dressed as one: it must
    # carry a run of at least six literal characters, so a pattern that would
    # match a real secret cannot be written without saying which one.
    for regex in allowlist["regexes"]:
        assert ".*" not in regex and ".+" not in regex, f"{regex} is a wildcard"
        literals = re.findall(r"[A-Za-z0-9_-]{6,}", regex)
        assert literals, f"{regex} has no literal long enough to be specific"


def test_a_pull_request_is_not_failed_by_an_unfixable_finding() -> None:
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
