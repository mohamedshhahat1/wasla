import type { Meta, StoryObj } from "@storybook/react-vite";
import { Bot, UserRound } from "lucide-react";
import { useState } from "react";

import {
  ConsentHistory,
  ContactRow,
  FieldConfirmedMark,
  FilterBar,
  IdentityList,
  InlineEditField,
  LeadRow,
  NoteComposer,
  NoteList,
  PreviewNotice,
  RecordHeader,
  RecordList,
  RecordPanel,
  ScoreField,
  StatusMenu,
  Timeline,
} from "../composites/customers";
import { SegmentedControl } from "../composites/inbox";
import { AuthLayout, BrandPanel, Card } from "../composites/layout";
import { Badge } from "../composites/status";
import { Button } from "../primitives/button";
import { Input } from "../primitives/input";
import { localeOf } from "./helpers";

/** FE-5's customers composites (UI5-02), every state, in en and ar, at 390 and 1280. */
const meta: Meta = { title: "Customers" };
export default meta;
type Story = StoryObj;

const text = (locale: string) =>
  locale === "ar"
    ? {
        contacts: "العملاء",
        leads: "العملاء المحتملون",
        nour: "نور حسن (عميلة مميزة)",
        ipek: "İpek Işık",
        unnamed: "عميل بلا اسم",
        optedOut: "أوقف الرسائل",
        statuses: {
          new: "جديد",
          contacted: "تم التواصل",
          qualified: "مؤهل",
          proposal: "عرض سعر",
          won: "تم الفوز",
          lost: "خسارة",
        },
        interest: "تشطيب مطبخ بمساحة 12 مترًا مربعًا",
        score: "التقييم",
        owner: "المسؤول",
        you: "أنت",
        unassigned: "غير مسند",
        budget: "الميزانية",
        loadMore: "تحميل المزيد",
        search: "ابحث بالاسم أو الرقم",
        filters: "عوامل التصفية",
        status: "الحالة",
        all: "الكل",
        editName: "تعديل الاسم",
        name: "الاسم",
        save: "حفظ",
        cancel: "إلغاء",
        nameError: "أدخل اسمًا أو أزله.",
        whatsappName: "الاسم على واتساب: نور",
        identities: "طرق التعريف",
        phone: "رقم الهاتف",
        userId: "معرّف مستخدم واتساب",
        copy: "نسخ",
        history: "سجل الموافقة",
        historyEmpty: "لم يُسجَّل شيء بعد.",
        optedByCustomer: "أوقف العميل الرسائل",
        clearedBy: "أعادت منى الرسائل",
        before: "أوقف الرسائل (قبل بدء حفظ السجل)",
        change: "تغيير الحالة",
        wonClosed: "تم الفوز — مغلق",
        scoreHint: "من 0 إلى 100",
        saveScore: "حفظ التقييم",
        confirmed: "أكّده شخص",
        notes: "الملاحظات",
        notesEmpty: "لا توجد ملاحظات بعد.",
        ai: "الذكاء الاصطناعي",
        mona: "منى عادل",
        note1:
          "طلب العميل عرض سعر ويفضّل الاتصال بعد الخامسة مساءً.\nالتفاصيل: https://example.test/quote",
        note2: "تم الاتفاق على معاينة يوم الخميس.",
        addNote: "إضافة ملاحظة",
        noteLabel: "أضف ملاحظة",
        noteHint: "الملاحظات لا تُرسل إلى العميل أبدًا.",
        activity: "النشاط",
        activityEmpty: "لا نشاط بعد.",
        act1: "غيّرت منى الحالة من «جديد» إلى «تم التواصل»",
        act2: "حدّث الذكاء الاصطناعي الاهتمام",
        act3: "أزال النظام الإسناد: غادر المسؤول مساحة العمل",
        previewTitle: "هذه معاينة لشاشات وصلة",
        previewBody: "لا يوجد خادم خلفها، لذلك لا يمكنك تسجيل الدخول هنا.",
        previewAction: "عرض صفحة تسجيل الدخول",
        promise: "موظفو ذكاء اصطناعي لواتساب",
        time: "منذ 5 د",
        full: "14 نوفمبر 2026، 10:37",
        whatsapp: "واتساب",
      }
    : {
        contacts: "Contacts",
        leads: "Leads",
        nour: "Nour Hassan (VIP)",
        ipek: "İpek Işık",
        unnamed: "Unnamed customer",
        optedOut: "Opted out",
        statuses: {
          new: "New",
          contacted: "Contacted",
          qualified: "Qualified",
          proposal: "Proposal",
          won: "Won",
          lost: "Lost",
        },
        interest: "Kitchen finishing, 12 square metres",
        score: "Score",
        owner: "Owner",
        you: "You",
        unassigned: "Unassigned",
        budget: "Budget",
        loadMore: "Load more",
        search: "Search by name or number",
        filters: "Filters",
        status: "Status",
        all: "All",
        editName: "Edit name",
        name: "Name",
        save: "Save",
        cancel: "Cancel",
        nameError: "Enter a name, or remove it.",
        whatsappName: "WhatsApp name: Nour",
        identities: "Identities",
        phone: "Phone number",
        userId: "WhatsApp user id",
        copy: "Copy",
        history: "Consent history",
        historyEmpty: "Nothing recorded yet.",
        optedByCustomer: "Opted out by the customer",
        clearedBy: "Opt-out cleared by Mona Adel",
        before: "Opted out (before history was kept)",
        change: "Change status",
        wonClosed: "Won — closed",
        scoreHint: "0 to 100",
        saveScore: "Save score",
        confirmed: "Confirmed by a person",
        notes: "Notes",
        notesEmpty: "No notes yet.",
        ai: "AI",
        mona: "Mona Adel",
        note1:
          "The customer asked for a quote and prefers a call after 5 pm.\nDetails: https://example.test/quote",
        note2: "Agreed on a site visit on Thursday.",
        addNote: "Add note",
        noteLabel: "Add a note",
        noteHint: "Notes are never sent to the customer.",
        activity: "Activity",
        activityEmpty: "Nothing has happened yet.",
        act1: "Mona Adel changed the status from New to Contacted",
        act2: "The AI updated the interest",
        act3: "The system cleared the owner: they left the workspace",
        previewTitle: "This is a preview of Wasla's screens",
        previewBody: "There is no server behind it, so you cannot sign in here.",
        previewAction: "See the sign-in page",
        promise: "AI employees for WhatsApp",
        time: "5 min",
        full: "14 November 2026, 10:37",
        whatsapp: "WhatsApp",
      };

