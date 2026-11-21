"use client";

/**
 * FE-8's shared parts (ADR-202): the billing area's frame and tabs, money and dates as the API
 * states them, the role rules (OQ-5 - the code's roles: the owner reads and acts on everything; an
 * administrator reads the plan, usage and capacity; a member has no billing), and the one function
 * every hosted payment goes through (CK-01 .. CK-04).
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import NextLink from "next/link";
import { useLocale, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import {
  ApiError,
  queryKey,
  unwrap,
  type QueryKey,
  type QueryParams,
  type QueryScope,
} from "@wasla/api-client";
import { Button, EmptyState, ErrorState, SettingsNav } from "@wasla/ui";

import { checkoutUrlAllowed, newAttemptKey } from "../../lib/billing/checkout";
import { formatMoney } from "../../lib/billing/money";
import { focusIfLost } from "../../lib/focus";
import { workspaceKey } from "../../lib/team/keys";
import { hooksOn } from "../../lib/test-hooks";
import { useWorkspace } from "../app/workspace-context";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";
import { ScreenHead } from "../team/screen-head";

export type BillingTab = "plan" | "usage" | "capacity" | "invoices" | "methods";

export const BILLING_HEADING = "billing-title";

/** The billing resources of plan section 21's keys. */
export const billingKey = (scope: QueryScope, resource: string, params?: QueryParams): QueryKey =>
  queryKey(scope, `billing-${resource}`, params ?? {});
/** Every billing read of this workspace at once (after a payment or a change). */
export const billingFamily = (scope: QueryScope): readonly [string, number] => [
  scope.workspaceId,
  scope.sessionGeneration,
];

export function useBillingRoles(): { owner: boolean; reader: boolean } {
  const { role } = useWorkspace();
  return {
    owner: role === "tenant_owner",
    reader: role === "tenant_owner" || role === "tenant_admin",
  };
}

/** The tabs a role may open: absent, never disabled (OQ-5). */
export function billingTabs(owner: boolean): BillingTab[] {
  return owner
    ? ["plan", "usage", "capacity", "invoices", "methods"]
    : ["plan", "usage", "capacity"];
}

const TAB_HREF: Record<BillingTab, string> = {
  plan: "/billing",
  usage: "/billing/usage",
  capacity: "/billing/capacity",
  invoices: "/billing/invoices",
  methods: "/billing/payment-methods",
};

export function BillingFrame({
  current,
  line,
  children,
}: {
  current: BillingTab;
  line?: ReactNode;
  children: ReactNode;
}) {
  const t = useTranslations("app.billing");
  const { owner } = useBillingRoles();
  // The billing area's own navigation sits where the settings area's does (LY7-01, DESIGN.md):
  // beside the page from `md`, a wrapped row of links above it on a phone - never stacked inside
  // the head band, which made the band half the screen (FEF-46).
  return (
    <div
      className="flex flex-col gap-6 md:flex-row md:items-start"
      data-screen={`billing-${current}`}
    >
      <div className="md:w-nav md:shrink-0">
        <SettingsNav
          label={t("tabs.label")}
          items={billingTabs(owner).map((tab) => ({
            key: tab,
            href: TAB_HREF[tab],
            label: t(`tabs.${tab}`),
          }))}
          current={current}
          renderLink={({ href, children: content, ...rest }) => (
            <NextLink href={href} {...rest}>
              {content}
            </NextLink>
          )}
        />
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-6">
        <ScreenHead id={BILLING_HEADING} title={t("title")} line={line ?? t("line")} />
        {children}
      </div>
    </div>
  );
}

/** A billing page reached by a role it is not for: words and a way home, and no request. */
export function NotForBillingRole() {
  const t = useTranslations("app.billing");
  return (
    <EmptyState
      headingLevel={1}
      title={t("ownerOnly")}
      action={
        <NextLink href="/" className="text-primary underline underline-offset-4">
          {t("title")}
        </NextLink>
      }
    />
  );
}

export function useMoney(): (amount: string | null | undefined, currency: string) => string {
  const locale = useLocale();
  return (amount, currency) => formatMoney(amount, currency, locale);
}

/** The workspace's own record (its address and time zone), FE-7's query shared. */
export function useWorkspaceRecord() {
  const { scope } = useWorkspace();
  const { session } = useSession();
  return useQuery({
    queryKey: workspaceKey(scope),
    queryFn: () => unwrap(session.api.GET("/api/v1/workspace")),
  });
}

