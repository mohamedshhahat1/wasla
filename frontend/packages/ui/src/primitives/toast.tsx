"use client";

import { X } from "lucide-react";
import { Toast as ToastPrimitive } from "radix-ui";
import type { ReactNode } from "react";

import { cn } from "../cn";
import { IconButton } from "./icon-button";

export type ToastTone = "info" | "success" | "warning" | "danger";

const tones: Record<ToastTone, string> = {
  info: "border-info/40 bg-info-subtle",
  success: "border-success/40 bg-success-subtle",
  warning: "border-warning/40 bg-warning-subtle",
  danger: "border-danger/40 bg-danger-subtle",
};

export interface ToastProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  tone?: ToastTone;
  closeLabel: string;
}

/**
 * A notification. Radix's "background" type is announced politely (aria-live="polite"), so a
 * toast never interrupts what a screen reader is reading.
 */
export function Toast({
  open,
  onOpenChange,
  title,
  description,
  tone = "info",
  closeLabel,
}: ToastProps) {
  return (
    <ToastPrimitive.Root
      type="background"
      open={open}
      onOpenChange={onOpenChange}
      className={cn(
        "wasla-toast flex items-start gap-4 rounded-lg border p-5 text-text shadow-lg",
        tones[tone],
      )}
    >
      <div className="flex-1">
        <ToastPrimitive.Title className="text-md font-semibold">{title}</ToastPrimitive.Title>
        {description ? (
          <ToastPrimitive.Description className="mt-1 text-sm">
            {description}
          </ToastPrimitive.Description>
        ) : null}
      </div>
      <ToastPrimitive.Close asChild>
        <IconButton icon={X} iconName="X" label={closeLabel} />
      </ToastPrimitive.Close>
    </ToastPrimitive.Root>
  );
}

/** Where toasts appear: the bottom inline-end corner, above everything else. */
export function ToastViewport({ label }: { label: string }) {
  return (
    <ToastPrimitive.Viewport
      label={label}
      className="fixed bottom-0 end-0 z-(--z-toast) m-0 flex w-full max-w-prose list-none flex-col gap-3 p-5 outline-none"
    />
  );
}
