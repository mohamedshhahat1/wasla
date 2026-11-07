"use client";

import { X } from "lucide-react";
import { Dialog as DialogPrimitive } from "radix-ui";
import { useId, useState, type ReactNode } from "react";

import { cn } from "../cn";
import { Button } from "./button";
import { IconButton } from "./icon-button";
import { Input } from "./input";

export interface DialogProps {
  open?: boolean;
  defaultOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** The element that opens the dialog (rendered as the Radix trigger). */
  trigger?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  /** The close button's accessible name, in the page's language. */
  closeLabel: string;
  children?: ReactNode;
  footer?: ReactNode;
}

/** A modal dialog: focus trapped, Escape closes, focus returns, the close button at the inline end. */
export function Dialog({
  open,
  defaultOpen,
  onOpenChange,
  trigger,
  title,
  description,
  closeLabel,
  children,
  footer,
}: DialogProps) {
  const rootProps = {
    ...(open === undefined ? {} : { open }),
    ...(defaultOpen === undefined ? {} : { defaultOpen }),
    ...(onOpenChange ? { onOpenChange } : {}),
  };
  return (
    <DialogPrimitive.Root {...rootProps}>
      {trigger ? <DialogPrimitive.Trigger asChild>{trigger}</DialogPrimitive.Trigger> : null}
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-(--z-overlay) bg-text/40" />
        <DialogPrimitive.Content
          className={cn(
            "fixed inset-x-0 top-1/2 z-(--z-modal) mx-auto w-full max-w-prose -translate-y-1/2",
            "max-h-full overflow-y-auto rounded-lg bg-surface-raised p-7 shadow-lg",
          )}
          {...(description ? {} : { "aria-describedby": undefined })}
        >
          <div className="flex items-start justify-between gap-4">
            <DialogPrimitive.Title className="m-0 text-xl font-semibold text-text">
              {title}
            </DialogPrimitive.Title>
            <DialogPrimitive.Close asChild>
              <IconButton icon={X} iconName="X" label={closeLabel} data-dialog-close="" />
            </DialogPrimitive.Close>
          </div>
          {description ? (
            <DialogPrimitive.Description className="mt-3 text-md text-text-muted">
              {description}
            </DialogPrimitive.Description>
          ) : null}
          {children ? <div className="mt-5">{children}</div> : null}
          {footer ? <div className="mt-7 flex flex-wrap justify-end gap-3">{footer}</div> : null}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

export interface ConfirmDialogProps extends Omit<DialogProps, "children" | "footer"> {
  /** The text the person must type to enable the action (a slug, an email). */
  confirmationText: string;
  /** The instruction above the field, e.g. "Type acme to confirm". */
  confirmationLabel: ReactNode;
  confirmLabel: string;
  cancelLabel: string;
  onConfirm: () => void;
}

/** A dialog for irreversible actions: the action stays disabled until the target is typed. */
export function ConfirmDialog({
  confirmationText,
  confirmationLabel,
  confirmLabel,
  cancelLabel,
  onConfirm,
  onOpenChange,
  ...dialog
}: ConfirmDialogProps) {
  const fieldId = useId();
  const [typed, setTyped] = useState("");
  const matches = typed === confirmationText;
  const close = (next: boolean) => {
    if (!next) setTyped("");
    onOpenChange?.(next);
  };
  return (
    <Dialog
      {...dialog}
      onOpenChange={close}
      footer={
        <>
          <DialogPrimitive.Close asChild>
            <Button variant="secondary">{cancelLabel}</Button>
          </DialogPrimitive.Close>
          <Button variant="danger" disabled={!matches} onClick={() => matches && onConfirm()}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <label htmlFor={fieldId} className="mb-2 block text-sm text-text">
        {confirmationLabel}
      </label>
      <Input
        id={fieldId}
        value={typed}
        autoComplete="off"
        spellCheck={false}
        onChange={(event) => setTyped(event.target.value)}
      />
    </Dialog>
  );
}
