import type { Meta, StoryObj } from "@storybook/react-vite";

import { Card } from "../composites/layout";
import {
  ActivityRow,
  DangerZone,
  InvitationRow,
  MemberRow,
  RoleBadge,
  SettingsNav,
} from "../composites/team";
import { Button } from "../primitives/button";
import { localeOf } from "./helpers";

/** FE-7's team, settings and account composites (UI7-02, ADR-201), in en and ar, at 390 and 1280. */
const meta: Meta = { title: "Team and settings" };
export default meta;
type Story = StoryObj;

const text = (locale: string) =>
  locale === "ar"
    ? {
        owner: "المالك",
        admin: "مسؤول",
        member: "عضو",
        you: "أنت",
        joined: "انضم منذ 3 أيام",
        left: "غادر منذ يومين",
        full: "17 نوفمبر 2026، 10:00",
        remove: "إزالة",
        makeOwner: "اجعله المالك",
        reinstate: "إعادة",
        lastOwner: "أنت المالك الوحيد. اجعل شخصًا آخر مالكًا قبل أن تغادر.",
        expires: "تنتهي بعد 3 أيام",
        expired: "منتهية",
        emailIn: "البريد بـ",
        revoke: "إلغاء الدعوة",
        nav: ["مساحة العمل", "النشاط", "إغلاق مساحة العمل", "الحساب"],
        navLabel: "صفحات الإعدادات",
        invited: "دعا شخصًا",
        removed: "أزال شخصًا",
        roleChange: "الدور: عضو ← مسؤول",
        close: "إغلاق مساحة العمل",
        closeLine: "يفقد الجميع الوصول فورًا، وأنت منهم.",
        closeButton: "إغلاق مساحة العمل هذه",
        names: ["منى عبد الرحمن", "Karim Nassar (مثال)", "سارة"],
      }
    : {
        owner: "Owner",
        admin: "Administrator",
        member: "Member",
        you: "You",
        joined: "Joined 3 days ago",
        left: "Left 2 days ago",
        full: "17 November 2026, 10:00",
        remove: "Remove",
        makeOwner: "Make owner",
        reinstate: "Reinstate",
        lastOwner: "You are the only owner. Make someone else the owner before you leave.",
        expires: "Expires in 3 days",
        expired: "Expired",
        emailIn: "E-mail in",
        revoke: "Revoke",
        nav: ["Workspace", "Activity", "Close workspace", "Account"],
        navLabel: "Settings pages",
        invited: "invited someone",
        removed: "removed someone",
        roleChange: "Role: Member → Administrator",
        close: "Close workspace",
        closeLine: "Everybody loses access at once, you included.",
        closeButton: "Close this workspace",
        names: [
          "Mona Abdelrahman",
          "Karim Nassar with a very long family name for truncation",
          "Sara",
        ],
      };

export const Members: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <Card as="section">
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          <MemberRow
            name={t.names[0]!}
            email="owner@example.test"
            role={<RoleBadge tone="owner">{t.owner}</RoleBadge>}
            youLabel={t.you}
            when={t.joined}
            whenFull={t.full}
            note={t.lastOwner}
          />
          <MemberRow
            name={t.names[1]!}
            email="karim.nassar.long.address@example.test"
            role={<RoleBadge tone="admin">{t.admin}</RoleBadge>}
            when={t.joined}
            whenFull={t.full}
            actions={
              <>
                <Button variant="secondary">{t.makeOwner}</Button>
                <Button variant="ghost">{t.remove}</Button>
              </>
            }
          />
          <MemberRow
            name={t.names[2]!}
            email="sara@example.test"
            role={<RoleBadge tone="member">{t.member}</RoleBadge>}
            when={t.joined}
            whenFull={t.full}
          />
        </ul>
      </Card>
    );
  },
};

export const PeopleWhoLeft: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <ul className="m-0 flex list-none flex-col gap-2 p-0">
        <MemberRow
          name={t.names[2]!}
          email="sara@example.test"
          role={<RoleBadge tone="member">{t.member}</RoleBadge>}
          when={t.left}
          whenFull={t.full}
          removed
          actions={<Button variant="secondary">{t.reinstate}</Button>}
        />
      </ul>
    );
  },
};

export const Invitations: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <ul className="m-0 flex list-none flex-col gap-2 p-0">
        <InvitationRow
          email="new.colleague@example.test"
          role={<RoleBadge tone="member">{t.member}</RoleBadge>}
          state={t.expires}
          expired={false}
          stateFull={t.full}
          language="العربية"
          languageLabel={t.emailIn}
          actions={<Button variant="ghost">{t.revoke}</Button>}
        />
        <InvitationRow
          email="late@example.test"
          role={<RoleBadge tone="admin">{t.admin}</RoleBadge>}
          state={t.expired}
          expired
          stateFull={t.full}
        />
      </ul>
    );
  },
};

export const SettingsNavigation: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    const keys = ["workspace", "activity", "close", "account"];
    return (
      <SettingsNav
        label={t.navLabel}
        current="activity"
        items={keys.map((key, index) => ({ key, href: `#${key}`, label: t.nav[index]! }))}
        renderLink={({ children, ...props }) => <a {...props}>{children}</a>}
      />
    );
  },
};

export const Activity: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <Card as="section">
        <ul className="m-0 list-none p-0">
          <ActivityRow
            actor={t.names[0]!}
            action={t.invited}
            target="new.colleague@example.test"
            when={t.joined}
            whenFull={t.full}
          />
          <ActivityRow
            actor={t.names[0]!}
            action={t.removed}
            target={t.names[2] ?? null}
            when={t.left}
            whenFull={t.full}
            change={t.roleChange}
          />
          <ActivityRow
            actor="Wasla"
            action="usage_threshold_crossed"
            actionIsCode
            when={t.left}
            whenFull={t.full}
          />
        </ul>
      </Card>
    );
  },
};

export const Danger: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <DangerZone title={t.close} action={<Button variant="danger">{t.closeButton}</Button>}>
        <p className="m-0">{t.closeLine}</p>
      </DangerZone>
    );
  },
};
