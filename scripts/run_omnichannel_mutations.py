"""The omnichannel foundation's mutation campaign (OMNI-R10, mutants M1-M14).

    python -m scripts.run_omnichannel_mutations            # control, then every mutant
    python -m scripts.run_omnichannel_mutations M3 M11     # control, then these

Run from the repository root with `TEST_DATABASE_URL` (and, for the Redis-backed
suites, `WASLA_TEST_REDIS_HOST`) pointed at isolated services.

Each mutant removes one property the foundation promises - the connection in an
identity lookup, the participant as the address, the echo's classification -
by editing the source in place, runs the tests that are supposed to notice, and
restores every byte. The campaign is only evidence if all of this holds, so all
of it is checked:

- **A control first.** Every killer test runs against the unmutated tree and
  must pass; otherwise a "kill" would only be a test that was failing anyway.
- **Exactly one match per edit.** A target that is missing or appears twice is
  an error, never a silent no-op or a mutation in the wrong place.
- **The source comes back.** Each edited file's SHA-256 is compared after the
  restore, and `git status --porcelain` must be what it was before.
- **A kill must be a test's verdict.** A run that failed to collect, could not
  import, or hit a syntax error is reported as `invalid`, not as a kill: it
  proves the mutant broke Python, not that a test guards the property.
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
class Edit:
    path: str
    old: str
    new: str


@dataclass(frozen=True)
class Mutant:
    identifier: str
    #: The property the mutant removes, in the words of the brief.
    removes: str
    edits: tuple[Edit, ...]
    tests: tuple[str, ...]


IDENTITY = "tests/integration/test_omnichannel_identity.py"
ECHO = "tests/integration/test_omnichannel_echo_and_dedup.py"
SECOND = "tests/integration/test_omnichannel_second_channel.py"
OPERATIONS = "tests/integration/test_omnichannel_operations.py"
ORACLES = "tests/integration/test_omnichannel_oracles.py"
STATUS = "tests/integration/test_omnichannel_status_contract.py"
POLICY = "tests/unit/test_channel_policy_contract.py"
PARSING = "tests/unit/test_whatsapp_identity_parsing.py"
OUTBOUND = "tests/unit/test_channel_outbound_contract.py"
LIVE = "tests/integration/test_live_turn_identity.py"

CHANNEL_REPOSITORY = "app/repositories/channel_repository.py"
CONVERSATION_REPOSITORY = "app/repositories/conversation_repository.py"
MESSAGING = "app/services/messaging_service.py"
INGESTION = "app/services/channel_ingestion_service.py"
IDENTITIES = "app/services/contact_identity_service.py"
PROJECTION = "app/services/conversation_service.py"

MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        "M1",
        "ignore the connection (the provider scope) in identity lookup",
        (
            Edit(
                CHANNEL_REPOSITORY,
                "                ContactIdentity.scope == scope,\n"
                "                ContactIdentity.scope_ref == scope_ref,\n"
                "                ContactIdentity.value == value,\n"
                "            )\n"
                "        )\n"
                "\n"
                "    async def map_by_ids(",
                "                ContactIdentity.scope == scope,\n"
                "                ContactIdentity.value == value,\n"
                "            )\n"
                "        )\n"
                "\n"
                "    async def map_by_ids(",
            ),
        ),
        (
            f"{IDENTITY}::test_a_business_scoped_id_is_scoped_by_the_business_account",
            f"{ORACLES}::test_the_identity_partition_is_exactly_what_providers_asserted",
        ),
    ),
    Mutant(
        "M2",
        "ignore the channel/connection in provider-message dedup",
        (
            Edit(
                CONVERSATION_REPOSITORY,
                '        """The message this provider id names on this connection (ADR-120)."""\n'
                "        return await self._first(\n"
                "            self._select().where(\n"
                "                Message.connection_id == connection_id,\n",
                '        """The message this provider id names on this connection (ADR-120)."""\n'
                "        return await self._first(\n"
                "            self._select().where(\n",
            ),
        ),
        (
            f"{ECHO}::test_a_customer_message_id_seen_on_another_number_is_not_a_duplicate",
            f"{STATUS}::test_a_receipt_on_one_connection_never_moves_anothers_message",
        ),
    ),
    Mutant(
        "M3",
        "route outbound to the contact's phone instead of the conversation's participant",
        (
            Edit(
                MESSAGING,
                "        participant = await self._identities.require_by_id("
                "conversation.participant_identity_id)\n",
                "        participant = await self._identities.phone_of(conversation.contact_id)"
                " or await self._identities.require_by_id("
                "conversation.participant_identity_id)\n",
            ),
        ),
        (f"{IDENTITY}::test_a_reply_goes_to_the_participant_even_after_the_contact_gains_a_phone",),
    ),
    Mutant(
        "M4",
        "accept an external identity from another workspace",
        (
            Edit(
                CHANNEL_REPOSITORY,
                '        """The identity with exactly this scoped value, in this workspace, '
                'if one exists."""\n'
                "        return await self._first(\n"
                "            self._select().where(\n",
                '        """The identity with exactly this scoped value, in this workspace, '
                'if one exists."""\n'
                "        return await self._first(\n"
                '            __import__("sqlalchemy").select(ContactIdentity).where(\n',
            ),
        ),
        (f"{IDENTITY}::test_an_identifier_is_never_matched_across_workspaces",),
    ),
    Mutant(
        "M5",
        "apply the WhatsApp policy to a non-WhatsApp conversation",
        (
            Edit(
                MESSAGING,
                "        adapter = self._channels.adapter_for(conversation.channel)\n"
                "        policy = adapter.policy\n",
                "        adapter = self._channels.adapter_for(conversation.channel)\n"
                "        policy = self._channels.adapter_for(Channel.WHATSAPP).policy\n",
            ),
        ),
        (
            f"{SECOND}::test_another_channels_window_is_its_own_not_whatsapps",
            f"{SECOND}::test_a_byte_bounded_channel_refuses_text_that_fits_whatsapp",
        ),
    ),
    Mutant(
        "M6",
        "let a non-WhatsApp conversation use the WhatsApp outbound adapter",
        (
            Edit(
                MESSAGING,
                "        adapter = self._channels.adapter_for(conversation.channel)\n"
                "        policy = adapter.policy\n",
                "        adapter = self._channels.adapter_for(Channel.WHATSAPP)\n"
                "        policy = self._channels.adapter_for(conversation.channel).policy\n",
            ),
        ),
        (f"{SECOND}::test_a_reply_goes_through_the_conversations_own_adapter",),
    ),
    Mutant(
        "M7",
        "drop the connection filter from the inbox query",
        (
            Edit(
                CONVERSATION_REPOSITORY,
                "        if connection_id is not None:\n"
                "            query = query.where(Conversation.account_id == connection_id)\n",
                "        if connection_id is not None:\n            pass\n",
            ),
        ),
        (f"{SECOND}::test_the_inbox_narrows_to_one_channel_and_one_connection",),
    ),
    Mutant(
        "M8",
        "merge identities by display name",
        (
            Edit(
                IDENTITIES,
                "        for _ in range(MAX_RESOLUTION_ATTEMPTS):\n",
                "        self._name = profile_name\n"
                "        for _ in range(MAX_RESOLUTION_ATTEMPTS):\n",
            ),
            Edit(
                IDENTITIES,
                "        if not known:\n            return await self._create(\n",
                "        if not known:\n"
                '            twin = await self._session.scalar(__import__("sqlalchemy")'
                ".select(Contact).where(Contact.tenant_id == self._tenant_id,"
                ' Contact.display_name == getattr(self, "_name", None)))\n'
                "            if twin is not None:\n"
                "                for identifier in identifiers:\n"
                "                    held = await self._attach(contact=twin,"
                " connection=connection, identifier=identifier, scopes=scopes)\n"
                "                    if held is not None:\n"
                "                        known[identifier] = held\n"
                "                if known:\n"
                "                    return SenderResolution(contact=twin,"
                " participant=_preferred(known, participant_preference))\n"
                "            return await self._create(\n",
            ),
        ),
        (
            f"{IDENTITY}::test_the_same_display_name_never_links_two_senders",
            f"{ORACLES}::test_the_identity_partition_is_exactly_what_providers_asserted",
        ),
    ),
    Mutant(
        "M9",
        "reuse a provider message id across connections",
        (
            Edit(
                PROJECTION,
                "        if elsewhere is not None:\n"
                "            return self._collision(connection, elsewhere)\n",
                "        if elsewhere is not None:\n"
                "            return ProjectedMessage("
                "ProjectionOutcome.DUPLICATE, message=elsewhere)\n",
            ),
        ),
        (
            # The Y1 regression is what reaches this branch: an inbound event
            # carrying our own outbound id from another number. A customer's
            # id repeated on another number is intercepted earlier, by the
            # event log's workspace-wide key, and never gets here.
            f"{ECHO}::test_an_inbound_event_carrying_our_own_sent_id_on_another_number_is_a_collision",
            f"{ECHO}::test_a_customer_message_id_seen_on_another_number_is_not_a_duplicate",
        ),
    ),
    Mutant(
        "M10",
        "send a campaign through a connection other than its own",
        (
            Edit(
                "app/services/campaign_service.py",
                "        if conversation.account_id != campaign.account_id or "
                "conversation.contact_id != contact.id:\n",
                "        if False:\n",
            ),
        ),
        (f"{OPERATIONS}::test_a_campaign_never_sends_through_another_numbers_conversation",),
    ),
    Mutant(
        "M11",
        "treat an echo as a customer message and hand off an AI turn",
        (Edit(INGESTION, "        if event.kind is InboundKind.ECHO:\n", "        if False:\n"),),
        (f"{SECOND}::test_an_echo_of_our_own_send_is_evidence_not_a_turn",),
    ),
    Mutant(
        "M12",
        "count characters instead of UTF-8 bytes for a byte-limited policy",
        (
            Edit(
                "app/channels/policy.py",
                '    if unit is TextUnit.UTF8_BYTES:\n        return len(text.encode("utf-8"))\n',
                "    if unit is TextUnit.UTF8_BYTES:\n        return len(text)\n",
            ),
        ),
        (
            f"{POLICY}::test_a_byte_channel_refuses_arabic_that_fits_whatsapp",
            f"{SECOND}::test_a_byte_bounded_channel_refuses_text_that_fits_whatsapp",
        ),
    ),
    Mutant(
        "M13",
        "drop a WhatsApp message whose sender carries only a business-scoped id",
        (
            Edit(
                "app/integrations/whatsapp/payload.py",
                "                if phone is None and user_id is None:\n",
                "                if phone is None:\n",
            ),
        ),
        (
            f"{PARSING}::test_p2_a_username_sender_is_parsed_not_dropped",
            f"{IDENTITY}::test_a_username_sender_is_stored_and_answered_by_business_scoped_id",
        ),
    ),
    Mutant(
        "M14",
        "resolve the workspace from the customer's identifier instead of the connection",
        (
            Edit(
                INGESTION,
                "        connection = resolution.connection\n        if connection is None:\n",
                "        connection = (await _by_customer(self._session, event))"
                " or resolution.connection\n"
                "        if connection is None:\n",
            ),
            Edit(
                INGESTION,
                "\n\n__all__ = [\n",
                "\n\nasync def _by_customer(session, event):\n"
                "    from sqlalchemy import select as _select\n"
                "    from app.db.models.channel import ChannelConnection as _C, "
                "ContactIdentity as _I\n"
                "    if not event.sender:\n"
                "        return None\n"
                "    tenant = await session.scalar(_select(_I.tenant_id)"
                ".where(_I.value == event.sender[0].value).limit(1))\n"
                "    if tenant is None:\n"
                "        return None\n"
                "    return await session.scalar(_select(_C).where(_C.tenant_id == tenant,"
                " _C.channel == event.channel, _C.released_at.is_(None)).limit(1))\n"
                "\n\n__all__ = [\n",
            ),
        ),
        (f"{IDENTITY}::test_the_workspace_is_the_numbers_not_the_customers",),
    ),
    # ----------------------------------------------- backstops beyond M1-M14
    Mutant(
        "B1",
        "let an agent turn be claimed for a message that is not a customer's",
        (
            Edit(
                "app/repositories/agent_turn_repository.py",
                "            row.conversation_id != conversation_id\n"
                "            or row.direction is not MessageDirection.INBOUND\n"
                "            or row.origin is not MessageOrigin.CUSTOMER\n",
                "            False\n",
            ),
        ),
        (
            f"{ECHO}::test_a_turn_is_never_claimed_for_our_own_message",
            f"{ECHO}::test_a_turn_is_never_claimed_for_another_conversations_message",
        ),
    ),
    Mutant(
        "B2",
        "hand a live text turn off before its message has an id",
        (
            Edit(
                CONVERSATION_REPOSITORY,
                "            id=uuid.uuid4(),\n            tenant_id=self.tenant_id,\n"
                "            conversation_id=conversation_id,\n"
                "            connection_id=connection_id,\n",
                "            tenant_id=self.tenant_id,\n"
                "            conversation_id=conversation_id,\n"
                "            connection_id=connection_id,\n",
            ),
        ),
        (f"{LIVE}::test_a_text_message_hands_off_a_turn_naming_that_message",),
    ),
    Mutant(
        "B3",
        "address a business-scoped participant with `to`",
        (
            Edit(
                "app/integrations/whatsapp/adapter.py",
                '        return {"recipient_user_id": recipient.value}\n',
                '        return {"to": recipient.value}\n',
            ),
        ),
        (f"{OUTBOUND}::test_a_business_scoped_participant_is_sent_as_recipient_and_never_to",),
    ),
    Mutant(
        "B4",
        "read another Meta product's payload as WhatsApp's",
        (
            Edit(
                "app/integrations/whatsapp/payload.py",
                "    if obj != WHATSAPP_OBJECT:\n",
                "    if False:\n",
            ),
        ),
        (f"{PARSING}::test_p5_p6_another_products_delivery_is_refused_and_counted",),
    ),
)

