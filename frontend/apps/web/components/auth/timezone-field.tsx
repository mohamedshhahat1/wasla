"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useMemo } from "react";

import { unwrap } from "@wasla/api-client";
import { Combobox, FormField } from "@wasla/ui";

import { useSession } from "../session-provider";

/** The device's zone, when the API lists it (AU-02: default from Intl, chosen from the list). */
function deviceZone(): string | null {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch {
    return null;
  }
}

/** GET /timezones, shared by the field and the form that holds it (one request). */
export function useTimezones() {
  const { session } = useSession();
  return useQuery({
    queryKey: ["public", "timezones"],
    queryFn: () => unwrap(session.api.GET("/api/v1/timezones")),
    staleTime: Infinity,
  });
}

/**
 * FEF-36: a form with this field submits once the list has answered - its default zone is set
 * then. Pressed before, the press failed on the empty zone and the default, arriving a moment
 * later, cleared that error: the button seemed to do nothing. A failed list lets it submit (the
 * field says the list is unavailable).
 */
export function timezonesSettled(zones: { data?: unknown; isError: boolean }): boolean {
  return zones.data !== undefined || zones.isError;
}

/** GET /timezones as a searchable list; the value is an IANA id. */
export function TimezoneField({
  value,
  onChange,
  error,
}: {
  value: string | null;
  onChange: (zone: string | null) => void;
  error?: string | undefined;
}) {
  const t = useTranslations("auth");
  const zones = useTimezones();
  const list = useMemo(() => zones.data?.timezones ?? [], [zones.data]);

  useEffect(() => {
    if (value || !zones.data) return;
    const device = deviceZone();
    const known = list.find((zone) => zone.id === device) ?? zones.data.suggested[0] ?? list[0];
    if (known) onChange(known.id);
  }, [value, zones.data, list, onChange]);

  return (
    <FormField
      label={t("timezone")}
      error={error ?? (zones.isError ? t("timezoneUnavailable") : undefined)}
      required
      requiredLabel={t("required")}
    >
      {(control) => (
        <Combobox
          id={control.id}
          aria-labelledby={control["aria-labelledby"]}
          {...(control["aria-describedby"]
            ? { "aria-describedby": control["aria-describedby"] }
            : {})}
          {...(control["aria-invalid"] ? { "aria-invalid": true } : {})}
          options={list.map((zone) => ({
            value: zone.id,
            label: `${zone.label} (${zone.utc_offset})`,
          }))}
          value={value}
          onValueChange={onChange}
          emptyText={t("timezoneNone")}
          disabled={!zones.data}
        />
      )}
    </FormField>
  );
}

/** A workspace slug suggested from a name: Latin letters and digits joined by hyphens. */
export function suggestSlug(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLocaleLowerCase("en")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
}
