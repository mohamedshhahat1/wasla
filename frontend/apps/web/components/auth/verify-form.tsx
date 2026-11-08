"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";

import { ApiError, unwrap } from "@wasla/api-client";
import { Button } from "@wasla/ui";

import { afterSignIn } from "../../lib/routing";
import { z } from "../../lib/zod";
import { AuthCard } from "../page-gate";
import { useSession } from "../session-provider";
import { TextField } from "./fields";
import { FormAlert, FormNote } from "./form-alert";

/** The API's codes are six digits (email_verification_service: "six digits"). */
const schema = z.object({
  code: z
    .string()
    .trim()
    .regex(/^\d{6}$/),
});
type Values = z.infer<typeof schema>;

/** Seconds left of a cooldown that ends at `until` (epoch ms), from an injected now. */
export function secondsLeft(until: number | null, now: number): number {
  return until === null ? 0 : Math.max(0, Math.ceil((until - now) / 1000));
}

/**
 * APP-AUTH-04: enter the code. The same access token keeps working after it (plan 8.1); the
 * profile is re-read and the visitor goes on (WK-03).
 */
export function VerifyForm({ now = () => Date.now() }: { now?: () => number }) {
  const t = useTranslations("auth");
  const { session, profile, reloadProfile } = useSession();
  const router = useRouter();
  const [error, setError] = useState<unknown>(null);
  const [resendError, setResendError] = useState<unknown>(null);
  const [resent, setResent] = useState(false);
  const [cooldownUntil, setCooldownUntil] = useState<number | null>(null);
  const [, tick] = useState(0);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { code: "" } });
  const { errors, isSubmitting } = form.formState;
  const left = secondsLeft(cooldownUntil, now());

  useEffect(() => {
    if (left <= 0) return;
    const timer = setTimeout(() => tick((n) => n + 1), 1000);
    return () => clearTimeout(timer);
  }, [left]);

  const onSubmit = form.handleSubmit(async (values) => {
    setError(null);
    try {
      await unwrap(
        session.api.POST("/api/v1/auth/email/verification/verify", { body: { code: values.code } }),
      );
      const me = await reloadProfile();
      router.replace(afterSignIn({ verified: true, workspaceCount: me?.workspaces?.length ?? 0 }));
    } catch (caught) {
      setError(caught);
    }
  });

  const resend = async () => {
    setResendError(null);
    setResent(false);
    try {
      await unwrap(session.api.POST("/api/v1/auth/email/verification/send"));
      setResent(true);
    } catch (caught) {
      if (caught instanceof ApiError && caught.retryAfterSeconds !== null) {
        setCooldownUntil(now() + caught.retryAfterSeconds * 1000);
      } else {
        setResendError(caught);
      }
    }
  };

  const wrongCode =
    error instanceof ApiError && (error.code === "validation_error" || error.code === "bad_request")
      ? t("verify.wrongCode")
      : undefined;

  return (
    <AuthCard
      title={t("verify.title")}
      description={
        profile ? (
          <p className="m-0">
            {t.rich("verify.body", { email: () => <bdi>{profile.email}</bdi> })}
          </p>
        ) : null
      }
    >
      <form
        noValidate
        onSubmit={(event) => void onSubmit(event)}
        className="flex flex-col gap-5"
        aria-busy={isSubmitting}
      >
        <TextField
          label={t("verify.code")}
          autoComplete="one-time-code"
          inputMode="numeric"
          maxLength={6}
          dir="ltr"
          required
          error={errors.code ? t("verify.codeShape") : undefined}
          {...form.register("code")}
        />
        <FormAlert error={error} {...(wrongCode ? { override: wrongCode } : {})} />
        <Button type="submit" disabled={isSubmitting}>
          {t("verify.submit")}
        </Button>
      </form>
      <div className="flex flex-col gap-3">
        <Button variant="secondary" onClick={() => void resend()} disabled={left > 0}>
          {t("verify.resend")}
        </Button>
        {left > 0 ? (
          <p role="status" className="m-0 text-sm text-text-muted" data-testid="resend-cooldown">
            {t("verify.cooldown", { seconds: left })}
          </p>
        ) : null}
        {resent ? <FormNote>{t("verify.resent")}</FormNote> : null}
        <FormAlert error={resendError} />
      </div>
    </AuthCard>
  );
}
