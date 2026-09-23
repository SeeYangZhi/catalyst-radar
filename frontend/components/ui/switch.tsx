"use client";

import { Switch as SwitchPrimitive } from "radix-ui";
import type * as React from "react";

import { cn } from "@/lib/utils";

function Switch({
  className,
  size = "default",
  ...props
}: React.ComponentProps<typeof SwitchPrimitive.Root> & {
  size?: "sm" | "default";
}) {
  return (
    <SwitchPrimitive.Root
      className={cn(
        // Switch is the one signal-state primitive where accent-blue as
        // fill is defensible (a 1-bit indicator, not a surface). Off track
        // sits on surface-2 so it reads as inert on the dark canvas.
        "peer group/switch inline-flex shrink-0 items-center rounded-full border border-transparent outline-none transition-all focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 data-[size=default]:h-[1.15rem] data-[size=sm]:h-3.5 data-[size=default]:w-8 data-[size=sm]:w-6 data-[state=checked]:bg-accent data-[state=unchecked]:bg-surface-2",
        className
      )}
      data-size={size}
      data-slot="switch"
      {...props}
    >
      <SwitchPrimitive.Thumb
        className={cn(
          "pointer-events-none block rounded-full bg-ink ring-0 transition-transform data-[state=checked]:translate-x-[calc(100%-2px)] data-[state=unchecked]:translate-x-0 group-data-[size=default]/switch:size-4 group-data-[size=sm]/switch:size-3"
        )}
        data-slot="switch-thumb"
      />
    </SwitchPrimitive.Root>
  );
}

export { Switch };
