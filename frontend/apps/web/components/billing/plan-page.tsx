"use client";

/**
 * APP-BILL-01 at /billing (BP-01 .. BP-06, OF-01 / OF-02, ADR-202): the current plan and its state
 * in words, the payment owed, an open custom offer, the plans with their monthly and yearly prices
 * and what each "Choose" does by the API's rules (lib/billing/plans.ts), and cancel / resume /
 * withdraw a scheduled change. The owner acts; an administrator reads the plan and the plans with
 * one line saying who acts - no action control is drawn for them and /billing/summary and the
 * offers are never requested for them (each is the owner's in the API). Nothing is optimistic:
 * every answer redraws from the API.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { useState } from "react";

import { ApiError, unwrap, type components } from "@wasla/api-client";
import { Badge, Button, Card, Dialog, FormField, Input, RadioGroup, Textarea } from "@wasla/ui";

import { limitAmount } from "../../lib/billing/limits";
import { isZero } from "../../lib/billing/money";
import {
  cancelNowConfirmed,
  planAction,
  subscriptionWord,
  type CurrentTerms,
  type PlanAction,
} from "../../lib/billing/plans";
import { focusIfLost } from "../../lib/focus";
import { useWorkspace } from "../app/workspace-context";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";
import { busyProps } from "../team/screen-head";
import {
  BILLING_HEADING,
  BillingFrame,
  billingFamily,
  billingKey,
  HostedPaymentStatus,
  LoadFailed,
  Loading,
  NotForBillingRole,
  useBillingRoles,
  useDate,
  useHostedPayment,
  useMoney,
  useWorkspaceRecord,
} from "./shared";

type Subscription = components["schemas"]["SubscriptionRead"];
type Plan = components["schemas"]["PlanRead"];
type Offer = components["schemas"]["CustomPlanOfferRead"];
type Summary = components["schemas"]["TenantBillingSummary"];

const OFFER_REASON_MAX = 500;

function currentTerms(subscription: Subscription | null): CurrentTerms {
  if (!subscription || subscription.ended_at || subscription.status === "expired") {
    return { none: true, planCode: null, priceId: null, amount: null };
  }
  return {
    none: false,
    planCode: subscription.plan.code,
    priceId: subscription.plan_price?.id ?? null,
    amount: subscription.plan_price?.amount ?? subscription.plan.price,
  };
}

export function PlanPage() {
  const { reader } = useBillingRoles();
  return reader ? <PlanScreen /> : <NotForBillingRole />;
}

function PlanScreen() {
  const t = useTranslations("app.billing");
  const { owner } = useBillingRoles();
  const { scope } = useWorkspace();
  const { session } = useSession();
  const subscription = useQuery({
    queryKey: billingKey(scope, "subscription"),
    queryFn: () => unwrap(session.api.GET("/api/v1/billing/subscription")),
  });
  const plans = useQuery({
    queryKey: billingKey(scope, "plans"),
    queryFn: () => unwrap(session.api.GET("/api/v1/billing/plans")),
  });
  // The owner's alone in the API (OQ-5): never requested for an administrator.
  const summary = useQuery({
    queryKey: billingKey(scope, "summary"),
    queryFn: () => unwrap(session.api.GET("/api/v1/billing/summary")),
    enabled: owner,
  });
  const offers = useQuery({
    queryKey: billingKey(scope, "offers"),
    queryFn: () => unwrap(session.api.GET("/api/v1/billing/custom-offers")),
    enabled: owner,
  });

  let body: React.ReactNode;
  if (subscription.isError || plans.isError) {
    body = (
      <LoadFailed
        error={subscription.error ?? plans.error}
        retry={() => {
          void subscription.refetch();
          void plans.refetch();
        }}
      />
    );
  } else if (subscription.isPending || plans.isPending) {
    body = <Loading />;
  } else {
    const current = subscription.data.subscription ?? null;
    body = (
      <>
        <CurrentPlan subscription={current} summary={summary.data} plans={plans.data} />
        {owner && summary.data?.payment_required?.length ? (
          <OwedPayments owed={summary.data.payment_required} />
        ) : null}
        {owner && summary.data?.open_offer ? <OpenOffer offer={summary.data.open_offer} /> : null}
        <PlanList subscription={current} plans={plans.data} />
        {owner && current ? (
          <SubscriptionActions subscription={current} plans={plans.data} />
        ) : null}
        {owner && offers.data ? <OfferHistory offers={offers.data} /> : null}
      </>
    );
  }
  return (
    <BillingFrame current="plan" line={owner ? t("line") : t("ownerOnly")}>
      {body}
    </BillingFrame>
  );
}

/* ----------------------------------------------------------------- current */