/** A date in the workspace's time zone, in the page's language (FE-7's rule). */
export function useDate(): (iso: string | null | undefined) => string {
  const locale = useLocale();
  const timezone = useWorkspaceRecord().data?.timezone;
  return (iso) => {
    if (!iso) return "";
    const at = Date.parse(iso);
    if (!Number.isFinite(at)) return "";
    try {
      return new Intl.DateTimeFormat(locale, {
        dateStyle: "medium",
        ...(timezone ? { timeZone: timezone } : {}),
      }).format(at);
    } catch {
      return new Intl.DateTimeFormat(locale, { dateStyle: "medium" }).format(at);
    }
  };
}

export function LoadFailed({ error, retry }: { error: unknown; retry: () => void }) {
  const t = useTranslations("app.billing");
  const errors = useCodeText();
  return (
    <ErrorState
      headingLevel={2}
      title={t("loadError")}
      description={errors(error instanceof ApiError ? error.code : null)}
      requestId={error instanceof ApiError ? error.requestId : null}
      requestIdLabel={t("reference")}
      action={
        <Button variant="secondary" onClick={retry}>
          {t("retry")}
        </Button>
      }
    />
  );
}

export function Loading() {
  const t = useTranslations("app.billing");
  return (
    <p role="status" className="m-0 py-6 text-center text-md text-text-muted">
      {t("loading")}
    </p>
  );
}

/** What a hosted payment is for (CK-01). */
export type HostedPayment =
  | { kind: "plan"; planPriceId: string | null; planCode: string }
  | { kind: "invoice"; invoiceId: string }
  | { kind: "topup"; topupId: string }
  | { kind: "offer"; offerId: string };

/**
 * One function for every hosted payment (CK-01): a fresh idempotency key per press, the one POST,
 * then a top-level navigation to the URL the API returned - only after CK-03's check. Nothing of
 * the answer is stored, logged or rendered. A refusal keeps focus where it was (focusIfLost).
 */
export function useHostedPayment(options?: { onRefused?: () => void }) {
  const { session } = useSession();
  const t = useTranslations("app.billing");
  const errors = useCodeText();
  const start = useMutation({
    mutationFn: async (payment: HostedPayment) => {
      const key = newAttemptKey();
      const answer =
        payment.kind === "plan"
          ? await unwrap(
              session.api.POST("/api/v1/billing/checkout", {
                body: payment.planPriceId
                  ? { plan_price_id: payment.planPriceId, idempotency_key: key }
                  : { plan_code: payment.planCode, idempotency_key: key },
              }),
            )
          : payment.kind === "invoice"
            ? await unwrap(
                session.api.POST("/api/v1/billing/checkout", {
                  body: { invoice_id: payment.invoiceId, idempotency_key: key },
                }),
              )
            : payment.kind === "topup"
              ? await unwrap(
                  session.api.POST("/api/v1/billing/topups/{topup_id}/checkout", {
                    params: { path: { topup_id: payment.topupId } },
                    body: { idempotency_key: key },
                  }),
                )
              : await unwrap(
                  session.api.POST("/api/v1/billing/custom-offers/{offer_id}/accept", {
                    params: { path: { offer_id: payment.offerId } },
                    body: { idempotency_key: key },
                  }),
                );
      const url = (answer as { redirect_url?: unknown }).redirect_url;
      if (!checkoutUrlAllowed(url, { e2eHooks: hooksOn() })) throw new BadCheckoutUrl();
      return url as string;
    },
    onSuccess: (url) => {
      // The last thing the page does: leave for the payment page (CK-04).
      window.location.assign(url);
    },
    onError: () => {
      options?.onRefused?.();
      requestAnimationFrame(() => focusIfLost(document.getElementById(BILLING_HEADING)));
    },
  });
  const code = start.error instanceof ApiError ? start.error.code : null;
  const message = start.isError
    ? start.error instanceof BadCheckoutUrl
      ? t("checkout.badUrl")
      : errors(code)
    : null;
  return { start, message, code };
}

export class BadCheckoutUrl extends Error {
  constructor() {
    super("the payment page address was refused");
    this.name = "BadCheckoutUrl";
  }
}

/** The opening and refusal lines that sit under a pressed "Pay". */
export function HostedPaymentStatus({
  pending,
  message,
}: {
  pending: boolean;
  message: string | null;
}) {
  const t = useTranslations("app.billing");
  if (pending) {
    return (
      <p role="status" className="m-0 text-sm text-text-muted" data-checkout="opening">
        {t("checkout.opening")}
      </p>
    );
  }
  if (message) {
    return (
      <p role="alert" className="m-0 text-sm font-medium text-danger" data-checkout="refused">
        {message}
      </p>
    );
  }
  return null;
}
