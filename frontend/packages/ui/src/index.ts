export { cn } from "./cn";
export { FLIP_IN_RTL, Icon, NEVER_FLIP, shouldFlip, type IconProps } from "./icon";
export { UiProvider, useUiLocale, type Dir, type UiProviderProps } from "./provider";

export { Button, buttonClasses, type ButtonProps, type ButtonVariant } from "./primitives/button";
export { Checkbox, type CheckboxProps } from "./primitives/checkbox";
export { Combobox, type ComboboxOption, type ComboboxProps } from "./primitives/combobox";
export {
  ConfirmDialog,
  Dialog,
  type ConfirmDialogProps,
  type DialogProps,
} from "./primitives/dialog";
export { IconButton, type IconButtonProps } from "./primitives/icon-button";
export { Input, Textarea, type InputProps, type TextareaProps } from "./primitives/input";
export { Link, linkClasses, type LinkProps } from "./primitives/link";
export { DropdownMenu, type DropdownMenuProps, type MenuEntry } from "./primitives/menu";
export {
  physicalSide,
  Popover,
  Tooltip,
  type LogicalSide,
  type PopoverProps,
  type TooltipProps,
} from "./primitives/overlays";
export { RadioGroup, type RadioGroupProps, type RadioOption } from "./primitives/radio-group";
export { Select, type SelectOption, type SelectProps } from "./primitives/select";
export { Switch, type SwitchProps } from "./primitives/switch";
export { Tabs, type TabItem, type TabsProps } from "./primitives/tabs";
export { Toast, ToastViewport, type ToastProps, type ToastTone } from "./primitives/toast";

export { FormField, type FieldControlProps, type FormFieldProps } from "./composites/form-field";
export { EmptyState, ErrorState, type ErrorStateProps } from "./composites/states";
export { AuthLayout, BrandPanel, Card, PageHeader, Wordmark } from "./composites/layout";
export { Spinner } from "./primitives/button";
export {
  AppShell,
  NavList,
  useCloseNavigation,
  type AppShellProps,
  type NavItem,
} from "./composites/app";
export {
  Badge,
  Banner,
  BannerStack,
  Checklist,
  KeyValueList,
  LiveIndicator,
  Meter,
  StatCard,
  StatusState,
  type BannerProps,
  type ChecklistItem,
  type ChecklistState,
  type LiveState,
  type StatusTone,
} from "./composites/status";
export {
  SwitcherMenu,
  type SwitcherMenuProps,
  type SwitcherWorkspace,
} from "./composites/switcher";

/** The sixteen primitives of DS-04, for the Storybook coverage test. */
export const PRIMITIVES = [
  "Button",
  "IconButton",
  "Link",
  "Input",
  "Textarea",
  "Checkbox",
  "RadioGroup",
  "Switch",
  "Select",
  "Combobox",
  "Dialog",
  "DropdownMenu",
  "Popover",
  "Tooltip",
  "Tabs",
  "Toast",
] as const;
