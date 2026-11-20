import type { Meta, StoryObj } from "@storybook/react-vite";

import { BarSeries, InvoiceLines, PaymentStatusPanel, UsageMeter } from "../composites/billing";
import { Card } from "../composites/layout";
import { Button } from "../primitives/button";
import { localeOf } from "./helpers";

/** FE-8's billing composites (UI8-02, ADR-202), in en and ar, at 390 and 1280. */
const meta: Meta = { title: "Billing" };
export default meta;
type Story = StoryObj;

const text = (locale: string) =>
  locale === "ar"
    ? {
        confirming: "جارٍ تأكيد دفعتك…",
        slow: "لم تؤكده Paymob بعد. تستغرق بعض طرق الدفع بضع دقائق.",
        received: "تم استلام الدفعة",
        receivedFor: "99.00 ج.م عن فاتورة 20 نوفمبر – 20 ديسمبر.",
        failed: "لم تتم الدفعة",
        failedLine: "لم يُخصم أي مبلغ. يمكنك المحاولة مرة أخرى.",
        toBilling: "الانتقال إلى الفوترة",
        messages: "الرسائل في هذه الفترة",
        messagesValue: "412 من 1000",
        ai: "ردود الذكاء الاصطناعي في هذه الفترة",
        aiValue: "100 من 100",
        held: "3 محجوزة لردود قيد التنفيذ",
        storage: "التخزين (بايت)",
        storageValue: "12 مستخدم",
        daily: "الرسائل يوميًا",
        day: "اليوم",
        count: "الرسائل",
        showTable: "عرض كجدول",
        showChart: "عرض كرسم بياني",
        none: "لم يُسجل شيء في هذه الفترة بعد.",
        lines: "البنود",
        description: "الوصف",
        quantity: "الكمية",
        amount: "المبلغ",
        line: "خطة Pro – شهر واحد (20 نوفمبر – 20 ديسمبر)",
      }
    : {
        confirming: "Confirming your payment…",
        slow: "Paymob has not confirmed it yet. Some payment methods take a few minutes.",
        received: "Payment received",
        receivedFor: "EGP 99.00 for the invoice of 20 Nov – 20 Dec.",
        failed: "The payment did not go through",
        failedLine: "Nothing was charged. You can try again.",
        toBilling: "Go to Billing",
        messages: "Messages this period",
        messagesValue: "412 of 1,000",
        ai: "AI replies this period",
        aiValue: "100 of 100",
        held: "3 held for replies in progress",
        storage: "Storage (bytes)",
        storageValue: "12 used",
        daily: "Messages per day",
        day: "Day",
        count: "Messages",
        showTable: "Show as table",
        showChart: "Show as chart",
        none: "Nothing recorded in this period yet.",
        lines: "Lines",
        description: "Description",
        quantity: "Quantity",
        amount: "Amount",
        line: "Pro plan – one month (20 Nov – 20 Dec) with a long description that wraps",
      };

export const PaymentStates: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="flex flex-col gap-4">
        <Card as="section">
          <PaymentStatusPanel tone="progress" title={t.confirming} description={t.slow} />
        </Card>
        <Card as="section">
          <PaymentStatusPanel
            tone="success"
            title={t.received}
            description={t.receivedFor}
            action={<Button variant="secondary">{t.toBilling}</Button>}
          />
        </Card>
        <Card as="section">
          <PaymentStatusPanel tone="danger" title={t.failed} description={t.failedLine} />
        </Card>
      </div>
    );
  },
};

export const Usage: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <Card as="section">
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
          <UsageMeter label={t.messages} valueText={t.messagesValue} used={412} limit={1000} />
          <UsageMeter
            label={t.ai}
            valueText={t.aiValue}
            used={100}
            limit={100}
            over
            note={t.held}
          />
          <UsageMeter label={t.storage} valueText={t.storageValue} used={12} limit={null} />
        </div>
      </Card>
    );
  },
};

export const Series: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    const points = Array.from({ length: 14 }, (_, index) => ({
      label: `${index + 7} Nov`,
      value: (index * 37) % 90,
      valueText: String((index * 37) % 90),
    }));
    return (
      <Card as="section">
        <BarSeries
          title={t.daily}
          points={points}
          dayLabel={t.day}
          valueLabel={t.count}
          showTable={t.showTable}
          showChart={t.showChart}
          emptyText={t.none}
        />
      </Card>
    );
  },
};

export const Lines: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <InvoiceLines
        label={t.lines}
        descriptionLabel={t.description}
        quantityLabel={t.quantity}
        amountLabel={t.amount}
        lines={[{ key: "1", description: t.line, quantity: "1", amount: "EGP 99.00" }]}
      />
    );
  },
};
