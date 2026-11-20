"use client";

/**
 * APP-BILL-03 at /billing/payment-methods (PM-01 / PM-02, ADR-202), the owner's: the saved cards -
 * brand, never more than the last four digits (lib/billing/payment-return.ts maskedDigits), the
 * default - "Make default" and "Remove" behind a confirmation. No "Add card": no route adds one;
 * a card is saved by ticking the box on Paymob's page, which the page says.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { ApiError, unwrap, type components } from "@wasla/api-client";
import { Badge, Button, Card, Dialog } from "@wasla/ui";

import { maskedDigits } from "../../lib/billing/payment-return";
import { focusIfLost } from "../../lib/focus";
import { useWorkspace } from "../app/workspace-context";
import { useTimes } from "../customers/shared";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";
import { busyProps } from "../team/screen-head";
import {
  BILLING_HEADING,
  BillingFrame,
  billingFamily,
  billingKey,
  LoadFailed,
  Loading,
  NotForBillingRole,
  useBillingRoles,
} from "./shared";

type Method = components["schemas"]["PaymentMethodRead"];

export function PaymentMethodsPage() {
  const { owner } = useBillingRoles();
  return owner ? <MethodsScreen /> : <NotForBillingRole />;
}

function MethodsScreen() {
  const t = useTranslations("app.billing");
  const errors = useCodeText();
  const times = useTimes();
  const client = useQueryClient();
  const { scope } = useWorkspace();
  const { session } = useSession();
  const [removing, setRemoving] = useState<Method | null>(null);
  const methods = useQuery({
    queryKey: billingKey(scope, "payment-methods"),
    queryFn: () => unwrap(session.api.GET("/api/v1/billing/payment-methods")),
  });
  const act = useMutation({
    mutationFn: async (input: { kind: "default" | "remove"; method: Method }) => {
      const params = { params: { path: { method_id: input.method.id } } };
      if (input.kind === "default") {
        return unwrap(
          session.api.POST("/api/v1/billing/payment-methods/{method_id}/default", params),
        );
      }
      return unwrap(session.api.DELETE("/api/v1/billing/payment-methods/{method_id}", params));
    },
    onSuccess: async () => {
      setRemoving(null);
      await client.invalidateQueries({ queryKey: billingFamily(scope) });
      requestAnimationFrame(() => focusIfLost(document.getElementById(BILLING_HEADING)));
    },
  });
  const code = act.error instanceof ApiError ? act.error.code : null;

  const cardName = (method: Method) => {
    const last4 = maskedDigits(method.masked_pan);
    if (!last4) return t("methods.cardUnknown");
    return method.brand
      ? t("methods.card", { brand: method.brand, last4 })
      : t("methods.cardNoBrand", { last4 });
  };

  let body: React.ReactNode;
  if (methods.isError) {
    body = <LoadFailed error={methods.error} retry={() => void methods.refetch()} />;
  } else if (methods.isPending) {
    body = <Loading />;
  } else if (!methods.data.length) {
    body = (
      <p className="m-0 text-md text-text-muted" data-methods-empty="">
        {t("methods.empty")}
      </p>
    );
  } else {
    body = (
      <>
        <ul className="m-0 flex list-none flex-col gap-2 p-0" data-methods="">
          {methods.data.map((method) => (
            <li
              key={method.id}
              className="flex flex-col gap-2 rounded-lg bg-surface p-4 sm:flex-row sm:items-center sm:justify-between"
              data-method={method.is_default ? "default" : "other"}
            >
              <div className="flex flex-col gap-1">
                <span className="flex flex-wrap items-center gap-2 text-md font-semibold text-text">
                  <bdi>{cardName(method)}</bdi>
                  {method.is_default ? <Badge tone="success">{t("methods.default")}</Badge> : null}
                </span>
                <span className="text-sm text-text-muted">
                  {t("methods.added", { time: times(method.created_at)?.time ?? "" })}
                </span>
              </div>
              <div className="flex flex-wrap gap-2">
                {method.is_default ? null : (
                  <Button
                    variant="secondary"
                    {...busyProps(act.isPending)}
                    onClick={() => !act.isPending && act.mutate({ kind: "default", method })}
                    data-action="make-default"
                  >
                    {t("methods.makeDefault")}
                  </Button>
                )}
                <Button
                  variant="ghost"
                  onClick={() => setRemoving(method)}
                  data-action="remove-card"
                >
                  {t("methods.remove")}
                </Button>
              </div>
            </li>
          ))}
        </ul>
        <p className="m-0 text-sm text-text-muted">{t("methods.how")}</p>
      </>
    );
  }
  return (
    <BillingFrame current="methods">
      <Card as="section" aria-label={t("methods.title")}>
        {body}
        {act.isError && !removing ? (
          <p
            role="alert"
            className="m-0 mt-3 text-sm font-medium text-danger"
            data-error={code ?? ""}
          >
            {errors(code)}
          </p>
        ) : null}
      </Card>
      <Dialog
        open={removing !== null}
        onOpenChange={(next) => {
          if (!next) setRemoving(null);
        }}
        title={t("methods.removeTitle")}
        description={
          removing?.is_default ? t("methods.removeDefaultBody") : t("methods.removeBody")
        }
        closeLabel={t("close")}
        footer={
          <>
            <Button variant="secondary" onClick={() => setRemoving(null)}>
              {t("cancel")}
            </Button>
            <Button
              variant="danger"
              {...busyProps(act.isPending)}
              onClick={() =>
                removing && !act.isPending && act.mutate({ kind: "remove", method: removing })
              }
              data-action="confirm-remove-card"
            >
              {t("methods.remove")}
            </Button>
          </>
        }
      >
        {act.isError ? (
          <p role="alert" className="m-0 text-sm font-medium text-danger" data-error={code ?? ""}>
            {errors(code)}
          </p>
        ) : null}
      </Dialog>
    </BillingFrame>
  );
}
