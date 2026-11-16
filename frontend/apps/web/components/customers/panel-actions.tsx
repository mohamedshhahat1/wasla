"use client";

/**
 * APP-INBOX-03's FEB-19 part (PX-01 .. PX-04): what the inbox's customer panel can do with the
 * contact and the lead, beside what FE-4 drew. The contact's consent is not on the conversation,
 * so the panel reads GET /contacts/{id} once per opened conversation, under the key the contact
 * page shares. Clearing an opt-out and editing a lead's fields stay on their own pages.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import NextLink from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { unwrap } from "@wasla/api-client";
import { Button, Dialog, InlineEditField, buttonClasses } from "@wasla/ui";

import { CONTACTS, consentEventsKey, contactKey, resourcePrefix } from "../../lib/customers/keys";
import { CONTACT_NAME_MAX } from "../../lib/customers/lead-form";
import { contactHref, leadHref } from "../../lib/customers/view";
import { CONVERSATION, INBOX } from "../../lib/inbox/keys";
import { useWorkspace } from "../app/workspace-context";
import { useCodeText } from "../inbox/error-text";
import type { ConversationRead } from "../inbox/people";
import { useSession } from "../session-provider";
import { LeadCreateDialog } from "./lead-create-dialog";
import { LeadStatusControl } from "./lead-status";
import { codeOf, isNotFound, OptedOutBadge, useCounter, type LeadRead } from "./shared";

/** The name the team gives, the consent on this conversation's channel, and the way to the page. */
export function PanelContact({ conversation }: { conversation: ConversationRead }) {
  const t = useTranslations("app.customers");
  const panel = useTranslations("app.inbox.panel");
  const errors = useCodeText();
  const counter = useCounter();
  const queryClient = useQueryClient();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const [recording, setRecording] = useState(false);
  const contactId = conversation.participant.contact_id ?? null;

  const contact = useQuery({
    queryKey: contactKey(scope, contactId ?? "none"),
    queryFn: () =>
      unwrap(
        session.api.GET("/api/v1/contacts/{contact_id}", {
          params: { path: { contact_id: contactId } },
        }),
      ),
    enabled: Boolean(contactId),
    retry: (count, error) => !isNotFound(error) && count < 2,
  });

  const refresh = () => {
    if (!contactId) return;
    void queryClient.invalidateQueries({ queryKey: contactKey(scope, contactId) });
    void queryClient.invalidateQueries({ queryKey: consentEventsKey(scope, contactId) });
    void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, CONTACTS) });
    void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, CONVERSATION) });
    void queryClient.invalidateQueries({ queryKey: resourcePrefix(scope, INBOX) });
  };
  const rename = useMutation({
    mutationFn: (name: string | null) =>
      unwrap(
        session.api.PATCH("/api/v1/contacts/{contact_id}", {
          params: { path: { contact_id: contactId } },
          body: { name },
        }),
      ),
    onSuccess: (data) => {
      queryClient.setQueryData(contactKey(scope, contactId), data);
      refresh();
    },
  });
  const optOut = useMutation({
    mutationFn: () =>
      unwrap(
        session.api.POST("/api/v1/contacts/{contact_id}/opt-out", {
          params: { path: { contact_id: contactId } },
          body: { channel: conversation.channel, source: "team" },
        }),
      ),
    onSuccess: () => {
      setRecording(false);
      refresh();
    },
  });

  if (!contactId || !contact.data) return null;
  const consent = contact.data.channels.find((entry) => entry.channel === conversation.channel);
  const optedOut = Boolean(consent?.marketing_opt_out_at);

  return (
    <div className="flex flex-col gap-3" data-panel-contact="">
      <InlineEditField
        label={t("contact.nameLabel")}
        value={contact.data.name ?? ""}
        editLabel={contact.data.name ? t("contact.editName") : t("contact.giveName")}
        saveLabel={t("save")}
        cancelLabel={t("cancel")}
        maxLength={CONTACT_NAME_MAX}
        counter={counter}
        error={rename.isError ? errors(codeOf(rename.error)) : undefined}
        onReset={() => rename.reset()}
        onSave={(value) => rename.mutateAsync(value || null)}
      />
      <div
        className="flex flex-wrap items-center justify-between gap-2"
        data-panel-consent={optedOut ? "opted-out" : "receiving"}
      >
        <span className="flex flex-col gap-1">
          <span className="text-sm text-text-muted">{panel("consent")}</span>
          <span className="text-md text-text">
            {optedOut ? <OptedOutBadge /> : t("consent.receiving")}
          </span>
        </span>
        {optedOut ? null : (
          <Button
            variant="secondary"
            onClick={() => {
              optOut.reset();
              setRecording(true);
            }}
            data-record-opt-out=""
          >
            {t("consent.record")}
          </Button>
        )}
      </div>
      <NextLink
        href={contactHref(contactId)}
        className={buttonClasses("secondary")}
        data-open-customer=""
      >
        {t("lead.openCustomer")}
      </NextLink>
      <Dialog
        open={recording}
        onOpenChange={setRecording}
        title={t("consent.recordTitle")}
        description={t("consent.recordBody")}
        closeLabel={t("close")}
        footer={
          <>
            <Button variant="secondary" onClick={() => setRecording(false)}>
              {t("cancel")}
            </Button>
            <Button
              variant="primary"
              busy={optOut.isPending}
              onClick={() => optOut.mutate()}
              data-confirm-opt-out=""
            >
              {t("consent.record")}
            </Button>
          </>
        }
      >
        {optOut.isError ? (
          <p role="alert" className="m-0 text-sm font-medium text-danger">
            {errors(codeOf(optOut.error))}
          </p>
        ) : null}
      </Dialog>
    </div>
  );
}

/** With a lead: its status moves and the way to its page. */
export function PanelLead({ lead }: { lead: LeadRead }) {
  const t = useTranslations("app.customers");
  return (
    <div className="flex flex-col items-start gap-3" data-panel-lead-actions="">
      <LeadStatusControl lead={lead} />
      <NextLink href={leadHref(lead.id)} className={buttonClasses("secondary")} data-open-lead="">
        {t("lead.open")}
      </NextLink>
    </div>
  );
}

/** Without a lead: "Create lead", linked to the customer and this conversation (LD-10). */
export function PanelCreateLead({ conversation }: { conversation: ConversationRead }) {
  const panel = useTranslations("app.inbox.panel");
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        variant="secondary"
        className="self-start"
        onClick={() => setOpen(true)}
        data-create-lead=""
      >
        {panel("createLead")}
      </Button>
      {open ? (
        <LeadCreateDialog
          open
          stay
          onOpenChange={setOpen}
          contactId={conversation.participant.contact_id ?? null}
          conversationId={conversation.id}
          initialName={conversation.participant.display_name ?? null}
        />
      ) : null}
    </>
  );
}
