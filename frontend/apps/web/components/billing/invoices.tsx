"use client";

/**
 * APP-BILL-04 (IN-01 .. IN-04, ADR-202), the owner's in the API: the invoices newest first (the
 * latest 50 - a bare list), "Pay" on an open one; the invoice page with its lines, totals and every
 * payment attempt with its status and reason in words; "Ask for a refund review" on a succeeded
 * payment, which moves no money and says so; and "Print" - FEB-13's decision: no PDF is made, the
 * page prints as a record of Wasla charges (the browser's "Save as PDF" is the owner's).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import NextLink from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { ApiError, unwrap, type components } from "@wasla/api-client";
import {
  Badge,
  Button,
  Card,
  Dialog,
  FormField,
  InvoiceLines,
  linkClasses,
  Textarea,
} from "@wasla/ui";

import { focusIfLost } from "../../lib/focus";
import { useWorkspace } from "../app/workspace-context";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";
import { busyProps } from "../team/screen-head";
import {
  BILLING_HEADING,
  BillingFrame,
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

type Invoice = components["schemas"]["InvoiceRead"];
type Payment = components["schemas"]["PaymentRead"];

export const INVOICES_SHOWN = 50;
const REFUND_REASON_MAX = 300;

const STATUS_TONE: Record<string, "success" | "warning" | "danger" | "neutral"> = {
  paid: "success",
  open: "warning",
  uncollectible: "danger",
};

export function InvoicesPage() {
  const { owner } = useBillingRoles();
  return owner ? <InvoicesScreen /> : <NotForBillingRole />;
}

function InvoicesScreen() {
  const t = useTranslations("app.billing");
  const money = useMoney();
  const date = useDate();
  const { scope } = useWorkspace();
  const { session } = useSession();
  const payment = useHostedPayment();
  const invoices = useQuery({
    queryKey: billingKey(scope, "invoices", { limit: INVOICES_SHOWN }),
    queryFn: () =>
      unwrap(session.api.GET("/api/v1/invoices", { params: { query: { limit: INVOICES_SHOWN } } })),
  });
  const busy = payment.start.isPending || payment.start.isSuccess;

  let body: React.ReactNode;
  if (invoices.isError) {
    body = <LoadFailed error={invoices.error} retry={() => void invoices.refetch()} />;
  } else if (invoices.isPending) {
    body = <Loading />;
  } else if (!invoices.data.length) {
    body = <p className="m-0 text-md text-text-muted">{t("invoices.empty")}</p>;
  } else {
    body = (
      <>
        <ul className="m-0 flex list-none flex-col gap-2 p-0" data-invoices="">
          {invoices.data.map((invoice) => (
            <li
              key={invoice.id}
              className="flex flex-col gap-2 rounded-lg bg-surface p-4 sm:flex-row sm:items-center sm:justify-between"
              data-invoice={invoice.status}
            >
              <div className="flex min-w-0 flex-col gap-1">
                <span className="flex flex-wrap items-center gap-2 text-md font-semibold text-text">
                  {t(`invoices.purpose.${invoice.purpose}`)}
                  <Badge tone={STATUS_TONE[invoice.status] ?? "neutral"}>
                    {t(`invoices.status.${invoice.status}`)}
                  </Badge>
                </span>
                <span className="text-sm text-text-muted tabular-nums">
                  {money(invoice.amount_due, invoice.currency)} ·{" "}
                  {t("invoices.issued", { date: date(invoice.issued_at ?? invoice.period_start) })}
                </span>
                {invoice.status === "open" ? (
                  <span className="text-sm font-medium text-text tabular-nums">
                    {t("invoices.outstanding", {
                      amount: money(invoice.outstanding, invoice.currency),
                    })}
                  </span>
                ) : null}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {invoice.status === "open" ? (
                  <Button
                    {...busyProps(busy)}
                    onClick={() =>
                      !busy && payment.start.mutate({ kind: "invoice", invoiceId: invoice.id })
                    }
                    aria-label={t("invoices.payOf", {
                      amount: money(invoice.outstanding, invoice.currency),
                    })}
                    data-action="pay-invoice"
                  >
                    {t("payNow")}
                  </Button>
                ) : null}
                <NextLink
                  href={`/billing/invoices/${invoice.id}`}
                  className={linkClasses("text-md")}
                  data-action="open-invoice"
                >
                  {t("invoices.open")}
                </NextLink>
              </div>
            </li>
          ))}
        </ul>
        {invoices.data.length >= INVOICES_SHOWN ? (
          <p className="m-0 text-sm text-text-muted">{t("invoices.latest")}</p>
        ) : null}
        <HostedPaymentStatus pending={busy} message={payment.message} />
      </>
    );
  }
  return (
    <BillingFrame current="invoices">
      <Card as="section" aria-label={t("invoices.title")}>
        {body}
      </Card>
    </BillingFrame>
  );
}

/* ----------------------------------------------------------- the invoice */

