"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Eye, EyeOff } from "lucide-react";
import NextLink from "next/link";
import { useRouter } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useState, type ReactNode } from "react";

import { queryKey, unwrap } from "@wasla/api-client";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  Dialog,
  EmptyState,
  FormField,
  Icon,
  IconButton,
  Input,
  KeyValueList,
  linkClasses,
  PageHeader,
  Toast,
  type StatusTone,
} from "@wasla/ui";

import { canManageChannels, lastFourDigits } from "../../../lib/channels";
import { FormAlert } from "../../auth/form-alert";
import { useSession } from "../../session-provider";
import { useWorkspace } from "../workspace-context";
import { useChannelReads, useChannelsChanged } from "./queries";

const STATUS_TONE: Record<string, StatusTone> = {
  active: "success",
  disabled: "warning",
  released: "neutral",
};

/** CN-04 for the verify dialog's token: the same secret rules as the connect form. */
function VerifyDialog({
  open,
  onOpenChange,
  accountId,
  onDone,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  accountId: string;
  onDone: () => Promise<void>;
}) {
  const t = useTranslations("app");
  const auth = useTranslations("auth");
  const { session } = useSession();
  const [token, setToken] = useState("");
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const close = (next: boolean) => {
    if (!next) {
      setToken("");
      setError(null);
    }
    onOpenChange(next);
  };
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await unwrap(
        session.api.POST("/api/v1/whatsapp/accounts/{account_id}/verify", {
          params: { path: { account_id: accountId } },
          body: { access_token: token },
        }),
      );
      setToken("");
      await onDone();
      close(false);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={t("detail.verifyTitle")}
      description={t("detail.verifyBody")}
      closeLabel={t("close")}
      footer={
        <>
          <Button variant="secondary" onClick={() => close(false)}>
            {t("cancel")}
          </Button>
          <Button busy={busy} disabled={!token} onClick={() => void submit()}>
            {t("detail.verifySubmit")}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <FormField
          label={t("connect.accessToken")}
          hint={t("connect.accessTokenHint")}
          required
          requiredLabel={auth("required")}
        >
          {(control) => (
            <div className="flex items-center gap-2">
              <Input
                {...control}
                required
                value={token}
                onChange={(event) => setToken(event.target.value)}
                type={visible ? "text" : "password"}
                autoComplete="off"
                spellCheck={false}
                autoCapitalize="off"
                autoCorrect="off"
                maxLength={512}
                data-secret=""
                className="flex-1"
              />
              <IconButton
                icon={visible ? EyeOff : Eye}
                iconName={visible ? "EyeOff" : "Eye"}
                label={visible ? t("connect.hideToken") : t("connect.showToken")}
                aria-pressed={visible}
                onClick={() => setVisible((shown) => !shown)}
              />
            </div>
          )}
        </FormField>
        <FormAlert error={error} />
      </div>
    </Dialog>
  );
}

