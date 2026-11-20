"use client";

/**
 * APP-BILL-02's return at /billing/checkout/return (RT-01 .. RT-06, ADR-202). What decides is ONLY
 * the payment the API reads - GET /billing/payments/{id} - and success is shown only when its
 * status is `succeeded`. The redirect's query string is never read (it says success=true while a
 * payment is pending or declined - P02, P54): on arrival it is dropped from the address bar with
 * replaceState, unread. Which payment is followed comes from the API too - the newest payable
 * invoice issued in the last two hours and its newest attempt (OQ-4) - never from storage.
 *
 * Reads every 2 s for a minute, then every 10 s; paused while the tab is hidden (an immediate read
 * when it is shown again); a read that never reached the API (WebKit aborts fetches as it
 * navigates - FE-7's 3ac448d) is retried on the schedule and never shown as a result.
 */
import { useQueryClient } from "@tanstack/react-query";
import NextLink from "next/link";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, unwrap, type components } from "@wasla/api-client";
import { Card, linkClasses, PaymentStatusPanel } from "@wasla/ui";

import {
  newestPayment,
  nextPollDelay,
  recentPayableInvoices,
  returnView,
  type Found,
} from "../../lib/billing/payment-return";
import { appNow } from "../../lib/test-hooks";
import { useWorkspace } from "../app/workspace-context";
import { useSession } from "../session-provider";
import {
  BILLING_HEADING,
  billingFamily,
  NotForBillingRole,
  useBillingRoles,
  useDate,
  useMoney,
} from "./shared";

type Payment = components["schemas"]["PaymentRead"];
type Invoice = components["schemas"]["InvoiceRead"];

const RETURN_PATH = "/billing/checkout/return";

export function ReturnPage() {
  const { owner } = useBillingRoles();
  return owner ? <ReturnScreen /> : <NotForBillingRole />;
}

function failureText(reason: string | null, t: ReturnType<typeof useTranslations>): string {
  // The provider's own words are never shown as markup; a known one gets a sentence.
  if (reason && /AUTHENTICATION|3DS|DECLINED/i.test(reason)) return t("return.failedAuth");
  return t("return.failedGeneric");
}

