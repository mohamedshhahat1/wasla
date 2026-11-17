import type { Meta, StoryObj } from "@storybook/react-vite";
import { useState } from "react";

import { RecordHeader, RecordList, RecordPanel } from "../composites/customers";
import {
  AgentRow,
  AnswersMark,
  DocumentRow,
  DocumentStatusBadge,
  FileField,
  GapList,
  KnowledgeScopePicker,
  PassageList,
  SettingSwitchRow,
  TextCounter,
  ToolSwitchRow,
} from "../composites/knowledge";
import { Card } from "../composites/layout";
import { Badge, Banner } from "../composites/status";
import { Button } from "../primitives/button";
import { Switch } from "../primitives/switch";
import { localeOf } from "./helpers";

/** FE-6's AI agent and knowledge composites (UI6-02), every state, in en and ar, at 390 and 1280. */
const meta: Meta = { title: "AI and knowledge" };
export default meta;
type Story = StoryObj;

const text = (locale: string) =>
  locale === "ar"
    ? {
        agents: "وكلاء الذكاء الاصطناعي",
        sales: "وكيل المبيعات",
        support: "الدعم (مسودة طويلة الاسم جدًا لاختبار القص)",
        model: "النموذج",
        answers: "يرد على العملاء",
        active: "مفعّل",
        draft: "مسودة",
        disabled: "معطّل",
        defaultIdle: "افتراضي، لا يرد",
        time: "منذ 5 دقائق",
        full: "17 نوفمبر 2026",
        tools: "الأدوات",
        search: "البحث في معرفتك",
        searchLine: "يرد من المستندات الموجودة في قواعد معرفتك.",
        handoff: "التحويل إلى شخص",
        handoffLine: "ينقل المحادثة إلى فريقك.",
        on: "مفعّل",
        off: "متوقف",
        unknownLine: "أداة من إصدار أحدث من وصلة.",
        remove: "إزالة",
        scope: "أي معرفة يبحث فيها",
        all: "كل قواعد المعرفة",
        chosen: "هذه فقط",
        none: "اختر قاعدة معرفة واحدة على الأقل.",
        bases: ["الأسعار", "سياسات الاسترجاع"],
        docs: "المستندات",
        prices: "قائمة الأسعار 2026",
        statuses: {
          pending: "في الانتظار",
          processing: "قيد الفهرسة",
          ready: "جاهز",
          failed: "فشل",
        },
        pdf: "PDF",
        reindex: "يحتاج إلى إعادة فهرسة",
        file: "الملف",
        choose: "اختيار ملف",
        hint: "ملفات PDF حتى 293 ك.ب؛ والنصوص حتى 1.6 م.ب.",
        refused: "حجم هذا الملف 412 ك.ب؛ يمكن أن يصل حجم ملفات PDF إلى 293 ك.ب.",
        passages: "المقاطع",
        passage: "سعر تشطيب 150 م2 هو 500000 جنيه.\nيشمل الدهانات.",
        strong: "مطابقة قوية",
        gaps: "أسئلة بلا إجابة",
        gapQuery: "مواعيد العمل يوم الجمعة؟",
        asked: "مرات السؤال",
        last: "آخر سؤال",
        hybrid: "مطابقة الكلمات نفسها أيضًا",
        hybridLine: "يبحث بالمعنى وبالكلمات نفسها.",
        defaultSource: "يستخدم الافتراضي",
        workspaceSource: "مضبوط لمساحة العمل هذه",
        useDefault: "استخدام الافتراضي",
        warning: "لا يوجد وكيل ذكاء اصطناعي يرد على العملاء.",
      }
    : {
        agents: "AI agents",
        sales: "Sales agent",
        support: "Support (a draft with a very long name to test truncation)",
        model: "Model",
        answers: "Answers customers",
        active: "Active",
        draft: "Draft",
        disabled: "Disabled",
        defaultIdle: "Default, not answering",
        time: "5 min ago",
        full: "17 November 2026",
        tools: "Tools",
        search: "Search your knowledge",
        searchLine: "Answers from the documents in your knowledge bases.",
        handoff: "Hand over to a person",
        handoffLine: "Moves the conversation to your team.",
        on: "On",
        off: "Off",
        unknownLine: "A tool from a newer version of Wasla.",
        remove: "Remove",
        scope: "Which knowledge it searches",
        all: "Every knowledge base",
        chosen: "Only these",
        none: "Choose at least one knowledge base.",
        bases: ["Prices", "Refund policies"],
        docs: "Documents",
        prices: "Price list 2026",
        statuses: { pending: "Waiting", processing: "Indexing", ready: "Ready", failed: "Failed" },
        pdf: "PDF",
        reindex: "Needs re-indexing",
        file: "File",
        choose: "Choose a file",
        hint: "PDF files up to 300 kB; text up to 1.6 MB.",
        refused: "This file is 412 kB; PDF files can be up to 300 kB.",
        passages: "Passages",
        passage: "The price of a 150 m2 finishing is 500000 EGP.\nPaint is included.",
        strong: "Strong match",
        gaps: "Unanswered questions",
        gapQuery: "Opening hours on Friday?",
        asked: "Asked",
        last: "Last asked",
        hybrid: "Match exact words too",
        hybridLine: "Searches by meaning and by the words themselves.",
        defaultSource: "Uses the default",
        workspaceSource: "Set for this workspace",
        useDefault: "Use the default",
        warning: "No AI agent is answering customers.",
      };

