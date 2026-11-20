"use client";

/**
 * APP-ACC-01 at /settings/account (AC-01 .. AC-05, ADR-201), for every signed-in person: who they
 * are, their language (FEB-09: the account and this device together), their password - changed,
 * or set for the first time on a Google-only account - Google connected or not, signing out on
 * every device, and closing the account with exactly one proof: the password, or Google's
 * re-authentication (SG-02). When the account still owns workspaces, the page lists exactly the
 * ones the API names, each with a way to hand it over or close it, and closes nothing.
 *
 * GET /auth/me says whether the account has a password and whether Google is connected (FEB-45,
 * ADR-202), so the page offers only the password mode, the Google action and the closing proofs
 * that apply (lib/account/methods.ts), and reads /auth/me again after each change.
 * Passwords live in this page's memory only and are cleared when the API answers.
 */
import { useMutation } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import { ApiError, unwrap } from "@wasla/api-client";
import { LOCALE_NAMES, LOCALES, isLocale, type Locale } from "@wasla/i18n";
import {
  Button,
  Card,
  ConfirmDialog,
  DangerZone,
  Dialog,
  FormField,
  Input,
  RadioGroup,
} from "@wasla/ui";

import { noteGoogleFlow } from "../../lib/account/google-flow";
import { postDeviceLocale } from "../../lib/account/locale";
import { accountModes, signInFacts } from "../../lib/account/methods";
import { tabFlags } from "../../lib/account/tab-flags";
import { googleEnabled } from "../../lib/env";
import { switchTo } from "../../lib/switch";
import { ownedWorkspaces } from "../../lib/team/rules";
import { useWorkspace } from "../app/workspace-context";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";
import { SettingsFrame } from "../settings/settings-frame";
import { busyProps, ScreenHead } from "../team/screen-head";

const HEADING = "account-title";

function Section({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <Card as="section" aria-labelledby={id}>
      <h2 id={id} className="m-0 mb-4 text-lg font-semibold text-text">
        {title}
      </h2>
      <div className="flex flex-col gap-4">{children}</div>
    </Card>
  );
}

/* --------------------------------------------------------------- language */

function LanguageSection() {
  const t = useTranslations("app.accountPage");
  const errors = useCodeText();
  const requested = useLocale();
  const current: Locale = isLocale(requested) ? requested : "en";
  const { session } = useSession();
  const [choice, setChoice] = useState<Locale>(current);
  const save = useMutation({
    mutationFn: (locale: Locale) =>
      unwrap(session.api.PATCH("/api/v1/auth/me", { body: { locale } })),
    // The account first, then this device: the page reloads in the new language and direction.
    onSuccess: (profile) =>
      postDeviceLocale((profile.locale as Locale | null) ?? choice, "/settings/account"),
  });
  const code = save.error instanceof ApiError ? save.error.code : null;
  return (
    <Section id="account-language" title={t("language.title")}>
      <p className="m-0 text-md text-text-muted">{t("language.line")}</p>
      <p id="account-language-label" className="sr-only">
        {t("language.title")}
      </p>
      <RadioGroup
        aria-labelledby="account-language-label"
        value={choice}
        onValueChange={(value) => isLocale(value) && setChoice(value)}
        options={LOCALES.map((locale) => ({
          value: locale,
          label: <span lang={locale}>{LOCALE_NAMES[locale]}</span>,
        }))}
      />
      <div>
        <Button
          {...busyProps(save.isPending)}
          {...(choice === current ? { "aria-disabled": true } : {})}
          onClick={() => choice !== current && !save.isPending && save.mutate(choice)}
          data-action="save-language"
        >
          {t("language.save")}
        </Button>
      </div>
      {save.isError ? (
        <p role="alert" className="m-0 text-sm font-medium text-danger">
          {errors(code)}
        </p>
      ) : null}
    </Section>
  );
}

/* --------------------------------------------------------------- password */

