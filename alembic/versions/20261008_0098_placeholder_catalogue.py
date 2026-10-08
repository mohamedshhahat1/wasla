"""The placeholder catalogue: channel capacity, channel types and channel top-ups.

Revision ID: 0098
Revises: 0097

ADR-131 (ENT-20, ENT-11, ENT-17; choice B of ENT-05). Every figure here is a
placeholder platform staff confirm or replace through the platform API (ENT-24):
nothing in the code reads a plan's name or code to decide behaviour.

1. **A top-up product may have no price yet.** `topup_products.price` becomes
   nullable, and `ck_topup_products_priced_when_active` refuses an active
   product without one: a product nobody has priced is never offered. Prices
   are never invented here (ENT-20).
2. **New versions of the seeded plans** - `starter`, `pro`, `business` and
   `enterprise`, the catalogue's own (not a workspace's custom plan) - for any
   whose latest version was published before ADR-131 (it states no channel
   types). Each is the latest version's terms with the retired
   `whatsapp_numbers` carried over as `channel_connections`, unchanged - the
   capacity the old version is already read at (choice A) - and the channel
   types it is now sold with:

   | plan | channel connections | channel types |
   | --- | --- | --- |
   | Starter | 1 | whatsapp |
   | Pro | 3 | whatsapp, instagram, messenger |
   | Business | 10 | all five |
   | Enterprise | unlimited | all five |

   Effective at this migration: new checkouts buy them; existing subscribers
   stay pinned to the version they bought until an explicit migration at their
   renewal (ENT-17). Every active price of the latest version is carried
   over - the headline one by the trigger that publishes a version's own
   price - so the new version sells at exactly the prices the old one did. The
   plan rows mirror the new terms, as a version published through the API does.
3. **Six channel top-up products**: one general slot and one per channel type,
   each +1 connection, global, offered to every plan, **inactive and
   unpriced** until an operator sets a price and activates it.

**Online.** One catalogue table's `DROP NOT NULL` (metadata only) and a CHECK
added `NOT VALID` then validated over a handful of rows; inserts into
catalogue tables. Nothing touches a hot table. `lock_timeout` is bounded.

**Downgrade refuses** while anything uses what this revision added: a
subscription, scheduled change, invoice, offer, migration, later version or
price change on one of these versions; a purchase of, or an operator's price,
activation or eligibility on, one of these products; or any other product
without a price. Otherwise it deletes the versions with their prices and the
products, restores each plan row from its now-latest version, and makes
`price` required again.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
RUNBOOK = "docs/RUNBOOK.md, 'Entitlements and channel capacity (0092-0098)'"

#: What every row this revision writes says it is, so the downgrade finds
#: exactly those rows and nothing an operator wrote.
REASON = (
    "ADR-131 placeholder catalogue (ENT-20): the number limit as channel connections, "
    "and the channel types the plan is sold with. Confirm or republish."
)
PRODUCT_DESCRIPTION = (
    "One more channel connection. A placeholder (ENT-20): inactive until platform staff "
    "set its price."
)
EVERY_CHANNEL = ["whatsapp", "instagram", "messenger", "telegram", "tiktok"]

#: The catalogue's plans and the channel types each is now sold with.
CHANNEL_TYPES: dict[str, list[str]] = {
    "starter": ["whatsapp"],
    "pro": ["whatsapp", "instagram", "messenger"],
    "business": EVERY_CHANNEL,
    "enterprise": EVERY_CHANNEL,
}

#: (code, name, channel type); `None` is a general slot.
PRODUCTS: tuple[tuple[str, str, str | None], ...] = (
    ("channel-connection-1", "Channel connection +1", None),
    ("whatsapp-connection-1", "WhatsApp connection +1", "whatsapp"),
    ("instagram-connection-1", "Instagram connection +1", "instagram"),
    ("messenger-connection-1", "Messenger connection +1", "messenger"),
    ("telegram-connection-1", "Telegram connection +1", "telegram"),
    ("tiktok-connection-1", "TikTok connection +1", "tiktok"),
)
PRODUCT_CODES = [code for code, _, _ in PRODUCTS]

PRICED_WHEN_ACTIVE = "ck_topup_products_priced_when_active"
PUBLISHED_PRICE_REASON = "Published with the plan version."

# The latest version's terms with the number limit as channel capacity.
NEW_LIMITS = (
    "(v.limits - 'whatsapp_numbers') || CASE WHEN v.limits ? 'whatsapp_numbers'"
    " THEN jsonb_build_object('channel_connections', v.limits -> 'whatsapp_numbers')"
    " ELSE '{}'::jsonb END"
)

DOWNGRADE_PRECHECKS: tuple[tuple[str, str], ...] = (
    (
        "subscriptions on a placeholder version, now or scheduled",
        "SELECT count(*) FROM subscriptions s JOIN plan_versions v"
        " ON v.id IN (s.plan_version_id, s.scheduled_plan_version_id) WHERE v.reason = :reason",
    ),
    (
        "invoices for a placeholder version",
        "SELECT count(*) FROM invoices i JOIN plan_versions v ON v.id = i.plan_version_id"
        " WHERE v.reason = :reason",
    ),
    (
        "custom plan offers of a placeholder version",
        "SELECT count(*) FROM custom_plan_offers o JOIN plan_versions v"
        " ON v.id = o.plan_version_id WHERE v.reason = :reason",
    ),
    (
        "migrations to or from a placeholder version",
        "SELECT count(*) FROM plan_version_migrations m JOIN plan_versions v"
        " ON v.id IN (m.from_version_id, m.to_version_id) WHERE v.reason = :reason",
    ),
    (
        "versions published after a placeholder version",
        "SELECT count(*) FROM plan_versions later JOIN plan_versions v"
        " ON v.plan_id = later.plan_id AND later.version > v.version WHERE v.reason = :reason",
    ),
    (
        "prices of a placeholder version an operator added or retired",
        "SELECT count(*) FROM plan_prices p JOIN plan_versions v ON v.id = p.plan_version_id"
        " WHERE v.reason = :reason AND (p.retired_at IS NOT NULL OR p.created_by IS NOT NULL"
        " OR p.reason NOT IN (:reason, :published))",
    ),
    (
        "purchases of a placeholder channel product",
        "SELECT count(*) FROM topup_purchases t JOIN topup_products p ON p.id = t.topup_product_id"
        " WHERE p.code = ANY(:codes) AND p.description = :description",
    ),
    (
        "placeholder channel products an operator has priced, activated or offered to plans",
        "SELECT count(*) FROM topup_products p WHERE p.code = ANY(:codes)"
        " AND p.description = :description AND (p.price IS NOT NULL OR p.is_active"
        " OR EXISTS (SELECT 1 FROM topup_product_plans e WHERE e.topup_product_id = p.id))",
    ),
    (
        "other top-up products with no price",
        "SELECT count(*) FROM topup_products p WHERE p.price IS NULL"
        " AND NOT (p.code = ANY(:codes) AND p.description = :description)",
    ),
)


def _publish(latest: dict[str, Any], types: list[str]) -> None:
    """One new version from `latest`, with every active price carried over."""
    bind = op.get_bind()
    slot = {"v": latest["id"], "interval": latest["interval"], "currency": latest["currency"]}
    # The headline price: the version's own slot as currently sold, which an
    # operator may have repriced since the version was published.
    headline = bind.execute(
        sa.text(
            "SELECT amount FROM plan_prices WHERE plan_version_id = :v AND retired_at IS NULL"
            " AND billing_interval::text = :interval AND interval_count = 1"
            " AND currency = :currency"
        ),
        slot,
    ).scalar()
    price = headline if headline is not None else latest["price"]
    version_id = bind.execute(
        sa.text(
            "INSERT INTO plan_versions (id, plan_id, version, name, price, currency, interval,"  # noqa: S608 - module constant
            " trial_days, limits, allowed_channel_types, effective_at, created_at, created_by,"
            " reason)"
            " SELECT gen_random_uuid(), v.plan_id, v.version + 1, v.name, :price, v.currency,"
            f" v.interval, v.trial_days, {NEW_LIMITS}, CAST(:types AS varchar[]), now(), now(),"
            " NULL, :reason FROM plan_versions v WHERE v.id = :v RETURNING id"
        ),
        {"v": latest["id"], "price": price, "types": types, "reason": REASON},
    ).scalar_one()
    # Every other active price, in its own slot; the headline one the version
    # trigger has just published.
    bind.execute(
        sa.text(
            "INSERT INTO plan_prices (id, plan_version_id, billing_interval, interval_count,"
            " amount, currency, created_at, created_by, reason)"
            " SELECT gen_random_uuid(), :new, p.billing_interval, p.interval_count, p.amount,"
            " p.currency, now(), NULL, :reason FROM plan_prices p"
            " WHERE p.plan_version_id = :v AND p.retired_at IS NULL"
            " AND NOT (p.billing_interval::text = :interval AND p.interval_count = 1"
            " AND p.currency = :currency)"
        ),
        {**slot, "new": version_id, "reason": REASON},
    )
    _mirror(latest["plan_id"])


def _mirror(plan_id: Any) -> None:
    """The plan row states its latest terms, as one published through the API does."""
    op.get_bind().execute(
        sa.text(
            "UPDATE plans p SET price = v.price, currency = v.currency, interval = v.interval,"
            " trial_days = v.trial_days, limits = v.limits,"
            " allowed_channel_types = v.allowed_channel_types, revision = p.revision + 1"
            " FROM (SELECT * FROM plan_versions WHERE plan_id = :plan"
            " ORDER BY version DESC LIMIT 1) v WHERE p.id = :plan"
        ),
        {"plan": plan_id},
    )


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    bind = op.get_bind()

    # 1. A product may wait for its price; an active one never does.
    op.execute("ALTER TABLE topup_products ALTER COLUMN price DROP NOT NULL")
    op.execute(
        f"ALTER TABLE topup_products ADD CONSTRAINT {PRICED_WHEN_ACTIVE}"
        " CHECK (NOT is_active OR price IS NOT NULL) NOT VALID"
    )
    op.execute(f"ALTER TABLE topup_products VALIDATE CONSTRAINT {PRICED_WHEN_ACTIVE}")

    # 2. The catalogue's plans, where their latest terms predate ADR-131.
    for code, types in CHANNEL_TYPES.items():
        latest = (
            bind.execute(
                sa.text(
                    "SELECT v.id, v.plan_id, v.price, v.interval::text AS interval, v.currency,"
                    " v.allowed_channel_types FROM plans p"
                    " JOIN plan_versions v ON v.plan_id = p.id"
                    " WHERE p.code = :code AND p.tenant_id IS NULL"
                    " ORDER BY v.version DESC LIMIT 1"
                ),
                {"code": code},
            )
            .mappings()
            .first()
        )
        if latest is None or latest["allowed_channel_types"] is not None:
            # Absent, or already sold under terms staff published since ADR-131.
            continue
        _publish(dict(latest), types)

    # 3. The channel top-up products, waiting for their prices.
    for code, name, channel in PRODUCTS:
        bind.execute(
            sa.text(
                "INSERT INTO topup_products (id, code, name, description, entitlement_key,"
                " channel_type, quantity, price, currency, scope, tenant_id, is_active,"
                " is_public, validity_policy, created_by, revision, created_at, updated_at)"
                " VALUES (gen_random_uuid(), :code, :name, :description, 'channel_connections',"
                " CAST(:channel AS channel_kind), 1, NULL, 'EGP', 'global', NULL, false, true,"
                " 'current_period_end', NULL, 1, now(), now())"
                " ON CONFLICT (code) DO NOTHING"
            ),
            {"code": code, "name": name, "description": PRODUCT_DESCRIPTION, "channel": channel},
        )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    params = {
        "reason": REASON,
        "published": PUBLISHED_PRICE_REASON,
        "codes": PRODUCT_CODES,
        "description": PRODUCT_DESCRIPTION,
    }
    found = []
    for label, query in DOWNGRADE_PRECHECKS:
        count = bind.execute(sa.text(query), params).scalar_one()
        if count:
            found.append(f"{label}: {count}")
    if found:
        raise RuntimeError(
            f"The placeholder catalogue cannot be removed ({RUNBOOK}). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    bind.execute(
        sa.text(
            "DELETE FROM topup_products WHERE code = ANY(:codes) AND description = :description"
        ),
        params,
    )
    plans = [
        row[0]
        for row in bind.execute(
            sa.text("SELECT DISTINCT plan_id FROM plan_versions WHERE reason = :reason"), params
        ).all()
    ]
    bind.execute(
        sa.text(
            "DELETE FROM plan_prices WHERE plan_version_id IN"
            " (SELECT id FROM plan_versions WHERE reason = :reason)"
        ),
        params,
    )
    bind.execute(sa.text("DELETE FROM plan_versions WHERE reason = :reason"), params)
    for plan_id in plans:
        _mirror(plan_id)
    op.execute(f"ALTER TABLE topup_products DROP CONSTRAINT {PRICED_WHEN_ACTIVE}")
    op.execute("ALTER TABLE topup_products ALTER COLUMN price SET NOT NULL")
    op.execute("RESET lock_timeout")