export const AgentsList: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="flex flex-col gap-4">
        <Banner tone="warning" title={t.warning} />
        <Card>
          <RecordList label={t.agents}>
            <AgentRow
              href="#a"
              name={t.sales}
              status={<Badge tone="success">{t.active}</Badge>}
              answers={<AnswersMark label={t.answers} />}
              model="gpt-4.1-mini"
              modelLabel={t.model}
              time={t.time}
              timeFull={t.full}
            />
            <AgentRow
              href="#b"
              name={t.support}
              status={<Badge icon={false}>{t.draft}</Badge>}
              model="gpt-4.1-mini"
              modelLabel={t.model}
            />
            <AgentRow
              href="#c"
              name="Old default"
              status={<Badge icon={false}>{t.disabled}</Badge>}
              answers={<Badge tone="warning">{t.defaultIdle}</Badge>}
              model="gpt-4.1"
              modelLabel={t.model}
            />
          </RecordList>
        </Card>
      </div>
    );
  },
};

/** FEF-39: the neutral badge keeps its pill on the secondary band it shares its fill with. */
export const BadgeOnSecondaryBand: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <RecordHeader
        title={t.sales}
        beside={
          <>
            <Badge icon={false}>{t.draft}</Badge>
            <Badge>{t.disabled}</Badge>
            <Badge tone="success">{t.active}</Badge>
            <Badge tone="warning">{t.defaultIdle}</Badge>
            <Badge tone="danger">{t.statuses.failed}</Badge>
          </>
        }
      />
    );
  },
};

function ToolsDemo({ locale, readOnly }: { locale: string; readOnly: boolean }) {
  const t = text(locale);
  const [on, setOn] = useState(true);
  const [choice, setChoice] = useState<"all" | "chosen">("chosen");
  const [chosen, setChosen] = useState<string[]>([]);
  return (
    <RecordPanel>
      <ul aria-label={t.tools} className="m-0 flex list-none flex-col gap-2 p-0">
        <ToolSwitchRow
          label={t.search}
          description={t.searchLine}
          on={on}
          readOnly={readOnly}
          stateLabel={on ? t.on : t.off}
          onChange={setOn}
        >
          {on ? (
            <KnowledgeScopePicker
              legend={t.scope}
              allLabel={t.all}
              chosenLabel={t.chosen}
              choice={choice}
              bases={t.bases.map((name, index) => ({ id: String(index), name }))}
              chosen={chosen}
              problem={choice === "chosen" && !chosen.length ? t.none : undefined}
              onChoiceChange={setChoice}
              onChosenChange={setChosen}
            />
          ) : null}
        </ToolSwitchRow>
        <ToolSwitchRow
          label={t.handoff}
          description={t.handoffLine}
          on={false}
          readOnly={readOnly}
          stateLabel={t.off}
          busy
        />
        <ToolSwitchRow
          label="lookup_order"
          apiName="lookup_order"
          description={t.unknownLine}
          on
          readOnly
          stateLabel={t.on}
          action={readOnly ? undefined : <Button variant="secondary">{t.remove}</Button>}
        />
      </ul>
    </RecordPanel>
  );
}

