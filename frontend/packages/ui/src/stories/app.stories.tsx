import type { Meta, StoryObj } from "@storybook/react-vite";
import { House, RadioTower } from "lucide-react";

import { AppShell, NavList } from "../composites/app";
import { Card, PageHeader, Wordmark } from "../composites/layout";
import {
  Badge,
  Banner,
  BannerStack,
  Checklist,
  KeyValueList,
  LiveIndicator,
  Meter,
  StatCard,
  StatusState,
} from "../composites/status";
import { SwitcherMenu } from "../composites/switcher";
import { Button } from "../primitives/button";
import { localeOf } from "./helpers";

/** FE-3's composites (UI3-02), in en and ar, at 390 and 1280 (the toolbar's widths). */
const meta: Meta = { title: "Composites/App" };
export default meta;
type Story = StoryObj;

const text = (locale: string) =>
  locale === "ar"
    ? {
        app: "وصلة",
        skip: "انتقل إلى المحتوى",
        nav: "التنقل الرئيسي",
        menu: "القائمة",
        close: "إغلاق",
        home: "الرئيسية",
        channels: "القنوات",
        workspace: "مساحة العمل",
        acme: "شركة النيل للتشطيبات",
        other: "مكتب الدلتا العقاري",
        owner: "مالك",
        admin: "مسؤول",
        suspended: "موقوفة",
        create: "إنشاء مساحة عمل",
        title: "الرئيسية",
        line: "ما يحدث في مساحة العمل اليوم.",
        payment: "يلزم الدفع لاستمرار عمل مساحة العمل هذه.",
        capacity: "تم استخدام كل خانات القنوات.",
        trial: "تنتهي الفترة التجريبية بعد ٣ أيام.",
        dismiss: "إخفاء",
        region: "تنبيهات مساحة العمل",
        more: (n: number) => `${n} تنبيهات أخرى`,
        fewer: "تنبيهات أقل",
        live: "مباشر",
        paused: "التحديثات المباشرة متوقفة - جارٍ إعادة المحاولة",
        open: "المحادثات المفتوحة",
        slots: "خانات القنوات",
        inUse: "٢ من ٣ مستخدمة",
        over: "تجاوز الحد",
        done: "تم",
        undone: "لم يتم",
        unknown: "تعذّر التحقق",
        connect: "اربط قناة",
        agent: "جهّز المساعد الذكي",
        later: "متاح في إصدار لاحق.",
        number: "الرقم",
        status: "الحالة",
        active: "نشطة",
        suspendedTitle: "مساحة العمل هذه موقوفة",
        suspendedBody: "لا يمكن للفريق استخدامها حتى تُستعاد.",
        check: "تحقّق مجددًا",
      }
    : {
        app: "Wasla",
        skip: "Skip to content",
        nav: "Main",
        menu: "Menu",
        close: "Close",
        home: "Home",
        channels: "Channels",
        workspace: "Workspace",
        acme: "Nile Finishing Co.",
        other: "Delta Real Estate Office",
        owner: "Owner",
        admin: "Admin",
        suspended: "Suspended",
        create: "Create workspace",
        title: "Home",
        line: "What is happening in this workspace today.",
        payment: "Payment is required to keep this workspace running.",
        capacity: "All channel slots are in use.",
        trial: "The trial ends in 3 days.",
        dismiss: "Dismiss",
        region: "Workspace notices",
        more: (n: number) => `${n} more notices`,
        fewer: "Fewer notices",
        live: "Live",
        paused: "Live updates paused - retrying",
        open: "Open conversations",
        slots: "Channel slots",
        inUse: "2 of 3 in use",
        over: "Over the limit",
        done: "Done",
        undone: "To do",
        unknown: "Could not check",
        connect: "Connect a channel",
        agent: "Set up the AI agent",
        later: "Available in a later version.",
        number: "Number",
        status: "Status",
        active: "Active",
        suspendedTitle: "This workspace is suspended",
        suspendedBody: "The team cannot use it until it is restored.",
        check: "Check again",
      };

