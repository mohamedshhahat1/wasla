"use client";

/**
 * APP-SET-01 at /settings/workspace (SW-01 .. SW-03, ADR-201): the workspace's name, its address
 * (the owner edits it; an administrator reads it), its time zone with the local time now, and the
 * automation disclosure customers are shown. Only changed fields are sent (FE-5's dirty-field
 * rule); nothing is optimistic; a refusal sits on its field. Bounds come from the contract.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useMemo, useState, type FormEvent } from "react";

import { ApiError, unwrap, type components } from "@wasla/api-client";
import { Button, Card, Combobox, ErrorState, FormField, Input, Textarea, cn } from "@wasla/ui";

import { workspaceKey } from "../../lib/team/keys";
import { isAdminRole, workspacePatch, type WorkspaceDraft } from "../../lib/team/rules";
import { useWorkspace } from "../app/workspace-context";
import { useTimezones } from "../auth/timezone-field";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";
import { busyProps, ScreenHead } from "../team/screen-head";
import { SettingsFrame } from "./settings-frame";

type Workspace = components["schemas"]["WorkspaceRead"];

const HEADING = "workspace-settings-title";
/** The disclosure wording's bound per language (DisclosureWording in the contract). */
export const DISCLOSURE_MAX = 200;

function draftOf(workspace: Workspace): WorkspaceDraft {
  return {
    name: workspace.name,
    slug: workspace.slug,
    timezone: workspace.timezone,
    disclosureEn: workspace.automation_disclosure?.["en"] ?? "",
    disclosureAr: workspace.automation_disclosure?.["ar"] ?? "",
  };
}

/**
 * The local time now in the chosen zone, in the page's language. Computed with Intl from the
 * zone itself: GET workspace/local-time resolves a wall time in the SAVED zone, so it cannot
 * say what time it is in a zone that is chosen but not yet saved (a plan-vs-code note, RS7-01).
 */
function LocalTimeLine({ zone }: { zone: string }) {
  const t = useTranslations("app.settings");
  const locale = useLocale();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  let time: string;
  try {
    time = new Intl.DateTimeFormat(locale, {
      timeZone: zone,
      weekday: "long",
      hour: "2-digit",
      minute: "2-digit",
    }).format(now);
  } catch {
    return null;
  }
  return (
    <p className="m-0 text-sm text-text-muted" data-local-time="">
      {t("localNow", { time })}
    </p>
  );
}