function ReturnScreen() {
  const t = useTranslations("app.billing");
  const money = useMoney();
  const date = useDate();
  const client = useQueryClient();
  const { scope, recheck } = useWorkspace();
  const { session } = useSession();
  const [found, setFound] = useState<Found>("finding");
  const [invoice, setInvoice] = useState<Invoice | null>(null);
  const [others, setOthers] = useState<Invoice[]>([]);
  const [payment, setPayment] = useState<Payment | null>(null);
  const [startedAt] = useState(() => appNow());
  const [now, setNow] = useState(() => appNow());
  const [visible, setVisible] = useState(true);
  const settledOnce = useRef(false);
  const headingRef = useRef<HTMLHeadingElement>(null);

  // RT-03: the redirect's parameters leave the address bar on arrival, unread.
  useEffect(() => {
    window.history.replaceState(window.history.state, "", RETURN_PATH);
  }, []);

  useEffect(() => {
    const onVisibility = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  // RT-02: which payment - from the API alone.
  useEffect(() => {
    let cancelled = false;
    const find = async () => {
      try {
        const invoices = await unwrap(
          session.api.GET("/api/v1/invoices", { params: { query: { limit: 10 } } }),
        );
        const recent = recentPayableInvoices(invoices, appNow());
        const first = recent[0];
        if (!first) {
          if (!cancelled) setFound("none");
          return;
        }
        const payments = await unwrap(
          session.api.GET("/api/v1/invoices/{invoice_id}/payments", {
            params: { path: { invoice_id: first.id } },
          }),
        );
        const newest = newestPayment(payments);
        if (cancelled) return;
        setInvoice(first);
        setOthers(recent.slice(1));
        if (!newest) {
          setFound("none");
          return;
        }
        setPayment(newest);
        setFound("found");
      } catch (error) {
        if (cancelled) return;
        // A read that never reached the API decides nothing: try again shortly.
        if (error instanceof ApiError) setFound("none");
        else window.setTimeout(() => !cancelled && void find(), 2_000);
      }
    };
    void find();
    return () => {
      cancelled = true;
    };
  }, [session]);

  const read = useCallback(async () => {
    if (!payment) return;
    try {
      const fresh = await unwrap(
        session.api.GET("/api/v1/billing/payments/{payment_id}", {
          params: { path: { payment_id: payment.id } },
        }),
      );
      setPayment(fresh);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) setFound("elsewhere");
      // network_error and the rest: the schedule reads again; nothing is shown.
    } finally {
      setNow(appNow());
    }
  }, [payment, session]);

  // RT-04: the schedule, paused while hidden.
  const status = payment?.status ?? null;
  useEffect(() => {
    if (found !== "found" || !visible) return;
    const delay = nextPollDelay(status, appNow() - startedAt);
    if (delay === null) return;
    const timer = window.setTimeout(() => void read(), delay);
    return () => window.clearTimeout(timer);
  }, [found, visible, status, now, read, startedAt]);

  // Shown again: read at once.
  useEffect(() => {
    if (visible && found === "found") void read();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only on becoming visible
  }, [visible]);

  const view = returnView({ found, status, elapsedMs: now - startedAt });

  // A settled payment: the shell's gates and every billing read are read once again (GT-02's
  // invalidation, called, not changed), and the heading takes the focus once.
  useEffect(() => {
    if (view.kind === "finding" || view.kind === "confirming" || settledOnce.current) return;
    settledOnce.current = true;
    if (view.kind === "succeeded") {
      void client.invalidateQueries({ queryKey: billingFamily(scope) });
      void recheck();
    }
    headingRef.current?.focus();
  }, [view.kind, client, scope, recheck]);

  let content: React.ReactNode;
  switch (view.kind) {
    case "finding":
      content = <PaymentStatusPanel tone="progress" title={t("return.finding")} />;
      break;
    case "confirming":
      content = (
        <PaymentStatusPanel
          tone="progress"
          title={t("return.confirming")}
          description={view.slow ? t("return.slow") : undefined}
        />
      );
      break;
    case "succeeded":
      content = (
        <PaymentStatusPanel
          tone="success"
          title={t("return.succeeded")}
          description={
            payment && invoice
              ? t("return.succeededFor", {
                  amount: money(payment.amount, payment.currency),
                  start: date(invoice.period_start),
                  end: date(invoice.period_end),
                })
              : undefined
          }
          action={
            <NextLink href="/billing" className={linkClasses()} data-action="to-billing">
              {t("return.toBilling")}
            </NextLink>
          }
        />
      );
      break;
    case "failed":
      content = (
        <PaymentStatusPanel
          tone="danger"
          title={t("return.failed")}
          description={failureText(payment?.failure_reason ?? null, t)}
          action={
            <NextLink
              href={invoice ? `/billing/invoices/${invoice.id}` : "/billing"}
              className={linkClasses()}
              data-action="try-again"
            >
              {t("return.tryAgain")}
            </NextLink>
          }
        />
      );
      break;
    case "refunded":
      content = <PaymentStatusPanel tone="neutral" title={t("return.refunded")} />;
      break;
    case "elsewhere":
      content = <PaymentStatusPanel tone="warning" title={t("return.elsewhere")} />;
      break;
    default:
      content = (
        <PaymentStatusPanel
          tone="neutral"
          title={t("return.none")}
          action={
            <NextLink href="/billing/invoices" className={linkClasses()}>
              {t("tabs.invoices")}
            </NextLink>
          }
        />
      );
  }

  return (
    <div className="flex flex-col gap-6" data-screen="billing-return" data-return-state={view.kind}>
      <h1
        id={BILLING_HEADING}
        ref={headingRef}
        tabIndex={-1}
        className="m-0 text-3xl font-semibold leading-tight tracking-tight text-text"
      >
        {t("return.title")}
      </h1>
      <Card as="section" aria-live="polite" data-payment-status={status ?? ""}>
        {content}
      </Card>
      {others.length ? (
        <Card as="section" aria-labelledby="return-others">
          <h2 id="return-others" className="m-0 mb-2 text-md font-semibold text-text">
            {t("return.others")}
          </h2>
          <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {others.map((other) => (
              <li key={other.id}>
                <NextLink href={`/billing/invoices/${other.id}`} className={linkClasses("text-sm")}>
                  {money(other.amount_due, other.currency)} · {date(other.issued_at)}
                </NextLink>
              </li>
            ))}
          </ul>
        </Card>
      ) : null}
    </div>
  );
}
