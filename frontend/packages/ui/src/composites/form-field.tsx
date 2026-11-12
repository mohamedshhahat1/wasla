import { useId, type ReactNode } from "react";

import { cn } from "../cn";

/** What FormField hands its control so label, hint and error are announced together. */
export interface FieldControlProps {
  id: string;
  "aria-describedby"?: string;
  "aria-invalid"?: true;
  "aria-labelledby": string;
}

export interface FormFieldProps {
  label: ReactNode;
  hint?: ReactNode | undefined;
  /** A translated error sentence (never a backend sentence). */
  error?: ReactNode | undefined;
  required?: boolean | undefined;
  /** Shown beside a required label, translated ("required"). */
  requiredLabel?: string | undefined;
  children: (control: FieldControlProps) => ReactNode;
  className?: string | undefined;
}

/** Label, hint and error with aria-describedby (DS-04); the control is rendered by the caller. */
export function FormField({
  label,
  hint,
  error,
  required,
  requiredLabel,
  children,
  className,
}: FormFieldProps) {
  const id = useId();
  const labelId = `${id}-label`;
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [errorId, hintId].filter(Boolean).join(" ") || undefined;
  const control: FieldControlProps = {
    id,
    "aria-labelledby": labelId,
    ...(describedBy ? { "aria-describedby": describedBy } : {}),
    ...(error ? { "aria-invalid": true as const } : {}),
  };
  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <label id={labelId} htmlFor={id} className="text-sm font-medium text-text">
        {label}
        {required ? (
          <span className="ms-2 text-text-muted">
            <span aria-hidden="true">*</span>
            {requiredLabel ? <span className="sr-only">{requiredLabel}</span> : null}
          </span>
        ) : null}
      </label>
      {children(control)}
      {hint ? (
        <p id={hintId} className="m-0 text-sm text-text-muted">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} className="m-0 text-sm font-medium text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}
