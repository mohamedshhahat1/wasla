"use client";

/**
 * APP-SET-03 at /settings/workspace/close (CW-01 / CW-02, ADR-201), the owner's alone. What it
 * says is what the code does (docs/API.md "Closing one"): everybody loses access at once, the
 * records stay, the address is never given to anyone else, nobody's account is closed - not the
 * plan's "retention 30 days", which nothing implements. The address is typed exactly; the server
 * checks it again. Afterwards the session opens the next workspace the API chooses, or none.
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useId, useState, type FormEvent } from "react";

import { ApiError, unwrap } from "@wasla/api-client";
import { Button, DangerZone, FormField, Input } from "@wasla/ui";

import { workspaceKey } from "../../lib/team/keys";
import { confirmationMatches } from "../../lib/team/rules";
import { useWorkspace } from "../app/workspace-context";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";
import { busyProps, ScreenHead } from "../team/screen-head";
import { SettingsFrame } from "./settings-frame";

const HEADING = "close-workspace-title";

export function CloseWorkspace() {
  const t = useTranslations("app.settings");
  const errors = useCodeText();
  const router = useRouter();
  const formId = useId();
  const { session } = useSession();
  const { scope, role, workspaceName, leaveWorkspace } = useWorkspace();
  const owner = role === "tenant_owner";
  const [typed, setTyped] = useState("");
  const [tried, setTried] = useState(false);

  const workspace = useQuery({
    queryKey: workspaceKey(scope),
    queryFn: () => unwrap(session.api.GET("/api/v1/workspace")),
    enabled: owner,
  });
  const slug = workspace.data?.slug ?? "";
  const matches = Boolean(slug) && confirmationMatches(typed, slug);

  const close = useMutation({
    mutationFn: () =>
      unwrap(session.api.DELETE("/api/v1/workspace", { body: { confirmation: typed } })),
    onSuccess: async () => {
      setTyped("");
      // This workspace is closed for everybody: leave its stream, then let the API choose the
      // next workspace by FEB-36's rule - or none, and the "no workspace" page.
      leaveWorkspace();
      await session.openAnotherWorkspace();
      router.replace("/");
    },
  });
  const code = close.error instanceof ApiError ? close.error.code : null;

  if (!owner) {
    return (
      <SettingsFrame current="close">
        <ScreenHead id={HEADING} title={t("closeTitle")} line={t("ownerOnly")} />
      </SettingsFrame>
    );
  }

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    if (!matches || close.isPending) return;
    close.mutate();
  };

  return (
    <SettingsFrame current="close">
      <ScreenHead id={HEADING} title={t("closeTitle")} />
      <DangerZone
        title={t("close.what", { workspace: workspaceName })}
        action={
          <Button
            type="submit"
            form={formId}
            variant="danger"
            {...busyProps(close.isPending)}
            {...(!matches ? { "aria-disabled": true } : {})}
            data-action="close-workspace"
          >
            {t("close.submit")}
          </Button>
        }
      >
        <ul className="m-0 flex list-disc flex-col gap-1 ps-5" data-close-facts="">
          <li>{t("close.access")}</li>
          <li>{t("close.records")}</li>
          <li>{t.rich("close.address", { slug: () => <bdi dir="ltr">{slug}</bdi> })}</li>
          <li>{t("close.accounts")}</li>
          <li>{t("close.undo")}</li>
        </ul>
        <form id={formId} noValidate onSubmit={submit} className="mt-2 max-w-form">
          <FormField
            label={t.rich("close.type", { slug: () => <bdi dir="ltr">{slug}</bdi> })}
            error={
              code === "workspace_confirmation_invalid"
                ? errors(code)
                : tried && !matches
                  ? t("close.mismatch")
                  : undefined
            }
          >
            {(control) => (
              <Input
                {...control}
                dir="ltr"
                autoComplete="off"
                spellCheck={false}
                value={typed}
                onChange={(event) => setTyped(event.target.value)}
                data-field="close-confirmation"
              />
            )}
          </FormField>
        </form>
        {close.isError && code !== "workspace_confirmation_invalid" ? (
          <p role="alert" className="m-0 text-sm font-medium text-danger">
            {errors(code)}
          </p>
        ) : null}
      </DangerZone>
    </SettingsFrame>
  );
}
