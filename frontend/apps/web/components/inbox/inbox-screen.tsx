"use client";

/**
 * The inbox (APP-INBOX-01 .. 03) at three widths (LY-01): from 1024 px the list, the conversation
 * and the customer panel side by side (the panel hideable); from 768 px the list and the
 * conversation, the panel a drawer; below that one step at a time - list, conversation, panel - with
 * the URL tracking the step so Back works. The view lives in the URL's search params; the search
 * term never does.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { unwrap } from "@wasla/api-client";
import { SidePanel } from "@wasla/ui";

import { layoutFor, panelStartsOpen } from "../../lib/inbox/layout";
import { inboxHref, isConversationId, parseView } from "../../lib/inbox/view";
import { refreshInboxLists } from "../../lib/stream-handlers";
import { useWorkspace } from "../app/workspace-context";
import { useSession } from "../session-provider";
import { ConversationListPane } from "./conversation-list-pane";
import { ConversationPane, useConversation } from "./conversation-pane";
import { CustomerPanelBody } from "./customer-panel";
import { InboxRuntimeProvider } from "./inbox-runtime";

function useWidth(): number {
  return useSyncExternalStore(
    (notify) => {
      window.addEventListener("resize", notify);
      return () => window.removeEventListener("resize", notify);
    },
    () => window.innerWidth,
    () => 1280,
  );
}

/** LY-02: keep the composer above an on-screen keyboard (visualViewport). */
function useKeyboardInset(): number {
  const [inset, setInset] = useState(0);
  useEffect(() => {
    const viewport = window.visualViewport;
    if (!viewport) return;
    const update = () =>
      setInset(Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop));
    viewport.addEventListener("resize", update);
    viewport.addEventListener("scroll", update);
    return () => {
      viewport.removeEventListener("resize", update);
      viewport.removeEventListener("scroll", update);
    };
  }, []);
  return inset;
}

function Screen() {
  const t = useTranslations("app.inbox");
  const params = useParams<{ id?: string }>();
  const search = useSearchParams();
  const router = useRouter();
  const queryClient = useQueryClient();
  const { session } = useSession();
  const { scope } = useWorkspace();
  const width = useWidth();
  const layout = layoutFor(width);
  const inset = useKeyboardInset();
  const view = useMemo(() => parseView(new URLSearchParams(search.toString())), [search]);
  const openId = isConversationId(params.id) ? params.id : null;
  const panelStep = search.get("panel") === "customer";
  const conversation = useConversation(openId);
  const [panelOpen, setPanelOpen] = useState<boolean | null>(null);
  const showPanel = panelOpen ?? panelStartsOpen(width);
  const [drawer, setDrawer] = useState(false);
  const stepHeading = useRef<HTMLHeadingElement | null>(null);

  // Each phone step moves focus to its heading (LY-01).
  useEffect(() => {
    if (layout !== "steps") return;
    const target = panelStep
      ? stepHeading.current
      : openId
        ? document.getElementById("inbox-conversation-heading")
        : document.getElementById("inbox-list-heading");
    target?.focus();
  }, [layout, openId, panelStep]);

  const markUnread = async (id: string) => {
    await unwrap(
      session.api.POST("/api/v1/conversations/{conversation_id}/unread", {
        params: { path: { conversation_id: id } },
      }),
    );
    refreshInboxLists(queryClient, scope);
    if (id === openId) router.push(inboxHref(view));
  };

  const back = () => router.push(inboxHref(view));
  const list = (
    <ConversationListPane view={view} openId={openId} onMarkUnread={(id) => void markUnread(id)} />
  );
  const pane = openId ? (
    <ConversationPane
      key={openId}
      id={openId}
      inView
      onBack={layout === "steps" ? back : null}
      panelShown={layout === "panes" ? showPanel : false}
      onShowPanel={() => {
        if (layout === "panes") setPanelOpen(true);
        else if (layout === "drawer") setDrawer(true);
        else router.push(inboxHref(view, openId, { panel: true }));
      }}
      onClosedUnread={back}
    />
  ) : (
    <div
      className="flex flex-1 items-center justify-center p-8 text-center text-md text-text-muted"
      data-inbox-pane="empty"
    >
      <p className="m-0">{t("pickConversation")}</p>
    </div>
  );
  const panelBody = conversation.data ? (
    <CustomerPanelBody conversation={conversation.data} />
  ) : null;

  return (
    <div
      data-page="inbox"
      data-inbox-layout={layout}
      className="flex min-h-inbox flex-col overflow-hidden rounded-xl border border-border bg-surface shadow-sm"
      style={inset ? { paddingBlockEnd: inset } : undefined}
    >
      {layout === "steps" ? (
        !openId ? (
          list
        ) : panelStep ? (
          <section
            aria-labelledby="inbox-panel-heading"
            data-inbox-pane="panel"
            className="flex flex-col gap-5 p-5"
          >
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="sr-only focus:not-sr-only"
                onClick={() => router.push(inboxHref(view, openId))}
              >
                {t("back")}
              </button>
              <h2
                id="inbox-panel-heading"
                ref={stepHeading}
                tabIndex={-1}
                className="m-0 text-lg font-semibold text-text outline-none"
              >
                {t("panel.title")}
              </h2>
            </div>
            {panelBody}
          </section>
        ) : (
          pane
        )
      ) : (
        <div className="flex h-inbox min-h-0">
          <div className="flex w-inbox-list shrink-0 flex-col border-e border-border">{list}</div>
          {pane}
          {openId && layout === "panes" && showPanel ? (
            <SidePanel
              title={t("panel.title")}
              variant="pane"
              closeLabel={t("panel.hide")}
              headingId="inbox-panel-heading"
            >
              <button
                type="button"
                className="self-start text-sm text-primary underline underline-offset-4"
                onClick={() => setPanelOpen(false)}
              >
                {t("panel.hide")}
              </button>
              {panelBody}
            </SidePanel>
          ) : null}
          {openId && layout === "drawer" ? (
            <SidePanel
              title={t("panel.title")}
              variant="drawer"
              open={drawer}
              onOpenChange={setDrawer}
              closeLabel={t("close")}
              headingId="inbox-drawer-heading"
            >
              {panelBody}
            </SidePanel>
          ) : null}
        </div>
      )}
    </div>
  );
}

export function InboxScreen() {
  return (
    <InboxRuntimeProvider>
      <Screen />
    </InboxRuntimeProvider>
  );
}
