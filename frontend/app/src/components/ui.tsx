import * as DialogPrimitive from "@radix-ui/react-dialog";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import * as SelectPrimitive from "@radix-ui/react-select";
import * as SliderPrimitive from "@radix-ui/react-slider";
import * as SwitchPrimitive from "@radix-ui/react-switch";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { Check, ChevronDown, LoaderCircle, X } from "lucide-react";
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { cn } from "@/lib/format";

type Variant = "primary" | "accent" | "secondary" | "ghost" | "danger" | "outline";
type Size = "sm" | "md" | "lg";

const variants: Record<Variant, string> = {
  primary: "bg-primary text-on-primary hover:opacity-90 shadow-[0_1px_0_rgb(0_0_0/0.08)]",
  accent: "bg-accent text-on-accent hover:bg-accent-strong shadow-[0_1px_0_rgb(0_0_0/0.08)]",
  secondary: "bg-surface-3 text-ink hover:bg-line",
  ghost: "text-ink-2 hover:text-ink hover:bg-surface-3",
  danger: "bg-red-soft text-red hover:bg-red hover:text-white",
  outline: "border border-line-strong text-ink hover:bg-surface-2",
};
const sizes: Record<Size, string> = {
  sm: "h-8 px-3 text-[13px] gap-1.5 rounded-[var(--radius-control)]",
  md: "h-9 px-3.5 text-[14px] gap-2 rounded-[var(--radius-control)]",
  lg: "h-11 px-5 text-[15px] gap-2 rounded-[var(--radius-field)]",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  icon?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", loading, icon, className, children, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      className={cn(
        "inline-flex shrink-0 items-center justify-center font-medium whitespace-nowrap transition-[background,color,opacity,box-shadow] duration-150 select-none disabled:opacity-45",
        variants[variant],
        sizes[size],
        className,
      )}
      disabled={disabled || loading}
      {...rest}
    >
      {loading ? <LoaderCircle className="size-4 animate-spin" /> : icon}
      {children}
    </button>
  );
});

export const IconButton = forwardRef<
  HTMLButtonElement,
  ButtonHTMLAttributes<HTMLButtonElement> & { label: string; active?: boolean; size?: "sm" | "md" }
>(function IconButton({ label, active, size = "md", className, children, ...rest }, ref) {
  return (
    <Tooltip content={label}>
      <button
        ref={ref}
        aria-label={label}
        className={cn(
          "inline-flex shrink-0 items-center justify-center rounded-[var(--radius-control)] text-ink-2 transition-colors duration-150 hover:bg-surface-3 hover:text-ink disabled:opacity-40",
          size === "sm" ? "size-7" : "size-9",
          active && "bg-accent-soft text-accent-ink hover:bg-accent-soft",
          className,
        )}
        {...rest}
      >
        {children}
      </button>
    </Tooltip>
  );
});

