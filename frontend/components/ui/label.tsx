import * as React from "react";

import { cn } from "@/lib/utils";

const Label = React.forwardRef<
  HTMLLabelElement,
  React.LabelHTMLAttributes<HTMLLabelElement>
>(({ className, ...props }, ref) => (
  <label
    className={cn(
      "font-medium text-ink-muted text-xs uppercase tracking-wide",
      className
    )}
    ref={ref}
    {...props}
  />
));
Label.displayName = "Label";

export { Label };