function scheduledPlanName(subscription: Subscription, plans: Plan[]): string {
  const change = subscription.scheduled_change;
  if (!change) return "";
  const plan = plans.find((candidate) =>
    candidate.prices?.some((price) => price.id === change.plan_price_id),
  );
  return plan?.name ?? "";
}

function CurrentPlan({
  subscription,
  summary,
  plans,
}: {
  subscription: Subscription | null;
  summary: Summary | undefined;
  plans: Plan[];
}) {
  const t = useTranslations("app.billing");
  const money = useMoney();
  const date = useDate();
  const word = subscriptionWord(subscription);
  let status = "";
  if (subscription) {
    status =
      word === "trialing"
        ? t("status.trialing", { date: date(subscription.trial_ends_at) })
        : word === "cancelling"
          ? t("status.cancelling", { date: date(subscription.current_period_end) })
          : word === "changing"
            ? t("status.changing", {
                plan: scheduledPlanName(subscription, plans),
                date: date(subscription.scheduled_change?.effective_at),
              })
            : t(`status.${word}`);
  }
  const tone = word === "pastDue" ? "danger" : word === "cancelling" ? "warning" : "neutral";
  return (
    <Card as="section" aria-labelledby="billing-current">
      <h2 id="billing-current" className="m-0 mb-3 text-lg font-semibold text-text">
        {t("current.title")}
      </h2>
      {subscription ? (
        <div className="flex flex-col gap-2" data-current-plan={subscription.plan.code}>
          <p className="m-0 flex flex-wrap items-center gap-3 text-xl font-semibold text-text">
            <bdi>{subscription.plan.name}</bdi>
            <span data-subscription-status={word}>
              <Badge tone={tone}>{status}</Badge>
            </span>
          </p>
          {subscription.next_renewal_amount && subscription.next_renewal_at ? (
            <p className="m-0 text-md text-text tabular-nums">
              {t("current.renews", {
                amount: money(
                  subscription.next_renewal_amount,
                  subscription.next_renewal_currency ?? subscription.plan.currency,
                ),
                date: date(subscription.next_renewal_at),
              })}
            </p>
          ) : null}
          <p className="m-0 text-sm text-text-muted">
            {t("current.period", {
              start: date(subscription.current_period_start),
              end: date(subscription.current_period_end),
            })}
          </p>
          {summary && !isZero(subscription.plan_price?.amount ?? subscription.plan.price) ? (
            <p
              className="m-0 text-sm text-text-muted"
              data-auto-renewal={summary.automatic_renewal}
            >
              {summary.automatic_renewal ? t("current.autoOn") : t("current.autoOff")}
            </p>
          ) : null}
        </div>
      ) : (
        <p className="m-0 text-md text-text-muted">{t("current.none")}</p>
      )}
    </Card>
  );
}

/* -------------------------------------------------------------------- owed */

