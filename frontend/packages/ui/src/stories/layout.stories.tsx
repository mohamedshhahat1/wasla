import type { Meta, StoryObj } from "@storybook/react-vite";

import { FormField } from "../composites/form-field";
import { AuthLayout, BrandPanel, Card, PageHeader, Wordmark } from "../composites/layout";
import { Button } from "../primitives/button";
import { Input } from "../primitives/input";
import { labelsOf, localeOf } from "./helpers";

/** CM-03's layout composites, in en and ar, at 390 and 1280 (the toolbar's widths). */
const meta: Meta = { title: "Composites/Layout" };
export default meta;
type Story = StoryObj;

const text = (locale: string) =>
  locale === "ar"
    ? {
        name: "وصلة",
        promise: "خدمة العملاء والمبيعات على كل قناة.",
        title: "تسجيل الدخول",
        email: "البريد الإلكتروني",
      }
    : {
        name: "Wasla",
        promise: "Customer service and sales on every channel.",
        title: "Log in",
        email: "Email",
      };

export const AuthScreen: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <AuthLayout brand={<BrandPanel promise={t.promise} />}>
        <section className="flex flex-col gap-6">
          <h1 className="m-0 text-3xl font-semibold leading-tight tracking-tight text-text">
            {t.title}
          </h1>
          <FormField label={t.email}>{(control) => <Input type="email" {...control} />}</FormField>
          <Button>{labelsOf(ctx).save}</Button>
        </section>
      </AuthLayout>
    );
  },
};

export const BrandPanelWithWordmark: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return <BrandPanel name={t.name} promise={t.promise} className="min-h-hero" />;
  },
};

export const CardAndPageHeader: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="flex flex-col gap-6 bg-canvas p-5">
        <PageHeader
          title={t.title}
          description={t.promise}
          actions={<Button variant="secondary">{labelsOf(ctx).cancel}</Button>}
        />
        <Card>
          <p className="m-0 text-md text-text">{t.promise}</p>
        </Card>
      </div>
    );
  },
};

export const Wordmarks: Story = {
  render: (_, ctx) => {
    const t = text(localeOf(ctx));
    return (
      <div className="flex flex-col gap-5">
        <p className="m-0 bg-surface p-5">
          <Wordmark name={t.name} />
        </p>
        <p className="m-0 bg-brand p-5">
          <Wordmark name={t.name} tone="inverse" />
        </p>
      </div>
    );
  },
};
