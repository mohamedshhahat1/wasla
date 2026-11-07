import type { InputHTMLAttributes, TextareaHTMLAttributes } from "react";

import { cn } from "../cn";
import { controlBase } from "./styles";

export type InputProps = InputHTMLAttributes<HTMLInputElement>;

export function Input({ className, ...props }: InputProps) {
  return <input className={cn(controlBase, className)} {...props} />;
}

export type TextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement>;

export function Textarea({ className, rows, ...props }: TextareaProps) {
  return <textarea rows={rows ?? 4} className={cn(controlBase, "py-3", className)} {...props} />;
}
