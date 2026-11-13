"use client";

import NextLink from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { Banner, BannerStack, Button, buttonClasses, StatusState } from "@wasla/ui";

import type { GateBanner } from "../../lib/gates";
import { useSession } from "../session-provider";
import { useWorkspace } from "./workspace-context";

/** One banner's words (GT-05..GT-08), chosen by its key, for the role the reducer chose it for. */
function BannerFor({ banner }: { banner: GateBanner }) {
  const t = useTranslations("app");
  const format = useFormatter();
  const { gates, dismiss, reconnectStream } = useWorkspace();
  const dismissProps = banner.dismissible
    ? { onDismiss: () => dismiss(banner.key), dismissLabel: t("gates.dismiss") }
    : {};
  switch (banner.key) {
    case "payment":
      return (
        <Banner name="payment" tone="danger" title={t("gates.payment.owner")}>
          {t("gates.payment.ownerDetail")}
        </Banner>
      );
    case "billing-member":
      return <Banner name="billing-member" tone="warning" title={t("gates.payment.member")} />;
    case "ended":
      return (
        <Banner name="ended" tone="warning" title={t("gates.ended.title")}>
          {t("gates.ended.detail")}
        </Banner>
      );
    case "capacity":
      return (
        <Banner
          name="capacity"
          tone="warning"
          title={
            banner.detail === "over" ? t("gates.capacity.over") : t("gates.capacity.reduction")
          }
          action={
            <NextLink href="/channels" className={buttonClasses("secondary")}>
              {t("gates.capacity.open")}
            </NextLink>
          }
        />
      );
    case "trial": {
      const ends = gates?.trial_ends_at ? new Date(gates.trial_ends_at) : null;
      return (
        <Banner
          name="trial"
          tone="info"
          title={t("gates.trial", { days: banner.days ?? 0 })}
          {...dismissProps}
        >
          {ends ? t("gates.trialOn", { date: format.dateTime(ends, { dateStyle: "long" }) }) : null}
        </Banner>
      );
    }
    case "allowance":
      return (
        <Banner
          name="allowance"
          tone="warning"
          title={t("gates.allowance.title")}
          {...dismissProps}
        />
      );
    case "live":
      if (banner.detail === "opened-elsewhere") {
        return (
          <Banner
            name="live"
            tone="info"
            title={t("live.openedElsewhere")}
            action={
              <Button variant="secondary" onClick={reconnectStream}>
                {t("live.reconnectHere")}
              </Button>
            }
          />
        );
      }
      if (banner.detail === "stopped") {
        return (
          <Banner
            name="live"
            tone="warning"
            title={t("live.down")}
            action={
              <Button variant="secondary" onClick={reconnectStream}>
                {t("live.tryAgain")}
              </Button>
            }
          />
        );
      }
      return <Banner name="live" tone="info" title={t("live.paused")} />;
  }
}

function bannerTitleKey(banner: GateBanner): string {
  switch (banner.key) {
    case "payment":
      return "gates.payment.owner";
    case "billing-member":
      return "gates.payment.member";
    case "ended":
      return "gates.ended.title";
    case "capacity":
      return banner.detail === "over" ? "gates.capacity.over" : "gates.capacity.reduction";
    case "trial":
      return "gates.trial";
    case "allowance":
      return "gates.allowance.title";
    case "live":
      return banner.detail === "opened-elsewhere"
        ? "live.openedElsewhere"
        : banner.detail === "stopped"
          ? "live.down"
          : "live.paused";
  }
}

/**
 * The banner area (GT-09): the reducer's banners in its order, two visible, the rest behind
 * "n more"; a banner that appears is announced once, politely; one that stays is not repeated.
 */
export function GateBanners() {
  const t = useTranslations("app");
  const { decision } = useWorkspace();
  const seen = useRef<Set<string>>(new Set());
  const [announcement, setAnnouncement] = useState("");
  const keys = decision.banners.map((banner) => `${banner.key}:${banner.detail ?? ""}`).join("|");
  useEffect(() => {
    const fresh = decision.banners.find(
      (banner) => !seen.current.has(`${banner.key}:${banner.detail ?? ""}`),
    );
    seen.current = new Set(
      decision.banners.map((banner) => `${banner.key}:${banner.detail ?? ""}`),
    );
    if (fresh) {
      const title = t(bannerTitleKey(fresh), { days: fresh.days ?? 0 });
      setAnnouncement(t("gates.announce", { title }));
    }
    // keys: one announcement per change of the set, not per render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keys]);
  return (
    <>
      <p role="status" aria-live="polite" className="sr-only" data-testid="gate-announcement">
        {announcement}
      </p>
      <BannerStack
        label={t("gates.region")}
        banners={decision.banners.map((banner) => ({
          key: banner.key,
          node: <BannerFor banner={banner} />,
        }))}
        moreLabel={(count) => t("gates.more", { count })}
        fewerLabel={t("gates.fewer")}
      />
    </>
  );
}

/**
 * A gate that replaces the page (GT-03 suspended, GT-04 access withdrawn): the page's own calls
 * are not made while it holds (the page is not rendered); focus moves to its heading.
 */
export function GateFullState({ state }: { state: "suspended" | "revoked" }) {
  const t = useTranslations("app");
  const { recheck, workspaceId } = useWorkspace();
  const { profile } = useSession();
  const [checking, setChecking] = useState(false);
  // Suggest switching only when another workspace can actually be opened (an active one).
  const others = (profile?.workspaces ?? []).some(
    (item) => item.id !== workspaceId && item.status === "active",
  );
  useEffect(() => {
    document.getElementById("gate-heading")?.focus();
  }, [state]);
  if (state === "suspended") {
    return (
      <div data-gate="suspended">
        <StatusState
          tone="warning"
          headingId="gate-heading"
          title={t("gates.suspended.title")}
          description={
            <>
              <p className="m-0">{t("gates.suspended.body")}</p>
              {others ? <p className="m-0 mt-2">{t("gates.suspended.switch")}</p> : null}
            </>
          }
          action={
            <Button
              variant="secondary"
              busy={checking}
              onClick={() => {
                setChecking(true);
                void recheck().finally(() => setChecking(false));
              }}
            >
              {t("gates.suspended.checkAgain")}
            </Button>
          }
        />
      </div>
    );
  }
  return (
    <div data-gate="revoked">
      <StatusState
        tone="warning"
        headingId="gate-heading"
        title={t("gates.revoked.title")}
        description={
          <>
            <p className="m-0">{t("gates.revoked.body")}</p>
            {others ? <p className="m-0 mt-2">{t("gates.revoked.switch")}</p> : null}
          </>
        }
        action={
          others ? undefined : (
            <NextLink href="/workspaces/new" className={buttonClasses("primary")}>
              {t("gates.revoked.create")}
            </NextLink>
          )
        }
      />
    </div>
  );
}