export function InvoicePage({ id }: { id: string }) {
  const { owner } = useBillingRoles();
  return owner ? <InvoiceScreen id={id} /> : <NotForBillingRole />;
}

function InvoiceScreen({ id }: { id: string }) {
  const t = useTranslations("app.billing");
  const money = useMoney();
  const date = useDate();
  const { scope, workspaceName } = useWorkspace();
  const { session } = useSession();
  const record = useWorkspaceRecord();
  const payment = useHostedPayment();
  const invoice = useQuery({
    queryKey: billingKey(scope, "invoice", { id }),
    queryFn: () =>
      unwrap(
        session.api.GET("/api/v1/invoices/{invoice_id}", { params: { path: { invoice_id: id } } }),
      ),
  });
  const payments = useQuery({
    queryKey: billingKey(scope, "invoice-payments", { id }),
    queryFn: () =>
      unwrap(
        session.api.GET("/api/v1/invoices/{invoice_id}/payments", {
          params: { path: { invoice_id: id } },
        }),
      ),
    enabled: invoice.isSuccess,
  });
  const busy = payment.start.isPending || payment.start.isSuccess;

  if (invoice.isError) {
    const missing = invoice.error instanceof ApiError && invoice.error.status === 404;
    return (
      <BillingFrame current="invoices">
        {missing ? (
          <Card as="section">
            <p className="m-0 text-md text-text" data-invoice-missing="">
              {t("invoice.notFound")}
            </p>
            <NextLink href="/billing/invoices" className={linkClasses("text-md")}>
              {t("invoice.back")}
            </NextLink>
          </Card>
        ) : (
          <LoadFailed error={invoice.error} retry={() => void invoice.refetch()} />
        )}
      </BillingFrame>
    );
  }
  if (invoice.isPending) {
    return (
      <BillingFrame current="invoices">
        <Loading />
      </BillingFrame>
    );
  }
  const data: Invoice = invoice.data;
  return (
    <BillingFrame current="invoices">
      <article className="flex flex-col gap-6" data-invoice-page={data.status} data-print-root="">
        <Card as="section" aria-labelledby="invoice-heading">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="flex flex-col gap-1">
              <h2 id="invoice-heading" className="m-0 text-xl font-semibold text-text">
                {t("invoice.heading")}
              </h2>
              <p className="m-0 text-md text-text">
                <bdi>{record.data?.name ?? workspaceName}</bdi>
              </p>
              <p className="m-0 text-sm text-text-muted">
                {t("invoice.id", { id: data.id.slice(0, 8) })}{" "}
                <span className="sr-only print:not-sr-only">({data.id})</span>
              </p>
              <p className="m-0 text-sm text-text-muted">{t("invoice.recordNote")}</p>
            </div>
            <div className="flex flex-wrap gap-2 print:hidden">
              {data.status === "open" ? (
                <Button
                  {...busyProps(busy)}
                  onClick={() =>
                    !busy && payment.start.mutate({ kind: "invoice", invoiceId: data.id })
                  }
                  data-action="pay-invoice"
                >
                  {t("payNow")}
                </Button>
              ) : null}
              <Button variant="secondary" onClick={() => window.print()} data-action="print">
                {t("invoice.print")}
              </Button>
            </div>
          </div>
          <dl className="m-0 mt-4 grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
            <div className="flex justify-between gap-3">
              <dt className="text-text-muted">{t("invoice.period")}</dt>
              <dd className="m-0 text-text">
                {date(data.period_start)} – {date(data.period_end)}
              </dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-text-muted">{t("tabs.invoices")}</dt>
              <dd className="m-0 text-text">
                {t(`invoices.purpose.${data.purpose}`)} · {t(`invoices.status.${data.status}`)}
              </dd>
            </div>
          </dl>
          <HostedPaymentStatus pending={busy} message={payment.message} />
        </Card>
        <InvoiceLines
          label={t("invoice.lines")}
          descriptionLabel={t("invoice.description")}
          quantityLabel={t("invoice.quantity")}
          amountLabel={t("invoice.amount")}
          lines={data.lines.map((line, index) => ({
            key: `${index}`,
            description: line.description,
            quantity: `${line.quantity}${line.unit ? ` ${line.unit}` : ""}`,
            amount: money(line.amount, data.currency),
          }))}
        />
        <Card as="section" aria-label={t("invoice.amount")}>
          <dl className="m-0 flex flex-col gap-1 text-md tabular-nums">
            <div className="flex justify-between gap-3">
              <dt className="text-text-muted">{t("invoice.due")}</dt>
              <dd className="m-0 text-text">{money(data.amount_due, data.currency)}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-text-muted">{t("invoice.paid")}</dt>
              <dd className="m-0 text-text">{money(data.amount_paid, data.currency)}</dd>
            </div>
            <div className="flex justify-between gap-3 font-semibold">
              <dt className="text-text">{t("invoice.outstanding")}</dt>
              <dd className="m-0 text-text" data-outstanding="">
                {money(data.outstanding, data.currency)}
              </dd>
            </div>
          </dl>
        </Card>
        <Card as="section" aria-labelledby="invoice-payments">
          <h2 id="invoice-payments" className="m-0 mb-3 text-lg font-semibold text-text">
            {t("invoice.payments")}
          </h2>
          {payments.isPending ? (
            <Loading />
          ) : payments.isError ? (
            <LoadFailed error={payments.error} retry={() => void payments.refetch()} />
          ) : payments.data.length ? (
            <ul className="m-0 flex list-none flex-col gap-3 p-0">
              {payments.data.map((attempt) => (
                <PaymentRow key={attempt.id} payment={attempt} invoiceId={data.id} />
              ))}
            </ul>
          ) : (
            <p className="m-0 text-md text-text-muted">{t("invoice.noPayments")}</p>
          )}
        </Card>
      </article>
    </BillingFrame>
  );
}