const counter = (length: number, max: number) => `${length} / ${max}`;

export const ContactsList: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="flex flex-col gap-4">
        <FilterBar label={t.filters}>
          <label className="flex flex-col gap-1 text-sm font-medium text-text">
            {t.search}
            <Input type="search" defaultValue="" />
          </label>
        </FilterBar>
        <Card>
          <RecordList label={t.contacts} footer={<Button variant="secondary">{t.loadMore}</Button>}>
            <ContactRow
              href="#a"
              who={t.nour}
              handle="+201055501234"
              leadName={t.nour}
              leadStatus={<Badge icon={false}>{t.statuses.qualified}</Badge>}
              time={t.time}
              timeFull={t.full}
            />
            <ContactRow
              href="#b"
              who={t.ipek}
              handle="+905555550101"
              consent={<Badge tone="warning">{t.optedOut}</Badge>}
              time={t.time}
              timeFull={t.full}
              stateLabel={t.optedOut}
            />
            <ContactRow href="#c" who="+201055500002" time={t.time} timeFull={t.full} />
            <ContactRow href="#d" who={t.unnamed} />
          </RecordList>
        </Card>
      </div>
    );
  },
};

export const LeadsList: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    const rows = Object.entries(t.statuses) as Array<[keyof typeof t.statuses, string]>;
    return (
      <div className="flex flex-col gap-4">
        <FilterBar label={t.filters}>
          <SegmentedControl
            label={t.status}
            value="all"
            onChange={() => undefined}
            options={[
              { value: "all", label: t.all },
              ...rows.map(([value, label]) => ({ value, label })),
            ]}
          />
        </FilterBar>
        <Card>
          <RecordList label={t.leads}>
            {rows.map(([status, label], index) => (
              <LeadRow
                key={status}
                href={`#${status}`}
                who={index % 2 ? t.ipek : t.nour}
                status={
                  <Badge
                    tone={status === "won" ? "success" : status === "lost" ? "danger" : "neutral"}
                    icon={status === "won" || status === "lost"}
                  >
                    {label}
                  </Badge>
                }
                interest={t.interest}
                facts={[
                  { key: "score", label: t.score, value: <bdi>{40 + index * 10}</bdi> },
                  { key: "owner", label: t.owner, value: index % 2 ? t.unassigned : t.you },
                  { key: "budget", label: t.budget, value: <bdi>EGP 500,000.00</bdi> },
                ]}
                time={t.time}
                timeFull={t.full}
              />
            ))}
          </RecordList>
        </Card>
      </div>
    );
  },
};

function NameEditor({ t, failing }: { t: ReturnType<typeof text>; failing?: boolean }) {
  const [name, setName] = useState(t.nour);
  const [error, setError] = useState<string | undefined>(undefined);
  return (
    <InlineEditField
      label={t.name}
      value={name}
      editLabel={t.editName}
      saveLabel={t.save}
      cancelLabel={t.cancel}
      maxLength={200}
      counter={counter}
      error={error}
      onReset={() => setError(undefined)}
      onSave={(value) => {
        if (failing || !value) {
          setError(t.nameError);
          return Promise.reject(new Error("refused"));
        }
        setName(value);
        return Promise.resolve();
      }}
    />
  );
}

