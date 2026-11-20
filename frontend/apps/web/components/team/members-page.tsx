"use client";

/**
 * APP-TEAM-01 at /team (TM-01 .. TM-05, ADR-201): who is in the workspace, with the controls the
 * API's own rules allow the viewer (lib/team/rules.ts) - absent, never disabled. Remove, leave,
 * reinstate and make owner each sit behind a dialog that says what happens; nothing is optimistic.
 * "Show people who left" is in the address (?left=1); nothing personal ever is (LY7-02).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import NextLink from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { ApiError, unwrap, type components } from "@wasla/api-client";
import {
  Button,
  Card,
  Dialog,
  ErrorState,
  linkClasses,
  MemberRow,
  RadioGroup,
  RoleBadge,
  Switch,
  type RoleTone,
} from "@wasla/ui";

import { focusIfLost } from "../../lib/focus";
import { membersFamily, teamMembersKey } from "../../lib/team/keys";
import {
  activeOwners,
  invitableRoles,
  isAdminRole,
  memberControls,
  otherRole,
  type TenantRole,
} from "../../lib/team/rules";
import { useWorkspace } from "../app/workspace-context";
import { PlanLimitLink } from "../billing/plan-limit-link";
import { useTimes } from "../customers/shared";
import { useCodeText } from "../inbox/error-text";
import { useSession } from "../session-provider";
import { busyProps, ScreenHead } from "./screen-head";
import { TeamTabs } from "./team-tabs";

const HEADING = "team-title";

type Member = components["schemas"]["MemberResponse"];

/** The API answers a bare list; at this size the page says it shows the first 100 (plan 21). */
export const MEMBERS_SHOWN = 100;

export function roleTone(role: string): RoleTone {
  return role === "tenant_owner" ? "owner" : role === "tenant_admin" ? "admin" : "member";
}

export function useRoleWord(): (role: string) => string {
  const auth = useTranslations("auth");
  return (role) => auth(`roles.${role}` as "roles.member");
}

type Pending =
  | { kind: "remove"; member: Member }
  | { kind: "leave"; member: Member }
  | { kind: "reinstate"; member: Member }
  | { kind: "owner"; member: Member }
  | { kind: "role"; member: Member }
  | null;

