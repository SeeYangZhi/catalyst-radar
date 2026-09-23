"use client";

import { CalendarIcon, XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Calendar } from "@/components/ui/calendar";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn, formatDate } from "@/lib/utils";

export interface DatePickerProps {
  className?: string;
  disabled?: boolean;
  onChange: (value: string) => void;
  placeholder?: string;
  // ISO 8601 date string (yyyy-mm-dd) for symmetry with `<input type="date">`,
  // which is what the filter state already uses on the IPO/earnings pages.
  value: string;
}

function toIso(d: Date | undefined): string {
  if (!d) {
    return "";
  }
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function fromIso(s: string): Date | undefined {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) {
    return;
  }
  const [y, m, day] = s.split("-").map(Number);
  return new Date(y, m - 1, day);
}

export function DatePicker({
  value,
  onChange,
  placeholder = "Pick a date",
  className,
  disabled,
}: DatePickerProps) {
  const selected = fromIso(value);
  // Render the trigger and the clear-button as siblings (not nested)
  // so the clear button can be a real <button> with keyboard focus —
  // nested interactives are invalid HTML and skipped by screen readers.
  return (
    <div className={cn("relative inline-flex items-stretch", className)}>
      <Popover>
        <PopoverTrigger asChild>
          <Button
            className={cn(
              "h-7 justify-start gap-1.5 text-left font-normal text-xs",
              !value && "text-ink-muted",
              value && "pr-7"
            )}
            disabled={disabled}
            size="sm"
            variant="outline"
          >
            <CalendarIcon className="h-3.5 w-3.5" />
            {value ? formatDate(value) : placeholder}
          </Button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-auto p-0">
          <Calendar
            autoFocus
            mode="single"
            onSelect={(d) => onChange(toIso(d))}
            selected={selected}
          />
        </PopoverContent>
      </Popover>
      {value && !disabled && (
        <button
          aria-label="Clear date"
          className="-translate-y-1/2 absolute top-1/2 right-1.5 rounded p-0.5 text-ink-muted hover:text-ink focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            onChange("");
          }}
          type="button"
        >
          <XIcon className="h-3 w-3" />
        </button>
      )}
    </div>
  );
}