export const RecordHead: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <RecordHeader
        title={t.nour}
        beside={<NameEditor t={t} />}
        sub={<bdi>{t.whatsappName}</bdi>}
        facts={
          <IdentityList
            label={t.identities}
            onCopy={() => undefined}
            items={[
              {
                key: "p",
                channel: t.whatsapp,
                kind: t.phone,
                value: "+201055501234",
                copyLabel: t.copy,
              },
              {
                key: "b",
                channel: t.whatsapp,
                kind: t.userId,
                value: "EG.e2e1a2b3c4d5e6f",
                copyLabel: t.copy,
              },
            ]}
          />
        }
      />
    );
  },
};

export const InlineEditRefused: Story = {
  render: (_, ctx) => <NameEditor t={text(localeOf(ctx))} failing />,
};

export const Consent: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="flex flex-col gap-6">
        <ConsentHistory
          label={t.history}
          empty={t.historyEmpty}
          footer={<Button variant="secondary">{t.loadMore}</Button>}
          entries={[
            { key: "1", what: t.clearedBy, channel: t.whatsapp, time: t.time, timeFull: t.full },
            {
              key: "2",
              what: t.optedByCustomer,
              channel: t.whatsapp,
              time: t.time,
              timeFull: t.full,
            },
            { key: "3", what: t.before, channel: t.whatsapp, time: t.time, timeFull: t.full },
          ]}
        />
        <ConsentHistory label={t.history} empty={t.historyEmpty} entries={[]} />
      </div>
    );
  },
};

export const StatusAndScore: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="flex flex-col gap-6">
        <StatusMenu
          current={<Badge icon={false}>{t.statuses.new}</Badge>}
          changeLabel={t.change}
          moves={[
            { value: "contacted", label: t.statuses.contacted },
            { value: "qualified", label: t.statuses.qualified },
            { value: "lost", label: t.statuses.lost },
          ]}
          onMove={() => undefined}
        />
        <StatusMenu
          current={<Badge icon={false}>{t.statuses.proposal}</Badge>}
          changeLabel={t.change}
          moves={[{ value: "won", label: t.statuses.won }]}
          busy
          onMove={() => undefined}
        />
        <StatusMenu
          current={<Badge tone="success">{t.statuses.won}</Badge>}
          changeLabel={t.change}
          closedLabel={t.wonClosed}
          moves={[]}
          onMove={() => undefined}
        />
        <StatusMenu
          current={<Badge tone="danger">{t.statuses.lost}</Badge>}
          changeLabel={t.change}
          moves={[{ value: "new", label: t.statuses.new }]}
          onMove={() => undefined}
        />
        <RecordPanel>
          <ScoreField
            label={t.score}
            hint={t.scoreHint}
            value={40}
            min={0}
            max={100}
            saveLabel={t.saveScore}
            onSave={() => undefined}
          />
        </RecordPanel>
        <FieldConfirmedMark label={t.confirmed} />
      </div>
    );
  },
};

function Notes({ t }: { t: ReturnType<typeof text> }) {
  const [draft, setDraft] = useState("");
  const [first, link] = t.note1.split("https://");
  return (
    <div className="flex flex-col gap-6">
      <NoteComposer
        label={t.noteLabel}
        hint={t.noteHint}
        value={draft}
        maxLength={5000}
        counter={counter}
        submitLabel={t.addNote}
        onChange={setDraft}
        onSubmit={() => setDraft("")}
      />
      <NoteList
        label={t.notes}
        empty={t.notesEmpty}
        onLink={() => undefined}
        footer={<Button variant="secondary">{t.loadMore}</Button>}
        notes={[
          {
            key: "1",
            author: t.ai,
            time: t.time,
            timeFull: t.full,
            parts: [
              { kind: "text", text: first ?? "" },
              { kind: "link", text: `https://${link}`, url: `https://${link}` },
            ],
          },
          {
            key: "2",
            author: t.mona,
            time: t.time,
            timeFull: t.full,
            parts: [{ kind: "text", text: t.note2 }],
          },
        ]}
      />
      <NoteList label={t.notes} empty={t.notesEmpty} notes={[]} onLink={() => undefined} />
    </div>
  );
}

export const NotesAndComposer: Story = {
  render: (_, ctx) => <Notes t={text(localeOf(ctx))} />,
};

export const ActivityTimeline: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="flex flex-col gap-6">
        <Timeline
          label={t.activity}
          empty={t.activityEmpty}
          footer={<Button variant="secondary">{t.loadMore}</Button>}
          entries={[
            {
              key: "1",
              icon: UserRound,
              iconName: "UserRound",
              what: t.act1,
              time: t.time,
              timeFull: t.full,
            },
            { key: "2", icon: Bot, iconName: "Bot", what: t.act2, time: t.time, timeFull: t.full },
            { key: "3", what: t.act3, time: t.time, timeFull: t.full },
          ]}
        />
        <Timeline label={t.activity} empty={t.activityEmpty} entries={[]} />
      </div>
    );
  },
};

export const Preview: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <AuthLayout brand={<BrandPanel promise={t.promise} />}>
        <PreviewNotice
          title={t.previewTitle}
          body={<p className="m-0">{t.previewBody}</p>}
          action={<Button variant="primary">{t.previewAction}</Button>}
        />
      </AuthLayout>
    );
  },
};
