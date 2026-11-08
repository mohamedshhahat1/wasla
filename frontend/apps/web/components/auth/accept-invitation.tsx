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

const schema = z.object({ full_name: z.string().max(200), password: z.string() });
type Values = z.infer<typeof schema>;

/**
 * APP-AUTH-06: POST /invitations/accept is unauthenticated - the token is the authorisation. A
 * new account sets its name and password here, then signs in and verifies (J3: the invitee is
 * unverified after accepting, FEB-17). A signed-in person accepts with one press and switches to
 * the workspace. The API has no invitation preview, so the page names the workspace only after.
 */
export function AcceptInvitation() {
  const t = useTranslations("auth");
  const { session, status, reloadProfile } = useSession();
  const router = useRouter();
  const token = useTokenFromAddress("token");
  const [error, setError] = useState<unknown>(null);
  const [joined, setJoined] = useState<string | null>(null);
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { full_name: "", password: "" },
  });
  const { isSubmitting } = form.formState;

  const onSubmit = form.handleSubmit(async (values) => {
    if (!token) return;
    setError(null);
    try {
      const signedIn = status === "signed-in";
      const accepted = await unwrap(
        session.api.POST("/api/v1/invitations/accept", {
          body: signedIn
            ? { token }
            : {
                token,
                full_name: values.full_name.trim() || null,
                password: values.password || null,
              },
        }),
      );
      if (signedIn) {
        await session.switchWorkspace(accepted.workspace.slug);
        await reloadProfile();
        setJoined(accepted.workspace.name);
        router.replace("/");
        return;
      }
      await session.login(accepted.email, values.password, accepted.workspace.slug);
      router.replace("/verify");
    } catch (caught) {
      setError(caught);
    }
  });

  if (token === undefined || status === "loading") return null;
  if (token === null) {
    return (
      <AuthCard title={t("invitation.title")}>
        <FormNote>{t("invitation.invalidLink")}</FormNote>
      </AuthCard>
    );
  }
  if (joined) {
    return (
      <AuthCard title={t("invitation.title")}>
        <FormNote>{t.rich("invitation.joined", { workspace: () => <bdi>{joined}</bdi> })}</FormNote>
      </AuthCard>
    );
  }
  const signedIn = status === "signed-in";
  return (
    <AuthCard
      title={t("invitation.title")}
      description={signedIn ? t("invitation.bodySignedIn") : t("invitation.body")}
    >
      <form
        noValidate
        onSubmit={(event) => void onSubmit(event)}
        className="flex flex-col gap-5"
        aria-busy={isSubmitting}
      >
        {signedIn ? null : (
          <>
            <TextField
              label={t("fullName")}
              autoComplete="name"
              error={fieldErrorText(error, "full_name", t)}
              {...form.register("full_name")}
            />
            <PasswordField
              label={t("password")}
              hint={t("invitation.passwordHint")}
              autoComplete="new-password"
              error={fieldErrorText(error, "password", t)}
              {...form.register("password")}
            />
          </>
        )}
        <FormAlert error={error} />
        <Button type="submit" disabled={isSubmitting}>
          {t("invitation.submit")}
        </Button>
      </form>
      {signedIn ? null : (
        <NextLink href="/login" className={linkClasses("text-sm")}>
          {t("signup.haveAccount")}
        </NextLink>
      )}
    </AuthCard>
  );
}
