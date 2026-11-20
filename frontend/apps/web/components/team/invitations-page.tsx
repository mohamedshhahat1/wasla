"use client";

/**
 * APP-TEAM-02 at /team/invitations (TI-01 .. TI-03, ADR-201): owners and administrators invite by
 * address, with the role and the e-mail's language (LC-06: the inviter's own language unless they
 * choose another), and revoke what is still open. Seats used up are said plainly - no billing link
 * (FE-8). The address never goes into the URL; the form clears after a sent invitation.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { useId, useState, type FormEvent } from "react";

import { ApiError, unwrap, type components } from "@wasla/api-client";
import { LOCALE_NAMES, LOCALES, isLocale, type Locale } from "@wasla/i18n";
import {
  Button,
  Card,
  Dialog,
  EmptyState,
  ErrorState,
  FormField,
  Input,
  InvitationRow,
  RoleBadge,
  Select,
} from "@wasla/ui";

import { focusIfLost } from "../../lib/focus";
import { invitationsKey } from "../../lib/team/keys";
import {
  invitableRoles,
  invitationState,
  isAdminRole,
  type TenantRole,
} from "../../lib/team/rules";
import { useWorkspace } from "../app/workspace-context";
import { PlanLimitLink } from "../billing/plan-limit-link";
import { useMinuteClock, useTimes } from "../customers/shared";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";
import { roleTone, useRoleWord } from "./members-page";
import { busyProps, ScreenHead } from "./screen-head";
import { TeamTabs } from "./team-tabs";

type Invitation = components["schemas"]["InvitationResponse"];

const HEADING = "invitations-title";

/** The codes an invitation answers on its address field (EC7-02). */
const ADDRESS_CODES = new Set(["invitation_pending", "already_member"]);

function InviteForm() {
  const t = useTranslations("app.team");
  const errors = useCodeText();
  const roleWord = useRoleWord();
  const requested = useLocale();
  const client = useQueryClient();
  const { session } = useSession();
  const { scope, role } = useWorkspace();
  const formId = useId();
  const roles = invitableRoles(role ?? null);
  const [email, setEmail] = useState("");
  const [invitedRole, setInvitedRole] = useState<TenantRole>("member");
  const [language, setLanguage] = useState<Locale>(isLocale(requested) ? requested : "en");
  const [tried, setTried] = useState(false);
  const [sent, setSent] = useState(false);
  const problem = !email.trim()
    ? "required"
    : !/^[^\s@]+@[^\s@]+$/.test(email.trim())
      ? "email"
      : null;

  const invite = useMutation({
    mutationFn: () =>
      unwrap(
        session.api.POST("/api/v1/invitations", {
          body: { email: email.trim(), role: invitedRole, locale: language },
        }),
      ),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: invitationsKey(scope) });
      setEmail("");
      setTried(false);
      setSent(true);
    },
  });
  const code = invite.error instanceof ApiError ? invite.error.code : null;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    setSent(false);
    if (problem || invite.isPending) return;
    invite.mutate();
  };

  return (
    <Card as="section" aria-labelledby="invite-title">
      <h2 id="invite-title" className="m-0 mb-4 text-lg font-semibold text-text">
        {t("invite.title")}
      </h2>
      <form id={formId} noValidate className="flex max-w-form flex-col gap-4" onSubmit={submit}>
        <FormField
          label={t("invite.email")}
          required
          error={
            code && ADDRESS_CODES.has(code)
              ? errors(code)
              : tried && problem
                ? t(`invite.problems.${problem}`)
                : undefined
          }
        >
          {(control) => (
            <Input
              {...control}
              type="email"
              dir="ltr"
              autoComplete="off"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              data-field="invite-email"
            />
          )}
        </FormField>
        <FormField label={t("invite.role")}>
          {(control) => (
            <Select
              id={control.id}
              {...(control["aria-describedby"]
                ? { "aria-describedby": control["aria-describedby"] }
                : {})}
              value={invitedRole}
              onValueChange={(value) => setInvitedRole(value as TenantRole)}
              options={roles.map((option) => ({ value: option, label: roleWord(option) }))}
            />
          )}
        </FormField>
        <FormField label={t("invite.language")} hint={t("invite.languageHint")}>
          {(control) => (
            <Select
              id={control.id}
              {...(control["aria-describedby"]
                ? { "aria-describedby": control["aria-describedby"] }
                : {})}
              value={language}
              onValueChange={(value) => isLocale(value) && setLanguage(value)}
              options={LOCALES.map((option) => ({ value: option, label: LOCALE_NAMES[option] }))}
            />
          )}
        </FormField>
        {invite.isError && !(code && ADDRESS_CODES.has(code)) ? (
          <p role="alert" className="m-0 text-sm font-medium text-danger" data-error={code ?? ""}>
            {code === "plan_limit_exceeded" ? (
              <>
                {t("seatsUsed")}
                <PlanLimitLink />
              </>
            ) : (
              errors(code)
            )}
          </p>
        ) : null}
        {sent ? (
          <p role="status" className="m-0 text-sm text-text" data-invite-sent="">
            {t("invite.sent")}
          </p>
        ) : null}
        <div>
          <Button type="submit" {...busyProps(invite.isPending)} data-action="invite">
            {t("invite.submit")}
          </Button>
        </div>
      </form>
    </Card>
  );
}

