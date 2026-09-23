"use client";

import {
  Activity,
  Bell,
  Building2,
  CalendarClock,
  ClipboardCheck,
  LayoutDashboard,
  LogOut,
  Network,
  Radar,
  Rocket,
  Search,
  Settings,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar";
import { Toaster } from "@/components/ui/sonner";

const NAV = [
  { label: "Dashboard", icon: LayoutDashboard, href: "/dashboard" },
  { label: "Tracked Companies", icon: Building2, href: "/dashboard/companies" },
  { label: "Company Search", icon: Search, href: "/dashboard/search" },
  { label: "IPO Filters", icon: Rocket, href: "/dashboard/ipo" },
  { label: "Earnings", icon: CalendarClock, href: "/dashboard/earnings" },
  {
    label: "Catalyst Review",
    icon: ClipboardCheck,
    href: "/dashboard/review",
  },
  { label: "Notifications", icon: Bell, href: "/dashboard/notifications" },
  { label: "Source Runs", icon: Activity, href: "/dashboard/source-runs" },
  {
    label: "Relationships",
    icon: Network,
    href: "/dashboard/settings/relationships",
  },
  { label: "Alert Settings", icon: Settings, href: "/dashboard/settings" },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const { user, loading, logout } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!(loading || user)) {
      router.replace("/login");
    }
  }, [user, loading, router]);

  if (loading || !user) {
    return (
      <div className="flex min-h-screen items-center justify-center text-ink-muted text-sm">
        Loading…
      </div>
    );
  }

  return (
    <SidebarProvider>
      <Sidebar collapsible="icon">
        <SidebarHeader>
          <div className="flex items-center gap-2 px-2 py-1.5">
            <span className="flex h-7 w-7 items-center justify-center rounded-full bg-surface-2">
              <Radar className="h-4 w-4 text-accent" />
            </span>
            <span className="font-semibold text-sm tracking-tight group-data-[collapsible=icon]:hidden">
              Catalyst Radar
            </span>
          </div>
        </SidebarHeader>
        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupContent>
              <SidebarMenu>
                {NAV.map(({ label, icon: Icon, href }) => {
                  const active = pathname === href;
                  return (
                    <SidebarMenuItem key={label}>
                      <SidebarMenuButton
                        asChild
                        isActive={active}
                        tooltip={label}
                      >
                        <Link
                          aria-current={active ? "page" : undefined}
                          href={href}
                        >
                          <Icon className="h-4 w-4" />
                          <span>{label}</span>
                        </Link>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  );
                })}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>
        <SidebarFooter>
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton onClick={logout} tooltip="Sign out">
                <LogOut className="h-4 w-4" />
                <span>Sign out</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarFooter>
      </Sidebar>
      <SidebarInset>
        <header className="flex h-14 shrink-0 items-center gap-2 border-border border-b px-4">
          <SidebarTrigger />
          <Separator className="mx-1 h-5" orientation="vertical" />
          <span className="font-medium text-sm">Catalyst Radar</span>
          <div className="ml-auto flex items-center gap-2">
            <span className="hidden truncate text-ink-muted text-xs sm:block">
              {user.email}
            </span>
            <Button
              aria-label="Sign out"
              className="md:hidden"
              onClick={logout}
              size="icon-sm"
              variant="ghost"
            >
              <LogOut className="h-4 w-4" />
            </Button>
          </div>
        </header>
        <main className="flex-1 overflow-y-auto p-4 sm:p-5">{children}</main>
      </SidebarInset>
      <Toaster position="bottom-right" richColors />
    </SidebarProvider>
  );
}
