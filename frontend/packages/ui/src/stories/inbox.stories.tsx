import type { Meta, StoryObj } from "@storybook/react-vite";
import { MessageCircle, Paperclip, SendHorizontal } from "lucide-react";

import {
  AttachmentChip,
  Composer,
  ConversationList,
  ConversationRow,
  InertLink,
  MediaTile,
  MessageBubble,
  NewMessagesPill,
  SegmentedControl,
  SidePanel,
  ThreadNote,
  TranscriptBlock,
} from "../composites/inbox";
import { Badge } from "../composites/status";
import { Button } from "../primitives/button";
import { IconButton } from "../primitives/icon-button";
import { Textarea } from "../primitives/input";
import { localeOf } from "./helpers";

/** FE-4's inbox composites (UI4-02), every state, in en and ar, at 390 and 1280. */
const meta: Meta = { title: "Inbox" };
export default meta;
type Story = StoryObj;

const text = (locale: string) =>
  locale === "ar"
    ? {
        list: "المحادثات",
        nour: "نور حسن",
        omar: "عمر سعيد",
        excerpt: "مرحبًا، أريد أن أسأل عن سعر تشطيب المطبخ",
        reply: "رد:",
        open: "مفتوحة",
        ai: "الذكاء الاصطناعي",
        human: "شخص",
        high: "عالية",
        you: "أنت",
        unread: "3 غير مقروءة",
        loadMore: "تحميل المزيد",
        updated: "تم التحديث — اعرض الأحدث",
        inbound: "مرحبًا، هل الموعد ما زال متاحًا يوم الخميس؟",
        outbound: "نعم، الموعد متاح. هل يناسبك العاشرة صباحًا؟",
        ai_: "الذكاء الاصطناعي",
        sent: "أُرسلت",
        failed: "لم تُرسل",
        reason: "طلب هذا العميل عدم تلقي الرسائل.",
        retry: "إعادة المحاولة",
        discard: "تجاهل",
        day: "14 نوفمبر 2026",
        seen: "شاهدها أنت · 10:42",
        pending: "جارٍ تجهيز الملف…",
        unavailable: "لم يعد هذا الملف متاحًا",
        failedFile: "تعذّر استلام هذا الملف",
        document: "عرض-السعر.pdf",
        download: "تنزيل",
        transcript: "النص (تلقائي)",
        transcriptText: "أريد أن أعرف سعر المتر وموعد البدء.",
        show: "عرض النص",
        hide: "إخفاء النص",
        message: "الرسالة",
        attach: "إرفاق ملف",
        template: "نموذج",
        send: "إرسال",
        assigns: "سيُسند الرد هذه المحادثة إليك.",
        closed: "مرّ أكثر من 24 ساعة منذ آخر رسالة من هذا العميل. يمكنك إرسال نموذج معتمد.",
        choose: "اختر نموذجًا",
        newMessages: "رسالتان جديدتان",
        panel: "تفاصيل العميل",
        close: "إغلاق",
        status: "الحالة",
        statuses: ["مفتوحة", "معلّقة", "مغلقة", "الكل"],
        link: "https://example.test/catalogue",
      }
    : {
        list: "Conversations",
        nour: "Nour Hassan",
        omar: "Omar Said",
        excerpt: "Hello, I would like to ask about the price of the kitchen finishing",
        reply: "Reply:",
        open: "Open",
        ai: "AI",
        human: "Person",
        high: "High",
        you: "You",
        unread: "3 unread",
        loadMore: "Load more",
        updated: "Updated — show latest",
        inbound: "Hi, is Thursday still free for the visit?",
        outbound: "Yes, Thursday is free. Does 10 am suit you?",
        ai_: "AI",
        sent: "Sent",
        failed: "Not sent",
        reason: "This customer has asked not to receive messages.",
        retry: "Retry",
        discard: "Discard",
        day: "14 Nov 2026",
        seen: "Seen by you · 10:42",
        pending: "Preparing media…",
        unavailable: "This file is no longer available",
        failedFile: "This file could not be received",
        document: "quote-kitchen.pdf",
        download: "Download",
        transcript: "Transcript (automatic)",
        transcriptText: "I would like to know the price per metre and when you can start.",
        show: "Show transcript",
        hide: "Hide transcript",
        message: "Message",
        attach: "Attach a file",
        template: "Template",
        send: "Send",
        assigns: "Replying will assign this conversation to you.",
        closed:
          "It has been more than 24 hours since this customer wrote. You can send an approved template.",
        choose: "Choose template",
        newMessages: "2 new messages",
        panel: "Customer details",
        close: "Close",
        status: "Status",
        statuses: ["Open", "Pending", "Closed", "All"],
        link: "https://example.test/catalogue",
      };

