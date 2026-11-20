"use client";

/**
 * APP-BILL-05 at /billing/usage (US-01 / US-02) and APP-BILL-06 at /billing/capacity (CY-01 ..
 * CY-03), ADR-202. Usage - the owner's and an administrator's (GET /usage is the admins'): every
 * entitlement against its limit, the AI allowance with what is held, and the messages per day as a
 * small chart with the same numbers as a table. No forecast. Capacity - everyone with Billing reads
 * it; the top-up catalogue too, with "Buy" for the owner (a hosted checkout); the purchases and
 * grants are the owner's. A reduction is chosen where FE-3 built the choice, on the Channels page.
 */
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import NextLink from "next/link";
import { useFormatter, useTranslations } from "next-intl";

import { unwrap, type components } from "@wasla/api-client";
import { BarSeries, Button, Card, linkClasses, UsageMeter } from "@wasla/ui";

import { isZero } from "../../lib/billing/money";
import { useWorkspace } from "../app/workspace-context";
import { useSession } from "../session-provider";
import { busyProps } from "../team/screen-head";
import {
  BillingFrame,
  billingKey,
  HostedPaymentStatus,
  LoadFailed,
  Loading,
  NotForBillingRole,
  useBillingRoles,
  useDate,
  useHostedPayment,
  useMoney,
} from "./shared";

type Entitlement = components["schemas"]["EntitlementRead"];

const MESSAGE_EVENTS = ["whatsapp_message_received", "whatsapp_message_sent"] as const;
export const PURCHASES_PAGE = 20;

function useDigits(): (value: number) => string {
  const format = useFormatter();
  return (value) => format.number(value, { numberingSystem: "latn" });
}

export function UsagePage() {
  const { reader } = useBillingRoles();
  return reader ? <UsageScreen /> : <NotForBillingRole />;
}

function UsageScreen() {
  const t = useTranslations("app.billing");
  const digits = useDigits();
  const date = useDate();
  const { scope } = useWorkspace();
  const { session } = useSession();
  const entitlements = useQuery({
    queryKey: billingKey(scope, "entitlements"),
    queryFn: () => unwrap(session.api.GET("/api/v1/billing/entitlements")),
  });
  const daily = useQuery({
    queryKey: billingKey(scope, "usage-daily"),
    queryFn: () =>
      unwrap(
        session.api.GET("/api/v1/usage/daily", {
          params: { query: { event_type: [...MESSAGE_EVENTS] } },
        }),
      ),
  });

  const meter = (row: Entitlement) => {
    const limit = row.effective_limit ?? row.limit;
    return (
      <UsageMeter
        key={row.key}
        label={t(`limit.${row.key}`)}
        used={row.used}
        limit={limit}
        over={row.over_limit}
        valueText={
          limit === null
            ? t("usage.ofUnlimited", { used: digits(row.used) })
            : t("usage.of", { used: digits(row.used), limit: digits(limit) })
        }
        note={row.held ? t("usage.held", { held: digits(row.held) }) : null}
      />
    );
  };

  let body: React.ReactNode;
  if (entitlements.isError) {
    body = <LoadFailed error={entitlements.error} retry={() => void entitlements.refetch()} />;
  } else if (entitlements.isPending) {
    body = <Loading />;
  } else {
    const period = entitlements.data.find((row) => row.period_start && row.period_end);
    const perDay = new Map<string, number>();
    for (const point of daily.data?.points ?? []) {
      perDay.set(point.day, (perDay.get(point.day) ?? 0) + point.quantity);
    }
    const points = [...perDay.entries()]
      .sort(([a], [b]) => (a < b ? -1 : 1))
      .map(([day, value]) => ({ label: date(day), value, valueText: digits(value) }));
    body = (
      <>
        <Card as="section" aria-labelledby="usage-limits">
          <h2 id="usage-limits" className="m-0 mb-1 text-lg font-semibold text-text">
            {t("usage.title")}
          </h2>
          {period ? (
            <p className="m-0 mb-4 text-sm text-text-muted">
              {t("usage.period", {
                start: date(period.period_start),
                end: date(period.period_end),
              })}
            </p>
          ) : null}
          <div className="grid grid-cols-1 gap-5 md:grid-cols-2" data-usage="">
            {entitlements.data.map(meter)}
          </div>
        </Card>
        <Card as="section" aria-label={t("usage.daily")}>
          {daily.isError ? (
            <LoadFailed error={daily.error} retry={() => void daily.refetch()} />
          ) : daily.isPending ? (
            <Loading />
          ) : (
            <BarSeries
              title={t("usage.daily")}
              points={points}
              dayLabel={t("usage.day")}
              valueLabel={t("usage.messages")}
              showTable={t("usage.showTable")}
              showChart={t("usage.showChart")}
              emptyText={t("usage.noData")}
            />
          )}
        </Card>
      </>
    );
  }
  return <BillingFrame current="usage">{body}</BillingFrame>;
}

/* --------------------------------------------------------------- capacity */

export function CapacityPage() {
  const { reader } = useBillingRoles();
  return reader ? <CapacityScreen /> : <NotForBillingRole />;
}

