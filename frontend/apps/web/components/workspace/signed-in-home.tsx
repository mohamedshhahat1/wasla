"use client";

import NextLink from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button, Card, ConfirmDialog, FormField, linkClasses, Select } from "@wasla/ui";

import { FormAlert } from "../auth/form-alert";
import { useSession } from "../session-provider";

/**
 * The signed-in placeholder (FE-2; FE-3 replaces it with the shell): who is signed in, in which
 * workspace, as which role; the workspace switcher; sign out; sign out everywhere.
 */
export function SignedInHome() {
  const t = useTranslations("auth");
  const common = useTranslations("common");
  const { session, profile, workspace, reloadProfile } = useSession();
  const router = useRouter();
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  if (!profile) return null;
  const active = workspace ?? profile.workspaces?.[0] ?? null;
  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="mx-auto flex w-full max-w-prose flex-col gap-6" data-testid="signed-in">
      <h1 className="m-0 text-3xl font-semibold leading-tight tracking-tight text-text">
        {t("home.title")}
      </h1>
      <Card as="div" className="flex flex-col gap-5">
        <p className="m-0 text-md text-text" data-testid="signed-in-as">
          {t.rich("home.signedInAs", {
            name: () => <bdi>{profile.full_name || profile.email}</bdi>,
            email: () => <bdi>{profile.email}</bdi>,
            workspace: () => <bdi data-testid="active-workspace">{active?.name ?? ""}</bdi>,
            role: () => (
              <span data-testid="active-role">{active ? t(`roles.${active.role}`) : ""}</span>
            ),
          })}
        </p>
        {profile.workspaces && profile.workspaces.length > 1 ? (
          <FormField label={t("home.switcher")}>
            {(control) => (
              <Select
                id={control.id}
                aria-labelledby={control["aria-labelledby"]}
                value={active?.slug ?? ""}
                disabled={busy}
                options={profile.workspaces!.map((item) => ({
                  value: item.slug,
                  label: item.name,
                }))}
                onValueChange={(slug) =>
                  void run(async () => {
                    if (slug === active?.slug) return;
                    await session.switchWorkspace(slug);
                    await reloadProfile();
                  })
                }
              />
            )}
          </FormField>
        ) : null}
        <div className="flex flex-wrap gap-3">
          <NextLink href="/workspaces" className={linkClasses("text-sm")}>
            {t("home.allWorkspaces")}
          </NextLink>
          <NextLink href="/workspaces/new" className={linkClasses("text-sm")}>
            {t("workspace.createLink")}
          </NextLink>
        </div>
      </Card>
      <FormAlert error={error} />
      <div className="flex flex-wrap gap-3">
        <Button
          variant="secondary"
          disabled={busy}
          onClick={() =>
            void run(async () => {
              await session.signOut();
              router.replace("/login");
            })
          }
        >
          {t("home.signOut")}
        </Button>
        <Button variant="danger" disabled={busy} onClick={() => setConfirmOpen(true)}>
          {t("home.signOutEverywhere")}
        </Button>
      </div>
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={t("home.signOutEverywhereTitle")}
        description={t("home.signOutEverywhereBody")}
        confirmationText={profile.email}
        confirmationLabel={t.rich("home.signOutEverywhereConfirm", {
          email: () => <bdi>{profile.email}</bdi>,
        })}
        confirmLabel={t("home.signOutEverywhere")}
        cancelLabel={t("cancel")}
        closeLabel={common("close")}
        onConfirm={() =>
          void run(async () => {
            await session.signOutEverywhere();
            router.replace("/login");
          })
        }
      />
    </section>
  );
}