export const List: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="flex max-w-inbox-list flex-col gap-3 bg-secondary-subtle p-4">
        <SegmentedControl
          label={t.status}
          value="open"
          onChange={() => undefined}
          options={t.statuses.map((label, index) => ({
            value: ["open", "pending", "closed", "all"][index]!,
            label,
          }))}
        />
        <ConversationList
          label={t.list}
          loadMoreLabel={t.loadMore}
          onLoadMore={() => undefined}
          updateLabel={t.updated}
          onShowUpdate={() => undefined}
        >
          <ConversationRow
            href="#a"
            who={t.nour}
            handle="+201055501234"
            channelIcon={MessageCircle}
            channelIconName="MessageCircle"
            channelLabel="WhatsApp"
            previewText={t.excerpt}
            time="5 min"
            timeFull="14 November 2026, 10:37"
            unread="3"
            unreadLabel={t.unread}
            selected
            stateLabel={t.open}
            badges={
              <>
                <Badge tone="neutral" icon={false}>
                  {t.open}
                </Badge>
                <Badge tone="info" icon={false}>
                  {t.ai}
                </Badge>
                <Badge tone="warning">{t.high}</Badge>
              </>
            }
          />
          <ConversationRow
            href="#b"
            who={t.omar}
            channelIcon={MessageCircle}
            channelIconName="MessageCircle"
            channelLabel="WhatsApp"
            previewPrefix={t.reply}
            previewText={t.outbound}
            time="1 h"
            timeFull="14 November 2026, 09:40"
            stateLabel={t.open}
            badges={
              <>
                <Badge tone="neutral" icon={false}>
                  {t.open}
                </Badge>
                <Badge tone="info" icon={false}>
                  {t.human}
                </Badge>
              </>
            }
          />
        </ConversationList>
      </div>
    );
  },
};

export const ListLoading: Story = {
  render: (_, ctx) => (
    <div className="max-w-inbox-list bg-secondary-subtle p-4">
      <ConversationList label={text(localeOf(ctx)).list} loading loadingLabel="…" />
    </div>
  ),
};

export const Thread: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="relative flex max-w-page flex-col bg-canvas p-4">
        <ol className="m-0 flex list-none flex-col p-0">
          <ThreadNote>{t.day}</ThreadNote>
          <MessageBubble side="in" time="10:36" timeFull={t.day}>
            {t.inbound} <InertLink onActivate={() => undefined}>{t.link}</InertLink>
          </MessageBubble>
          <MessageBubble
            side="out"
            author={t.ai_}
            time="10:37"
            timeFull={t.day}
            delivery={{ state: "read", label: t.sent }}
          >
            {t.outbound}
          </MessageBubble>
          <ThreadNote kind="seen">{t.seen}</ThreadNote>
          <MessageBubble
            side="out"
            author={t.you}
            delivery={{ state: "failed", label: t.failed }}
            failure={{
              reason: t.reason,
              actions: (
                <>
                  <Button variant="secondary">{t.retry}</Button>
                  <Button variant="ghost">{t.discard}</Button>
                </>
              ),
            }}
          >
            {t.outbound}
          </MessageBubble>
        </ol>
        <NewMessagesPill label={t.newMessages} onClick={() => undefined} />
      </div>
    );
  },
};

export const Media: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <ol className="m-0 flex max-w-page list-none flex-col gap-3 bg-canvas p-4">
        <MessageBubble
          side="in"
          media={<MediaTile kind="document" state="pending" label={t.pending} />}
        />
        <MessageBubble
          side="in"
          media={<MediaTile kind="image" state="failed" label={t.failedFile} />}
        />
        <MessageBubble
          side="in"
          media={<MediaTile kind="image" state="unavailable" label={t.unavailable} />}
        />
        <MessageBubble
          side="in"
          media={
            <MediaTile
              kind="document"
              state="ask"
              label={t.document}
              filename={t.document}
              size="1.2 MB"
              actionLabel={t.download}
              onAction={() => undefined}
            />
          }
        />
        <MessageBubble
          side="in"
          media={
            <TranscriptBlock
              label={t.transcript}
              state="ready"
              text={t.transcriptText}
              showLabel={t.show}
              hideLabel={t.hide}
            />
          }
        />
      </ol>
    );
  },
};

export const ComposerStates: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="flex max-w-page flex-col gap-6 bg-canvas p-4">
        <Composer
          notice={<p className="m-0">{t.assigns}</p>}
          attachment={
            <AttachmentChip
              name={t.document}
              size="1.2 MB"
              removeLabel={t.close}
              onRemove={() => undefined}
            />
          }
          field={<Textarea aria-label={t.message} rows={2} defaultValue={t.outbound} />}
          tools={
            <>
              <IconButton icon={Paperclip} iconName="Paperclip" label={t.attach} />
              <Button variant="ghost">{t.template}</Button>
            </>
          }
          primary={
            <Button variant="primary">
              <SendHorizontal aria-hidden="true" size={16} className="wasla-flip-rtl" />
              {t.send}
            </Button>
          }
        />
        <Composer
          stopped={
            <>
              <p className="m-0 text-sm text-text">{t.closed}</p>
              <Button variant="primary">{t.choose}</Button>
            </>
          }
        />
      </div>
    );
  },
};

export const Panel: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="flex justify-end bg-canvas">
        <SidePanel title={t.panel} variant="pane" closeLabel={t.close} headingId="story-panel">
          <p className="m-0 text-md text-text">
            <bdi>{t.nour}</bdi>
          </p>
        </SidePanel>
      </div>
    );
  },
};

export const Drawer: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <SidePanel
        title={t.panel}
        variant="drawer"
        open
        closeLabel={t.close}
        headingId="story-drawer"
      >
        <p className="m-0 text-md text-text">
          <bdi>{t.nour}</bdi>
        </p>
      </SidePanel>
    );
  },
};
