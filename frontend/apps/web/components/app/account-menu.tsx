"use client";

import { CircleUserRound } from "lucide-react";
import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { useState } from "react";

import { dirOf, isLocale } from "@wasla/i18n";
import { Button, ConfirmDialog, Icon, Popover } from "@wasla/ui";

import { FormAlert } from "../auth/form-alert";
import { LanguageSwitcher } from "../language-switcher";
import { useSession } from "../session-provider";

/**
 * The account menu (SH-04): who is signed in, in which workspace and as which role, the language,
 * Sign out and Sign out everywhere (FE-2's flows, unchanged). Nothing else - account settings are
 * FE-7's.
 */
export function AccountMenu({
  workspaceName,
  roleLabel,
}: {
  workspaceName: string;
  roleLabel: string;
}) {
  const t = useTranslations("app");
  const auth = useTranslations("auth");
  const common = useTranslations("common");
  const { session, profile } = useSession();
  const requested = useLocale();
  const locale = isLocale(requested) ? requested : "en";
  const dir = dirOf(locale);
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  if (!profile) return null;
  const name = profile.full_name || profile.email;

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
    <>
      <Popover
        dir={dir}
        side="bottom"
        label={t("account.label")}
        open={menuOpen}
        onOpenChange={setMenuOpen}
        trigger={
          <button
            type="button"
            data-account-trigger=""
            className="wasla-press inline-flex min-h-target min-w-target cursor-pointer items-center justify-center gap-2 rounded-md border border-transparent bg-transparent px-2 text-md text-on-brand hover:bg-primary active:bg-primary-pressed focus-visible:outline-2 focus-visible:outline-offset-2"
          >
            <Icon icon={CircleUserRound} name="CircleUserRound" />
            <span className="sr-only">{t("account.label")}: </span>
            <bdi className="hidden max-w-form truncate md:inline">{name}</bdi>
          </button>
        }
      >
        <div className="flex flex-col gap-4" data-account-menu="">
          <div className="flex flex-col gap-1">
            <p className="m-0 text-md font-semibold text-text">
              {t.rich("account.signedInAs", { name: () => <bdi>{name}</bdi> })}
            </p>
            <p className="m-0 text-sm text-text-muted">
              <bdi>{profile.email}</bdi>
            </p>
            <p className="m-0 text-sm text-text-muted">
              {t.rich("account.workspaceRole", {
                workspace: () => <bdi>{workspaceName}</bdi>,
                role: () => <span>{roleLabel}</span>,
              })}
            </p>
          </div>
          <div className="flex flex-col gap-2">
            <p className="m-0 text-sm font-medium text-text">{t("account.language")}</p>
            <LanguageSwitcher current={locale} label={common("languageLabel")} />
          </div>
          <FormAlert error={error} />
          <div className="flex flex-wrap gap-3 border-t border-border pt-4">
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
              {auth("home.signOut")}
            </Button>
            <Button
              variant="danger"
              disabled={busy}
              onClick={() => {
                // The menu closes first and the dialog opens once it has left the page: a dialog
                // over a popover still fading out is never read or scanned half-transparent.
                setMenuOpen(false);
                const openWhenGone = () => {
                  if (document.querySelector("[data-account-menu]")) {
                    requestAnimationFrame(openWhenGone);
                  } else {
                    setConfirmOpen(true);
                  }
                };
                requestAnimationFrame(openWhenGone);
              }}
            >
              {auth("home.signOutEverywhere")}
            </Button>
          </div>
        </div>
      </Popover>
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={auth("home.signOutEverywhereTitle")}
        description={auth("home.signOutEverywhereBody")}
        confirmationText={profile.email}
        confirmationLabel={auth.rich("home.signOutEverywhereConfirm", {
          email: () => <bdi>{profile.email}</bdi>,
        })}
        confirmLabel={auth("home.signOutEverywhere")}
        cancelLabel={auth("cancel")}
        closeLabel={common("close")}
        onConfirm={() =>
          void run(async () => {
            await session.signOutEverywhere();
            router.replace("/login");
          })
        }
      />
    </>
  );
}