/** APP-CH-03 (CN-06): the connection's facts, its messaging limit and the owner's / admin's actions. */
export function ConnectionDetail({ id }: { id: string }) {
  const t = useTranslations("app");
  const format = useFormatter();
  const router = useRouter();
  const { session } = useSession();
  const { scope, role } = useWorkspace();
  const { connections, accounts } = useChannelReads();
  const changed = useChannelsChanged();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [dialog, setDialog] = useState<"verify" | "disable" | "release" | null>(null);

  const account =
    accounts.data?.accounts.find((row) => row.id === id && row.status !== "released") ?? null;
  const connection = connections.data?.find((row) => row.id === id) ?? null;
  const limit = useQuery({
    queryKey: queryKey(scope, "messaging-limit", { id }),
    queryFn: () =>
      unwrap(
        session.api.GET("/api/v1/whatsapp/accounts/{account_id}/messaging-limit", {
          params: { path: { account_id: id } },
        }),
      ),
    enabled: Boolean(account),
    retry: false,
  });

  const back = (
    <NextLink href="/channels" className={linkClasses("inline-flex items-center gap-2 text-sm")}>
      <Icon icon={ArrowLeft} name="ArrowLeft" size={16} />
      {t("detail.back")}
    </NextLink>
  );

  if (accounts.isPending || connections.isPending) {
    return (
      <p role="status" className="m-0 text-sm text-text-muted">
        {t("home.loading")}
      </p>
    );
  }
  if (!account) {
    // Another workspace's id, a released one and a random one look the same (no hint, CN-06).
    return (
      <div className="flex flex-col gap-6" data-page="connection-not-found">
        {back}
        <EmptyState
          headingLevel={1}
          title={t("detail.notFoundTitle")}
          description={t("detail.notFoundBody")}
        />
      </div>
    );
  }

  const status = connection?.status ?? account.status;
  const manager = canManageChannels(role);
  const date = (value: string | null | undefined) =>
    value ? format.dateTime(new Date(value), { dateStyle: "long" }) : t("detail.none");
  const number = (value: number) => format.number(value, { numberingSystem: "latn" });

  const act = async (
    name: string,
    action: () => Promise<unknown>,
    done: string,
    after?: () => void,
  ) => {
    setBusy(name);
    setError(null);
    try {
      await action();
      await changed();
      setNotice(done);
      after?.();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(null);
    }
  };
  const path = { params: { path: { account_id: id } } };

  const facts: Array<{ key: string; label: ReactNode; value: ReactNode }> = [
    {
      key: "number",
      label: t("detail.number"),
      value: <bdi dir="ltr">{account.display_phone_number}</bdi>,
    },
    {
      key: "displayName",
      label: t("detail.displayName"),
      value: account.display_name ? <bdi>{account.display_name}</bdi> : t("detail.none"),
    },
    {
      key: "verifiedName",
      label: t("detail.verifiedName"),
      value: account.verified_name ? <bdi>{account.verified_name}</bdi> : t("detail.none"),
    },
    {
      key: "status",
      label: t("detail.status"),
      value: (
        <Badge tone={STATUS_TONE[status] ?? "neutral"}>{t(`channels.status.${status}`)}</Badge>
      ),
    },
    ...(connection
      ? [
          {
            key: "health",
            label: t("detail.health"),
            value: t(`channels.health.${connection.health}`),
          },
        ]
      : []),
    { key: "wabaId", label: t("detail.wabaId"), value: <bdi dir="ltr">{account.waba_id}</bdi> },
    {
      key: "phoneNumberId",
      label: t("detail.phoneNumberId"),
      value: <bdi dir="ltr">{account.phone_number_id}</bdi>,
    },
    {
      key: "since",
      label: t("detail.since"),
      value: date(connection?.ownership_started_at ?? account.created_at),
    },
    {
      key: "verified",
      label: t("detail.verified"),
      value: account.ownership_verified_at
        ? t("detail.verifiedAt", { date: date(account.ownership_verified_at) })
        : t("detail.notVerified"),
    },
    ...(connection?.disabled_reason
      ? [
          {
            key: "disabled",
            label: t("channels.status.disabled"),
            value:
              connection.disabled_reason === "manual"
                ? t("channels.disabledManually", { date: date(connection.disabled_at) })
                : t("channels.disabledByPlan", { date: date(connection.disabled_at) }),
          },
        ]
      : []),
  ];

  return (
    <div className="flex flex-col gap-6" data-page="connection" data-status={status}>
      {back}
      <div className="rounded-xl bg-secondary-subtle px-6 py-7 sm:px-8">
        <PageHeader
          title={<bdi dir="ltr">{account.display_phone_number}</bdi>}
          description={
            account.display_name ? <bdi>{account.display_name}</bdi> : t("channels.whatsapp")
          }
        />
      </div>
      <Card as="div">
        <div className="flex flex-col gap-5">
          <h2 id="facts-title" className="m-0 text-lg font-semibold text-text">
            {t("detail.facts")}
          </h2>
          <KeyValueList items={facts} />
        </div>
      </Card>
      <Card as="div" data-testid="messaging-limit">
        <div className="flex flex-col gap-4">
          <h2 id="limit-title" className="m-0 text-lg font-semibold text-text">
            {t("detail.limit")}
          </h2>
          {limit.isPending ? (
            <p role="status" className="m-0 text-sm text-text-muted">
              {t("home.loading")}
            </p>
          ) : limit.isError ? (
            <p className="m-0 text-sm text-text">{t("detail.limitError")}</p>
          ) : !limit.data.known ? (
            <p className="m-0 text-md text-text" data-testid="limit-unknown">
              {t("detail.limitUnknown")}
            </p>
          ) : (
            <KeyValueList
              items={[
                {
                  key: "tier",
                  label: t("detail.limitTier"),
                  value: <bdi dir="ltr">{limit.data.tier ?? t("detail.none")}</bdi>,
                },
                {
                  key: "limit",
                  label: t("detail.limitValue"),
                  value:
                    limit.data.messaging_limit !== null
                      ? number(limit.data.messaging_limit)
                      : t("detail.none"),
                },
                {
                  key: "used",
                  label: t("detail.limitUsed"),
                  value: number(limit.data.used_last_24h),
                },
                ...(limit.data.shared_with_other_numbers
                  ? [
                      {
                        key: "shared",
                        label: t("detail.limitShared"),
                        value: <Badge tone="info">{t("detail.limitShared")}</Badge>,
                      },
                    ]
                  : []),
              ]}
            />
          )}
        </div>
      </Card>
      {manager ? (
        <Card as="div" data-testid="connection-actions">
          <div className="flex flex-col gap-4">
            <h2 id="actions-title" className="m-0 text-lg font-semibold text-text">
              {t("detail.actions")}
            </h2>
            <FormAlert error={error} />
            <div className="flex flex-wrap gap-3">
              <Button
                variant="secondary"
                disabled={busy !== null}
                onClick={() => setDialog("verify")}
              >
                {t("detail.verify")}
              </Button>
              {status === "active" ? (
                <Button
                  variant="secondary"
                  disabled={busy !== null}
                  busy={busy === "disable"}
                  onClick={() => setDialog("disable")}
                >
                  {t("detail.disable")}
                </Button>
              ) : (
                <Button
                  disabled={busy !== null}
                  busy={busy === "enable"}
                  onClick={() =>
                    void act(
                      "enable",
                      () =>
                        unwrap(
                          session.api.POST("/api/v1/whatsapp/accounts/{account_id}/enable", path),
                        ),
                      t("detail.enabled_ok"),
                    )
                  }
                >
                  {t("detail.enable")}
                </Button>
              )}
              <Button
                variant="danger"
                disabled={busy !== null}
                busy={busy === "release"}
                onClick={() => setDialog("release")}
              >
                {t("detail.release")}
              </Button>
            </div>
          </div>
        </Card>
      ) : (
        <p className="m-0 text-sm text-text-muted" data-testid="connect-role-note">
          {t("roles.onlyAdmins")}
        </p>
      )}

      {manager ? (
        <>
          <VerifyDialog
            open={dialog === "verify"}
            onOpenChange={(open) => setDialog(open ? "verify" : null)}
            accountId={id}
            onDone={async () => {
              await changed();
              setNotice(t("detail.verified_ok"));
            }}
          />
          <Dialog
            open={dialog === "disable"}
            onOpenChange={(open) => setDialog(open ? "disable" : null)}
            title={t("detail.disableTitle")}
            description={t("detail.disableBody")}
            closeLabel={t("close")}
            footer={
              <>
                <Button variant="secondary" onClick={() => setDialog(null)}>
                  {t("cancel")}
                </Button>
                <Button
                  variant="danger"
                  onClick={() => {
                    setDialog(null);
                    void act(
                      "disable",
                      () =>
                        unwrap(
                          session.api.POST("/api/v1/whatsapp/accounts/{account_id}/disable", path),
                        ),
                      t("detail.disabled_ok"),
                    );
                  }}
                >
                  {t("detail.disable")}
                </Button>
              </>
            }
          />
          <ConfirmDialog
            open={dialog === "release"}
            onOpenChange={(open) => setDialog(open ? "release" : null)}
            title={t("detail.releaseTitle")}
            description={t("detail.releaseBody")}
            confirmationText={lastFourDigits(account.display_phone_number)}
            confirmationLabel={t.rich("detail.releaseConfirm", {
              digits: () => <bdi dir="ltr">{lastFourDigits(account.display_phone_number)}</bdi>,
            })}
            confirmLabel={t("detail.release")}
            cancelLabel={t("cancel")}
            closeLabel={t("close")}
            onConfirm={() => {
              setDialog(null);
              void act(
                "release",
                () =>
                  unwrap(session.api.POST("/api/v1/whatsapp/accounts/{account_id}/release", path)),
                t("detail.released_ok"),
                () => router.push("/channels?notice=released"),
              );
            }}
          />
        </>
      ) : null}
      <Toast
        open={notice !== null}
        onOpenChange={(open) => {
          if (!open) setNotice(null);
        }}
        tone="success"
        title={notice ?? ""}
        closeLabel={t("close")}
      />
    </div>
  );
}
