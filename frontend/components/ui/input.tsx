import * as React from "react";

import { cn } from "@/lib/utils";

const Input = React.forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement>
>(({ className, type, ...props }, ref) => (
  <input
    className={cn(
      "flex h-10 w-full rounded-[10px] border border-input bg-surface-1 px-3 py-2 text-ink text-sm placeholder:text-ink-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50",
      className
    )}
    ref={ref}
    type={type}
    {...props}
  />
));
Input.displayName = "Input";

export { Input };
