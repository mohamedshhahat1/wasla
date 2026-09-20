"""Who can make the release pipeline publish or deploy (SEC-01).

`deploy.yml` runs on `workflow_run`, which fires in the context of this
repository's default branch - with its permissions and its secrets - after *any*
CI run concludes, including one started by a pull request from a fork. The
`branches: [main]` filter on that trigger is matched against the triggering
run's head-branch **name**, which a fork contributor chooses. The audit showed a
fork branch called `main` reaching both the image push and the production
deploy.

These tests do not search the workflow for the word `main`. They parse it and
**evaluate each job's `if:` expression** against the event payloads GitHub
would deliver for the cases that matter, with a small evaluator for the subset
of the Actions expression language the workflow uses. A guard that is present
but wrong - `||` where `&&` was meant, a clause dropped, the head branch trusted
alone - fails here in the case it gets wrong, which is what a string search
cannot do.

GitHub's own settings (fork-PR approval, environment reviewers) are outside
the repository and are deployment verification (DV-S1). The point of this file
is that the repository is safe *even if* a reviewer approves the wrong run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
REPOSITORY = "wasla-owner/wasla"


def _deploy() -> dict[Any, Any]:
    with (WORKFLOWS / "deploy.yml").open(encoding="utf-8") as handle:
        document: dict[Any, Any] = yaml.safe_load(handle)
    return document


# --- a minimal evaluator for GitHub Actions expressions -----------------------
#
# Enough of https://docs.github.com/actions/reference/evaluate-expressions to
# read the conditions in deploy.yml: literals, dotted context paths, `!`, `==`,
# `!=`, `&&`, `||` and parentheses. Anything else raises rather than guessing,
# so a condition written with a construct this does not know fails the suite
# loudly instead of being evaluated wrongly.
#
# Two semantics are reproduced deliberately because they affect security:
# `==` compares strings case-insensitively (a fork branch `Main` equals
# `main`), and `&&` / `||` return operands and are judged by truthiness.

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<op>==|!=|&&|\|\||!|\(|\))"
    r"|(?P<num>-?\d+(?:\.\d+)?)|(?P<ident>[A-Za-z_][A-Za-z0-9_\-]*(?:\.[A-Za-z_][A-Za-z0-9_\-]*)*))"
)


def _tokens(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    position = 0
    text = text.strip()
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            raise ValueError(f"unsupported expression syntax at: {text[position:]!r}")
        kind = match.lastgroup
        assert kind is not None
        out.append((kind, match.group(kind)))
        position = match.end()
    return out


def _truthy(value: Any) -> bool:
    return value not in (None, False, 0, "")


def _equal(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    return bool(left == right)


class _Parser:
    def __init__(self, text: str, context: dict[str, Any]) -> None:
        self.tokens = _tokens(text)
        self.index = 0
        self.context = context

    def parse(self) -> Any:
        value = self._or()
        if self.index != len(self.tokens):
            raise ValueError(f"trailing tokens: {self.tokens[self.index:]}")
        return value

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _take(self, op: str) -> bool:
        token = self._peek()
        if token == ("op", op):
            self.index += 1
            return True
        return False

    def _or(self) -> Any:
        value = self._and()
        while self._take("||"):
            right = self._and()
            value = value if _truthy(value) else right
        return value

    def _and(self) -> Any:
        value = self._comparison()
        while self._take("&&"):
            right = self._comparison()
            value = right if _truthy(value) else value
        return value

    def _comparison(self) -> Any:
        value = self._unary()
        while True:
            if self._take("=="):
                value = _equal(value, self._unary())
            elif self._take("!="):
                value = not _equal(value, self._unary())
            else:
                return value

    def _unary(self) -> Any:
        if self._take("!"):
            return not _truthy(self._unary())
        if self._take("("):
            value = self._or()
            if not self._take(")"):
                raise ValueError("unbalanced parenthesis")
            return value
        token = self._peek()
        if token is None:
            raise ValueError("unexpected end of expression")
        self.index += 1
        kind, text = token
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "num":
            return float(text)
        if kind == "ident":
            if text in ("true", "false"):
                return text == "true"
            if text == "null":
                return None
            if self._peek() == ("op", "("):
                raise ValueError(f"function calls are not supported: {text}")
            return self._lookup(text)
        raise ValueError(f"unexpected token {token}")

    def _lookup(self, path: str) -> Any:
        current: Any = self.context
        for part in path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current


def evaluate(condition: str | bool | None, context: dict[str, Any]) -> bool:
    """Whether a job with this `if:` runs. Absent means `success()`: it runs."""
    if condition is None:
        return True
    if isinstance(condition, bool):
        return condition
    text = condition.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    return _truthy(_Parser(text, context).parse())


# --- the events that matter ---------------------------------------------------


@dataclass(frozen=True)
class Run:
    """One concluded CI run, as `github.event.workflow_run` describes it."""

    label: str
    event: str
    head_branch: str
    head_repository: str
    conclusion: str = "success"

    def context(self) -> dict[str, Any]:
        return {
            "github": {
                "event_name": "workflow_run",
                "repository": REPOSITORY,
                "ref": "refs/heads/main",
                "event": {
                    "workflow_run": {
                        "event": self.event,
                        "head_branch": self.head_branch,
                        "head_sha": "f" * 40,
                        "conclusion": self.conclusion,
                        "head_repository": {"full_name": self.head_repository},
                        "repository": {"full_name": REPOSITORY},
                    }
                },
            },
            "inputs": {},
        }


TRUSTED = Run("same-repository push to main", "push", "main", REPOSITORY)

UNTRUSTED = [
    # The audited attack: a fork PR from a branch the contributor named `main`.
    Run("fork pull request from a branch named main", "pull_request", "main", "attacker/wasla"),
    # GitHub compares case-insensitively, so `Main` must not be a way round.
    Run("fork pull request from a branch named Main", "pull_request", "Main", "attacker/wasla"),
    # Even this repository's own PR is unreviewed code until it is merged.
    Run("same-repository pull request from main", "pull_request", "main", REPOSITORY),
    Run("pull_request_target-shaped run", "pull_request_target", "main", "attacker/wasla"),
    # A push event whose head lives in another repository.
    Run("push from another repository", "push", "main", "attacker/wasla"),
    Run("push to another branch", "push", "feature", REPOSITORY),
    Run("manually dispatched CI on another repository", "workflow_dispatch", "main", "a/b"),
    Run("failed CI on main", "push", "main", REPOSITORY, conclusion="failure"),
    Run("cancelled CI on main", "push", "main", REPOSITORY, conclusion="cancelled"),
]

PRIVILEGED_JOBS = ("publish", "deploy")


def _jobs() -> dict[str, Any]:
    jobs: dict[str, Any] = _deploy()["jobs"]
    return jobs


@pytest.mark.parametrize("run", UNTRUSTED, ids=lambda run: run.label)
@pytest.mark.parametrize("job", PRIVILEGED_JOBS)
def test_an_untrusted_ci_run_reaches_no_privileged_job(job: str, run: Run) -> None:
    """The SEC-01 regression, evaluated rather than searched for."""
    condition = _jobs()[job].get("if")

    assert not evaluate(condition, run.context()), f"{job} would run for: {run.label}"


@pytest.mark.parametrize("run", UNTRUSTED, ids=lambda run: run.label)
def test_no_job_at_all_runs_for_an_untrusted_ci_run(run: Run) -> None:
    """Every job, including any added later without a guard.

    A new job with no `if:` defaults to `success()` and would run on every
    `workflow_run` - so the rule is stated about the whole workflow, not about
    the two jobs that exist today.
    """
    running = [name for name, job in _jobs().items() if evaluate(job.get("if"), run.context())]

    assert running == [], f"{running} would run for: {run.label}"


@pytest.mark.parametrize("job", PRIVILEGED_JOBS)
def test_a_trusted_push_to_main_still_releases(job: str) -> None:
    """The guard must not simply be `false`: the real release path still works."""
    assert evaluate(_jobs()[job].get("if"), TRUSTED.context())


@pytest.mark.parametrize("job", PRIVILEGED_JOBS)
def test_each_privileged_job_carries_its_own_guard(job: str) -> None:
    """`deploy` must not rely on `needs: publish` being skipped.

    A `needs` chain is one `always()` away from running anyway; the job holding
    the production SSH key states the provenance rule itself. Checked by
    evaluating the job's condition in isolation, ignoring its dependencies.
    """
    fork = UNTRUSTED[0]
    assert not evaluate(_jobs()[job].get("if"), fork.context())


def _references_head_sha(value: object) -> bool:
    return "workflow_run.head_sha" in yaml.safe_dump(value)


def test_the_ci_verified_commit_is_only_checked_out_behind_the_guard() -> None:
    """Any job that touches `workflow_run.head_sha` is refused for every
    untrusted run - the fork's commit is never on disk in a privileged job."""
    jobs = _jobs()
    touching = [name for name, job in jobs.items() if _references_head_sha(job)]
    assert touching, "expected the release to pin the commit CI verified"

    for name in touching:
        for run in UNTRUSTED:
            assert not evaluate(
                jobs[name].get("if"), run.context()
            ), f"{name} checks out head_sha for: {run.label}"


