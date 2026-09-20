"""Run the remediation's targeted mutants, restoring each source byte for byte.

Run from the repository root with TEST_DATABASE_URL pointed at an isolated
database. Every mutant is one temporary source change and one focused pytest
selection. A failed assertion is a kill; an infrastructure error is not.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Mutant:
    identifier: str
    path: str
    old: str
    new: str
    test: str
    occurrence: int = 1
    regex: bool = False


RELEASE = "tests/unit/test_release_trust.py"
CONFIG = "tests/unit/test_security_config_regressions.py"
TEXT = "tests/unit/test_request_text_storability.py"
PAYMOB = "tests/integration/test_paymob_webhook_endpoint.py"

MUTANTS = (
    Mutant(
        "R01",
        ".github/workflows/deploy.yml",
        "github.event.workflow_run.event == 'push' &&",
        "true &&",
        RELEASE,
    ),
    Mutant(
        "R02",
        ".github/workflows/deploy.yml",
        "github.event.workflow_run.head_repository.full_name == github.repository",
        "true",
        RELEASE,
    ),
    Mutant(
        "R03",
        ".github/workflows/deploy.yml",
        r"    if: >-\n      github\.event_name != 'workflow_run'.*?\n    outputs:",
        "    if: true\n    outputs:",
        RELEASE,
        regex=True,
    ),
    Mutant(
        "R04",
        "app/core/limits.py",
        "return min(self.json_max_bytes, self.max_bytes)",
        "return self.max_bytes",
        "tests/integration/test_request_body_policy.py::test_an_oversized_login_body_is_refused_with_413",
    ),
    Mutant(
        "R05",
        "app/api/route.py",
        "            for hook in before_body:\n"
        "                await hook(request)\n"
        "            response = await handler(request)",
        "            response = await handler(request)\n"
        "            for hook in before_body:\n"
        "                await hook(request)",
        "tests/integration/test_request_body_policy.py::test_a_rate_limited_client_is_refused_before_its_body_is_read",
    ),
    Mutant(
        "R06",
        "app/core/secure_compare.py",
        "return hmac.compare_digest(ours, theirs)",
        "return hmac.compare_digest(expected, supplied)",
        "tests/unit/test_constant_time_comparison.py::test_the_helper_answers_and_never_raises",
    ),
    Mutant(
        "R07",
        "app/core/secure_compare.py",
        "return hmac.compare_digest(ours, theirs)",
        "return ours == theirs",
        "tests/unit/test_constant_time_comparison.py::test_the_helper_is_built_on_compare_digest_over_bytes",
    ),
    Mutant(
        "R08",
        "app/schemas/workspace.py",
        "name: StorableText = Field(min_length=1, max_length=MAXIMUM_NAME_LENGTH)",
        "name: str = Field(min_length=1, max_length=MAXIMUM_NAME_LENGTH)",
        TEXT,
    ),
    Mutant(
        "R09",
        "app/schemas/conversation.py",
        "body: StorableText = Field(min_length=1, max_length=MAX_TEXT_LENGTH)",
        "body: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)",
        TEXT,
    ),
    Mutant(
        "R10",
        "app/api/v1/leads.py",
        "SearchQuery = Annotated[StorableText | None, Query(min_length=1, max_length=200)]",
        "SearchQuery = Annotated[str | None, Query(min_length=1, max_length=200)]",
        TEXT,
    ),
    Mutant(
        "R11",
        "docker-compose.prod.yml",
        "      DATABASE_URL: ${DATABASE_URL:?DATABASE_URL is required}",
        "      MIGRATION_DATABASE_URL: "
        "${MIGRATION_DATABASE_URL:?MIGRATION_DATABASE_URL is required}",
        "tests/integration/test_deployment_configuration.py::test_production_database_owner_secret_reaches_only_migrate",
        occurrence=2,
    ),
    Mutant(
        "R12",
        "app/core/config.py",
        "    problems: list[str] = []\n    for origin in origins:",
        "    return []\n    problems: list[str] = []\n    for origin in origins:",
        CONFIG,
    ),
    Mutant(
        "R13",
        "app/core/config.py",
        'or parsed.scheme.lower() != "https"',
        'or parsed.scheme.lower() not in ("https", "http")',
        CONFIG,
    ),
    Mutant(
        "R14",
        "app/core/config.py",
        "if network.prefixlen == 0 or not (network.is_private or network.is_loopback):",
        "if False:",
        CONFIG,
    ),
    Mutant(
        "R15",
        "app/core/config.py",
        "            if not self.rate_limit_enabled:\n                # The per-address",
        "            if False:\n                # The per-address",
        CONFIG,
    ),
    Mutant(
        "R16",
        "app/core/config.py",
        "if parsed.username is not None or parsed.password is not None:",
        "if False:",
        CONFIG,
        occurrence=0,
    ),
    Mutant(
        "R17",
        "app/integrations/whatsapp/ownership.py",
        "follow_redirects=False,",
        "follow_redirects=True,",
        "tests/unit/test_security_structural_regressions.py::test_graph_redirect_never_receives_the_bearer_at_a_second_host",
    ),
    Mutant(
        "R18",
        "app/api/v1/leads.py",
        "LimitQuery = Annotated[int, Query(ge=1, le=100)]",
        "LimitQuery = Annotated[int, Query(ge=1, le=1000000000)]",
        "tests/unit/test_security_structural_regressions.py::test_lead_page_limit_is_enforced_at_the_http_boundary",
    ),
    Mutant(
        "R19",
        "app/api/v1/auth.py",
        "    except EmailAlreadyRegisteredError:\n"
        "        await service.notify_existing_registration(payload.email)",
        "    except EmailAlreadyRegisteredError:\n        raise EmailAlreadyRegisteredError()",
        "tests/integration/test_account_enumeration.py",
    ),
    Mutant(
        "R20",
        "app/api/v1/payment_webhooks.py",
        '            "billing.card_token_processed",',
        '            f"billing.card_token_processed {saved.token}",',
        f"{PAYMOB}::test_signed_card_token_is_stored_encrypted_and_retries_once",
    ),
    Mutant(
        "R21",
        "app/integrations/billing/paymob.py",
        "if not signature or not secrets_match(expected, signature):",
        "if False:",
        "tests/unit/test_paymob_card_tokens.py::test_changing_any_signed_field_invalidates_the_token",
        occurrence=2,
    ),
    Mutant(
        "R22",
        "app/api/v1/payment_webhooks.py",
        ".where(Payment.provider_intent_reference == str(order_reference))",
        '.where(Payment.provider == "paymob")',
        f"{PAYMOB}::test_card_token_hmac_or_order_mismatch_stores_nothing",
    ),
)


def _mutate(source: bytes, mutant: Mutant) -> bytes:
    newline = "\r\n" if b"\r\n" in source else "\n"
    original = source.decode("utf-8")
    old = mutant.old.replace("\n", newline)
    new = mutant.new.replace("\n", newline)
    if mutant.regex:
        changed, count = re.subn(old, new, original, count=1, flags=re.DOTALL)
        if count != 1:
            raise ValueError(f"{mutant.identifier}: regex target not unique")
    else:
        if mutant.occurrence == 0:
            if original.count(old) != 2:
                raise ValueError(f"{mutant.identifier}: expected two redundant guards")
            return original.replace(old, new).encode("utf-8")
        if original.count(old) < mutant.occurrence:
            raise ValueError(f"{mutant.identifier}: target missing")
        position = -1
        for _ in range(mutant.occurrence):
            position = original.find(old, position + 1)
        changed = original[:position] + new + original[position + len(old) :]
    return changed.encode("utf-8")


def run(mutant: Mutant) -> dict[str, object]:
    path = ROOT / mutant.path
    original = path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    altered = _mutate(original, mutant)
    if altered == original:
        raise ValueError(f"{mutant.identifier}: no source change")
    result: subprocess.CompletedProcess[str] | None = None
    try:
        path.write_bytes(altered)
        result = subprocess.run(  # noqa: S603 - fixed interpreter and test selectors
            [sys.executable, "-m", "pytest", "-q", "--maxfail=1", mutant.test],
            cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
    finally:
        path.write_bytes(original)
    restored = hashlib.sha256(path.read_bytes()).hexdigest()
    if restored != digest:
        raise RuntimeError(f"{mutant.identifier}: source SHA-256 not restored")
    if result is None:
        raise RuntimeError(f"{mutant.identifier}: test process did not run")
    output = result.stdout + result.stderr
    return {
        "id": mutant.identifier,
        "property_removed": mutant.old[:90],
        "applied": True,
        "killed": result.returncode != 0 and ("FAILED " in output or "AssertionError" in output),
        "survived": result.returncode == 0,
        "inapplicable": False,
        "killer_test": mutant.test,
        "sha256_restored": restored,
        "exit_code": result.returncode,
        "test_tail": output[-500:],
    }


def main() -> int:
    selected = set(sys.argv[1:])
    for mutant in MUTANTS:
        if selected and mutant.identifier not in selected:
            continue
        try:
            result = run(mutant)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            result = {"id": mutant.identifier, "applied": False, "error": str(error)}
        print(json.dumps(result, ensure_ascii=False), flush=True)  # noqa: T201 - CLI output
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
