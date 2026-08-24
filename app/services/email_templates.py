"""The transactional email this product can send. A closed vocabulary.

Every template is code in this module: there is no user-supplied template, no
template-by-name lookup outside the enum, and no way for a caller to add a
header or a recipient through a variable. Three rules hold for all of them:

**Every variable is escaped.** Values are HTML-escaped into the HTML body and
placed verbatim only in the plain-text body. Nothing caller-influenced
reaches a subject - subjects are constants.

**Every link is built here.** From `APP_PUBLIC_URL` plus a fixed path, with
the token URL-encoded into the query. A template variable can never redirect
a link somewhere else, because no variable is ever a URL.

**Transactional only.** These exist because an account or a workspace needs
to be told something. There is no marketing template and no unsubscribe
link - a security notice is not something to opt out of, and the day a
marketing email exists it must be a different system with different consent.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from urllib.parse import urlencode


class EmailTemplate(StrEnum):
    """Everything the product knows how to say by email."""

    WORKSPACE_INVITATION = "workspace_invitation"
    PASSWORD_RESET = "password_reset"
    PASSWORD_CHANGED = "password_changed"
    SESSIONS_REVOKED = "sessions_revoked"
    ACCOUNT_DISABLED = "account_disabled"
    ACCOUNT_ENABLED = "account_enabled"
    INVOICE_ISSUED = "invoice_issued"
    TRIAL_EXPIRED = "trial_expired"
    SUBSCRIPTION_CANCELLED = "subscription_cancelled"


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    subject: str
    text: str
    html: str


# Constants, deliberately: a subject travels in a header, and the way to be
# certain nothing caller-influenced reaches a header is for no variable to.
_SUBJECTS: Final[dict[EmailTemplate, str]] = {
    EmailTemplate.WORKSPACE_INVITATION: "You have been invited to a workspace on Wasla",
    EmailTemplate.PASSWORD_RESET: "Reset your Wasla password",
    EmailTemplate.PASSWORD_CHANGED: "Your Wasla password was changed",
    EmailTemplate.SESSIONS_REVOKED: "You were signed out of every Wasla session",
    EmailTemplate.ACCOUNT_DISABLED: "Your Wasla account has been suspended",
    EmailTemplate.ACCOUNT_ENABLED: "Your Wasla account has been restored",
    EmailTemplate.INVOICE_ISSUED: "Your Wasla invoice is ready",
    EmailTemplate.TRIAL_EXPIRED: "Your Wasla trial has ended",
    EmailTemplate.SUBSCRIPTION_CANCELLED: "Your Wasla subscription has been cancelled",
}

_REQUIRED_KEYS: Final[dict[EmailTemplate, frozenset[str]]] = {
    EmailTemplate.WORKSPACE_INVITATION: frozenset({"workspace_name", "token"}),
    EmailTemplate.PASSWORD_RESET: frozenset({"token"}),
    EmailTemplate.PASSWORD_CHANGED: frozenset(),
    EmailTemplate.SESSIONS_REVOKED: frozenset(),
    EmailTemplate.ACCOUNT_DISABLED: frozenset(),
    EmailTemplate.ACCOUNT_ENABLED: frozenset(),
    EmailTemplate.INVOICE_ISSUED: frozenset(
        {"amount_due", "currency", "period_start", "period_end"}
    ),
    EmailTemplate.TRIAL_EXPIRED: frozenset({"workspace_name"}),
    EmailTemplate.SUBSCRIPTION_CANCELLED: frozenset({"workspace_name"}),
}


def subject_for(template: EmailTemplate) -> str:
    return _SUBJECTS[template]


def _link(public_url: str, path: str, **params: str) -> str:
    """A link to the product's own frontend, and nowhere else.

    The origin is configuration and the path is a literal in this module, so
    the only variable part is the URL-encoded query - which is how an emailed
    link stays incapable of pointing anywhere a caller chose.
    """
    base = public_url.rstrip("/")
    if not base:
        raise ValueError("public_url is required to render an email link")
    query = urlencode(params)
    return f"{base}{path}?{query}" if query else f"{base}{path}"


def _layout(title: str, paragraphs: list[str], link: tuple[str, str] | None) -> str:
    """One HTML shape for every message: readable, accessible, unbranded-plain.

    Table-free and inline-styled because email clients are what they are. The
    paragraphs arrive already escaped; this function adds no variable content
    of its own.
    """
    body = "".join(
        f'<p style="margin:0 0 16px 0;font-size:15px;line-height:1.6;color:#1f2933;">{p}</p>'
        for p in paragraphs
    )
    button = ""
    if link is not None:
        href, label = link
        button = (
            f'<p style="margin:24px 0;"><a href="{html.escape(href, quote=True)}" '
            'style="background:#1f6feb;color:#ffffff;text-decoration:none;'
            'padding:12px 20px;border-radius:6px;font-size:15px;display:inline-block;">'
            f"{html.escape(label)}</a></p>"
        )
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title></head>"
        '<body style="margin:0;padding:0;background:#f5f7fa;">'
        '<div role="article" aria-roledescription="email" '
        'style="max-width:560px;margin:0 auto;padding:32px 24px;'
        'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">'
        f'<h1 style="font-size:18px;margin:0 0 20px 0;color:#111827;">{html.escape(title)}</h1>'
        f"{body}{button}"
        '<p style="margin:24px 0 0 0;font-size:12px;color:#6b7280;">'
        "This is a transactional message from Wasla about your account or workspace.</p>"
        "</div></body></html>"
    )


def render(
    template: EmailTemplate,
    context: Mapping[str, str],
    *,
    public_url: str,
) -> RenderedEmail:
    """Render one message, refusing an incomplete context.

    Raises ValueError rather than rendering around a hole: a reset email
    without its token is not a degraded email, it is a broken promise, and
    the outbox worker records the refusal as a permanent failure of that row.
    """
    missing = sorted(_REQUIRED_KEYS[template] - set(context))
    if missing:
        raise ValueError(
            f"template {template.value} is missing context keys: {', '.join(missing)}"
        )
    subject = _SUBJECTS[template]

    if template is EmailTemplate.WORKSPACE_INVITATION:
        workspace = context["workspace_name"]
        link = _link(public_url, "/invitations/accept", token=context["token"])
        text = (
            f"You have been invited to join the workspace \"{workspace}\" on Wasla.\n\n"
            f"Accept the invitation:\n{link}\n\n"
            "The invitation expires in 7 days and can be used once. If you were not "
            "expecting it, ignore this message and nothing will happen."
        )
        html_body = _layout(
            subject,
            [
                "You have been invited to join the workspace "
                f"<strong>{html.escape(workspace)}</strong> on Wasla.",
                "The invitation expires in 7 days and can be used once. If you were "
                "not expecting it, ignore this message and nothing will happen.",
            ],
            (link, "Accept the invitation"),
        )
    elif template is EmailTemplate.PASSWORD_RESET:
        link = _link(public_url, "/reset-password", token=context["token"])
        text = (
            "A password reset was requested for your Wasla account.\n\n"
            f"Reset your password:\n{link}\n\n"
            "The link expires in 30 minutes and can be used once. If you did not "
            "request it, ignore this message - your password has not changed. "
            "Wasla will never ask for your password by email."
        )
        html_body = _layout(
            subject,
            [
                "A password reset was requested for your Wasla account.",
                "The link expires in 30 minutes and can be used once. If you did "
                "not request it, ignore this message - your password has not "
                "changed. Wasla will never ask for your password by email.",
            ],
            (link, "Reset your password"),
        )
    elif template is EmailTemplate.PASSWORD_CHANGED:
        text = (
            "The password on your Wasla account was just changed, and every open "
            "session was signed out.\n\n"
            "If this was you, nothing further is needed. If it was not, reset your "
            "password immediately from the sign-in page and contact support."
        )
        html_body = _layout(
            subject,
            [
                "The password on your Wasla account was just changed, and every "
                "open session was signed out.",
                "If this was you, nothing further is needed. If it was not, reset "
                "your password immediately from the sign-in page and contact support.",
            ],
            None,
        )
    elif template is EmailTemplate.SESSIONS_REVOKED:
        text = (
            "Every session on your Wasla account was just signed out.\n\n"
            "If this was you, sign in again to continue. If it was not, change "
            "your password from the sign-in page immediately."
        )
        html_body = _layout(
            subject,
            [
                "Every session on your Wasla account was just signed out.",
                "If this was you, sign in again to continue. If it was not, change "
                "your password from the sign-in page immediately.",
            ],
            None,
        )
    elif template is EmailTemplate.ACCOUNT_DISABLED:
        text = (
            "Your Wasla account has been suspended by platform staff and every "
            "session was signed out.\n\n"
            "If you believe this is a mistake, contact support."
        )
        html_body = _layout(
            subject,
            [
                "Your Wasla account has been suspended by platform staff and every "
                "session was signed out.",
                "If you believe this is a mistake, contact support.",
            ],
            None,
        )
    elif template is EmailTemplate.ACCOUNT_ENABLED:
        text = (
            "Your Wasla account has been restored.\n\n"
            "Sessions from before the suspension remain signed out; sign in again "
            "to continue."
        )
        html_body = _layout(
            subject,
            [
                "Your Wasla account has been restored.",
                "Sessions from before the suspension remain signed out; sign in "
                "again to continue.",
            ],
            None,
        )
    elif template is EmailTemplate.INVOICE_ISSUED:
        amount = f"{context['amount_due']} {context['currency']}"
        period = f"{context['period_start']} to {context['period_end']}"
        text = (
            f"An invoice for your Wasla workspace is ready: {amount} for the "
            f"period {period}.\n\n"
            "Sign in to your workspace to view it. This message contains no "
            "payment link - Wasla never asks you to pay through a link in an email."
        )
        html_body = _layout(
            subject,
            [
                "An invoice for your Wasla workspace is ready: "
                f"<strong>{html.escape(amount)}</strong> for the period "
                f"{html.escape(period)}.",
                "Sign in to your workspace to view it. This message contains no "
                "payment link - Wasla never asks you to pay through a link in an "
                "email.",
            ],
            None,
        )
    elif template is EmailTemplate.TRIAL_EXPIRED:
        workspace = context["workspace_name"]
        text = (
            f"The trial for your workspace \"{workspace}\" on Wasla has ended.\n\n"
            "Sign in and choose a plan to continue where you left off."
        )
        html_body = _layout(
            subject,
            [
                "The trial for your workspace "
                f"<strong>{html.escape(workspace)}</strong> on Wasla has ended.",
                "Sign in and choose a plan to continue where you left off.",
            ],
            None,
        )
    else:  # EmailTemplate.SUBSCRIPTION_CANCELLED
        workspace = context["workspace_name"]
        text = (
            f"The subscription for your workspace \"{workspace}\" on Wasla has "
            "been cancelled.\n\n"
            "Your data is retained; sign in and choose a plan at any time to "
            "continue."
        )
        html_body = _layout(
            subject,
            [
                "The subscription for your workspace "
                f"<strong>{html.escape(workspace)}</strong> on Wasla has been "
                "cancelled.",
                "Your data is retained; sign in and choose a plan at any time to "
                "continue.",
            ],
            None,
        )

    return RenderedEmail(subject=subject, text=text, html=html_body)
