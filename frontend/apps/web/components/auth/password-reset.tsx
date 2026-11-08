"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import NextLink from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { unwrap } from "@wasla/api-client";
import { Button, linkClasses } from "@wasla/ui";

import { AuthCard } from "../page-gate";
import { useSession } from "../session-provider";
import { fieldErrorText, PasswordField, TextField } from "./fields";
import { FormAlert, FormNote } from "./form-alert";
import { useTokenFromAddress } from "./use-token-from-address";

/** APP-AUTH-05 (request): always the same answer, so it tells nobody which addresses exist. */
export function ForgotForm() {
  const t = useTranslations("auth");
  const { session } = useSession();
  const [error, setError] = useState<unknown>(null);
  const [sent, setSent] = useState(false);
  const form = useForm<{ email: string }>({
    resolver: zodResolver(z.object({ email: z.email() })),
    defaultValues: { email: "" },
  });
  const { errors, isSubmitting } = form.formState;
  const onSubmit = form.handleSubmit(async (values) => {
    setError(null);
    try {
      await unwrap(
        session.api.POST("/api/v1/auth/password-reset/request", { body: { email: values.email } }),
      );
      setSent(true);
    } catch (caught) {
      setError(caught);
    }
  });
  return (
    <AuthCard title={t("forgot.title")} description={t("forgot.body")}>
      {sent ? (
        <FormNote>{t("forgot.sent")}</FormNote>
      ) : (
        <form
          noValidate
          onSubmit={(event) => void onSubmit(event)}
          className="flex flex-col gap-5"
          aria-busy={isSubmitting}
        >
          <TextField
            label={t("email")}
            type="email"
            autoComplete="email"
            inputMode="email"
            required
            error={errors.email ? t("fieldErrors.email") : undefined}
            {...form.register("email")}
          />
          <FormAlert error={error} />
          <Button type="submit" disabled={isSubmitting}>
            {t("forgot.submit")}
          </Button>
        </form>
      )}
      <NextLink href="/login" className={linkClasses("text-sm")}>
        {t("signup.toLogin")}
      </NextLink>
    </AuthCard>
  );
}

const resetSchema = z
  .object({ password: z.string().min(1), confirm: z.string().min(1) })
  .refine((values) => values.password === values.confirm, { path: ["confirm"] });

/**
 * APP-AUTH-05 (confirm): the token is read and removed from the address bar before anything else
 * (AU-02); a used, expired or unknown token is one refusal by code.
 */
export function ResetForm() {
  const t = useTranslations("auth");
  const { session } = useSession();
  const router = useRouter();
  const token = useTokenFromAddress("token");
  const [error, setError] = useState<unknown>(null);
  const form = useForm<{ password: string; confirm: string }>({
    resolver: zodResolver(resetSchema),
    defaultValues: { password: "", confirm: "" },
  });
  const { errors, isSubmitting } = form.formState;
  const onSubmit = form.handleSubmit(async (values) => {
    if (!token) return;
    setError(null);
    try {
      await unwrap(
        session.api.POST("/api/v1/auth/password-reset/confirm", {
          body: { token, new_password: values.password },
        }),
      );
      router.replace("/login?note=reset");
    } catch (caught) {
      setError(caught);
    }
  });
  if (token === undefined) return null;
  if (token === null) {
    return (
      <AuthCard title={t("reset.title")}>
        <FormNote>{t("reset.invalidLink")}</FormNote>
        <NextLink href="/forgot" className={linkClasses("text-sm")}>
          {t("reset.again")}
        </NextLink>
      </AuthCard>
    );
  }
  return (
    <AuthCard title={t("reset.title")}>
      <form
        noValidate
        onSubmit={(event) => void onSubmit(event)}
        className="flex flex-col gap-5"
        aria-busy={isSubmitting}
      >
        <PasswordField
          label={t("newPassword")}
          autoComplete="new-password"
          required
          error={
            errors.password ? t("fieldErrors.missing") : fieldErrorText(error, "new_password", t)
          }
          {...form.register("password")}
        />
        <PasswordField
          label={t("confirmPassword")}
          autoComplete="new-password"
          required
          error={errors.confirm ? t("fieldErrors.mismatch") : undefined}
          {...form.register("confirm")}
        />
        <FormAlert error={error} />
        <Button type="submit" disabled={isSubmitting}>
          {t("reset.submit")}
        </Button>
      </form>
      <NextLink href="/forgot" className={linkClasses("text-sm")}>
        {t("reset.again")}
      </NextLink>
    </AuthCard>
  );
}
