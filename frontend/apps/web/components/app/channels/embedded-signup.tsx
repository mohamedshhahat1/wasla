"use client";

import { ArrowLeft, Copy } from "lucide-react";
import NextLink from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import { unwrap } from "@wasla/api-client";
import { Badge, Banner, Button, Card, Icon, linkClasses, PageHeader } from "@wasla/ui";

import { availableAdapters, connectDecision } from "../../../lib/channels";
import {
  cancelStepKey,
  codeStillFresh,
  errorSentenceKey,
  loginExtras,
  readSessionEvent,
  sdkSource,
  type SessionEvent,
} from "../../../lib/embedded-signup";
import { FormAlert } from "../../auth/form-alert";
import { useSession } from "../../session-provider";
import { useWorkspace } from "../workspace-context";
import { useChannelReads, useChannelsChanged, useEmbeddedSignupConfig } from "./queries";

/** What Meta's SDK exposes on window (implementation, read 2026-11-21). */
interface FacebookSdk {
  init(options: Record<string, unknown>): void;
  login(callback: (response: LoginResponse) => void, options: Record<string, unknown>): void;
}
interface LoginResponse {
  authResponse?: { code?: string } | null;
}
declare global {
  interface Window {
    FB?: FacebookSdk;
    fbAsyncInit?: () => void;
  }
}

export type FlowState =
  | { step: "idle" }
  | { step: "open" }
  | { step: "connecting" }
  | { step: "retry"; error: unknown }
  | { step: "expired" }
  | { step: "noCode" }
  | { step: "cancelled"; stepKey: string }
  | { step: "failed"; sentenceKey: string; sessionId: string | null }
  | { step: "refused"; error: unknown }
  | { step: "sdkFailed" };

const SCRIPT_ID = "wasla-meta-sdk";

/**
 * SD-01 .. SD-03, SD-07, SD-10: Meta's SDK, loaded once on this route only, initialised with the
 * API's app id and Graph version. Resolves to the SDK, or null when the script did not load.
 */
function useFacebookSdk(appId: string | null, graphVersion: string | null) {
  const [sdk, setSdk] = useState<FacebookSdk | null | undefined>(undefined);
  useEffect(() => {
    if (!appId || !graphVersion) return;
    const source = sdkSource();
    const ready = () => {
      if (!window.FB) return;
      window.FB.init({ appId, autoLogAppEvents: true, xfbml: true, version: graphVersion });
      setSdk(window.FB);
    };
    if (window.FB) {
      ready();
      return;
    }
    window.fbAsyncInit = ready;
    let script = document.getElementById(SCRIPT_ID) as HTMLScriptElement | null;
    if (!script) {
      script = document.createElement("script");
      script.id = SCRIPT_ID;
      script.async = true;
      script.defer = true;
      script.crossOrigin = "anonymous";
      script.src = source.url;
      document.body.appendChild(script);
    }
    const failed = () => setSdk(null);
    script.addEventListener("error", failed);
    return () => {
      script?.removeEventListener("error", failed);
      if (window.fbAsyncInit === ready) delete window.fbAsyncInit;
    };
  }, [appId, graphVersion]);
  return sdk;
}