export function InvitationsPage() {
  const t = useTranslations("app.team");
  const errors = useCodeText();
  const roleWord = useRoleWord();
  const times = useTimes();
  const now = useMinuteClock();
  const client = useQueryClient();
  const { session } = useSession();
  const { scope, role } = useWorkspace();
  const admin = isAdminRole(role);
  const [revoking, setRevoking] = useState<Invitation | null>(null);

  const invitations = useQuery({
    queryKey: invitationsKey(scope),
    queryFn: () => unwrap(session.api.GET("/api/v1/invitations")),
    // A member never asks (TI-01): the page draws its refusal without a request.
    enabled: admin,
  });

  const revoke = useMutation({
    mutationFn: (invitation: Invitation) =>
      unwrap(
        session.api.DELETE("/api/v1/invitations/{invitation_id}", {
          params: { path: { invitation_id: invitation.id } },
        }),
      ),
    onSuccess: async () => {
      setRevoking(null);
      await client.invalidateQueries({ queryKey: invitationsKey(scope) });
      requestAnimationFrame(() => focusIfLost(document.getElementById(HEADING)));
    },
  });
  const revokeCode = revoke.error instanceof ApiError ? revoke.error.code : null;

  if (!admin) {
    return (
      <div className="flex flex-col gap-6" data-screen="invitations">
        <ScreenHead id={HEADING} title={t("invitationsTitle")}>
          <TeamTabs current="invitations" />
        </ScreenHead>
        <EmptyState headingLevel={2} title={t("adminsOnly")} />
      </div>
    );
  }

  const open = (invitations.data ?? []).filter((item) => item.status === "pending");
  let list: React.ReactNode;
  if (invitations.isError) {
    const error = invitations.error;
    list = (
      <ErrorState
        headingLevel={2}
        title={t("loadError")}
        description={errors(error instanceof ApiError ? error.code : null)}
        requestId={error instanceof ApiError ? error.requestId : null}
        requestIdLabel={t("reference")}
        action={
          <Button variant="secondary" onClick={() => void invitations.refetch()}>
            {t("retry")}
          </Button>
        }
      />
    );
  } else if (invitations.isPending) {
    list = (
      <p role="status" className="m-0 py-6 text-center text-md text-text-muted">
        {t("loading")}
      </p>
    );
  } else if (!open.length) {
    list = <p className="m-0 text-md text-text-muted">{t("noInvitations")}</p>;
  } else {
    list = (
      <ul className="m-0 flex list-none flex-col gap-2 p-0" data-invitations-list="">
        {open.map((invitation) => {
          const state = invitationState(invitation, now);
          const at = times(invitation.expires_at);
          const sentAt = times(invitation.created_at);
          const lang = isLocale(invitation.locale) ? invitation.locale : null;
          return (
            <InvitationRow
              key={invitation.id}
              email={invitation.email}
              role={
                <RoleBadge tone={roleTone(invitation.role)}>{roleWord(invitation.role)}</RoleBadge>
              }
              state={state === "expired" ? t("expired") : t("expiresIn", { time: at?.time ?? "" })}
              expired={state === "expired"}
              stateFull={at?.full ?? null}
              language={lang ? LOCALE_NAMES[lang] : null}
              languageLabel={t("emailIn")}
              sender={
                invitation.invited_by
                  ? t("invitedBy", {
                      name: invitation.invited_by.full_name || invitation.invited_by.email,
                    })
                  : t("invitedByGone")
              }
              sent={sentAt ? t("sentAt", { time: sentAt.time }) : null}
              sentFull={sentAt?.full ?? null}
              actions={
                <Button
                  variant="ghost"
                  onClick={() => setRevoking(invitation)}
                  aria-label={t("revokeOf", { email: invitation.email })}
                  data-action="revoke"
                >
                  {t("revoke")}
                </Button>
              }
            />
          );
        })}
      </ul>
    );
  }

  return (
    <div className="flex flex-col gap-6" data-screen="invitations">
      <ScreenHead id={HEADING} title={t("invitationsTitle")} line={t("invitationsLine")}>
        <TeamTabs current="invitations" />
      </ScreenHead>
      <InviteForm />
      <Card as="section" aria-labelledby="open-invitations">
        <h2 id="open-invitations" className="m-0 mb-4 text-lg font-semibold text-text">
          {t("openTitle")}
        </h2>
        {list}
        <p className="m-0 mt-3 text-sm text-text-muted">{t("noResend")}</p>
      </Card>
      <Dialog
        open={revoking !== null}
        onOpenChange={(next) => {
          if (!next) {
            setRevoking(null);
            revoke.reset();
          }
        }}
        title={t("revokeTitle")}
        description={revoking ? t("revokeBody", { email: revoking.email }) : undefined}
        closeLabel={t("close")}
        footer={
          <>
            <Button variant="secondary" onClick={() => setRevoking(null)}>
              {t("cancel")}
            </Button>
            <Button
              variant="danger"
              {...busyProps(revoke.isPending)}
              onClick={() => revoking && !revoke.isPending && revoke.mutate(revoking)}
              data-action="confirm"
            >
              {t("revoke")}
            </Button>
          </>
        }
      >
        {revoke.isError ? (
          <p
            role="alert"
            className="m-0 text-sm font-medium text-danger"
            data-error={revokeCode ?? ""}
          >
            {errors(revokeCode)}
          </p>
        ) : null}
      </Dialog>
    </div>
  );
}
