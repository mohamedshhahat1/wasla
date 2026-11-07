"""The signed client-IP forward, as a pure function (FEB-01, ADR-190).

The verifier takes the headers, the method, the path, a clock reading and the
secret, and answers with a result word and, only when valid, an address. These
tests pin every refusal to its reason with an injected clock - nothing here
sleeps or reads the wall clock - and hold the frontend's test vectors to the
same verifier the API runs, so FE-2's signer and this check cannot drift apart.

The property that matters most is the one asserted in many shapes below: a
forward that is not exactly right yields no address, so it cannot move a
request into a bucket of the caller's choosing.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from starlette.datastructures import Headers

from app.core.client_ip_forward import (
    FORWARD_IP_HEADER,
    FORWARD_SIG_HEADER,
    FORWARD_TS_HEADER,
    FRESHNESS_SECONDS,
    ForwardResult,
    sign_forward,
    verify_forward,
)
from app.core.config import Settings
from app.core.telemetry import CLIENT_IP_FORWARD_RESULTS

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "frontend" / "packages" / "api-client" / "fixtures" / "forward-signature.v1.json"
SECRET = "unit-test-forward-secret-0123456789abcdef"
NOW = 1_794_000_000


def _cases() -> list[dict[str, Any]]:
    document = json.loads(VECTORS.read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = document["cases"]
    return cases


def _headers(*pairs: tuple[str, str]) -> Headers:
    return Headers(raw=[(name.lower().encode(), value.encode()) for name, value in pairs])


def _signed(
    client_ip: str = "203.0.113.7",
    *,
    ts: int = NOW,
    method: str = "POST",
    path: str = "/api/v1/auth/login",
    secret: str = SECRET,
) -> Headers:
    signature = sign_forward(secret, ts=str(ts), client_ip=client_ip, method=method, path=path)
    return _headers(
        (FORWARD_IP_HEADER, client_ip),
        (FORWARD_TS_HEADER, str(ts)),
        (FORWARD_SIG_HEADER, signature),
    )


def _verify(headers: Headers, **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "method": "POST",
        "path": "/api/v1/auth/login",
        "now": NOW,
        "secret": SECRET,
    }
    arguments.update(overrides)
    return verify_forward(headers, **arguments)


# ------------------------------------------------------------------- F-1 vectors


def test_there_are_enough_vectors_to_mean_something() -> None:
    cases = _cases()
    assert len(cases) >= 6
    methods = {case["method"] for case in cases}
    assert {"GET", "POST"} <= methods


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["name"])
def test_every_vector_is_accepted_and_names_its_identity(case: dict[str, Any]) -> None:
    headers = _headers(
        (FORWARD_IP_HEADER, case["client_ip"]),
        (FORWARD_TS_HEADER, case["ts"]),
        (FORWARD_SIG_HEADER, case["signature"]),
    )

    verdict = verify_forward(
        headers,
        method=case["method"],
        path=case["path"],
        now=int(case["ts"]),
        secret=case["secret"],
    )

    assert verdict.result is ForwardResult.VALID
    assert verdict.address == case["identity"]
    assert case["canonical"] == "\n".join(
        ("v1", case["ts"], case["client_ip"], case["method"], case["path"])
    )


def _one_field_changed(case: dict[str, Any], field: str) -> dict[str, Any]:
    changed = dict(case)
    changed[field] = {
        "ts": str(int(case["ts"]) + 1),
        "client_ip": "198.51.100.200",
        "method": "PUT" if case["method"] != "PUT" else "POST",
        "path": case["path"] + "x",
        "signature": ("0" if case["signature"][0] != "0" else "1") + case["signature"][1:],
        "secret": case["secret"] + "-other",
    }[field]
    return changed


@pytest.mark.parametrize("field", ["ts", "client_ip", "method", "path", "signature", "secret"])
@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["name"])
def test_every_vector_with_one_field_changed_is_refused(case: dict[str, Any], field: str) -> None:
    changed = _one_field_changed(case, field)
    headers = _headers(
        (FORWARD_IP_HEADER, changed["client_ip"]),
        (FORWARD_TS_HEADER, changed["ts"]),
        (FORWARD_SIG_HEADER, changed["signature"]),
    )

    verdict = verify_forward(
        headers,
        method=changed["method"],
        path=changed["path"],
        now=int(case["ts"]),
        secret=changed["secret"],
    )

    assert verdict.result is ForwardResult.BAD_SIGNATURE
    assert verdict.address is None


# ----------------------------------------------------------------- F-2 freshness


@pytest.mark.parametrize("offset", [-FRESHNESS_SECONDS, 0, FRESHNESS_SECONDS])
def test_a_signature_inside_the_window_is_accepted(offset: int) -> None:
    verdict = _verify(_signed(ts=NOW + offset))

    assert verdict.result is ForwardResult.VALID
    assert verdict.address == "203.0.113.7"


@pytest.mark.parametrize("offset", [-FRESHNESS_SECONDS - 1, FRESHNESS_SECONDS + 1, -3600])
def test_a_signature_outside_the_window_is_stale(offset: int) -> None:
    verdict = _verify(_signed(ts=NOW + offset))

    assert verdict.result is ForwardResult.STALE
    assert verdict.address is None


def test_the_window_is_a_minute() -> None:
    assert FRESHNESS_SECONDS == 60


# ----------------------------------------------------------------- F-3 malformed


@pytest.mark.parametrize(
    "client_ip",
    [
        "203.0.113.7:443",
        "[2001:db8::1]",
        "fe80::1%eth0",
        "203.0.113.0/24",
        "not-an-address",
        "1" * 46,
        "",
    ],
)
def test_a_signed_value_that_is_not_one_address_is_malformed(client_ip: str) -> None:
    # Signed correctly, so only the address itself is wrong.
    verdict = _verify(_signed(client_ip))

    assert verdict.result is ForwardResult.MALFORMED
    assert verdict.address is None


def test_an_address_list_is_malformed_even_when_signed() -> None:
    verdict = _verify(_signed("203.0.113.7, 198.51.100.1"))

    assert verdict.result is ForwardResult.MALFORMED


@pytest.mark.parametrize("name", [FORWARD_IP_HEADER, FORWARD_TS_HEADER, FORWARD_SIG_HEADER])
def test_a_repeated_header_is_malformed(name: str) -> None:
    signed = _signed()
    pairs = [(key, value) for key, value in signed.items()]
    pairs.append((name, signed[name]))

    verdict = _verify(_headers(*pairs))

    assert verdict.result is ForwardResult.MALFORMED


@pytest.mark.parametrize("ts", ["1794000000.5", "soon", "-1794000000", "", "1e9"])
def test_a_timestamp_that_is_not_an_integer_is_malformed(ts: str) -> None:
    signed = _signed()
    headers = _headers(
        (FORWARD_IP_HEADER, signed[FORWARD_IP_HEADER]),
        (FORWARD_TS_HEADER, ts),
        (FORWARD_SIG_HEADER, signed[FORWARD_SIG_HEADER]),
    )

    assert _verify(headers).result is ForwardResult.MALFORMED


@pytest.mark.parametrize(
    "signature",
    ["a" * 63, "a" * 65, "g" * 64, "", "sha256=" + "a" * 57],
)
def test_a_value_that_cannot_be_a_signature_is_refused_without_computing(signature: str) -> None:
    headers = _headers(
        (FORWARD_IP_HEADER, "203.0.113.7"),
        (FORWARD_TS_HEADER, str(NOW)),
        (FORWARD_SIG_HEADER, signature),
    )

    verdict = _verify(headers)

    # FW-07: the shape of the signature is checked before any HMAC is computed.
    assert verdict.result is ForwardResult.BAD_SIGNATURE
    assert verdict.address is None


def test_a_partial_forward_is_malformed() -> None:
    signed = _signed()

    verdict = _verify(_headers((FORWARD_IP_HEADER, signed[FORWARD_IP_HEADER])))

    assert verdict.result is ForwardResult.MALFORMED


def test_no_forward_at_all_is_absent() -> None:
    assert _verify(_headers()).result is ForwardResult.ABSENT


def test_an_unsigned_claim_is_never_believed() -> None:
    """What a browser, or anybody with curl, can send on its own."""
    verdict = _verify(_headers((FORWARD_IP_HEADER, "198.51.100.77")))

    assert verdict.address is None


def test_the_signature_is_checked_before_the_clock() -> None:
    """A stale forward with a forged signature says nothing about the clock."""
    signed = _signed(ts=NOW - 3600, secret="somebody-elses-secret-0123456789abcdef")

    assert _verify(signed).result is ForwardResult.BAD_SIGNATURE


def test_an_uppercase_signature_is_the_same_signature() -> None:
    signed = _signed()
    headers = _headers(
        (FORWARD_IP_HEADER, signed[FORWARD_IP_HEADER]),
        (FORWARD_TS_HEADER, signed[FORWARD_TS_HEADER]),
        (FORWARD_SIG_HEADER, signed[FORWARD_SIG_HEADER].upper()),
    )

    assert _verify(headers).result is ForwardResult.VALID


# ------------------------------------------------------------------ F-4 disabled


@pytest.mark.parametrize("secret", [None, ""])
def test_a_perfectly_signed_forward_is_ignored_when_the_feature_is_off(
    secret: str | None,
) -> None:
    verdict = _verify(_signed(), secret=secret)

    assert verdict.result is ForwardResult.DISABLED
    assert verdict.address is None


def test_no_forward_with_the_feature_off_is_absent() -> None:
    assert _verify(_headers(), secret=None).result is ForwardResult.ABSENT


# ---------------------------------------------------------------- F-5 identities


def test_an_ipv4_mapped_address_shares_the_ipv4_bucket() -> None:
    mapped = _verify(_signed("::ffff:203.0.113.7"))
    plain = _verify(_signed("203.0.113.7"))

    assert mapped.address == plain.address == "203.0.113.7"


def test_an_ipv6_address_is_compressed() -> None:
    verdict = _verify(_signed("2001:0db8:0000:0000:0000:0000:0000:0042"))

    assert verdict.address == "2001:db8::42"


def test_surrounding_space_is_signed_but_not_part_of_the_identity() -> None:
    verdict = _verify(_signed(" 203.0.113.7 "))

    assert verdict.address == "203.0.113.7"


# ------------------------------------------------------------- F-11 the setting


def test_the_feature_is_off_by_default() -> None:
    assert Settings(_env_file=None, environment="test").session_forward_secret is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_value_is_unset(blank: str) -> None:
    """Compose passes `${SESSION_FORWARD_SECRET:-}`, which is an empty string."""
    settings = Settings(_env_file=None, environment="test", session_forward_secret=blank)

    assert settings.session_forward_secret is None


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_internet_facing_tiers_refuse_a_short_secret(environment: str) -> None:
    with pytest.raises(ValidationError, match="SESSION_FORWARD_SECRET"):
        Settings(
            _env_file=None,
            environment=environment,
            jwt_secret=secrets.token_urlsafe(48),
            docs_enabled=False,
            meta_app_secret="an-app-secret",
            meta_verify_token="synthetic-verify-token",
            session_forward_secret="x" * 31,
        )


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_internet_facing_tiers_accept_it_unset_or_long(environment: str) -> None:
    common: dict[str, Any] = {
        "_env_file": None,
        "environment": environment,
        "jwt_secret": secrets.token_urlsafe(48),
        "docs_enabled": False,
        "meta_app_secret": "an-app-secret",
        "meta_verify_token": "synthetic-verify-token",
    }
    assert Settings(**common).session_forward_secret is None
    settings = Settings(**common, session_forward_secret="y" * 32)
    assert settings.session_forward_secret is not None
    assert settings.session_forward_secret.get_secret_value() == "y" * 32


def test_the_secret_never_appears_in_the_settings_repr() -> None:
    settings = Settings(_env_file=None, environment="test", session_forward_secret=SECRET)

    assert SECRET not in repr(settings)
    assert SECRET not in str(settings.model_dump())


# ------------------------------------------------------------- FW-06 the counter


def test_the_counter_domain_is_the_result_vocabulary() -> None:
    assert frozenset(result.value for result in ForwardResult) == CLIENT_IP_FORWARD_RESULTS