export function Tooltip({ content, children, side = "top" }: { content: ReactNode; children: ReactNode; side?: "top" | "bottom" | "left" | "right" }) {
  return (
    <TooltipPrimitive.Root delayDuration={350}>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          side={side}
          sideOffset={6}
          className="z-50 max-w-72 rounded-md bg-ink px-2 py-1 text-[12px] leading-snug text-surface shadow-[var(--shadow-pop)]"
        >
          {content}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}

export const TooltipProvider = TooltipPrimitive.Provider;

export function Spinner({ className }: { className?: string }) {
  return <LoaderCircle className={cn("size-4 animate-spin text-ink-3", className)} />;
}

export function Badge({
  children,
  tone = "neutral",
  className,
}: {
  children: ReactNode;
  tone?: "neutral" | "accent" | "blue" | "green" | "red" | "outline";
  className?: string;
}) {
  const tones = {
    neutral: "bg-surface-3 text-ink-2",
    accent: "bg-accent-soft text-accent-ink",
    blue: "bg-blue-soft text-blue",
    green: "bg-green-soft text-green",
    red: "bg-red-soft text-red",
    outline: "border border-line text-ink-2",
  } as const;
  return (
    <span className={cn("inline-flex h-5 items-center gap-1 rounded-full px-2 text-[11.5px] font-medium whitespace-nowrap", tones[tone], className)}>
      {children}
    </span>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="inline-flex h-5 min-w-5 items-center justify-center rounded border border-line bg-surface px-1 text-[11px] text-ink-3">
      {children}
    </kbd>
  );
}

export function Field({ label, hint, children, className }: { label: ReactNode; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <label className={cn("flex flex-col gap-1.5", className)}>
      <span className="text-[13px] font-medium text-ink-2">{label}</span>
      {children}
      {hint && <span className="text-[12px] leading-relaxed text-ink-3">{hint}</span>}
    </label>
  );
}

/* ---------------- dialog ---------------- */
export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  width = 520,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-[rgb(10_12_16/0.45)] backdrop-blur-[2px] data-[state=open]:animate-[fadeIn_.15s_ease-out]" />
        <DialogPrimitive.Content
          style={{ maxWidth: width }}
          className="fixed top-1/2 left-1/2 z-50 flex max-h-[min(88dvh,820px)] w-[calc(100vw-24px)] -translate-x-1/2 -translate-y-1/2 flex-col rounded-[var(--radius-sheet)] border border-line bg-surface shadow-[var(--shadow-pop)] focus:outline-none"
        >
          <div className="flex items-start justify-between gap-4 px-6 pt-5 pb-2">
            <div className="min-w-0">
              <DialogPrimitive.Title className="text-[17px] font-semibold">{title}</DialogPrimitive.Title>
              {description && (
                <DialogPrimitive.Description className="mt-1 text-[13px] leading-relaxed text-ink-3">{description}</DialogPrimitive.Description>
              )}
            </div>
            <DialogPrimitive.Close asChild>
              <button aria-label="关闭" className="-mr-2 rounded-md p-1.5 text-ink-3 hover:bg-surface-3 hover:text-ink">
                <X className="size-4" />
              </button>
            </DialogPrimitive.Close>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-6 py-3">{children}</div>
          {footer && <div className="flex items-center justify-end gap-2 border-t border-line px-6 py-3.5">{footer}</div>}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/* ---------------- menu ---------------- */
export const Menu = DropdownMenu.Root;
export const MenuTrigger = DropdownMenu.Trigger;

export function MenuContent({ children, align = "end", className }: { children: ReactNode; align?: "start" | "end" | "center"; className?: string }) {
  return (
    <DropdownMenu.Portal>
      <DropdownMenu.Content
        align={align}
        sideOffset={6}
        className={cn("z-50 min-w-44 rounded-[12px] border border-line bg-surface p-1 shadow-[var(--shadow-pop)]", className)}
      >
        {children}
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  );
}

export function MenuItem({
  children,
  onSelect,
  danger,
  icon,
  disabled,
}: {
  children: ReactNode;
  onSelect?: () => void;
  danger?: boolean;
  icon?: ReactNode;
  disabled?: boolean;
}) {
  return (
    <DropdownMenu.Item
      disabled={disabled}
      onSelect={onSelect}
      className={cn(
        "flex cursor-pointer items-center gap-2.5 rounded-[8px] px-2.5 py-1.5 text-[13.5px] outline-none select-none data-[disabled]:opacity-40 data-[highlighted]:bg-surface-3",
        danger ? "text-red" : "text-ink",
      )}
    >
      {icon && <span className="text-ink-3 [&>svg]:size-4">{icon}</span>}
      {children}
    </DropdownMenu.Item>
  );
}

export function MenuSeparator() {
  return <DropdownMenu.Separator className="my-1 h-px bg-line" />;
}

export function MenuLabel({ children }: { children: ReactNode }) {
  return <DropdownMenu.Label className="px-2.5 pt-1.5 pb-1 text-[12px] text-ink-3">{children}</DropdownMenu.Label>;
}

/* ---------------- select ---------------- */
export interface Option {
  value: string;
  label: ReactNode;
  hint?: ReactNode;
  disabled?: boolean;
}

export function Select({
  value,
  onChange,
  options,
  placeholder,
  className,
  size = "md",
  ariaLabel,
}: {
  value: string;
  onChange: (value: string) => void;
  options: Option[];
  placeholder?: string;
  className?: string;
  size?: "sm" | "md";
  ariaLabel?: string;
}) {
  return (
    <SelectPrimitive.Root value={value || undefined} onValueChange={onChange}>
      <SelectPrimitive.Trigger
        aria-label={ariaLabel}
        className={cn(
          "field inline-flex items-center justify-between gap-2 text-left",
          size === "sm" ? "h-8 py-0 text-[13px]" : "h-9 py-0",
          className,
        )}
      >
        <span className="truncate">
          <SelectPrimitive.Value placeholder={<span className="text-ink-3">{placeholder}</span>} />
        </span>
        <ChevronDown className="size-4 shrink-0 text-ink-3" />
      </SelectPrimitive.Trigger>
      <SelectPrimitive.Portal>
        <SelectPrimitive.Content
          position="popper"
          sideOffset={6}
          className="z-50 max-h-80 min-w-[var(--radix-select-trigger-width)] overflow-hidden rounded-[12px] border border-line bg-surface shadow-[var(--shadow-pop)]"
        >
          <SelectPrimitive.Viewport className="p-1">
            {options.map((option) => (
              <SelectPrimitive.Item
                key={option.value}
                value={option.value}
                disabled={option.disabled}
                className="relative flex cursor-pointer flex-col rounded-[8px] py-1.5 pr-8 pl-2.5 text-[13.5px] outline-none select-none data-[disabled]:opacity-40 data-[highlighted]:bg-surface-3"
              >
                <SelectPrimitive.ItemText>{option.label}</SelectPrimitive.ItemText>
                {option.hint && <span className="text-[12px] text-ink-3">{option.hint}</span>}
                <SelectPrimitive.ItemIndicator className="absolute top-2 right-2">
                  <Check className="size-4 text-accent-ink" />
                </SelectPrimitive.ItemIndicator>
              </SelectPrimitive.Item>
            ))}
          </SelectPrimitive.Viewport>
        </SelectPrimitive.Content>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  );
}

/* ---------------- switch & slider ---------------- */
export function Switch({ checked, onChange, label }: { checked: boolean; onChange: (value: boolean) => void; label?: string }) {
  return (
    <SwitchPrimitive.Root
      checked={checked}
      onCheckedChange={onChange}
      aria-label={label}
      className="relative h-5 w-9 shrink-0 rounded-full bg-line-strong transition-colors data-[state=checked]:bg-accent"
    >
      <SwitchPrimitive.Thumb className="block size-4 translate-x-0.5 rounded-full bg-white shadow transition-transform data-[state=checked]:translate-x-[18px]" />
    </SwitchPrimitive.Root>
  );
}

export function Slider({
  value,
  onChange,
  min,
  max,
  step,
  label,
}: {
  value: number;
  onChange: (value: number) => void;
  min: number;
  max: number;
  step: number;
  label?: string;
}) {
  return (
    <SliderPrimitive.Root
      value={[value]}
      min={min}
      max={max}
      step={step}
      onValueChange={(next) => onChange(next[0] ?? value)}
      aria-label={label}
      className="relative flex h-5 w-full touch-none items-center select-none"
    >
      <SliderPrimitive.Track className="relative h-1 grow rounded-full bg-line">
        <SliderPrimitive.Range className="absolute h-full rounded-full bg-accent" />
      </SliderPrimitive.Track>
      <SliderPrimitive.Thumb className="block size-4 rounded-full border-2 border-accent bg-surface shadow focus-visible:outline-2 focus-visible:outline-accent" />
    </SliderPrimitive.Root>
  );
}

/* ---------------- segmented ---------------- */
export function Segmented<T extends string>({
  value,
  onChange,
  options,
  size = "md",
  className,
}: {
  value: T;
  onChange: (value: T) => void;
  options: { value: T; label: ReactNode; icon?: ReactNode }[];
  size?: "sm" | "md";
  className?: string;
}) {
  return (
    <div role="radiogroup" className={cn("inline-flex rounded-[10px] bg-surface-3 p-0.5", className)}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={value === option.value}
          onClick={() => onChange(option.value)}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-[8px] px-3 font-medium whitespace-nowrap text-ink-2 transition-all duration-150",
            size === "sm" ? "h-7 text-[12.5px]" : "h-8 text-[13.5px]",
            value === option.value ? "bg-surface text-ink shadow-[0_1px_2px_rgb(0_0_0/0.08)]" : "hover:text-ink",
          )}
        >
          {option.icon}
          {option.label}
        </button>
      ))}
    </div>
  );
}

/* ---------------- states ---------------- */
export function Empty({ icon, title, children, action }: { icon?: ReactNode; title: ReactNode; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-14 text-center">
      {icon && <div className="mb-4 text-ink-3 [&>svg]:size-8 [&>svg]:stroke-[1.4]">{icon}</div>}
      <div className="text-[15px] font-semibold">{title}</div>
      {children && <div className="mt-1.5 max-w-md text-[13.5px] leading-relaxed text-ink-3">{children}</div>}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return <div className="rounded-[10px] bg-red-soft px-3 py-2 text-[13px] leading-relaxed text-red">{children}</div>;
}

export function ProgressBar({ value, className, live }: { value: number; className?: string; live?: boolean }) {
  return (
    <div className={cn("h-1 w-full overflow-hidden rounded-full bg-line", className)}>
      <div
        className={cn("h-full rounded-full bg-accent transition-[width] duration-500", live && "animate-pulse")}
        style={{ width: `${Math.max(0, Math.min(100, value * 100))}%` }}
      />
    </div>
  );
}
