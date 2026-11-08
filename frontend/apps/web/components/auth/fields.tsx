"use client";

import { Eye, EyeOff } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState, type InputHTMLAttributes } from "react";

import { ApiError } from "@wasla/api-client";
import { FormField, IconButton, Input } from "@wasla/ui";

type InputAttrs = Omit<InputHTMLAttributes<HTMLInputElement>, "id">;

/** One labelled text field (FE-1's FormField), its error a translated sentence. */
export function TextField({
  label,
  hint,
  error,
  required,
  ...input
}: InputAttrs & { label: string; hint?: string; error?: string | undefined }) {
  const t = useTranslations("auth");
  return (
    <FormField
      label={label}
      hint={hint}
      error={error}
      required={required}
      requiredLabel={t("required")}
    >
      {(control) => <Input {...control} required={required} {...input} />}
    </FormField>
  );
}

/** A password field with a show / hide button that has an accessible name (AU-01). */
export function PasswordField({
  label,
  hint,
  error,
  required,
  ...input
}: InputAttrs & { label: string; hint?: string; error?: string | undefined }) {
  const t = useTranslations("auth");
  const [visible, setVisible] = useState(false);
  return (
    <FormField
      label={label}
      hint={hint}
      error={error}
      required={required}
      requiredLabel={t("required")}
    >
      {(control) => (
        <div className="flex items-center gap-2">
          <Input
            {...control}
            required={required}
            {...input}
            type={visible ? "text" : "password"}
            className="flex-1"
          />
          <IconButton
            icon={visible ? EyeOff : Eye}
            iconName={visible ? "EyeOff" : "Eye"}
            label={visible ? t("hidePassword") : t("showPassword")}
            aria-pressed={visible}
            onClick={() => setVisible((shown) => !shown)}
          />
        </div>
      )}
    </FormField>
  );
}

/** The API's 422 field errors, as a translated sentence per form field (AU-01). */
export function fieldErrorText(
  error: unknown,
  field: string,
  t: (key: string) => string,
): string | undefined {
  if (!(error instanceof ApiError) || !error.fieldErrors) return undefined;
  const type = error.fieldErrors[field];
  if (!type) return undefined;
  return t(`fieldErrors.${fieldErrorKey(type)}`);
}

/** Pydantic error types folded into the few sentences a person needs. */
export function fieldErrorKey(type: string): string {
  if (type === "missing") return "missing";
  if (type.startsWith("string_too_short")) return "tooShort";
  if (type.startsWith("string_too_long")) return "tooLong";
  if (type.startsWith("string_pattern") || type.startsWith("value_error")) return "invalid";
  return "invalid";
}
