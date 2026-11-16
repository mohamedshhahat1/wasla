"use client";

/**
 * "New lead" (LL-04, LD-10): POST /leads from the fields LeadCreateRequest has. A budget that is
 * not a plain number ("500k") is stopped here, before any request. From a customer or a
 * conversation the lead is linked to them and the name starts from the customer's - a field the
 * person submits is a field a person confirmed. 409 open_lead_exists (the AI may create one at the
 * same moment) is answered with a link to the lead that is open, found through the list - the
 * refusal itself names no lead.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import NextLink from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState, type FormEvent } from "react";

import { ApiError, unwrap } from "@wasla/api-client";
import { Button, Dialog, FormField, Input, Textarea, buttonClasses } from "@wasla/ui";

import { CONTACT_LEADS, LEADS, LEAD_STATISTICS, resourcePrefix } from "../../lib/customers/keys";
import {
  checkBudget,
  checkCurrency,
  EMPTY_LEAD_FORM,
  LEAD_BOUNDS,
  leadProblems,
  type FieldProblem,
  type LeadFormValues,
} from "../../lib/customers/lead-form";
import { leadHref, OPEN_LEAD_STATUSES } from "../../lib/customers/view";
import { CONVERSATION, INBOX } from "../../lib/inbox/keys";
import { useWorkspace } from "../app/workspace-context";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";
import { TagEditor } from "./tag-editor";

const FIELDS = ["name", "phone", "email", "interest", "budget_amount", "budget_currency"] as const;

export function useProblemText(): (
  problem: FieldProblem | undefined,
  max?: number,
) => string | undefined {
  const t = useTranslations("app.customers");
  return (problem, max) =>
    problem ? t(`problems.${problem}` as "problems.budget", { max: max ?? 0 }) : undefined;
}

export function LeadCreateDialog({
  open,
  onOpenChange,
  contactId,
  conversationId,
  initialName,
  stay = false,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  contactId?: string | null;
  conversationId?: string | null;
  initialName?: string | null;
  /** Stay on the screen that opened the dialog (the inbox panel) instead of opening the lead. */
  stay?: boolean;
}) {
  const t = useTranslations("app.customers");
  const errors = useCodeText();
  const problemText = useProblemText();
  const router = useRouter();
  const queryClient = useQueryClient();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const [values, setValues] = useState<LeadFormValues>({
    ...EMPTY_LEAD_FORM,
    name: initialName ?? "",
    tags: [],
  });
  const [shown, setShown] = useState(false);
  const [openLead, setOpenLead] = useState<string | null>(null);
  const problems = leadProblems(values, FIELDS);
  const set = (field: keyof LeadFormValues) => (value: string) =>
    setValues((current) => ({ ...current, [field]: value }));

  const create = useMutation({
    mutationFn: async () => {
      const budget = checkBudget(values.budget_amount);
      const currency = checkCurrency(values.budget_currency);
      const text = (value: string) => value.trim() || null;
      return unwrap(
        session.api.POST("/api/v1/leads", {
          body: {
            ...(contactId ? { contact_id: contactId } : {}),
            ...(conversationId ? { conversation_id: conversationId } : {}),
            name: text(values.name),
            phone: text(values.phone),
            email: text(values.email),
            interest: text(values.interest),
            budget_amount: budget.ok ? budget.value : null,
            budget_currency: currency.ok ? currency.value : null,
            ...(values.tags.length ? { tags: values.tags } : {}),
          },
        }),
      );
    },
    onSuccess: (lead) => {
      void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, LEADS) });
      void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, LEAD_STATISTICS) });
      void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, CONTACT_LEADS) });
      void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, CONVERSATION) });
      void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, INBOX) });
      onOpenChange(false);
      if (!stay) router.push(leadHref(lead.id));
    },
    onError: async (error) => {
      if (error instanceof ApiError && error.code === "open_lead_exists" && contactId) {
        // The refusal names no lead (EC5-03): the open one is read from the list.
        try {
          const page = await unwrap(
            session.api.GET("/api/v1/leads", {
              params: {
                query: { contact_id: contactId, status: [...OPEN_LEAD_STATUSES], limit: 1 },
              },
            }),
          );
          setOpenLead(page.items[0]?.id ?? null);
        } catch {
          setOpenLead(null);
        }
      }
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setShown(true);
    if (Object.keys(problems).length || create.isPending) return;
    setOpenLead(null);
    create.mutate();
  };
  const problem = (field: keyof LeadFormValues, max?: number) =>
    shown ? problemText(problems[field], max) : undefined;
  const failure = create.error instanceof ApiError ? create.error : null;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) create.reset();
        onOpenChange(next);
      }}
      title={t("create.title")}
      description={t("create.description")}
      closeLabel={t("close")}
    >
      <form className="flex flex-col gap-5" onSubmit={submit} noValidate data-lead-create="">
        <FormField label={t("fields.name")} error={problem("name", LEAD_BOUNDS.name)}>
          {(control) => (
            <Input
              {...control}
              dir="auto"
              autoComplete="off"
              maxLength={LEAD_BOUNDS.name}
              value={values.name}
              onChange={(event) => set("name")(event.target.value)}
            />
          )}
        </FormField>
        <FormField label={t("fields.phone")} error={problem("phone")}>
          {(control) => (
            <Input
              {...control}
              dir="ltr"
              inputMode="tel"
              autoComplete="off"
              maxLength={LEAD_BOUNDS.phone}
              value={values.phone}
              onChange={(event) => set("phone")(event.target.value)}
            />
          )}
        </FormField>
        <FormField label={t("fields.email")} error={problem("email")}>
          {(control) => (
            <Input
              {...control}
              dir="ltr"
              inputMode="email"
              autoComplete="off"
              maxLength={LEAD_BOUNDS.email}
              value={values.email}
              onChange={(event) => set("email")(event.target.value)}
            />
          )}
        </FormField>
        <FormField label={t("fields.interest")} error={problem("interest", LEAD_BOUNDS.interest)}>
          {(control) => (
            <Textarea
              {...control}
              dir="auto"
              rows={2}
              maxLength={LEAD_BOUNDS.interest}
              value={values.interest}
              onChange={(event) => set("interest")(event.target.value)}
            />
          )}
        </FormField>
        <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
          <FormField
            label={t("fields.budget")}
            hint={t("fields.budgetHint")}
            error={problem("budget_amount")}
          >
            {(control) => (
              <Input
                {...control}
                dir="ltr"
                inputMode="decimal"
                autoComplete="off"
                value={values.budget_amount}
                onChange={(event) => set("budget_amount")(event.target.value)}
                data-budget=""
              />
            )}
          </FormField>
          <FormField
            label={t("fields.currency")}
            hint={t("fields.currencyHint")}
            error={problem("budget_currency")}
          >
            {(control) => (
              <Input
                {...control}
                dir="ltr"
                autoComplete="off"
                maxLength={LEAD_BOUNDS.currency}
                value={values.budget_currency}
                onChange={(event) => set("budget_currency")(event.target.value)}
              />
            )}
          </FormField>
        </div>
        <TagEditor
          tags={values.tags}
          onChange={(tags) => setValues((current) => ({ ...current, tags }))}
        />
        {failure ? (
          <div
            role="alert"
            className="flex flex-col gap-2 text-sm font-medium text-danger"
            data-create-error=""
          >
            <p className="m-0">{errors(failure.code)}</p>
            {failure.code === "open_lead_exists" && openLead ? (
              <NextLink href={leadHref(openLead)} className={buttonClasses("secondary")}>
                {t("create.openExisting")}
              </NextLink>
            ) : null}
          </div>
        ) : null}
        <div className="flex flex-wrap justify-end gap-3">
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            {t("cancel")}
          </Button>
          <Button type="submit" variant="primary" busy={create.isPending}>
            {t("create.submit")}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
