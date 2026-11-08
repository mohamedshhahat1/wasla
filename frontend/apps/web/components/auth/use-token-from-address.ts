"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Reads the named query parameters once and removes the whole query from the address bar and
 * the history entry (history.replaceState) before the page does anything with them (AU-02):
 * reset, invitation and Google tokens never stay in the URL. `undefined` until read; null when
 * absent.
 */
export function useParamsFromAddress<K extends string>(
  names: readonly K[],
): Record<K, string | null> | undefined {
  const read = useRef<Record<K, string | null> | undefined>(undefined);
  const [values, setValues] = useState<Record<K, string | null> | undefined>(undefined);
  useEffect(() => {
    if (!read.current) {
      const params = new URLSearchParams(window.location.search);
      const found = {} as Record<K, string | null>;
      for (const name of names) found[name] = params.get(name);
      if (window.location.search || window.location.hash) {
        window.history.replaceState(null, "", window.location.pathname);
      }
      read.current = found;
    }
    setValues(read.current);
    // Once per page: the names never change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return values;
}

/** One token from the address bar (see useParamsFromAddress). */
export function useTokenFromAddress(name: string): string | null | undefined {
  const values = useParamsFromAddress([name]);
  return values === undefined ? undefined : values[name];
}