const PAYMENT_TONE: Record<string, "success" | "warning" | "danger" | "neutral"> = {
  succeeded: "success",
  pending: "warning",
  failed: "danger",
};

function PaymentRow({ payment, invoiceId }: { payment: Payment; invoiceId: string }) {
  const t = useTranslations("app.billing");
  const errors = useCodeText();
  const money = useMoney();
  const date = useDate();
  const client = useQueryClient();
  const { scope } = useWorkspace();
  const { session } = useSession();
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  const [requested, setRequested] = useState(false);
  const review = useMutation({
    mutationFn: () =>
      unwrap(
        session.api.POST("/api/v1/billing/payments/{payment_id}/refund", {
          params: { path: { payment_id: payment.id } },
          body: { reason: reason.trim() || null },
        }),
      ),
    onSuccess: async () => {
      setAsking(false);
      setReason("");
      setRequested(true);
      await client.invalidateQueries({
        queryKey: billingKey(scope, "invoice-payments", { id: invoiceId }),
      });
      requestAnimationFrame(() => focusIfLost(document.getElementById(BILLING_HEADING)));
    },
    onSettled: () => setReason(""),
  });
  const code = review.error instanceof ApiError ? review.error.code : null;
  const refundable =
    payment.status === "succeeded" && !payment.refund_pending && !payment.refunded_at;
  return (
    <li
      className="flex flex-col gap-2 rounded-lg bg-surface-sunken p-3 sm:flex-row sm:items-center sm:justify-between"
      data-payment={payment.status}
    >
      <div className="flex flex-col gap-1">
        <span className="flex flex-wrap items-center gap-2 text-md text-text tabular-nums">
          {money(payment.amount, payment.currency)}
          <Badge tone={PAYMENT_TONE[payment.status] ?? "neutral"}>
            {t(`payment.status.${payment.status}`)}
          </Badge>
        </span>
        <span className="text-sm text-text-muted">
          {date(payment.processed_at ?? payment.created_at)}
        </span>
        {payment.status === "failed" ? (
          <span className="text-sm text-text">{t("return.failedGeneric")}</span>
        ) : null}
        {payment.refund_pending ? (
          <span className="text-sm text-text">{t("payment.refundPending")}</span>
        ) : null}
        {requested ? (
          <span role="status" className="text-sm font-medium text-text" data-review="requested">
            {t("payment.reviewRequested")}
          </span>
        ) : null}
      </div>
      {refundable && !requested ? (
        <Button
          variant="ghost"
          className="print:hidden"
          onClick={() => setAsking(true)}
          data-action="ask-refund"
        >
          {t("refund.ask")}
        </Button>
      ) : null}
      <Dialog
        open={asking}
        onOpenChange={(next) => {
          if (!next) setAsking(false);
        }}
        title={t("refund.title")}
        description={t("refund.body")}
        closeLabel={t("close")}
        footer={
          <>
            <Button variant="secondary" onClick={() => setAsking(false)}>
              {t("cancel")}
            </Button>
            <Button
              {...busyProps(review.isPending)}
              onClick={() => !review.isPending && review.mutate()}
              data-action="send-refund"
            >
              {t("refund.send")}
            </Button>
          </>
        }
      >
        <FormField label={t("refund.reason")}>
          {(control) => (
            <Textarea
              {...control}
              value={reason}
              maxLength={REFUND_REASON_MAX}
              onChange={(event) => setReason(event.target.value)}
              data-field="refund-reason"
            />
          )}
        </FormField>
        {review.isError ? (
          <p
            role="alert"
            className="m-0 mt-3 text-sm font-medium text-danger"
            data-error={code ?? ""}
          >
            {errors(code)}
          </p>
        ) : null}
      </Dialog>
    </li>
  );
}