function PasswordSection() {
  const t = useTranslations("app.accountPage");
  const errors = useCodeText();
  const router = useRouter();
  const { session, profile } = useSession();
  const formId = useId();
  const mode = accountModes(signInFacts(profile), googleEnabled()).password;
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [tried, setTried] = useState(false);
  const mismatch = next !== confirm;
  const short = next.length < 12;

  const change = useMutation({
    mutationFn: () =>
      mode === "change"
        ? unwrap(
            session.api.POST("/api/v1/auth/password", {
              body: { current_password: current, new_password: next },
            }),
          )
        : unwrap(session.api.POST("/api/v1/auth/password/set", { body: { new_password: next } })),
    onSettled: () => {
      // AC-02: a password never outlives the answer it was sent for.
      setCurrent("");
      setNext("");
      setConfirm("");
      setTried(false);
    },
    onSuccess: async () => {
      // Every session ended, this one too (the API raised the account's token version).
      await session.signOut();
      router.replace("/login?note=password_changed");
    },
  });
  const code = change.error instanceof ApiError ? change.error.code : null;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    if (short || mismatch || (mode === "change" && !current) || change.isPending) return;
    change.mutate();
  };

  return (
    <Section id="account-password" title={t("password.title")}>
      <p className="m-0 text-md text-text-muted">{t("password.sessions")}</p>
      {mode === "set" ? (
        <p className="m-0 text-md text-text-muted" data-password-mode="set">
          {t("password.setLine")}
        </p>
      ) : null}
      <form id={formId} noValidate className="flex max-w-form flex-col gap-4" onSubmit={submit}>
        {mode === "change" ? (
          <FormField
            label={t("password.current")}
            required
            error={
              code === "current_password_invalid"
                ? errors(code)
                : tried && !current
                  ? t("password.required")
                  : undefined
            }
          >
            {(control) => (
              <Input
                {...control}
                type="password"
                autoComplete="current-password"
                value={current}
                onChange={(event) => setCurrent(event.target.value)}
                data-field="current-password"
              />
            )}
          </FormField>
        ) : null}
        <FormField
          label={t("password.new")}
          hint={t("password.newHint")}
          required
          error={
            code === "password_unchanged"
              ? errors(code)
              : tried && short
                ? t("password.short")
                : undefined
          }
        >
          {(control) => (
            <Input
              {...control}
              type="password"
              autoComplete="new-password"
              value={next}
              onChange={(event) => setNext(event.target.value)}
              data-field="new-password"
            />
          )}
        </FormField>
        <FormField
          label={t("password.confirm")}
          required
          error={tried && mismatch ? t("password.mismatch") : undefined}
        >
          {(control) => (
            <Input
              {...control}
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
              data-field="confirm-password"
            />
          )}
        </FormField>
        {change.isError && code !== "current_password_invalid" && code !== "password_unchanged" ? (
          <p role="alert" className="m-0 text-sm font-medium text-danger" data-error={code ?? ""}>
            {code === "validation_error" && mode === "change"
              ? t("password.noPasswordYet")
              : code === "validation_error"
                ? t("password.alreadyHas")
                : errors(code)}
          </p>
        ) : null}
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" {...busyProps(change.isPending)} data-action="save-password">
            {mode === "change" ? t("password.change") : t("password.set")}
          </Button>
        </div>
      </form>
    </Section>
  );
}

/* ----------------------------------------------------------------- google */