def test_no_privileged_checkout_persists_a_git_credential() -> None:
    """`persist-credentials: false` on every checkout in the release workflow.

    Nothing in it pushes to git, so a token written into `.git/config` is only
    ever a write credential sitting beside the code being built.
    """
    for name, job in _jobs().items():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert (
                    step.get("with", {}).get("persist-credentials") is False
                ), f"a checkout in {name} persists its token"


def test_production_deployment_remains_environment_gated() -> None:
    """Defence in depth: the job with the SSH key waits on the `production`
    environment, whose reviewers and branch rule are DV-S1."""
    environment = _jobs()["deploy"].get("environment")
    name = environment.get("name") if isinstance(environment, dict) else environment

    assert name == "production"


def test_the_release_trigger_does_not_listen_to_pull_request_events() -> None:
    """No `pull_request_target`, no `pull_request`: the release workflow is
    only ever started by CI concluding, a tag, or somebody with write access."""
    triggers = _deploy()[True]  # PyYAML reads `on:` as True

    assert set(triggers) <= {"workflow_run", "push", "workflow_dispatch"}
    assert "branches" not in triggers.get("push", {}), "branch pushes must go through CI"


# --- the evaluator itself -----------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("'a' == 'A'", True),
        ("'a' != 'b' && 'x' == 'x'", True),
        ("false || 'x' == 'y'", False),
        ("!(true && false)", True),
        ("github.missing.path == null", True),
    ],
)
def test_the_evaluator_follows_the_documented_semantics(expression: str, expected: bool) -> None:
    assert evaluate(expression, {"github": {}}) is expected


def test_the_evaluator_refuses_what_it_does_not_understand() -> None:
    with pytest.raises(ValueError, match="function calls"):
        evaluate("always()", {})