export function MembersPage() {
  const t = useTranslations("app.team");
  const errors = useCodeText();
  const roleWord = useRoleWord();
  const times = useTimes();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const client = useQueryClient();
  const { session, profile, reloadProfile } = useSession();
  const { scope, role, workspaceName } = useWorkspace();
  const admin = isAdminRole(role);
  // Only owners and administrators may ask for people who left (the API's include_revoked).
  const left = admin && params.get("left") === "1";
  const [pending, setPending] = useState<Pending>(null);
  const [reinstateRole, setReinstateRole] = useState<TenantRole>("member");
  const [newRole, setNewRole] = useState<"member" | "tenant_admin">("tenant_admin");

  const members = useQuery({
    queryKey: teamMembersKey(scope, left),
    queryFn: () =>
      unwrap(
        session.api.GET("/api/v1/workspace/members", {
          params: { query: left ? { include_revoked: true } : {} },
        }),
      ),
  });

  const list = members.data?.members ?? [];
  const owners = activeOwners(
    list.map((m) => ({ userId: m.user_id, role: m.role, status: m.status })),
  );
  const viewer = { userId: profile?.id ?? "", role: role ?? null };

  const afterChange = async () => {
    await client.invalidateQueries({ queryKey: membersFamily(scope) });
  };

  const act = useMutation({
    mutationFn: async (action: NonNullable<Pending>) => {
      const id = action.member.user_id;
      if (action.kind === "remove" || action.kind === "leave") {
        await unwrap(
          session.api.DELETE("/api/v1/workspace/members/{user_id}", {
            params: { path: { user_id: id } },
          }),
        );
      } else if (action.kind === "reinstate") {
        await unwrap(
          session.api.POST("/api/v1/workspace/members/{user_id}/reinstate", {
            params: { path: { user_id: id } },
            body: { role: reinstateRole },
          }),
        );
      } else if (action.kind === "role") {
        await unwrap(
          session.api.PATCH("/api/v1/workspace/members/{user_id}", {
            params: { path: { user_id: id } },
            body: { role: newRole },
          }),
        );
      } else {
        await unwrap(session.api.POST("/api/v1/workspace/ownership", { body: { user_id: id } }));
      }
      return action;
    },
    onSuccess: async (action) => {
      setPending(null);
      if (action.kind === "leave") {
        // This workspace is gone for this person: the next one the API opens (FEB-36's rule), or
        // the "no workspace" page when there is none.
        await session.openAnotherWorkspace();
        router.replace("/");
        return;
      }
      await afterChange();
      if (action.kind === "owner") await reloadProfile();
      if (action.kind === "role") {
        // The row stays and keeps its controls (KY8-04): focus goes back to its "Change role".
        const id = action.member.user_id;
        requestAnimationFrame(() =>
          focusIfLost(
            document.querySelector<HTMLElement>(`[data-change-role="${id}"]`) ??
              document.getElementById(HEADING),
          ),
        );
        return;
      }
      // The pressed row's controls changed or left: the page heading takes the focus (KY7-04).
      requestAnimationFrame(() => focusIfLost(document.getElementById(HEADING)));
    },
  });

  const toggleLeft = (next: boolean) => {
    const query = new URLSearchParams(params.toString());
    if (next) query.set("left", "1");
    else query.delete("left");
    const search = query.toString();
    router.replace(search ? `${pathname}?${search}` : pathname, { scroll: false });
  };

  const actionsFor = (member: Member) => {
    const controls = memberControls(
      viewer,
      { userId: member.user_id, role: member.role, status: member.status },
      owners,
    );
    const name = member.full_name || member.email;
    const buttons = [];
    if (controls.makeOwner && member.status === "active") {
      buttons.push(
        <Button
          key="owner"
          variant="secondary"
          onClick={() => setPending({ kind: "owner", member })}
          aria-label={t("makeOwnerOf", { name })}
          data-action="make-owner"
        >
          {t("makeOwner")}
        </Button>,
      );
    }
    if (controls.changeRole) {
      buttons.push(
        <Button
          key="role"
          variant="secondary"
          onClick={() => {
            setNewRole(otherRole(member.role));
            setPending({ kind: "role", member });
          }}
          aria-label={t("changeRoleOf", { name })}
          data-action="change-role"
          data-change-role={member.user_id}
        >
          {t("changeRole")}
        </Button>,
      );
    }
    if (controls.remove) {
      buttons.push(
        <Button
          key="remove"
          variant="ghost"
          onClick={() => setPending({ kind: "remove", member })}
          aria-label={t("removeOf", { name })}
          data-action="remove"
        >
          {t("remove")}
        </Button>,
      );
    }
    if (controls.leave) {
      buttons.push(
        <Button
          key="leave"
          variant="ghost"
          onClick={() => setPending({ kind: "leave", member })}
          data-action="leave"
        >
          {t("leave")}
        </Button>,
      );
    }
    if (controls.reinstate) {
      buttons.push(
        <Button
          key="reinstate"
          variant="secondary"
          onClick={() => {
            setReinstateRole(member.role === "tenant_admin" ? "tenant_admin" : "member");
            setPending({ kind: "reinstate", member });
          }}
          aria-label={t("reinstateOf", { name })}
          data-action="reinstate"
        >
          {t("reinstate")}
        </Button>,
      );
    }
    return {
      node: buttons.length ? <>{buttons}</> : undefined,
      note: controls.leaveBlocked ? t("lastOwner") : undefined,
    };
  };

  let body: React.ReactNode;
  if (members.isError) {
    const error = members.error;
    body = (
      <ErrorState
        headingLevel={2}
        title={t("loadError")}
        description={errors(error instanceof ApiError ? error.code : null)}
        requestId={error instanceof ApiError ? error.requestId : null}
        requestIdLabel={t("reference")}
        action={
          <Button variant="secondary" onClick={() => void members.refetch()}>
            {t("retry")}
          </Button>
        }
      />
    );
  } else if (members.isPending) {
    body = (
      <p role="status" className="m-0 py-6 text-center text-md text-text-muted">
        {t("loading")}
      </p>
    );
  } else {
    const active = list.filter((m) => m.status === "active");
    const gone = list.filter((m) => m.status !== "active");
    const row = (member: Member) => {
      const removed = member.status !== "active";
      const at = times(removed ? member.revoked_at : member.joined_at);
      const { node, note } = actionsFor(member);
      return (
        <MemberRow
          key={member.id}
          name={member.full_name || member.email}
          email={member.email}
          role={<RoleBadge tone={roleTone(member.role)}>{roleWord(member.role)}</RoleBadge>}
          youLabel={member.user_id === viewer.userId ? t("you") : null}
          when={at ? t(removed ? "leftAt" : "joinedAt", { time: at.time }) : null}
          whenFull={at?.full ?? null}
          removed={removed}
          actions={node}
          note={note}
        />
      );
    };
    body = (
      <>
        <Card as="section" aria-labelledby="team-active">
          <h2 id="team-active" className="m-0 mb-4 text-lg font-semibold text-text">
            {t("activeTitle", { count: active.length })}
          </h2>
          <ul className="m-0 flex list-none flex-col gap-2 p-0" data-members-list="">
            {active.map(row)}
          </ul>
          {list.length >= MEMBERS_SHOWN ? (
            <p className="m-0 mt-3 text-sm text-text-muted">{t("first100")}</p>
          ) : null}
        </Card>
        {left ? (
          <Card as="section" aria-labelledby="team-left">
            <h2 id="team-left" className="m-0 mb-4 text-lg font-semibold text-text">
              {t("leftTitle")}
            </h2>
            {gone.length ? (
              <ul className="m-0 flex list-none flex-col gap-2 p-0" data-left-list="">
                {gone.map(row)}
              </ul>
            ) : (
              <p className="m-0 text-md text-text-muted">{t("noneLeft")}</p>
            )}
          </Card>
        ) : null}
      </>
    );
  }

  const target = pending?.member;
  const targetName = target ? target.full_name || target.email : "";
  const code = act.error instanceof ApiError ? act.error.code : null;
  const dialogTitle =
    pending?.kind === "remove"
      ? t("removeTitle", { name: targetName })
      : pending?.kind === "leave"
        ? t("leaveTitle", { workspace: workspaceName })
        : pending?.kind === "reinstate"
          ? t("reinstateTitle", { name: targetName })
          : pending?.kind === "role"
            ? t("roleTitle", { name: targetName })
            : t("ownerTitle", { name: targetName });
  const dialogBody =
    pending?.kind === "remove"
      ? t("removeBody")
      : pending?.kind === "leave"
        ? t("leaveBody")
        : pending?.kind === "reinstate"
          ? t("reinstateBody")
          : pending?.kind === "role"
            ? t("roleBody")
            : t("ownerBody");
  const confirmWord =
    pending?.kind === "remove"
      ? t("remove")
      : pending?.kind === "leave"
        ? t("leave")
        : pending?.kind === "reinstate"
          ? t("reinstate")
          : pending?.kind === "role"
            ? t("saveRole")
            : t("makeOwner");

  return (
    <div className="flex flex-col gap-6" data-screen="team">
      <ScreenHead id={HEADING} title={t("title")} line={t("line")}>
        <TeamTabs current="members" />
      </ScreenHead>
      {admin ? (
        <div className="flex items-center gap-3">
          <Switch
            id="team-show-left"
            checked={left}
            onCheckedChange={toggleLeft}
            data-toggle="left"
          />
          <label htmlFor="team-show-left" className="text-md text-text">
            {t("showLeft")}
          </label>
        </div>
      ) : null}
      {body}
      {admin ? null : (
        <p className="m-0 text-sm text-text-muted" data-member-note="">
          {t("memberNote")}{" "}
          <NextLink href="/settings/account" className={linkClasses("text-sm")}>
            {t("accountLink")}
          </NextLink>
        </p>
      )}
      <Dialog
        open={pending !== null}
        onOpenChange={(open) => {
          if (!open) {
            setPending(null);
            act.reset();
          }
        }}
        title={dialogTitle}
        description={dialogBody}
        closeLabel={t("close")}
        footer={
          <>
            <Button variant="secondary" onClick={() => setPending(null)}>
              {t("cancel")}
            </Button>
            <Button
              variant={
                pending?.kind === "reinstate" ||
                pending?.kind === "owner" ||
                pending?.kind === "role"
                  ? "primary"
                  : "danger"
              }
              {...busyProps(act.isPending)}
              onClick={() => pending && !act.isPending && act.mutate(pending)}
              data-action="confirm"
            >
              {confirmWord}
            </Button>
          </>
        }
      >
        {pending?.kind === "reinstate" ? (
          <p id="reinstate-role" className="m-0 mb-2 text-sm font-medium text-text">
            {t("reinstateRole")}
          </p>
        ) : null}
        {pending?.kind === "reinstate" ? (
          <RadioGroup
            aria-labelledby="reinstate-role"
            value={reinstateRole}
            onValueChange={(value) => setReinstateRole(value as TenantRole)}
            options={invitableRoles(role ?? null)
              .filter((option) => option !== "tenant_owner")
              .map((option) => ({ value: option, label: roleWord(option) }))}
          />
        ) : null}
        {pending?.kind === "role" ? (
          <>
            <p id="change-role-label" className="m-0 mb-2 text-sm font-medium text-text">
              {t("roleLabel")}
            </p>
            <RadioGroup
              name="change-role"
              aria-labelledby="change-role-label"
              value={newRole}
              onValueChange={(value) => setNewRole(value as "member" | "tenant_admin")}
              options={(["member", "tenant_admin"] as const).map((value) => ({
                value,
                label: (
                  <span className="flex flex-col">
                    <span>{roleWord(value)}</span>
                    <span className="text-sm text-text-muted">
                      {t(value === "member" ? "roleMemberLine" : "roleAdminLine")}
                    </span>
                  </span>
                ),
              }))}
            />
          </>
        ) : null}
        {act.isError ? (
          <p
            role="alert"
            className="m-0 mt-3 text-sm font-medium text-danger"
            data-error={code ?? ""}
          >
            {code === "plan_limit_exceeded" ? (
              <>
                {t("seatsUsed")}
                <PlanLimitLink />
              </>
            ) : (
              errors(code)
            )}
          </p>
        ) : null}
      </Dialog>
    </div>
  );
}