function GoogleSection() {
  const t = useTranslations("app.accountPage");
  const errors = useCodeText();
  const { session, profile, reloadProfile } = useSession();
  const action = accountModes(signInFacts(profile), googleEnabled()).google;
  const params = useSearchParams();
  const [confirming, setConfirming] = useState(false);
  const [disconnected, setDisconnected] = useState(false);
  const connect = useMutation({
    mutationFn: () => session.googleLinkUrl(),
    onSuccess: (url) => {
      noteGoogleFlow(tabFlags(), "link");
      window.location.assign(url);
    },
  });
  const disconnect = useMutation({
    mutationFn: () => unwrap(session.api.DELETE("/api/v1/auth/identities/google")),
    onSuccess: async () => {
      setConfirming(false);
      setDisconnected(true);
      // The account's ways in changed: read them again so the section redraws (AC8-02).
      await reloadProfile();
    },
  });
  const connectCode = connect.error instanceof ApiError ? connect.error.code : null;
  const code = disconnect.error instanceof ApiError ? disconnect.error.code : null;
  const connected = params.get("google") === "connected";
  // Back from linking Google: the profile this tab loaded predates the link, so the account's ways
  // in are read again (AC8-02) - the closing proofs and this section follow the fresh answer.
  useEffect(() => {
    if (connected) void reloadProfile();
  }, [connected, reloadProfile]);
  return (
    <Section id="account-google" title={t("google.title")}>
      <p className="m-0 text-md text-text-muted">{t("google.line")}</p>
      {connected && !disconnected ? (
        <p role="status" className="m-0 text-md text-text" data-google-status="connected">
          {t("google.connected")}
        </p>
      ) : null}
      {disconnected ? (
        <p role="status" className="m-0 text-md text-text" data-google-status="disconnected">
          {t("google.disconnected")}
        </p>
      ) : null}
      {action === "disconnect-blocked" ? (
        <p className="m-0 text-md text-text" data-google-mode="blocked">
          {t("google.setPasswordFirst")}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-3">
        {action === "connect" ? (
          <Button
            variant="secondary"
            {...busyProps(connect.isPending)}
            onClick={() => !connect.isPending && connect.mutate()}
            data-action="connect-google"
          >
            {t("google.connect")}
          </Button>
        ) : null}
        {action === "disconnect" ? (
          <Button
            variant="ghost"
            onClick={() => setConfirming(true)}
            data-action="disconnect-google"
          >
            {t("google.disconnect")}
          </Button>
        ) : null}
      </div>
      {connect.isError ? (
        <p role="alert" className="m-0 text-sm font-medium text-danger">
          {errors(connectCode)}
        </p>
      ) : null}
      {code === "google_unlink_requires_password" ? (
        <p role="alert" className="m-0 text-sm font-medium text-danger" data-error={code}>
          {t("google.setPasswordFirst")}
        </p>
      ) : disconnect.isError ? (
        <p role="alert" className="m-0 text-sm font-medium text-danger" data-error={code ?? ""}>
          {code === "not_found" ? t("google.notConnected") : errors(code)}
        </p>
      ) : null}
      <Dialog
        open={confirming}
        onOpenChange={(open) => {
          if (!open) setConfirming(false);
        }}
        title={t("google.disconnectTitle")}
        description={t("google.disconnectBody")}
        closeLabel={t("dismiss")}
        footer={
          <>
            <Button variant="secondary" onClick={() => setConfirming(false)}>
              {t("cancel")}
            </Button>
            <Button
              variant="danger"
              {...busyProps(disconnect.isPending)}
              onClick={() => !disconnect.isPending && disconnect.mutate()}
              data-action="confirm"
            >
              {t("google.disconnect")}
            </Button>
          </>
        }
      />
    </Section>
  );
}

/* --------------------------------------------------------------- sessions */

function SessionsSection() {
  const t = useTranslations("app.accountPage");
  const auth = useTranslations("auth");
  const common = useTranslations("common");
  const errors = useCodeText();
  const router = useRouter();
  const { session, profile } = useSession();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!profile) return null;
  return (
    <Section id="account-sessions" title={t("sessions.title")}>
      <p className="m-0 text-md text-text-muted">{auth("home.signOutEverywhereBody")}</p>
      <div>
        <Button variant="secondary" onClick={() => setOpen(true)} data-action="sign-out-everywhere">
          {auth("home.signOutEverywhere")}
        </Button>
      </div>
      {error ? (
        <p role="alert" className="m-0 text-sm font-medium text-danger">
          {error}
        </p>
      ) : null}
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
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
          void (async () => {
            try {
              await session.signOutEverywhere();
              router.replace("/login?note=signed_out_everywhere");
            } catch (caught) {
              setError(errors(caught instanceof ApiError ? caught.code : null));
            }
          })()
        }
      />
    </Section>
  );
}