export function WorkspaceSettings() {
  const t = useTranslations("app.settings");
  const errors = useCodeText();
  const client = useQueryClient();
  const { session, reloadProfile } = useSession();
  const { scope, role } = useWorkspace();
  const owner = role === "tenant_owner";
  const zones = useTimezones();

  const workspace = useQuery({
    queryKey: workspaceKey(scope),
    queryFn: () => unwrap(session.api.GET("/api/v1/workspace")),
    enabled: isAdminRole(role),
  });
  const initial = useMemo(
    () => (workspace.data ? draftOf(workspace.data) : null),
    [workspace.data],
  );
  const [draft, setDraft] = useState<WorkspaceDraft | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (initial) setDraft(initial);
  }, [initial]);

  const patch = initial && draft ? workspacePatch(initial, draft, { owner }) : {};
  const dirty = Object.keys(patch).length > 0;

  const save = useMutation({
    mutationFn: () => unwrap(session.api.PATCH("/api/v1/workspace", { body: patch })),
    onSuccess: async (next) => {
      client.setQueryData(workspaceKey(scope), next);
      setSaved(true);
      // The name and address live in the profile's workspace list too (the switcher, the menu).
      if ("name" in patch || "slug" in patch) await reloadProfile();
      if ("slug" in patch) {
        // The session names its workspace by address: follow the new one (SW-03).
        await session.switchWorkspace(next.slug);
      }
    },
  });
  const code = save.error instanceof ApiError ? save.error.code : null;
  const fieldErrors = save.error instanceof ApiError ? (save.error.fieldErrors ?? {}) : {};

  if (!isAdminRole(role)) {
    return (
      <SettingsFrame current="workspace">
        <ScreenHead id={HEADING} title={t("workspaceTitle")} line={t("adminsOnly")} />
      </SettingsFrame>
    );
  }

  const set = (field: keyof WorkspaceDraft) => (value: string) => {
    setSaved(false);
    setDraft((current) => (current ? { ...current, [field]: value } : current));
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!dirty || save.isPending) return;
    save.mutate();
  };

  let body: React.ReactNode;
  if (workspace.isError) {
    const error = workspace.error;
    body = (
      <ErrorState
        headingLevel={2}
        title={t("loadError")}
        description={errors(error instanceof ApiError ? error.code : null)}
        requestId={error instanceof ApiError ? error.requestId : null}
        requestIdLabel={t("reference")}
        action={
          <Button variant="secondary" onClick={() => void workspace.refetch()}>
            {t("retry")}
          </Button>
        }
      />
    );
  } else if (!draft || !initial) {
    body = (
      <p role="status" className="m-0 py-6 text-center text-md text-text-muted">
        {t("loading")}
      </p>
    );
  } else {
    body = (
      <form noValidate className="flex flex-col gap-6" onSubmit={submit} data-workspace-form="">
        <Card as="section" aria-labelledby="ws-identity">
          <h2 id="ws-identity" className="m-0 mb-4 text-lg font-semibold text-text">
            {t("identity")}
          </h2>
          <div className="flex max-w-form flex-col gap-4">
            <FormField
              label={t("name")}
              required
              error={
                draft.name.trim() === ""
                  ? t("nameRequired")
                  : fieldErrors["name"]
                    ? t("nameInvalid")
                    : undefined
              }
            >
              {(control) => (
                <Input
                  {...control}
                  dir="auto"
                  value={draft.name}
                  maxLength={200}
                  onChange={(event) => set("name")(event.target.value)}
                  data-field="workspace-name"
                />
              )}
            </FormField>
            {owner ? (
              <FormField
                label={t("address")}
                hint={t("addressHint")}
                error={
                  code === "workspace_slug_taken"
                    ? errors(code)
                    : fieldErrors["slug"]
                      ? t("addressInvalid")
                      : undefined
                }
              >
                {(control) => (
                  <Input
                    {...control}
                    dir="ltr"
                    autoComplete="off"
                    spellCheck={false}
                    value={draft.slug}
                    maxLength={100}
                    onChange={(event) => set("slug")(event.target.value)}
                    data-field="workspace-slug"
                  />
                )}
              </FormField>
            ) : (
              <div className="flex flex-col gap-1" data-field="workspace-slug-read">
                <span className="text-sm font-medium text-text">{t("address")}</span>
                <bdi dir="ltr" className="text-md text-text">
                  {initial.slug}
                </bdi>
                <span className="text-sm text-text-muted">{t("addressOwnerOnly")}</span>
              </div>
            )}
          </div>
        </Card>

        <Card as="section" aria-labelledby="ws-time">
          <h2 id="ws-time" className="m-0 mb-4 text-lg font-semibold text-text">
            {t("timeTitle")}
          </h2>
          <div className="flex max-w-form flex-col gap-2">
            <FormField
              label={t("timezone")}
              error={
                fieldErrors["timezone"] || code === "timezone_unknown"
                  ? t("timezoneInvalid")
                  : undefined
              }
            >
              {(control) => (
                <Combobox
                  id={control.id}
                  aria-labelledby={control["aria-labelledby"]}
                  value={draft.timezone}
                  onValueChange={(zone) => zone && set("timezone")(zone)}
                  emptyText={t("timezoneNone")}
                  options={(zones.data?.timezones ?? []).map((zone) => ({
                    value: zone.id,
                    label: `${zone.label} (${zone.utc_offset})`,
                  }))}
                />
              )}
            </FormField>
            <LocalTimeLine zone={initial.timezone} />
            <p className="m-0 text-sm text-text-muted">{t("timezoneUse")}</p>
          </div>
        </Card>

        <Card as="section" aria-labelledby="ws-disclosure">
          <h2 id="ws-disclosure" className="m-0 mb-2 text-lg font-semibold text-text">
            {t("disclosureTitle")}
          </h2>
          <p className="m-0 mb-4 text-md text-text-muted">{t("disclosureLine")}</p>
          <div className="flex max-w-prose flex-col gap-4">
            <FormField label={t("disclosureEn")} hint={t("disclosureEmpty")}>
              {(control) => (
                <Textarea
                  {...control}
                  dir="ltr"
                  lang="en"
                  rows={2}
                  maxLength={DISCLOSURE_MAX}
                  value={draft.disclosureEn}
                  onChange={(event) => set("disclosureEn")(event.target.value)}
                  data-field="disclosure-en"
                />
              )}
            </FormField>
            <FormField label={t("disclosureAr")} hint={t("disclosureEmpty")}>
              {(control) => (
                <Textarea
                  {...control}
                  dir="rtl"
                  lang="ar"
                  rows={2}
                  maxLength={DISCLOSURE_MAX}
                  value={draft.disclosureAr}
                  onChange={(event) => set("disclosureAr")(event.target.value)}
                  data-field="disclosure-ar"
                />
              )}
            </FormField>
          </div>
        </Card>

        <div
          className={cn(
            "z-10 flex flex-wrap items-center gap-3 rounded-xl border border-border bg-surface p-4 shadow-sm",
            // Sticky from sm only: on a phone a sticky bar still covered the field WebKit focused
            // (frontend.yml 44648984555, KY7-03), so there Save sits at the form's end.
            dirty && "sm:sticky sm:bottom-0",
          )}
          data-save-bar=""
        >
          <Button
            type="submit"
            {...busyProps(save.isPending)}
            {...(!dirty ? { "aria-disabled": true } : {})}
            data-action="save-workspace"
          >
            {t("save")}
          </Button>
          {dirty ? (
            <Button
              variant="secondary"
              onClick={() => {
                setDraft(initial);
                save.reset();
              }}
              data-action="discard-workspace"
            >
              {t("discard")}
            </Button>
          ) : null}
          <p
            role="status"
            aria-live="polite"
            // While dirty on a phone the line would wrap the sticky bar onto a second row, taller
            // than the page's scroll padding, and hide the field focus moved to (WCAG 2.4.11,
            // KY7-03 in WebKit). There Discard already says something changed; the line is still
            // read out. "Saved." stays visible.
            className={cn("m-0 text-sm text-text-muted", dirty && "max-sm:sr-only")}
            data-save-status=""
          >
            {saved && !dirty ? t("saved") : dirty ? t("unsaved") : ""}
          </p>
          {save.isError &&
          code !== "workspace_slug_taken" &&
          code !== "timezone_unknown" &&
          !Object.keys(fieldErrors).length ? (
            <p role="alert" className="m-0 text-sm font-medium text-danger">
              {errors(code)}
            </p>
          ) : null}
        </div>
      </form>
    );
  }

  return (
    <SettingsFrame current="workspace">
      <ScreenHead id={HEADING} title={t("workspaceTitle")} line={t("workspaceLine")} />
      {body}
    </SettingsFrame>
  );
}
