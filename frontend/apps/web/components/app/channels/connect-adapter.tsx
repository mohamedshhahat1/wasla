"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { ArrowLeft, ExternalLink, Eye, EyeOff } from "lucide-react";
import NextLink from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { useForm } from "react-hook-form";

import { unwrap } from "@wasla/api-client";
import {
  Button,
  Card,
  FormField,
  Icon,
  IconButton,
  Input,
  Link,
  linkClasses,
  PageHeader,
} from "@wasla/ui";

import { CONNECT_ADAPTERS, connectDecision } from "../../../lib/channels";
import { z } from "../../../lib/zod";
import { fieldErrorText, TextField } from "../../auth/fields";
import { FormAlert } from "../../auth/form-alert";
import { useSession } from "../../session-provider";
import { useWorkspace } from "../workspace-context";
import { useChannelReads, useChannelsChanged } from "./queries";

/** Meta's own pages the explainer links to (CN-03), plain external links. */
export const META_DOCS = {
  systemUsers: "https://www.facebook.com/business/help/503306463479099",
  cloudApi: "https://developers.facebook.com/docs/whatsapp/cloud-api/get-started",
} as const;

const schema = z.object({
  phone_number_id: z.string().trim().min(1).max(64),
  waba_id: z.string().trim().max(64),
  access_token: z.string().min(1).max(512),
  display_name: z.string().trim().max(120),
});
type Values = z.infer<typeof schema>;

/**
 * The access token field (CN-04): a secret end to end - type password with show / hide, no
 * autocomplete, no spell check, never prefilled, never in a URL, a cache, a log or storage.
 */
function SecretField({
  label,
  hint,
  error,
  ...input
}: React.InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  hint: string;
  error?: string | undefined;
}) {
  const t = useTranslations("app");
  const auth = useTranslations("auth");
  const [visible, setVisible] = useState(false);
  return (
    <FormField label={label} hint={hint} error={error} required requiredLabel={auth("required")}>
      {(control) => (
        <div className="flex items-center gap-2">
          <Input
            {...control}
            {...input}
            required
            type={visible ? "text" : "password"}
            autoComplete="off"
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
            data-secret=""
            className="flex-1"
          />
          <IconButton
            icon={visible ? EyeOff : Eye}
            iconName={visible ? "EyeOff" : "Eye"}
            label={visible ? t("connect.hideToken") : t("connect.showToken")}
            aria-pressed={visible}
            onClick={() => setVisible((shown) => !shown)}
          />
        </div>
      )}
    </FormField>
  );
}

function ExternalDocLink({ href, children }: { href: string; children: string }) {
  const t = useTranslations("app");
  return (
    <Link
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1"
    >
      {children}
      <Icon icon={ExternalLink} name="ExternalLink" size={16} />
      <span className="sr-only"> {t("connect.guide.newTab")}</span>
    </Link>
  );
}

/** The guided explainer (CN-03) - a draft for the owner, checked against Meta's pages. */
function Guide() {
  const t = useTranslations("app");
  return (
    <div className="flex flex-col gap-4" data-testid="connect-guide">
      <ol className="m-0 flex list-decimal flex-col gap-3 ps-6 text-md text-text">
        <li>{t("connect.guide.step1")}</li>
        <li>{t("connect.guide.step2")}</li>
        <li>{t("connect.guide.step3")}</li>
        <li>{t("connect.guide.step4")}</li>
      </ol>
      <p className="m-0 text-sm text-text-muted">{t("connect.guide.why")}</p>
      <ul className="m-0 flex list-none flex-col gap-2 p-0">
        <li>
          <ExternalDocLink href={META_DOCS.systemUsers}>{t("connect.guide.docs")}</ExternalDocLink>
        </li>
        <li>
          <ExternalDocLink href={META_DOCS.cloudApi}>
            {t("connect.guide.docsCloud")}
          </ExternalDocLink>
        </li>
      </ul>
    </div>
  );
}

