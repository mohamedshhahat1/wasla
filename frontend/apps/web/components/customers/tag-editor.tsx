"use client";

/** A lead's tags (LD-02): chips with add and remove; each tag is text a person typed, in `<bdi>`. */
import { X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useState, type KeyboardEvent } from "react";

import { Button, IconButton, Input } from "@wasla/ui";

import { addTag, LEAD_BOUNDS } from "../../lib/customers/lead-form";
import { useNumber } from "./shared";

export function TagEditor({
  tags,
  onChange,
}: {
  tags: string[];
  onChange: (tags: string[]) => void;
}) {
  const t = useTranslations("app.customers");
  const number = useNumber();
  const id = useId();
  const [draft, setDraft] = useState("");
  const full = tags.length >= LEAD_BOUNDS.tags;
  const add = () => {
    const next = addTag(tags, draft);
    if (!next) return;
    onChange(next);
    setDraft("");
  };
  return (
    <div className="flex flex-col gap-2" data-tag-editor="">
      <label htmlFor={id} className="text-sm font-medium text-text">
        {t("fields.tags")}
      </label>
      {tags.length ? (
        <ul className="m-0 flex list-none flex-wrap gap-2 p-0" aria-label={t("fields.tags")}>
          {tags.map((tag) => (
            <li
              key={tag}
              className="inline-flex items-center gap-1 rounded-full bg-secondary-subtle ps-3 text-sm text-text"
            >
              <bdi dir="auto">{tag}</bdi>
              <IconButton
                icon={X}
                iconName="X"
                label={t("fields.removeTag", { tag })}
                onClick={() => onChange(tags.filter((item) => item !== tag))}
              />
            </li>
          ))}
        </ul>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <Input
          id={id}
          className="max-w-64"
          dir="auto"
          autoComplete="off"
          value={draft}
          maxLength={LEAD_BOUNDS.tag}
          disabled={full}
          aria-describedby={`${id}-hint`}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event: KeyboardEvent<HTMLInputElement>) => {
            if (event.key === "Enter") {
              event.preventDefault();
              add();
            }
          }}
        />
        <Button variant="secondary" disabled={full || !draft.trim()} onClick={add}>
          {t("fields.addTag")}
        </Button>
      </div>
      <p id={`${id}-hint`} className="m-0 text-sm text-text-muted">
        {t("fields.tagsHint", { max: number(LEAD_BOUNDS.tags) })}
      </p>
    </div>
  );
}
