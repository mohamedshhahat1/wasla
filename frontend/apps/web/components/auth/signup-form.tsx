"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import NextLink from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { z } from "zod";

import { Button, linkClasses } from "@wasla/ui";

import { AuthCard } from "../page-gate";
import { useSession } from "../session-provider";
import { fieldErrorText, PasswordField, TextField } from "./fields";
import { FormAlert, FormNote } from "./form-alert";
import { GoogleButton } from "./google-button";
import { suggestSlug, TimezoneField } from "./timezone-field";

/** Presence and shape only; the password policy and the slug rules are the API's (AU-01). */
const schema = z.object({
  full_name: z.string().max(200),
  email: z.email(),
  password: z.string().min(1),
  workspace_name: z.string().trim().min(1).max(200),
  workspace_slug: z.string().trim().min(1).max(100),
  timezone: z.string().min(1),
});
type Values = z.infer<typeof schema>;

/**
 * APP-AUTH-01: sign up. POST /auth/register takes the first workspace with the account, so the
 * form asks for it here. An address already registered gets "check your inbox" - the same page
 * whether or not it exists (AU-04).
 */
export function SignupForm() {
  const t = useTranslations("auth");
  const { session } = useSession();
  const router = useRouter();
  const [error, setError] = useState<unknown>(null);
  const [checkInbox, setCheckInbox] = useState(false);
  const slugEdited = useRef(false);
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: {
      full_name: "",
      email: "",
      password: "",
      workspace_name: "",
      workspace_slug: "",
      timezone: "",
    },
  });
  const { errors, isSubmitting } = form.formState;
  const apiField = (field: string) => fieldErrorText(error, field, t);

  const onSubmit = form.handleSubmit(async (values) => {
    setError(null);
    try {
      const outcome = await session.registerLogin({
        ...values,
        full_name: values.full_name.trim() || null,
      });
      if (outcome.kind === "check_inbox") setCheckInbox(true);
      else router.replace("/verify");
    } catch (caught) {
      setError(caught);
    }
  });

  if (checkInbox) {
    return (
      <AuthCard title={t("signup.checkInboxTitle")}>
        <FormNote>{t("signup.checkInboxBody")}</FormNote>
        <NextLink href="/login" className={linkClasses()}>
          {t("signup.toLogin")}
        </NextLink>
      </AuthCard>
    );
  }

  const nameField = form.register("workspace_name");
  return (
    <AuthCard title={t("signup.title")} description={t("signup.body")}>
      <form
        noValidate
        onSubmit={(event) => void onSubmit(event)}
        className="flex flex-col gap-5"
        aria-busy={isSubmitting}
      >
        <TextField
          label={t("fullName")}
          autoComplete="name"
          error={apiField("full_name")}
          {...form.register("full_name")}
        />
        <TextField
          label={t("email")}
          type="email"
          autoComplete="email"
          inputMode="email"
          required
          error={errors.email ? t("fieldErrors.email") : apiField("email")}
          {...form.register("email")}
        />
        <PasswordField
          label={t("password")}
          autoComplete="new-password"
          required
          error={errors.password ? t("fieldErrors.missing") : apiField("password")}
          {...form.register("password")}
        />
        <TextField
          label={t("workspaceName")}
          autoComplete="organization"
          required
          error={errors.workspace_name ? t("fieldErrors.missing") : apiField("workspace_name")}
          {...nameField}
          onChange={(event) => {
            void nameField.onChange(event);
            if (!slugEdited.current) {
              form.setValue("workspace_slug", suggestSlug(event.target.value));
            }
          }}
        />
        <TextField
          label={t("workspaceSlug")}
          hint={t("workspaceSlugHint")}
          autoComplete="off"
          spellCheck={false}
          dir="ltr"
          required
          error={errors.workspace_slug ? t("fieldErrors.missing") : apiField("workspace_slug")}
          {...form.register("workspace_slug", {
            onChange: () => {
              slugEdited.current = true;
            },
          })}
        />
        <Controller
          control={form.control}
          name="timezone"
          render={({ field }) => (
            <TimezoneField
              value={field.value || null}
              onChange={(zone) => field.onChange(zone ?? "")}
              error={errors.timezone ? t("fieldErrors.missing") : apiField("timezone")}
            />
          )}
        />
        <FormAlert error={error} />
        <Button type="submit" disabled={isSubmitting}>
          {t("signup.submit")}
        </Button>
      </form>
      <GoogleButton />
      <NextLink href="/login" className={linkClasses("text-sm")}>
        {t("signup.haveAccount")}
      </NextLink>
    </AuthCard>
  );
}