function CapacityScreen() {
  const t = useTranslations("app.billing");
  const digits = useDigits();
  const money = useMoney();
  const date = useDate();
  const { owner } = useBillingRoles();
  const { scope } = useWorkspace();
  const { session } = useSession();
  const payment = useHostedPayment();
  const capacity = useQuery({
    queryKey: billingKey(scope, "channel-capacity"),
    queryFn: () => unwrap(session.api.GET("/api/v1/billing/channel-capacity")),
  });
  const topups = useQuery({
    queryKey: billingKey(scope, "topups"),
    queryFn: () => unwrap(session.api.GET("/api/v1/billing/topups")),
  });
  const purchases = useInfiniteQuery({
    queryKey: billingKey(scope, "topup-purchases"),
    queryFn: ({ pageParam }) =>
      unwrap(
        session.api.GET("/api/v1/billing/topup-purchases", {
          params: { query: { limit: PURCHASES_PAGE, offset: pageParam } },
        }),
      ),
    initialPageParam: 0,
    getNextPageParam: (last) =>
      last.offset + last.items.length < last.total ? last.offset + last.items.length : undefined,
    enabled: owner,
  });
  const busy = payment.start.isPending || payment.start.isSuccess;

  let capacityBody: React.ReactNode;
  if (capacity.isError) {
    capacityBody = <LoadFailed error={capacity.error} retry={() => void capacity.refetch()} />;
  } else if (capacity.isPending) {
    capacityBody = <Loading />;
  } else {
    const c = capacity.data;
    capacityBody = (
      <div className="flex flex-col gap-3" data-capacity={c.over_limit ? "over" : "ok"}>
        <UsageMeter
          label={t("capacity.title")}
          used={c.active}
          limit={c.effective_limit}
          over={c.over_limit}
          valueText={
            c.effective_limit === null
              ? t("capacity.lineUnlimited", { active: digits(c.active) })
              : t("capacity.line", { active: digits(c.active), limit: digits(c.effective_limit) })
          }
          note={t("capacity.parts", {
            base: c.base_limit === null ? t("unlimited") : digits(c.base_limit),
            topups: digits(c.topup_limit),
            granted: digits(c.platform_grant_limit),
          })}
        />
        {c.over_limit ? (
          <p className="m-0 text-md font-medium text-text">{t("capacity.over")}</p>
        ) : null}
        {c.reduction && owner ? (
          <p className="m-0 text-md text-text" data-capacity-reduction="">
            {t("capacity.reduction")}{" "}
            <NextLink href="/channels" className={linkClasses("text-md")}>
              {t("capacity.toChannels")}
            </NextLink>
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <BillingFrame current="capacity">
      <Card as="section" aria-label={t("capacity.title")}>
        {capacityBody}
      </Card>
      <Card as="section" aria-labelledby="billing-topups">
        <h2 id="billing-topups" className="m-0 mb-3 text-lg font-semibold text-text">
          {t("topups.title")}
        </h2>
        {topups.isError ? (
          <LoadFailed error={topups.error} retry={() => void topups.refetch()} />
        ) : topups.isPending ? (
          <Loading />
        ) : topups.data.filter((product) => product.price && !isZero(product.price)).length ? (
          <ul className="m-0 flex list-none flex-col gap-2 p-0" data-topups="">
            {topups.data
              .filter((product) => product.price && !isZero(product.price))
              .map((product) => {
                const price = money(product.price, product.currency);
                return (
                  <li
                    key={product.id}
                    className="flex flex-col gap-2 rounded-lg bg-surface p-4 sm:flex-row sm:items-center sm:justify-between"
                  >
                    <div className="flex flex-col gap-1">
                      <span className="text-md font-semibold text-text">
                        <bdi>{product.name}</bdi>
                      </span>
                      <span className="text-sm text-text-muted">
                        {t("topups.adds", { quantity: digits(product.quantity) })} · {price}
                      </span>
                    </div>
                    {owner ? (
                      <Button
                        {...busyProps(busy)}
                        onClick={() =>
                          !busy && payment.start.mutate({ kind: "topup", topupId: product.id })
                        }
                        aria-label={t("topups.buyOf", { name: product.name, price })}
                        data-action="buy-topup"
                      >
                        {t("topups.buy")}
                      </Button>
                    ) : null}
                  </li>
                );
              })}
          </ul>
        ) : (
          <p className="m-0 text-md text-text-muted">{t("topups.empty")}</p>
        )}
        <HostedPaymentStatus pending={busy} message={payment.message} />
      </Card>
      {owner ? (
        <Card as="section" aria-labelledby="billing-purchases">
          <h2 id="billing-purchases" className="m-0 mb-3 text-lg font-semibold text-text">
            {t("purchases.title")}
          </h2>
          {purchases.isError ? (
            <LoadFailed error={purchases.error} retry={() => void purchases.refetch()} />
          ) : purchases.isPending ? (
            <Loading />
          ) : purchases.data.pages[0]?.items.length ? (
            <>
              <ul className="m-0 flex list-none flex-col gap-2 p-0" data-purchases="">
                {purchases.data.pages
                  .flatMap((page) => page.items)
                  .map((purchase) => (
                    <li
                      key={purchase.id}
                      className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-surface p-3 text-md"
                    >
                      <span className="text-text">
                        <bdi>{purchase.product_name}</bdi> · {digits(purchase.quantity)}
                      </span>
                      <span className="text-sm text-text-muted">
                        {purchase.source === "platform_grant"
                          ? t("purchases.granted")
                          : t("purchases.bought")}{" "}
                        · {t("purchases.until", { date: date(purchase.expires_at) })}
                      </span>
                    </li>
                  ))}
              </ul>
              {purchases.hasNextPage ? (
                <Button
                  variant="secondary"
                  className="mt-3"
                  {...busyProps(purchases.isFetchingNextPage)}
                  onClick={() => !purchases.isFetchingNextPage && void purchases.fetchNextPage()}
                  data-action="more-purchases"
                >
                  {t("purchases.more")}
                </Button>
              ) : null}
            </>
          ) : (
            <p className="m-0 text-md text-text-muted">{t("purchases.empty")}</p>
          )}
        </Card>
      ) : null}
    </BillingFrame>
  );
}