/** The assisted WhatsApp adapter (CN-01, CN-04). */
function WhatsAppAssistedForm() {
  const t = useTranslations("app");
  const auth = useTranslations("auth");
  const { session } = useSession();
  const router = useRouter();
  const changed = useChannelsChanged();
  const [error, setError] = useState<unknown>(null);
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { phone_number_id: "", waba_id: "", access_token: "", display_name: "" },
  });
  const { errors, isSubmitting } = form.formState;
  const sentence = (issue: { type?: unknown } | undefined) =>
    issue
      ? auth(issue.type === "too_big" ? "fieldErrors.tooLong" : "fieldErrors.missing")
      : undefined;

  // A plain call, not a TanStack mutation: the mutation cache would keep the variables - the
  // token - after it settles (CN-04; a unit test holds it).
  const onSubmit = form.handleSubmit(async (values) => {
    setError(null);
    try {
      const account = await unwrap(
        session.api.POST("/api/v1/whatsapp/accounts", {
          body: {
            phone_number_id: values.phone_number_id,
            access_token: values.access_token,
            ...(values.waba_id ? { waba_id: values.waba_id } : {}),
            ...(values.display_name ? { display_name: values.display_name } : {}),
          },
        }),
      );
      form.reset();
      await changed();
      router.push(`/channels/${account.id}`);
    } catch (caught) {
      // The token stays in this form's memory only, so a typo elsewhere needs no re-paste; it is
      // gone when the page is left (the form unmounts).
      setError(caught);
    }
  });

  return (
    <form
      onSubmit={(event) => void onSubmit(event)}
      noValidate
      className="flex flex-col gap-5"
      data-testid="connect-form"
    >
      <TextField
        label={t("connect.phoneNumberId")}
        hint={t("connect.phoneNumberIdHint")}
        required
        inputMode="numeric"
        autoComplete="off"
        spellCheck={false}
        error={sentence(errors.phone_number_id) ?? fieldErrorText(error, "phone_number_id", auth)}
        {...form.register("phone_number_id")}
      />
      <TextField
        label={t("connect.wabaId")}
        hint={t("connect.wabaIdHint")}
        inputMode="numeric"
        autoComplete="off"
        spellCheck={false}
        error={fieldErrorText(error, "waba_id", auth)}
        {...form.register("waba_id")}
      />
      <SecretField
        label={t("connect.accessToken")}
        hint={t("connect.accessTokenHint")}
        maxLength={512}
        error={sentence(errors.access_token) ?? fieldErrorText(error, "access_token", auth)}
        {...form.register("access_token")}
      />
      <TextField
        label={t("connect.displayName")}
        hint={t("connect.displayNameHint")}
        autoComplete="off"
        error={fieldErrorText(error, "display_name", auth)}
        {...form.register("display_name")}
      />
      <FormAlert error={error} />
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" busy={isSubmitting}>
          {isSubmitting ? t("connect.busy") : t("connect.submit")}
        </Button>
        <NextLink href="/channels" className={linkClasses("text-sm")}>
          {t("connect.back")}
        </NextLink>
      </div>
    </form>
  );
}

/** /channels/connect/<adapter> (CN-02): the registry decides; owners and admins only. */
export function ConnectAdapterPage({ adapterKey }: { adapterKey: string }) {
  const t = useTranslations("app");
  const { role } = useWorkspace();
  const { capacity } = useChannelReads();
  const [guideOpen, setGuideOpen] = useState(false);
  const adapter = CONNECT_ADAPTERS.find((entry) => entry.key === adapterKey);
  const cap = capacity.data ?? null;
  const decision = adapter
    ? connectDecision({
        role,
        channel: adapter.channel,
        capacity: cap
          ? { remaining: cap.remaining, allowed_channel_types: cap.breakdown.allowed_channel_types }
          : null,
      })
    : null;
  const back = (
    <NextLink href="/channels" className={linkClasses("inline-flex items-center gap-2 text-sm")}>
      <Icon icon={ArrowLeft} name="ArrowLeft" size={16} />
      {t("connect.back")}
    </NextLink>
  );
  if (!adapter || (decision && !decision.allowed && decision.reason === "role")) {
    return (
      <div className="flex flex-col gap-6" data-page="connect">
        {back}
        <PageHeader title={t("connect.title")} />
        <Card>
          <p className="m-0 text-md text-text" data-testid="connect-role-note">
            {t("roles.onlyAdmins")}
          </p>
        </Card>
      </div>
    );
  }
  const blocked =
    decision &&
    !decision.allowed &&
    (decision.reason === "noSlots" || decision.reason === "notAllowed")
      ? decision.reason === "noSlots"
        ? t("channels.noSlots")
        : t("channels.notAllowed")
      : null;
  return (
    <div className="flex flex-col gap-6" data-page="connect">
      {back}
      <div className="rounded-xl bg-secondary-subtle px-6 py-7 sm:px-8">
        <PageHeader title={t("connect.title")} description={t("connect.line")} />
      </div>
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-5">
        <Card as="div" className="lg:col-span-3">
          {blocked ? (
            <p className="m-0 text-md text-text" data-testid="connect-reason">
              {blocked}
            </p>
          ) : (
            <WhatsAppAssistedForm />
          )}
        </Card>
        <Card as="div" className="lg:col-span-2">
          <div className="flex flex-col gap-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 id="guide-title" className="m-0 text-lg font-semibold text-text">
                {t("connect.guide.title")}
              </h2>
              <Button
                variant="ghost"
                className="lg:hidden"
                aria-expanded={guideOpen}
                aria-controls="connect-guide-body"
                onClick={() => setGuideOpen((open) => !open)}
              >
                {t("connect.guide.toggle")}
              </Button>
            </div>
            <div id="connect-guide-body" className={guideOpen ? "block" : "hidden lg:block"}>
              <Guide />
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
}