function OwedPayments({ owed }: { owed: components["schemas"]["PaymentRequiredRead"][] }) {
  const t = useTranslations("app.billing");
  const money = useMoney();
  const date = useDate();
  const { start, message } = useHostedPayment();
  return (
    <Card as="section" aria-labelledby="billing-owed" data-owed="">
      <h2 id="billing-owed" className="m-0 mb-3 text-lg font-semibold text-text">
        {t("owed.title")}
      </h2>
      <ul className="m-0 flex list-none flex-col gap-3 p-0">
        {owed.map((row) => (
          <li
            key={row.invoice_id}
            className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between"
          >
            <span className="text-md text-text tabular-nums">
              {t("owed.row", {
                amount: money(row.amount_due, row.currency),
                start: date(row.period_start),
                end: date(row.period_end),
              })}
            </span>
            <Button
              {...busyProps(start.isPending)}
              onClick={() =>
                !start.isPending && start.mutate({ kind: "invoice", invoiceId: row.invoice_id })
              }
              data-action="pay-owed"
            >
              {t("payNow")}
            </Button>
          </li>
        ))}
      </ul>
      <HostedPaymentStatus pending={start.isPending || start.isSuccess} message={message} />
    </Card>
  );
}

/* ------------------------------------------------------------------- plans */

