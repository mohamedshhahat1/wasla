/**
 * The knowledge screens' query resources. A title search lives in a key in memory only; no key is
 * ever persisted. `READY_DOCUMENT` is home's one-row read for the checklist (HM6-03).
 */
import { queryKey, type QueryKey, type QueryScope } from "@wasla/api-client";

export const KNOWLEDGE_BASES = "knowledge-bases";
export const KNOWLEDGE_BASE = "knowledge-base";
export const DOCUMENTS = "knowledge-documents";
export const DOCUMENT = "knowledge-document";
export const SIMILAR = "knowledge-similar";
export const KNOWLEDGE_SETTINGS = "knowledge-settings";
export const KNOWLEDGE_GAPS = "knowledge-gaps";
export const KNOWLEDGE_LIMITS = "knowledge-limits";
export const READY_DOCUMENT = "knowledge-ready-document";

export const basesKey = (scope: QueryScope): QueryKey => queryKey(scope, KNOWLEDGE_BASES);
export const baseKey = (scope: QueryScope, id: string): QueryKey =>
  queryKey(scope, KNOWLEDGE_BASE, { id });
export const documentKey = (scope: QueryScope, id: string): QueryKey =>
  queryKey(scope, DOCUMENT, { id });
export const similarKey = (scope: QueryScope, id: string): QueryKey =>
  queryKey(scope, SIMILAR, { id });
export const settingsKey = (scope: QueryScope): QueryKey => queryKey(scope, KNOWLEDGE_SETTINGS);
export const gapsKey = (scope: QueryScope, days: number): QueryKey =>
  queryKey(scope, KNOWLEDGE_GAPS, { days });
export const limitsKey = (scope: QueryScope): QueryKey => queryKey(scope, KNOWLEDGE_LIMITS);