/** The Embedded Signup flow (ST-01 .. ST-10): the code in a ref, sent once, never kept. */
function EmbeddedSignupFlow({
  config,
}: {
  config: {
    app_id: string;
    config_id: string;
    es_version: string;
    graph_version: string;
    coexistence: boolean;
  };
}) {
  const t = useTranslations("app");
  const router = useRouter();
  const { session } = useSession();
  const changed = useChannelsChanged();
  const sdk = useFacebookSdk(config.app_id, config.graph_version);
  const [state, setState] = useState<FlowState>({ step: "idle" });
  // ST-01: held in refs, never in state that persists, a URL, storage or a query cache.
  const code = useRef<string | null>(null);
  const codeAt = useRef<number>(0);
  const sessionInfo = useRef<Extract<SessionEvent, { kind: "finish" }> | null>(null);
  const idempotencyKey = useRef<string | null>(null);
  const settled = useRef(false);

  const send = useCallback(async () => {
    const info = sessionInfo.current;
    const current = code.current;
    if (!info || !current) return;
    if (!codeStillFresh(codeAt.current, Date.now())) {
      code.current = null;
      setState({ step: "expired" });
      return;
    }
    setState({ step: "connecting" });
    try {
      const answer = await unwrap(
        session.api.POST("/api/v1/whatsapp/embedded-signup", {
          params: { header: { "Idempotency-Key": idempotencyKey.current ?? "" } },
          body: {
            code: current,
            phone_number_id: info.phoneNumberId,
            messaging_account_id: info.messagingAccountId,
            is_on_biz_app: info.onBusinessApp,
            es_version: config.es_version === "v4" ? "v4" : "v5",
            ...(info.businessId ? { business_id: info.businessId } : {}),
          },
        }),
      );
      code.current = null;
      sessionInfo.current = null;
      await changed();
      router.push(`/channels/${answer.account.id}`);
    } catch (error) {
      const status = (error as { status?: number } | null)?.status;
      if (status === undefined || status === 0 || status >= 500) {
        // ST-09: a network failure keeps the code for a retry with the same key, while fresh.
        setState({ step: "retry", error });
        return;
      }
      code.current = null;
      setState({ step: "refused", error });
    }
  }, [changed, config.es_version, router, session.api]);

  const finish = useCallback(() => {
    if (settled.current) return;
    if (sessionInfo.current && code.current) {
      settled.current = true;
      void send();
    }
  }, [send]);

  useEffect(() => {
    const source = sdkSource();
    const listener = (event: MessageEvent) => {
      const read = readSessionEvent(event.origin, event.data, source);
      if (!read) return;
      if (read.kind === "finish") {
        sessionInfo.current = read;
        finish();
      } else if (read.kind === "cancel") {
        settled.current = true;
        setState({ step: "cancelled", stepKey: cancelStepKey(read.step) });
      } else {
        settled.current = true;
        setState({
          step: "failed",
          sentenceKey: errorSentenceKey(read.code),
          sessionId: read.sessionId,
        });
      }
    };
    window.addEventListener("message", listener);
    return () => window.removeEventListener("message", listener);
  }, [finish]);

  const launch = () => {
    // ST-07: a second click while one flow is open does nothing.
    if (!sdk || state.step === "open" || state.step === "connecting") return;
    code.current = null;
    sessionInfo.current = null;
    settled.current = false;
    // ST-08: one key per flow.
    idempotencyKey.current = crypto.randomUUID();
    setState({ step: "open" });
    sdk.login(
      (response) => {
        const received = response.authResponse?.code;
        if (received) {
          code.current = received;
          codeAt.current = Date.now();
          finish();
          return;
        }
        // ST-03: Meta finished but no code arrived - or the window closed on a cancel/error,
        // which the session listener has already described.
        window.setTimeout(() => {
          if (!settled.current) {
            settled.current = true;
            setState(sessionInfo.current ? { step: "noCode" } : { step: "idle" });
          }
        }, 300);
      },
      {
        config_id: config.config_id,
        response_type: "code",
        override_default_response_type: true,
        extras: loginExtras(config.es_version, config.coexistence),
      },
    );
  };

  const copySession = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      // Copy is a convenience; the id stays visible to read out.
    }
  };

  return (
    <div className="flex flex-col gap-5" data-testid="embedded-signup">
      {sdk === null ? (
        <Banner tone="danger" name="es-sdk-failed" title={t("embedded.sdkFailed")} />
      ) : null}
      {state.step === "connecting" ? (
        <p role="status" className="m-0 text-md text-text" data-testid="es-connecting">
          {t("embedded.connecting")}
        </p>
      ) : null}
      {state.step === "cancelled" ? (
        <p role="status" className="m-0 text-md text-text" data-testid="es-cancelled">
          {t("embedded.cancelled", { step: t(`embedded.steps.${state.stepKey}`) })}
        </p>
      ) : null}
      {state.step === "noCode" ? (
        <p role="status" className="m-0 text-md text-text" data-testid="es-no-code">
          {t("embedded.noCode")}
        </p>
      ) : null}
      {state.step === "expired" ? (
        <p role="status" className="m-0 text-md text-text" data-testid="es-expired">
          {t("embedded.expired")}
        </p>
      ) : null}
      {state.step === "failed" ? (
        <div className="flex flex-col gap-2" data-testid="es-failed">
          <p role="alert" className="m-0 text-md text-text">
            {t(`embedded.errors.${state.sentenceKey}`)}
          </p>
          {state.sessionId ? (
            <div className="flex flex-wrap items-center gap-2 text-sm text-text-muted">
              <span>
                {t("embedded.sessionId")} <bdi dir="ltr">{state.sessionId}</bdi>
              </span>
              <Button variant="ghost" onClick={() => void copySession(state.sessionId ?? "")}>
                <Icon icon={Copy} name="Copy" size={16} />
                {t("embedded.copySession")}
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}
      {state.step === "refused" ? <FormAlert error={state.error} /> : null}
      {state.step === "retry" ? (
        <div className="flex flex-col gap-3" data-testid="es-retry">
          <FormAlert error={state.error} />
          <Button
            onClick={() => {
              settled.current = true;
              void send();
            }}
          >
            {t("embedded.retry")}
          </Button>
        </div>
      ) : null}
      {state.step !== "connecting" && state.step !== "retry" ? (
        <div className="flex flex-wrap items-center gap-3">
          <Button
            onClick={launch}
            busy={state.step === "open"}
            disabled={!sdk}
            data-testid="es-launch"
          >
            {state.step === "idle" || state.step === "open"
              ? t("embedded.launch")
              : t("embedded.again")}
          </Button>
          <NextLink
            href="/channels/connect/whatsapp"
            className={linkClasses("text-sm")}
            data-testid="es-assisted"
          >
            {t("embedded.useToken")}
          </NextLink>
        </div>
      ) : null}
    </div>
  );
}

/** UA-04: the explainer - a new number, or the one already in the WhatsApp Business app. */
function Explainer({ coexistence, reminderDays }: { coexistence: boolean; reminderDays: number }) {
  const t = useTranslations("app");
  return (
    <div className="flex flex-col gap-5" data-testid="es-explainer">
      <section aria-labelledby="es-new-title" className="flex flex-col gap-2">
        <h3 id="es-new-title" className="m-0 text-md font-semibold text-text">
          {t("embedded.newTitle")}
        </h3>
        <p className="m-0 text-sm text-text">{t("embedded.newBody")}</p>
      </section>
      {coexistence ? (
        <section aria-labelledby="es-app-title" className="flex flex-col gap-2">
          <h3 id="es-app-title" className="m-0 text-md font-semibold text-text">
            {t("embedded.appTitle")}
          </h3>
          <ul className="m-0 flex list-disc flex-col gap-1 ps-5 text-sm text-text">
            <li>{t("embedded.appVersion")}</li>
            <li>{t("embedded.appReminder", { days: reminderDays })}</li>
            <li>{t("embedded.appFeatures")}</li>
            <li>{t("embedded.appCompanions")}</li>
            <li>{t("embedded.appHistory")}</li>
          </ul>
        </section>
      ) : null}
      <p className="m-0 text-sm text-text-muted">{t("embedded.payment")}</p>
    </div>
  );
}

/** /channels/connect/whatsapp-embedded (UA-03): offered only when the API says enabled (UA-02). */
export function EmbeddedSignupPage() {
  const t = useTranslations("app");
  const { role } = useWorkspace();
  const { capacity } = useChannelReads();
  const config = useEmbeddedSignupConfig();
  const cap = capacity.data ?? null;
  const enabled = config.data?.enabled === true;
  const decision = connectDecision({
    role,
    channel: "whatsapp",
    capacity: cap
      ? { remaining: cap.remaining, allowed_channel_types: cap.breakdown.allowed_channel_types }
      : null,
    adapters: availableAdapters(enabled),
  });
  const back = (
    <NextLink href="/channels" className={linkClasses("inline-flex items-center gap-2 text-sm")}>
      <Icon icon={ArrowLeft} name="ArrowLeft" size={16} />
      {t("connect.back")}
    </NextLink>
  );
  if (!decision.allowed && decision.reason === "role") {
    return (
      <div className="flex flex-col gap-6" data-page="connect">
        {back}
        <PageHeader title={t("embedded.title")} />
        <Card>
          <p className="m-0 text-md text-text" data-testid="connect-role-note">
            {t("roles.onlyAdmins")}
          </p>
        </Card>
      </div>
    );
  }
  const blocked =
    !decision.allowed && (decision.reason === "noSlots" || decision.reason === "notAllowed")
      ? decision.reason === "noSlots"
        ? t("channels.noSlots")
        : t("channels.notAllowed")
      : null;
  const data = config.data;
  return (
    <div className="flex flex-col gap-6" data-page="connect-embedded">
      {back}
      <div className="rounded-xl bg-secondary-subtle px-6 py-7 sm:px-8">
        <PageHeader title={t("embedded.title")} description={t("embedded.line")} />
      </div>
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-5">
        <Card as="div" className="lg:col-span-3">
          {blocked ? (
            <p className="m-0 text-md text-text" data-testid="connect-reason">
              {blocked}
            </p>
          ) : config.isPending ? (
            <p className="m-0 text-md text-text-muted">{t("embedded.loading")}</p>
          ) : data && data.enabled && data.app_id && data.config_id ? (
            <EmbeddedSignupFlow
              config={{
                app_id: data.app_id,
                config_id: data.config_id,
                es_version: data.es_version ?? "v5",
                graph_version: data.graph_version ?? "v27.0",
                coexistence: data.coexistence === true,
              }}
            />
          ) : (
            <div className="flex flex-col gap-3" data-testid="es-disabled">
              <p className="m-0 text-md text-text">{t("embedded.disabled")}</p>
              <NextLink href="/channels/connect/whatsapp" className={linkClasses("text-sm")}>
                {t("embedded.useToken")}
              </NextLink>
            </div>
          )}
        </Card>
        <Card as="div" className="lg:col-span-2">
          <div className="flex flex-col gap-4">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="m-0 text-lg font-semibold text-text">
                {t("embedded.explainerTitle")}
              </h2>
              <Badge tone="neutral">{t("embedded.withMeta")}</Badge>
            </div>
            <Explainer coexistence={data?.coexistence === true} reminderDays={13} />
          </div>
        </Card>
      </div>
    </div>
  );
}