function PlanList({ subscription, plans }: { subscription: Subscription | null; plans: Plan[] }) {
  const t = useTranslations("app.billing");
  const locale = useLocale();
  const { owner } = useBillingRoles();
  const money = useMoney();
  const client = useQueryClient();
  const { scope } = useWorkspace();
  const { session } = useSession();
  const errors = useCodeText();
  const terms = currentTerms(subscription);
  const payment = useHostedPayment();
  const change = useMutation({
    mutationFn: async (input: { action: PlanAction; plan: Plan; priceId: string | null }) => {
      const body = input.priceId
        ? { plan_price_id: input.priceId }
        : { plan_code: input.plan.code };
      if (input.action.kind === "start-free") {
        return unwrap(session.api.POST("/api/v1/billing/subscription", { body }));
      }
      return unwrap(session.api.POST("/api/v1/billing/subscription/plan", { body }));
    },
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: billingFamily(scope) });
    },
    onError: (error, input) => {
      // The API is the judge: a plan it says must be paid for goes to checkout with that price.
      if (error instanceof ApiError && error.code === "plan_requires_checkout") {
        payment.start.mutate({
          kind: "plan",
          planCode: input.plan.code,
          planPriceId: input.priceId,
        });
        return;
      }
      requestAnimationFrame(() => focusIfLost(document.getElementById(BILLING_HEADING)));
    },
  });
  const busy = change.isPending || payment.start.isPending || payment.start.isSuccess;
  const changeCode = change.error instanceof ApiError ? change.error.code : null;

  const choose = (plan: Plan, priceId: string | null, action: PlanAction) => {
    if (busy) return;
    if (action.kind === "checkout") {
      payment.start.mutate({ kind: "plan", planCode: plan.code, planPriceId: priceId });
    } else if (action.kind !== "current") {
      change.mutate({ action, plan, priceId });
    }
  };

  return (
    <Card as="section" aria-labelledby="billing-plans">
      <h2 id="billing-plans" className="m-0 mb-4 text-lg font-semibold text-text">
        {t("plans.title")}
      </h2>
      <ul className="m-0 grid list-none grid-cols-1 gap-4 p-0 lg:grid-cols-3" data-plans="">
        {plans.map((plan) => {
          const prices = plan.prices?.length
            ? plan.prices
            : [
                {
                  id: "",
                  amount: plan.price,
                  currency: plan.currency,
                  interval: plan.interval,
                  interval_count: 1,
                },
              ];
          return (
            <li
              key={plan.id}
              className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-5"
              data-plan={plan.code}
            >
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="m-0 text-lg font-semibold text-text">
                  <bdi>{plan.name}</bdi>
                </h3>
                {plan.is_custom ? <Badge tone="neutral">{t("plans.madeForYou")}</Badge> : null}
              </div>
              {plan.description ? (
                <p className="m-0 text-sm text-text-muted">
                  <bdi>{plan.description}</bdi>
                </p>
              ) : null}
              <ul className="m-0 flex list-none flex-col gap-1 p-0 text-sm text-text">
                {plan.limits.map((limit) => (
                  <li key={limit.key} className="flex justify-between gap-3">
                    <span className="text-text-muted">{t(`limit.${limit.key}`)}</span>
                    <span className="tabular-nums">
                      {limit.limit === null
                        ? t("unlimited")
                        : limitAmount(limit.key, limit.limit, locale)}
                    </span>
                  </li>
                ))}
              </ul>
              <ul className="m-0 mt-auto flex list-none flex-col gap-2 p-0">
                {prices.map((price) => {
                  const priceId = price.id || null;
                  const action = planAction(terms, {
                    planCode: plan.code,
                    priceId,
                    amount: price.amount,
                    billingRequired: plan.billing_required ?? !isZero(price.amount),
                  });
                  const label = isZero(price.amount)
                    ? t("plans.free")
                    : `${money(price.amount, price.currency)} ${t(
                        price.interval === "yearly" ? "interval.year" : "interval.month",
                      )}`;
                  const verb =
                    action.kind === "checkout"
                      ? terms.none
                        ? t("plans.choose")
                        : t("plans.upgrade")
                      : action.kind === "switch-at-period-end"
                        ? t("plans.switchLater")
                        : action.kind === "switch-now"
                          ? t("plans.switchNow")
                          : t("plans.choose");
                  return (
                    <li
                      key={price.id || plan.code}
                      className="flex flex-wrap items-center justify-between gap-2"
                      data-price={price.interval}
                    >
                      <span className="text-md font-medium text-text tabular-nums">{label}</span>
                      {action.kind === "current" ? (
                        <Badge tone="success">{t("plans.current")}</Badge>
                      ) : owner ? (
                        <Button
                          variant={action.kind === "checkout" ? "primary" : "secondary"}
                          {...busyProps(busy)}
                          onClick={() => choose(plan, priceId, action)}
                          aria-label={t("plans.chooseOf", {
                            action: verb,
                            plan: plan.name,
                            price: label,
                          })}
                          data-action={`plan-${action.kind}`}
                          data-plan-price={`${plan.code}:${price.interval}`}
                        >
                          {verb}
                        </Button>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            </li>
          );
        })}
      </ul>
      <HostedPaymentStatus
        pending={payment.start.isPending || payment.start.isSuccess}
        message={payment.message}
      />
      {change.isError && changeCode !== "plan_requires_checkout" ? (
        <p
          role="alert"
          className="m-0 mt-3 text-sm font-medium text-danger"
          data-error={changeCode ?? ""}
        >
          {errors(changeCode)}
        </p>
      ) : null}
    </Card>
  );
}

/* ---------------------------------------------------------- cancel, resume */

function SubscriptionActions({
  subscription,
  plans,
}: {
  subscription: Subscription;
  plans: Plan[];
}) {
  const t = useTranslations("app.billing");
  const errors = useCodeText();
  const date = useDate();
  const client = useQueryClient();
  const { scope } = useWorkspace();
  const { session } = useSession();
  const slug = useWorkspaceRecord().data?.slug ?? "";
  const [open, setOpen] = useState(false);
  const [when, setWhen] = useState<"end" | "now">("end");
  const [typed, setTyped] = useState("");
  const [tried, setTried] = useState(false);
  const act = useMutation({
    mutationFn: async (kind: "cancel" | "resume" | "keep") => {
      if (kind === "cancel") {
        return unwrap(
          session.api.POST("/api/v1/billing/subscription/cancel", {
            body: { immediately: when === "now" },
          }),
        );
      }
      if (kind === "resume") {
        return unwrap(session.api.POST("/api/v1/billing/subscription/resume"));
      }
      return unwrap(session.api.POST("/api/v1/billing/subscription/scheduled-change/cancel"));
    },
    onSuccess: async () => {
      setOpen(false);
      setTyped("");
      await client.invalidateQueries({ queryKey: billingFamily(scope) });
      requestAnimationFrame(() => focusIfLost(document.getElementById(BILLING_HEADING)));
    },
  });
  const code = act.error instanceof ApiError ? act.error.code : null;
  const ended = Boolean(subscription.ended_at) || subscription.status === "cancelled";
  if (ended) return null;
  const confirmOk = when === "end" || cancelNowConfirmed(typed, slug);
  const scheduledName = scheduledPlanName(subscription, plans);
  return (
    <Card as="section" aria-labelledby="billing-actions" data-subscription-actions="">
      <div className="flex flex-wrap gap-3">
        {subscription.scheduled_change ? (
          <Button
            variant="secondary"
            {...busyProps(act.isPending)}
            onClick={() => !act.isPending && act.mutate("keep")}
            data-action="keep-plan"
          >
            {t("actions.keep", { plan: subscription.plan.name })}
          </Button>
        ) : null}
        {subscription.cancel_at_period_end ? (
          <Button
            variant="secondary"
            {...busyProps(act.isPending)}
            onClick={() => !act.isPending && act.mutate("resume")}
            data-action="resume"
          >
            {t("actions.resume")}
          </Button>
        ) : (
          <Button
            variant="ghost"
            onClick={() => {
              setWhen("end");
              setTyped("");
              setTried(false);
              act.reset();
              setOpen(true);
            }}
            data-action="cancel-subscription"
          >
            {t("actions.cancel")}
          </Button>
        )}
      </div>
      <h2 id="billing-actions" className="sr-only">
        {t("actions.cancel")}
        {scheduledName ? ` – ${scheduledName}` : ""}
      </h2>
      {act.isError && !open ? (
        <p
          role="alert"
          className="m-0 mt-3 text-sm font-medium text-danger"
          data-error={code ?? ""}
        >
          {errors(code)}
        </p>
      ) : null}
      <Dialog
        open={open}
        onOpenChange={(next) => {
          if (!next) setOpen(false);
        }}
        title={t("cancelDialog.title")}
        description={t("cancelDialog.body")}
        closeLabel={t("close")}
        footer={
          <>
            <Button variant="secondary" onClick={() => setOpen(false)}>
              {t("cancelDialog.keep")}
            </Button>
            <Button
              variant="danger"
              {...busyProps(act.isPending)}
              onClick={() => {
                setTried(true);
                if (!act.isPending && confirmOk) act.mutate("cancel");
              }}
              data-action="confirm-cancel"
            >
              {t("cancelDialog.confirm")}
            </Button>
          </>
        }
      >
        <RadioGroup
          name="cancel-when"
          aria-label={t("cancelDialog.body")}
          value={when}
          onValueChange={(value) => setWhen(value === "now" ? "now" : "end")}
          options={[
            {
              value: "end",
              label: t("cancelDialog.atEnd", { date: date(subscription.current_period_end) }),
            },
            { value: "now", label: t("cancelDialog.now") },
          ]}
        />
        {when === "now" ? (
          <div className="mt-4">
            <FormField
              label={t("cancelDialog.type", { slug })}
              required
              error={tried && !confirmOk ? t("cancelDialog.mismatch") : undefined}
            >
              {(control) => (
                <Input
                  {...control}
                  value={typed}
                  onChange={(event) => setTyped(event.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                  data-field="cancel-confirm"
                />
              )}
            </FormField>
          </div>
        ) : null}
        {act.isError ? (
          <p
            role="alert"
            className="m-0 mt-3 text-sm font-medium text-danger"
            data-error={code ?? ""}
          >
            {errors(code)}
          </p>
        ) : null}
      </Dialog>
    </Card>
  );
}

/* ------------------------------------------------------------------ offers */

function OpenOffer({ offer }: { offer: Offer }) {
  const t = useTranslations("app.billing");
  const locale = useLocale();
  const money = useMoney();
  const date = useDate();
  const errors = useCodeText();
  const client = useQueryClient();
  const { scope } = useWorkspace();
  const { session } = useSession();
  const payment = useHostedPayment();
  const [declining, setDeclining] = useState(false);
  const [reason, setReason] = useState("");
  const decline = useMutation({
    mutationFn: () =>
      unwrap(
        session.api.POST("/api/v1/billing/custom-offers/{offer_id}/decline", {
          params: { path: { offer_id: offer.id } },
          body: { reason: reason.trim() || null },
        }),
      ),
    onSuccess: async () => {
      setDeclining(false);
      setReason("");
      await client.invalidateQueries({ queryKey: billingFamily(scope) });
      requestAnimationFrame(() => focusIfLost(document.getElementById(BILLING_HEADING)));
    },
  });
  const code = decline.error instanceof ApiError ? decline.error.code : null;
  const busy = payment.start.isPending || payment.start.isSuccess;
  return (
    <Card as="section" aria-labelledby="billing-offer" data-offer={offer.id}>
      <h2 id="billing-offer" className="m-0 mb-3 text-lg font-semibold text-text">
        {t("offer.title")}
      </h2>
      <p className="m-0 text-xl font-semibold text-text">
        <bdi>{offer.name}</bdi>
      </p>
      <p className="m-0 mt-1 text-md text-text tabular-nums">
        {money(offer.price, offer.currency)}{" "}
        {t(offer.billing_interval === "yearly" ? "interval.year" : "interval.month")}
      </p>
      {offer.expires_at ? (
        <p className="m-0 mt-1 text-sm text-text-muted">
          {t("offer.expires", { date: date(offer.expires_at) })}
        </p>
      ) : null}
      <ul className="m-0 mt-3 flex list-none flex-col gap-1 p-0 text-sm">
        {offer.limits.map((limit) => (
          <li key={limit.key} className="flex justify-between gap-3">
            <span className="text-text-muted">{t(`limit.${limit.key}`)}</span>
            <span className="tabular-nums text-text">
              {limit.limit === null ? t("unlimited") : limitAmount(limit.key, limit.limit, locale)}
            </span>
          </li>
        ))}
      </ul>
      {offer.can_accept ? (
        <div className="mt-4 flex flex-wrap gap-3">
          <Button
            {...busyProps(busy)}
            onClick={() => !busy && payment.start.mutate({ kind: "offer", offerId: offer.id })}
            data-action="accept-offer"
          >
            {t("offer.accept")}
          </Button>
          <Button variant="ghost" onClick={() => setDeclining(true)} data-action="decline-offer">
            {t("offer.decline")}
          </Button>
        </div>
      ) : null}
      <HostedPaymentStatus pending={busy} message={payment.message} />
      <Dialog
        open={declining}
        onOpenChange={(next) => {
          if (!next) setDeclining(false);
        }}
        title={t("offer.declineTitle")}
        description={t("offer.declineBody")}
        closeLabel={t("close")}
        footer={
          <>
            <Button variant="secondary" onClick={() => setDeclining(false)}>
              {t("cancel")}
            </Button>
            <Button
              variant="danger"
              {...busyProps(decline.isPending)}
              onClick={() => !decline.isPending && decline.mutate()}
              data-action="confirm-decline"
            >
              {t("offer.decline")}
            </Button>
          </>
        }
      >
        <FormField label={t("offer.reason")}>
          {(control) => (
            <Textarea
              {...control}
              value={reason}
              maxLength={OFFER_REASON_MAX}
              onChange={(event) => setReason(event.target.value)}
              data-field="decline-reason"
            />
          )}
        </FormField>
        {decline.isError ? (
          <p
            role="alert"
            className="m-0 mt-3 text-sm font-medium text-danger"
            data-error={code ?? ""}
          >
            {errors(code)}
          </p>
        ) : null}
      </Dialog>
    </Card>
  );
}

function OfferHistory({ offers }: { offers: Offer[] }) {
  const t = useTranslations("app.billing");
  const date = useDate();
  const past = offers.filter((offer) => offer.status !== "offered");
  if (!past.length) return null;
  return (
    <Card as="section" aria-labelledby="billing-offers-past" data-offer-history="">
      <h2 id="billing-offers-past" className="m-0 mb-3 text-lg font-semibold text-text">
        {t("offer.past")}
      </h2>
      <ul className="m-0 flex list-none flex-col gap-2 p-0">
        {past.map((offer) => (
          <li key={offer.id} className="flex flex-wrap items-center justify-between gap-2 text-md">
            <span className="text-text">
              <bdi>{offer.name}</bdi>
            </span>
            <span className="text-sm text-text-muted">
              {t.has(`offer.state.${offer.status}`)
                ? t(`offer.state.${offer.status}` as "offer.state.offered")
                : offer.status}{" "}
              · {date(offer.created_at)}
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}