# What a run that never reached a test's assertions looks like.
_INVALID = (
    "ERROR collecting",
    "SyntaxError",
    "IndentationError",
    "ImportError while importing",
    "found no collectors",
    "no tests ran",
)


def _git_status() -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"],  # noqa: S607 - git on PATH, like CI
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _apply(source: str, edit: Edit, identifier: str) -> str:
    newline = "\r\n" if "\r\n" in source else "\n"
    old = edit.old.replace("\n", newline)
    new = edit.new.replace("\n", newline)
    found = source.count(old)
    if found != 1:
        raise ValueError(f"{identifier}: target in {edit.path} matched {found} times, not once")
    return source.replace(old, new)


def _pytest(tests: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and test selectors
        [
            sys.executable,
            "-m",
            "pytest",
            "-o",
            "addopts=--strict-markers --strict-config",
            "-q",
            "-p",
            "no:cacheprovider",
            *tests,
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )


def _verdict(result: subprocess.CompletedProcess[str]) -> tuple[str, str]:
    output = result.stdout + result.stderr
    if result.returncode == 0:
        return "survived", ""
    if any(marker in output for marker in _INVALID):
        return "invalid", next((line for line in output.splitlines() if "Error" in line), "")
    failed = [line for line in output.splitlines() if line.startswith("FAILED ")]
    if not failed:
        return "invalid", output.strip().splitlines()[-1] if output.strip() else ""
    # The first assertion or exception the failing test raised: what an
    # inspector reads to judge whether the kill is the property, not an accident.
    raised = next((line for line in output.splitlines() if line.startswith("E ")), "")
    return "killed", f"{failed[0]} || {raised.strip()}"


def control(mutants: tuple[Mutant, ...]) -> dict[str, object]:
    tests = tuple(dict.fromkeys(test for mutant in mutants for test in mutant.tests))
    result = _pytest(tests)
    summary = (result.stdout.strip().splitlines() or [""])[-1]
    return {
        "id": "control",
        "tests": len(tests),
        "passed": result.returncode == 0,
        "summary": summary,
    }


def run(mutant: Mutant) -> dict[str, object]:
    before = _git_status()
    originals: dict[str, bytes] = {}
    for edit in mutant.edits:
        path = ROOT / edit.path
        if edit.path not in originals:
            originals[edit.path] = path.read_bytes()
    altered: dict[str, str] = {name: content.decode("utf-8") for name, content in originals.items()}
    for edit in mutant.edits:
        altered[edit.path] = _apply(altered[edit.path], edit, mutant.identifier)
    result: subprocess.CompletedProcess[str] | None = None
    try:
        for name, text in altered.items():
            (ROOT / name).write_bytes(text.encode("utf-8"))
        result = _pytest(mutant.tests)
    finally:
        for name, content in originals.items():
            (ROOT / name).write_bytes(content)
    restored = all(
        hashlib.sha256((ROOT / name).read_bytes()).digest() == hashlib.sha256(content).digest()
        for name, content in originals.items()
    )
    clean = _git_status() == before
    if not (restored and clean):
        raise RuntimeError(f"{mutant.identifier}: the source was not restored exactly")
    if result is None:
        raise RuntimeError(f"{mutant.identifier}: the tests did not run")
    verdict, evidence = _verdict(result)
    return {
        "id": mutant.identifier,
        "removes": mutant.removes,
        "files": sorted(originals),
        "verdict": verdict,
        "evidence": re.sub(r"\s+", " ", evidence)[:500],
        "restored": restored and clean,
    }


def main(argv: list[str]) -> int:
    selected = set(argv)
    chosen = tuple(m for m in MUTANTS if not selected or m.identifier in selected)
    baseline = control(chosen)
    print(json.dumps(baseline), flush=True)  # noqa: T201 - CLI output
    if not baseline["passed"]:
        return 2
    verdicts: dict[str, int] = {}
    for mutant in chosen:
        try:
            outcome = run(mutant)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            outcome = {"id": mutant.identifier, "verdict": "error", "evidence": str(error)}
        verdicts[str(outcome["verdict"])] = verdicts.get(str(outcome["verdict"]), 0) + 1
        print(json.dumps(outcome, ensure_ascii=False), flush=True)  # noqa: T201 - CLI output
    print(json.dumps({"summary": verdicts, "mutants": len(chosen)}), flush=True)  # noqa: T201
    return 0 if verdicts.get("killed", 0) == len(chosen) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
