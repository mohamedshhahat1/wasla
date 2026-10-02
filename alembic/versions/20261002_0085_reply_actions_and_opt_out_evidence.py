"""Reply actions on messages, and the evidence an opt-out arrived by.

Revision ID: 0085
Revises: 0084

OMNI-030 (ADR-124). A WhatsApp template quick reply arrives as `"type": "button"`
and an interactive reply as `interactive.button_reply|list_reply`; both used to be
stored with no text, so the agent read `[interactive]` and a "Stop promotions"
tap opted nobody out.

1. **`messages.action_source` / `action_payload` / `action_title`** - what the
   customer tapped, beside the words. A new enum type `reply_action_source`.
2. **`contacts.opt_out_via`** - by which evidence an opt-out arrived (a stop
   word, a tap, the provider's preference record, a provider refusal, a replay,
   a colleague); a new enum type `opt_out_via`. **`contacts.marketing_resumed_at`**
   - the last re-admission, so a replay of older evidence never overrides it.
3. **`whatsapp_templates.opt_out_payloads`** - the quick-reply payloads a
   workspace marked as its marketing opt-out.

**Online.** Every column is nullable with no default: a metadata-only `ADD
COLUMN`, a brief exclusive lock and no rewrite of `messages`. `lock_timeout` is
bounded, as in 0082. No backfill: raw events are retained for 30 days, and the
operator command `recover-button-opt-outs` replays the opt-outs they hold
(docs/RUNBOOK.md).

**Downgrade refuses** while any row holds an action, an opt-out route, a resume
or a payload mark - dropping them would discard a customer's recorded consent.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"

ENUMS: dict[str, tuple[str, ...]] = {
    "reply_action_source": ("button", "button_reply", "list_reply", "quick_reply", "postback"),
    "opt_out_via": (
        "message",
        "reply_action",
        "provider_preference",
        "provider_refusal",
        "replay",
        "team",
    ),
}

DOWNGRADE_PRECHECKS: tuple[tuple[str, str], ...] = (
    (
        "messages with a reply action",
        "SELECT count(*) FROM messages WHERE action_source IS NOT NULL",
    ),
    (
        "contacts with an opt-out route or a resume",
        "SELECT count(*) FROM contacts"
        " WHERE opt_out_via IS NOT NULL OR marketing_resumed_at IS NOT NULL",
    ),
    (
        "templates with opt-out payload marks",
        "SELECT count(*) FROM whatsapp_templates WHERE opt_out_payloads IS NOT NULL",
    ),
)


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def _refuse(checks: tuple[tuple[str, str], ...], preamble: str) -> None:
    connection = op.get_bind()
    found = []
    for label, query in checks:
        count = connection.exec_driver_sql(query).scalar_one()
        if count:
            found.append(f"{label}: {count}")
    if found:
        raise RuntimeError(
            f"{preamble} (docs/RUNBOOK.md, 'Omnichannel final remediation (0085-0090)'). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    bind = op.get_bind()
    for name in ENUMS:
        _enum(name).create(bind, checkfirst=True)

    op.add_column(
        "messages", sa.Column("action_source", _enum("reply_action_source"), nullable=True)
    )
    op.add_column("messages", sa.Column("action_payload", sa.String(1000), nullable=True))
    op.add_column("messages", sa.Column("action_title", sa.String(255), nullable=True))

    op.add_column("contacts", sa.Column("opt_out_via", _enum("opt_out_via"), nullable=True))
    op.add_column(
        "contacts",
        sa.Column("marketing_resumed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "whatsapp_templates",
        sa.Column("opt_out_payloads", postgresql.JSONB(), nullable=True),
    )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    _refuse(
        DOWNGRADE_PRECHECKS,
        "Rows hold reply actions or consent evidence the pre-0085 schema cannot represent",
    )
    op.drop_column("whatsapp_templates", "opt_out_payloads")
    op.drop_column("contacts", "marketing_resumed_at")
    op.drop_column("contacts", "opt_out_via")
    op.drop_column("messages", "action_title")
    op.drop_column("messages", "action_payload")
    op.drop_column("messages", "action_source")
    bind = op.get_bind()
    for name in reversed(tuple(ENUMS)):
        _enum(name).drop(bind, checkfirst=True)
    op.execute("RESET lock_timeout")
