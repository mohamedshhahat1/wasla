import type { ReactNode } from "react";

import { cn } from "../cn";

/**
 * Layout composites (FE-RD, CM-03): the frames screens are built from, so apps/* never carry a
 * raw value. None of them holds product logic or calls the API; every word comes in as a prop.
 */

/** The product's name as text, in the brand weight (RS-05: no logo is drawn). */
export function Wordmark({
  name,
  tone = "default",
  className,
}: {
  /** "Wasla" in Latin locales, "وصلة" in Arabic - the app-name translation. */
  name: string;
  /** `inverse` on the dark-blue brand band. */
  tone?: "default" | "inverse";
  className?: string;
}) {
  return (
    <span
      data-wordmark=""
      className={cn(
        "inline-flex items-center gap-3 text-xl font-semibold tracking-tight",
        tone === "inverse" ? "text-on-brand" : "text-text",
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "inline-block size-3 rounded-full",
          tone === "inverse" ? "bg-on-brand" : "bg-primary",
        )}
      />
      {name}
    </span>
  );
}

/**
 * The dark-blue brand panel (the 30 % secondary's dark half): the wordmark, one line of promise
 * and a quiet dot pattern drawn in CSS (no image file, no other origin).
 */
export function BrandPanel({
  name,
  promise,
  detail,
  className,
}: {
  /** The wordmark at the top; left out where the page header already carries it. */
  name?: string;
  /** One short line (RS-04). */
  promise: ReactNode;
  /** An optional second, quieter line. */
  detail?: ReactNode;
  className?: string;
}) {
  return (
    <div
      data-brand-panel=""
      data-brand-surface=""
      className={cn(
        "wasla-brand-pattern relative flex flex-col justify-between gap-8 overflow-hidden bg-brand",
        "px-7 py-10 text-on-brand lg:p-10",
        className,
      )}
    >
      {name ? <Wordmark name={name} tone="inverse" /> : <span aria-hidden="true" />}
      <div className="flex flex-col gap-3">
        <p className="m-0 max-w-form text-2xl font-semibold leading-tight tracking-tight text-balance text-on-brand lg:text-3xl">
          {promise}
        </p>
        {detail ? <p className="m-0 text-md text-on-brand-muted">{detail}</p> : null}
      </div>
    </div>
  );
}

/**
 * The auth screens' frame: on a wide screen one card split in two - the form on white (dominant)
 * and the brand panel at the inline end (inline-start in RTL, by logical order); on a phone the
 * brand panel becomes a short band above the form.
 */
export function AuthLayout({
  brand,
  children,
  className,
}: {
  /** A BrandPanel. */
  brand: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      data-auth-layout=""
      className={cn(
        "mx-auto grid w-full max-w-page overflow-hidden rounded-xl border border-border bg-surface",
        "wasla-auth-grid shadow-md lg:min-h-hero",
        className,
      )}
    >
      <div className="order-2 flex justify-center px-5 py-8 sm:px-10 lg:order-1 lg:py-12">
        <div className="w-full max-w-form">{children}</div>
      </div>
      <div className="wasla-auth-brand order-1 flex lg:order-2">{brand}</div>
    </div>
  );
}

/** A white card on the canvas: one recipe for every card (taste-skill: one card style). */
export function Card({
  children,
  className,
  as: Tag = "section",
  ...rest
}: {
  children: ReactNode;
  className?: string;
  as?: "section" | "div" | "article";
} & Record<`data-${string}` | `aria-${string}`, string | undefined>) {
  return (
    <Tag
      className={cn("rounded-xl border border-border bg-surface p-6 shadow-sm sm:p-8", className)}
      {...rest}
    >
      {children}
    </Tag>
  );
}

/** A page's heading: the h1, an optional line under it and the actions at the inline end. */
export function PageHeader({
  title,
  description,
  actions,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <header
      className={cn("flex flex-wrap items-end justify-between gap-x-6 gap-y-4", className)}
      data-page-header=""
    >
      <div className="flex min-w-0 flex-col gap-2">
        <h1 className="m-0 text-3xl font-semibold leading-tight tracking-tight text-text">
          {title}
        </h1>
        {description ? <p className="m-0 text-md text-text-muted">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap gap-3">{actions}</div> : null}
    </header>
  );
}
