"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import NextLink from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { z } from "zod";

import { unwrap } from "@wasla/api-client";
import { Button, linkClasses } from "@wasla/ui";

import { fieldErrorText, TextField } from "../auth/fields";
import { FormAlert } from "../auth/form-alert";
import { suggestSlug, TimezoneField } from "../auth/timezone-field";
import { AuthCard } from "../page-gate";
import { useSession } from "../session-provider";

const schema = z.object({
  name: z.string().trim().min(1).max(200),
  slug: z.string().trim().min(1).max(100),
  timezone: z.string().min(1),
});
type Values = z.infer<typeof schema>;

/** APP-WS-01: POST /workspaces, then /session/switch to it (WK-01), then the next page. */
export function CreateWorkspaceForm() {
  const t = useTranslations("auth");
  const { session, reloadProfile, profile } = useSession();
  const router = useRouter();
  const [error, setError] = useState<unknown>(null);
  const slugEdited = useRef(false);
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { name: "", slug: "", timezone: "" },
  });
  const { errors, isSubmitting } = form.formState;
  const nameField = form.register("name");

  const onSubmit = form.handleSubmit(async (values) => {
    setError(null);
    try {
      const created = await unwrap(session.api.POST("/api/v1/workspaces", { body: values }));
      await session.switchWorkspace(created.workspace.slug);
      await reloadProfile();
      router.replace("/");
    } catch (caught) {
      setError(caught);
    }
  });

  return (
    <AuthCard title={t("workspace.createTitle")} description={t("workspace.createBody")}>
      <form
        noValidate
        onSubmit={(event) => void onSubmit(event)}
        className="flex flex-col gap-5"
        aria-busy={isSubmitting}
      >
        <TextField
          label={t("workspaceName")}
          autoComplete="organization"
          required
          error={errors.name ? t("fieldErrors.missing") : fieldErrorText(error, "name", t)}
          {...nameField}
          onChange={(event) => {
            void nameField.onChange(event);
            if (!slugEdited.current) form.setValue("slug", suggestSlug(event.target.value));
          }}
        />
        <TextField
          label={t("workspaceSlug")}
          hint={t("workspaceSlugHint")}
          autoComplete="off"
          spellCheck={false}
          dir="ltr"
          required
          error={errors.slug ? t("fieldErrors.missing") : fieldErrorText(error, "slug", t)}
          {...form.register("slug", {
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
              error={
                errors.timezone ? t("fieldErrors.missing") : fieldErrorText(error, "timezone", t)
              }
            />
          )}
        />
        <FormAlert error={error} />
        <Button type="submit" disabled={isSubmitting}>
          {t("workspace.createSubmit")}
        </Button>
      </form>
      {profile?.workspaces?.length ? (
        <NextLink href="/workspaces" className={linkClasses("text-sm")}>
          {t("workspace.chooseExisting")}
        </NextLink>
      ) : null}
    </AuthCard>
  );
}

/** APP-WS-02: GET /auth/me's workspaces; choosing one switches every tab (WK-02). */
export function WorkspaceList() {
  const t = useTranslations("auth");
  const { session, profile, workspace, reloadProfile } = useSession();
  const router = useRouter();
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const workspaces = profile?.workspaces ?? [];
  return (
    <AuthCard title={t("workspace.chooseTitle")}>
      <ul className="m-0 flex list-none flex-col gap-3 p-0">
        {workspaces.map((item) => {
          const current = item.slug === workspace?.slug;
          return (
            <li
              key={item.id}
              className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border p-4"
            >
              <span className="flex flex-col">
                <bdi className="font-medium text-text">{item.name}</bdi>
                <span className="text-sm text-text-muted">{t(`roles.${item.role}`)}</span>
              </span>
              <Button
                variant={current ? "ghost" : "secondary"}
                disabled={current || busy !== null}
                aria-current={current ? "true" : undefined}
                onClick={() => {
                  setBusy(item.slug);
                  setError(null);
                  void (async () => {
                    try {
                      await session.switchWorkspace(item.slug);
                      await reloadProfile();
                      router.replace("/");
                    } catch (caught) {
                      setError(caught);
                    } finally {
                      setBusy(null);
                    }
                  })();
                }}
              >
                {current ? t("workspace.current") : t("workspace.switchTo")}
              </Button>
            </li>
          );
        })}
      </ul>
      <FormAlert error={error} />
      <NextLink href="/workspaces/new" className={linkClasses("text-sm")}>
        {t("workspace.createLink")}
      </NextLink>
    </AuthCard>
  );
}
