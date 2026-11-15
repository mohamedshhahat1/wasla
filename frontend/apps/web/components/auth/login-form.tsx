"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import NextLink from "next/link";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { useForm } from "react-hook-form";

import { ApiError } from "@wasla/api-client";
import { Button, linkClasses } from "@wasla/ui";

import { z } from "../../lib/zod";
import { AuthCard } from "../page-gate";
import { useSession } from "../session-provider";
import { PasswordField, TextField } from "./fields";
import { FormAlert, FormNote } from "./form-alert";
import { GoogleButton } from "./google-button";

const schema = z.object({
  email: z.email(),
  password: z.string().min(1),
});
type Values = z.infer<typeof schema>;

/** APP-AUTH-02: log in. Wrong credentials are one neutral sentence, whatever the reason (AU-04). */
export function LoginForm() {
  const t = useTranslations("auth");
  const { session } = useSession();
  const search = useSearchParams();
  const [error, setError] = useState<unknown>(null);
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { email: "", password: "" },
  });
  const { errors, isSubmitting } = form.formState;
  const note = search.get("note");

  const onSubmit = form.handleSubmit(async (values) => {
    setError(null);
    try {
      // Signed in: PageGate sends the person on (afterSignIn, with `next`) as soon as the session
      // knows the profile - the one place that decides (FEF-38). A second replace here, made when
      // this form's own /auth/me answered, could land after the person had already gone on and
      // pull them back to home.
      await session.login(values.email, values.password, search.get("workspace"));
    } catch (caught) {
      setError(caught);
    }
  });

  const neutral =
    error instanceof ApiError &&
    (error.code === "unauthenticated" || error.code === "validation_error")
      ? t("login.wrongCredentials")
      : undefined;

  return (
    <AuthCard title={t("login.title")}>
      {note === "reset" ? <FormNote>{t("reset.done")}</FormNote> : null}
      {note === "google_cancelled" ? <FormNote>{t("google.cancelled")}</FormNote> : null}
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
        <PasswordField
          label={t("password")}
          autoComplete="current-password"
          required
          error={errors.password ? t("fieldErrors.missing") : undefined}
          {...form.register("password")}
        />
        <FormAlert error={error} {...(neutral ? { override: neutral } : {})} />
        <Button type="submit" disabled={isSubmitting}>
          {t("login.submit")}
        </Button>
      </form>
      <GoogleButton />
      <div className="flex flex-wrap justify-between gap-3 text-sm">
        <NextLink href="/forgot" className={linkClasses()}>
          {t("login.forgot")}
        </NextLink>
        <NextLink href="/signup" className={linkClasses()}>
          {t("login.noAccount")}
        </NextLink>
      </div>
    </AuthCard>
  );
}