/* ---------------------------------------------------------- close account */

function CloseAccountSection({ reauthReady }: { reauthReady: boolean }) {
  const t = useTranslations("app.accountPage");
  const errors = useCodeText();
  const router = useRouter();
  const { session, profile } = useSession();
  const proofs = accountModes(signInFacts(profile), googleEnabled()).closeProofs;
  const [open, setOpen] = useState(reauthReady);
  const [proof, setProof] = useState<"password" | "google">(
    reauthReady || !proofs.includes("password") ? "google" : "password",
  );
  const [password, setPassword] = useState("");
  const [tried, setTried] = useState(false);
  const formId = useId();
  const { leaveWorkspace, rejoinWorkspace } = useWorkspace();
  const goTo = useMutation({
    mutationFn: async ({ slug, path }: { slug: string; path: string }) => {
      // Into that workspace the switcher's way (ST-05), then to the page that hands it over or
      // closes it: no workspace address ever travels in the URL (LY7-02).
      await switchTo(slug, {
        leave: leaveWorkspace,
        switchWorkspace: (target) => session.switchWorkspace(target),
        rejoin: rejoinWorkspace,
      });
      router.push(path);
    },
  });

  const close = useMutation({
    mutationFn: async () => {
      if (proof === "google") {
        await session.closeAccountWithGoogle();
        return;
      }
      await unwrap(session.api.DELETE("/api/v1/auth/me", { body: { current_password: password } }));
      // The account is closed and every session with it; this browser's cookie goes too.
      await session.signOut();
    },
    onSettled: () => setPassword(""),
    onSuccess: () => router.replace("/login?note=account_closed"),
  });
  const reauth = useMutation({
    mutationFn: () => session.googleReauthUrl(),
    onSuccess: (url) => {
      noteGoogleFlow(tabFlags(), "reauth");
      window.location.assign(url);
    },
  });
  const code =
    close.error instanceof ApiError
      ? close.error.code
      : reauth.error instanceof ApiError
        ? reauth.error.code
        : null;
  const owned =
    close.error instanceof ApiError && close.error.code === "account_owns_workspaces"
      ? ownedWorkspaces(close.error.details)
      : [];

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    if (close.isPending) return;
    if (proof === "password" && !password) return;
    if (proof === "google" && !reauthReady) {
      if (!reauth.isPending) reauth.mutate();
      return;
    }
    close.mutate();
  };

  return (
    <DangerZone
      title={t("close.title")}
      action={
        <Button variant="danger" onClick={() => setOpen(true)} data-action="close-account">
          {t("close.open")}
        </Button>
      }
    >
      <ul className="m-0 flex list-disc flex-col gap-1 ps-5">
        <li>{t("close.sessions")}</li>
        <li>{t("close.workspaces")}</li>
        <li>{t("close.noWayBack")}</li>
        <li>{t("close.address")}</li>
      </ul>
      {owned.length ? (
        <div className="flex flex-col gap-2" role="alert" data-owned-workspaces="">
          <p className="m-0 text-md font-medium text-text">{t("close.ownsFirst")}</p>
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {owned.map((workspace) => (
              <li key={workspace.id} className="flex flex-wrap items-center gap-3">
                <bdi className="font-semibold">{workspace.name}</bdi>
                <Button
                  variant="ghost"
                  {...busyProps(goTo.isPending)}
                  onClick={() =>
                    !goTo.isPending && goTo.mutate({ slug: workspace.slug, path: "/team" })
                  }
                  data-action="hand-over"
                >
                  {t("close.handOver")}
                </Button>
                <Button
                  variant="ghost"
                  {...busyProps(goTo.isPending)}
                  onClick={() =>
                    !goTo.isPending &&
                    goTo.mutate({ slug: workspace.slug, path: "/settings/workspace/close" })
                  }
                  data-action="close-owned"
                >
                  {t("close.closeIt")}
                </Button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <Dialog
        open={open}
        onOpenChange={(next) => {
          if (!next) {
            setOpen(false);
            setPassword("");
            setTried(false);
          }
        }}
        title={t("close.dialogTitle")}
        description={t("close.dialogBody")}
        closeLabel={t("close.cancel")}
        footer={
          <>
            <Button variant="secondary" onClick={() => setOpen(false)} data-action="cancel">
              {t("cancel")}
            </Button>
            <Button
              type="submit"
              form={formId}
              variant="danger"
              {...busyProps(close.isPending || reauth.isPending)}
              data-action="confirm-close-account"
            >
              {proof === "google" && !reauthReady ? t("close.withGoogle") : t("close.confirm")}
            </Button>
          </>
        }
      >
        <form id={formId} noValidate onSubmit={submit} className="flex flex-col gap-4">
          {proofs.length > 1 ? (
            <>
              <p id="close-proof" className="m-0 text-sm font-medium text-text">
                {t("close.proof")}
              </p>
              <RadioGroup
                aria-labelledby="close-proof"
                value={proof}
                onValueChange={(value) => setProof(value === "google" ? "google" : "password")}
                options={[
                  { value: "password", label: t("close.byPassword") },
                  { value: "google", label: t("close.byGoogle") },
                ]}
              />
            </>
          ) : null}
          {proof === "password" ? (
            <FormField
              label={t("password.current")}
              required
              error={
                code === "current_password_invalid"
                  ? errors(code)
                  : tried && !password
                    ? t("password.required")
                    : undefined
              }
            >
              {(control) => (
                <Input
                  {...control}
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  data-field="close-password"
                />
              )}
            </FormField>
          ) : (
            <p
              className="m-0 text-md text-text-muted"
              data-reauth={reauthReady ? "ready" : "needed"}
            >
              {reauthReady ? t("close.googleReady") : t("close.googleNeeded")}
            </p>
          )}
          {code && code !== "current_password_invalid" && code !== "account_owns_workspaces" ? (
            <p role="alert" className="m-0 text-sm font-medium text-danger" data-error={code}>
              {code === "reauthentication_unavailable" ? t("close.noGoogle") : errors(code)}
            </p>
          ) : null}
          {code === "account_owns_workspaces" ? (
            <p role="alert" className="m-0 text-sm font-medium text-danger" data-error={code}>
              {t("close.ownsFirst")}
            </p>
          ) : null}
        </form>
      </Dialog>
    </DangerZone>
  );
}

/* ------------------------------------------------------------------- page */

export function AccountPage() {
  const t = useTranslations("app.accountPage");
  const params = useSearchParams();
  const router = useRouter();
  const { profile } = useSession();
  const google = googleEnabled();
  const reauthReady = params.get("reauth") === "ready";
  const cleaned = useRef(false);
  useEffect(() => {
    // The return's markers leave the address once read: nothing to bookmark or share.
    if (cleaned.current || (!reauthReady && !params.get("google"))) return;
    cleaned.current = true;
    window.history.replaceState(window.history.state, "", "/settings/account");
  }, [params, reauthReady, router]);
  if (!profile) return null;
  const name = profile.full_name || profile.email;
  return (
    <SettingsFrame current="account">
      <ScreenHead id={HEADING} title={t("title")} line={t("line")} />
      <Section id="account-profile" title={t("profile.title")}>
        <dl className="m-0 grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2">
          <dt className="text-sm text-text-muted">{t("profile.name")}</dt>
          <dd className="m-0 text-md text-text">
            <bdi>{name}</bdi>
          </dd>
          <dt className="text-sm text-text-muted">{t("profile.email")}</dt>
          <dd className="m-0 text-md text-text">
            <bdi dir="ltr">{profile.email}</bdi>
          </dd>
        </dl>
      </Section>
      <LanguageSection />
      <PasswordSection />
      {google ? <GoogleSection /> : null}
      <SessionsSection />
      <CloseAccountSection reauthReady={reauthReady} />
    </SettingsFrame>
  );
}