export const Tools: Story = {
  render: (_, ctx) => <ToolsDemo locale={localeOf(ctx)} readOnly={false} />,
};
export const ToolsReadOnly: Story = {
  render: (_, ctx) => <ToolsDemo locale={localeOf(ctx)} readOnly />,
};

export const Documents: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    const rows = (["pending", "processing", "ready", "failed"] as const).map((status) => (
      <DocumentRow
        key={status}
        title={`${t.prices} (${status})`}
        status={<DocumentStatusBadge status={status} label={t.statuses[status]} />}
        marks={status === "ready" ? [t.reindex] : []}
        type={t.pdf}
        size="212 kB"
        time={t.time}
        timeFull={t.full}
        selected={status === "failed"}
        onOpen={() => undefined}
      />
    ));
    return (
      <Card>
        <RecordList label={t.docs}>{rows}</RecordList>
      </Card>
    );
  },
};

export const UploadField: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <Card>
        <div className="flex flex-col gap-6">
          <FileField
            label={t.file}
            chooseLabel={t.choose}
            accept=".pdf,.txt"
            hint={t.hint}
            onFile={() => undefined}
          />
          <FileField
            label={t.file}
            chooseLabel={t.choose}
            accept=".pdf,.txt"
            chosen="prices.pdf, 412 kB"
            hint={t.hint}
            problem={t.refused}
            onFile={() => undefined}
          />
          <TextCounter length={120} max={20000} text="120 / 20,000" />
          <TextCounter length={19500} max={20000} text="19,500 / 20,000" />
          <TextCounter length={20010} max={20000} text="20,010 / 20,000" />
        </div>
      </Card>
    );
  },
};

export const SearchAndGaps: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="flex flex-col gap-6">
        <Card>
          <PassageList
            label={t.passages}
            passages={[
              {
                key: "1",
                title: t.prices,
                text: t.passage,
                match: t.strong,
                onOpen: () => undefined,
              },
              { key: "2", title: "<b>not bold</b>", text: "<img src=x>", match: "" },
            ]}
          />
        </Card>
        <Card>
          <GapList
            label={t.gaps}
            searchesLabel={t.asked}
            lastAskedLabel={t.last}
            gaps={[
              {
                key: "1",
                query: t.gapQuery,
                searches: "12",
                lastAsked: t.time,
                lastAskedFull: t.full,
              },
              {
                key: "2",
                query: "İade süresi ne kadar?",
                searches: "3",
                lastAsked: t.time,
                lastAskedFull: t.full,
              },
            ]}
          />
        </Card>
      </div>
    );
  },
};

export const Settings: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <Card>
        <ul className="m-0 flex list-none flex-col gap-2 p-0" aria-label={t.hybrid}>
          <SettingSwitchRow
            label={t.hybrid}
            description={t.hybridLine}
            sourceLabel={t.defaultSource}
            overridden={false}
            useDefaultLabel={t.useDefault}
            onUseDefault={() => undefined}
            control={({ labelId, descriptionId }) => (
              <Switch defaultChecked aria-labelledby={labelId} aria-describedby={descriptionId} />
            )}
          />
          <SettingSwitchRow
            label={t.hybrid}
            description={t.hybridLine}
            sourceLabel={t.workspaceSource}
            overridden
            useDefaultLabel={t.useDefault}
            onUseDefault={() => undefined}
            control={({ labelId, descriptionId }) => (
              <Switch aria-labelledby={labelId} aria-describedby={descriptionId} />
            )}
          />
        </ul>
      </Card>
    );
  },
};
