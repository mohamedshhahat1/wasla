"use client";

import type { ReactNode } from "react";

/**
 * The head of an FE-7 screen on the secondary band (the 30 %, as FE-6's), with a heading that can
 * take the focus when the control that held it went away (KY7-04, lib/focus.ts) - tabIndex -1, so
 * it is never a Tab stop of its own.
 */
export function ScreenHead({
  id,
  title,
  line,
  actions,
  children,
}: {
  id: string;
  title: ReactNode;
  line?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 rounded-xl bg-secondary-subtle px-6 py-7 sm:px-8">
      <header
        className="flex flex-wrap items-end justify-between gap-x-6 gap-y-4"
        data-page-header=""
      >
        <div className="flex min-w-0 flex-col gap-2">
          <h1
            id={id}
            tabIndex={-1}
            className="m-0 text-3xl font-semibold leading-tight tracking-tight text-text focus-visible:outline-2 focus-visible:outline-offset-2"
          >
            {title}
          </h1>
          {line ? <p className="m-0 text-md text-text-muted">{line}</p> : null}
        </div>
        {actions ? <div className="flex flex-wrap gap-3">{actions}</div> : null}
      </header>
      {children}
    </div>
  );
}

/**
 * A control busy with its request stays focusable (KY7-04, FE-6's 4a8f968): never `disabled` -
 * Firefox drops the focus of a control that becomes disabled - but aria-disabled and aria-busy,
 * with the handler ignoring a second press.
 */
export function busyProps(pending: boolean): {
  "aria-disabled"?: true;
  "aria-busy"?: true;
} {
  return pending ? { "aria-disabled": true, "aria-busy": true } : {};
}