export const Shell: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <AppShell
        skipLabel={t.skip}
        brand={<Wordmark name={t.app} tone="inverse" />}
        barEnd={
          <>
            <SwitcherMenu
              label={t.workspace}
              current={{ value: "acme", name: t.acme }}
              workspaces={[{ value: "acme", name: t.acme }]}
              onSelect={() => undefined}
            />
            <LiveIndicator state="live" label={t.live} />
          </>
        }
        navLabel={t.nav}
        navigation={
          <NavList
            items={[
              {
                key: "home",
                href: "#home",
                label: t.home,
                icon: House,
                iconName: "House",
                current: true,
              },
              {
                key: "channels",
                href: "#channels",
                label: t.channels,
                icon: RadioTower,
                iconName: "RadioTower",
              },
            ]}
          />
        }
        menuLabel={t.menu}
        closeLabel={t.close}
        banners={
          <BannerStack
            label={t.region}
            banners={[{ key: "trial", node: <Banner tone="info" title={t.trial} /> }]}
            moreLabel={t.more}
            fewerLabel={t.fewer}
          />
        }
      >
        <PageHeader title={t.title} description={t.line} />
        <Card>
          <p className="m-0 text-md text-text">{t.line}</p>
        </Card>
      </AppShell>
    );
  },
};

export const Banners: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="flex flex-col gap-5 bg-canvas">
        <Banner tone="danger" title={t.payment} />
        <Banner
          tone="warning"
          title={t.capacity}
          action={<Button variant="secondary">{t.channels}</Button>}
        />
        <Banner tone="info" title={t.trial} onDismiss={() => undefined} dismissLabel={t.dismiss} />
      </div>
    );
  },
};

export const BannerStackWithMore: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <BannerStack
        label={t.region}
        banners={[
          { key: "payment", node: <Banner tone="danger" title={t.payment} /> },
          { key: "capacity", node: <Banner tone="warning" title={t.capacity} /> },
          { key: "trial", node: <Banner tone="info" title={t.trial} /> },
        ]}
        moreLabel={t.more}
        fewerLabel={t.fewer}
      />
    );
  },
};

export const BadgesAndLive: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="flex flex-col gap-5">
        <div className="flex flex-wrap gap-3">
          <Badge tone="success">{t.active}</Badge>
          <Badge tone="warning">{t.suspended}</Badge>
          <Badge tone="danger">{t.over}</Badge>
          <Badge tone="info">{t.trial}</Badge>
          <Badge>{t.owner}</Badge>
        </div>
        <div className="flex flex-wrap gap-3 bg-brand p-4" data-brand-surface="">
          <LiveIndicator state="live" label={t.live} />
          <LiveIndicator state="paused" label={t.paused} />
        </div>
      </div>
    );
  },
};

export const StatsAndMeters: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="flex flex-col gap-6 bg-canvas p-5">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <StatCard label={t.open} value="20+" />
          <StatCard label={t.channels} value="2" sub={t.inUse} />
        </div>
        <Card>
          <div className="flex flex-col gap-6">
            <Meter label={t.slots} value={2} max={3} valueText={t.inUse} />
            <Meter label={t.slots} value={4} max={3} valueText={t.over} over overLabel={t.over} />
          </div>
        </Card>
      </div>
    );
  },
};

export const ChecklistStates: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <Checklist
        stateLabels={{ done: t.done, undone: t.undone, unknown: t.unknown }}
        items={[
          { key: "channel", label: t.connect, state: "done" },
          { key: "agent", label: t.agent, state: "undone", detail: t.later },
          { key: "plan", label: t.slots, state: "unknown" },
          {
            key: "connect",
            label: t.connect,
            state: "undone",
            action: <Button>{t.connect}</Button>,
          },
        ]}
      />
    );
  },
};

export const KeyValues: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <Card>
        <KeyValueList
          items={[
            { key: "number", label: t.number, value: <bdi>+20 100 000 1024</bdi> },
            { key: "status", label: t.status, value: <Badge tone="success">{t.active}</Badge> },
          ]}
        />
      </Card>
    );
  },
};

export const SwitcherOpen: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="bg-brand p-4" data-brand-surface="">
        <SwitcherMenu
          defaultOpen
          label={t.workspace}
          current={{ value: "acme", name: t.acme, detail: t.owner }}
          workspaces={[
            { value: "acme", name: t.acme, detail: t.owner },
            {
              value: "delta",
              name: t.other,
              detail: t.admin,
              badge: <Badge tone="warning">{t.suspended}</Badge>,
            },
          ]}
          actions={[{ key: "create", label: t.create, onSelect: () => undefined }]}
          onSelect={() => undefined}
        />
      </div>
    );
  },
};

export const FullStates: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="bg-canvas p-5">
        <StatusState
          tone="warning"
          title={t.suspendedTitle}
          description={t.suspendedBody}
          action={<Button variant="secondary">{t.check}</Button>}
        />
      </div>
    );
  },
};
