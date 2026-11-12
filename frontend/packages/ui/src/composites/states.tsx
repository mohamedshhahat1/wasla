import { CircleAlert, Inbox, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "../cn";
import { Icon } from "../icon";

interface StateProps {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
  /** The heading level of the title within the page (1 on a full page). */
  headingLevel?: 1 | 2 | 3;
}

function StateLayout({
  icon,
  iconName,
  tone,
  title,
  description,
  action,
  className,
  headingLevel = 2,
  children,
}: StateProps & { icon: LucideIcon; iconName: string; tone: string; children?: ReactNode }) {
  const Heading = `h${headingLevel}` as const;
  return (
    <section
      className={cn(
        "mx-auto flex w-full max-w-prose flex-col items-center gap-4 rounded-xl border border-border",
        "bg-surface px-6 py-10 text-center shadow-sm sm:px-10",
        className,
      )}
    >
      <span className={cn("inline-flex size-14 items-center justify-center rounded-full", tone)}>
        <Icon icon={icon} name={iconName} size={28} />
      </span>
      <Heading className="m-0 text-2xl font-semibold leading-tight tracking-tight text-text">
        {title}
      </Heading>
      {description ? <p className="m-0 text-md text-text-muted">{description}</p> : null}
      {children}
      {action ? <div className="mt-3 flex flex-wrap justify-center gap-3">{action}</div> : null}
    </section>
  );
}

export function EmptyState(props: StateProps) {
  return (
    <StateLayout icon={Inbox} iconName="Inbox" tone="bg-secondary-subtle text-primary" {...props} />
  );
}

export interface ErrorStateProps extends StateProps {
  /** The backend's request id, for support ("Reference: …"), when there is one. */
  requestId?: string | null;
  /** The translated label before the id. */
  requestIdLabel?: string;
}

/** An error page or panel: a translated sentence, never a stack or a backend sentence (AP-02). */
export function ErrorState({ requestId, requestIdLabel, ...props }: ErrorStateProps) {
  return (
    <StateLayout
      icon={CircleAlert}
      iconName="CircleAlert"
      tone="bg-danger-subtle text-danger"
      {...props}
    >
      {requestId ? (
        <p className="m-0 text-sm text-text-muted" data-testid="request-id">
          {requestIdLabel ? `${requestIdLabel} ` : null}
          <bdi className="font-medium text-text">{requestId}</bdi>
        </p>
      ) : null}
    </StateLayout>
  );
}
