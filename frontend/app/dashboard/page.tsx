"use client";

import {
  Bell,
  Building2,
  CalendarClock,
  Radar,
  Rocket,
  Send,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { type DashboardSummary, getDashboardSummary } from "@/lib/api";

export default function DashboardPage() {
  const { user } = useAuth();
  const [s, setS] = useState<DashboardSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setS(await getDashboardSummary());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load summary");
    }
  }, []);

  useEffect(() => {
    // Initial fetch — external-system sync, the intended use of an effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  const metrics = [
    {
      label: "Tracked companies",
      icon: Building2,
      value: s?.tracked_companies,
    },
    {
      label: "Upcoming earnings (tracked)",
      icon: CalendarClock,
      value: s?.earnings_events,
    },
    {
      label: "Upcoming IPOs (tracked)",
      icon: Rocket,
      value: s?.ipo_events,
    },
    {
      label: "Catalysts in review",
      icon: Radar,
      value: s?.catalysts_in_review,
    },
  ];

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div>
        <h1 className="font-semibold text-xl tracking-tight">Dashboard</h1>
        <p className="text-ink-muted text-sm">
          Signed in as {user?.email}. Operational overview across all sources.
        </p>
      </div>

      {error && <p className="text-destructive text-sm">{error}</p>}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {metrics.map(({ label, icon: Icon, value }) => (
          <Card key={label}>
            <CardHeader>
              <CardTitle className="flex items-center justify-between text-ink-muted">
                <span className="font-medium text-xs uppercase tracking-wide">
                  {label}
                </span>
                <Icon className="h-4 w-4" />
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="font-semibold text-2xl tabular-nums">
                {value ?? "—"}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Last source run</CardTitle>
            <CardDescription>Most recent ingestion job.</CardDescription>
          </CardHeader>
          <CardContent>
            {s?.last_source_run ? (
              <div className="space-y-2 text-sm">
                <Row label="Source">
                  <span className="font-mono text-xs">
                    {s.last_source_run.source_name}
                  </span>
                </Row>
                <Row label="Status">
                  <Badge
                    variant={
                      s.last_source_run.status === "success"
                        ? "success"
                        : s.last_source_run.status === "failed"
                          ? "danger"
                          : "neutral"
                    }
                  >
                    {s.last_source_run.status}
                  </Badge>
                </Row>
                <Row label="Items">
                  <span className="text-ink-muted tabular-nums">
                    {s.last_source_run.item_count}
                  </span>
                </Row>
              </div>
            ) : (
              <p className="text-ink-muted text-sm">No source runs yet.</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center justify-between">
              Telegram
              <Send className="h-4 w-4 text-ink-muted" />
            </CardTitle>
            <CardDescription>
              Subscribers receiving alerts (multi-subscriber).
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="flex items-center gap-2 text-sm">
              <Bell className="h-4 w-4 text-ink-muted" />
              <span className="tabular-nums">{s?.telegram_chats ?? "—"}</span>
              <span className="text-ink-muted">active chat(s)</span>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function Row({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between border-hairline-soft border-b pb-2 last:border-0 last:pb-0">
      <span className="text-ink-muted">{label}</span>
      {children}
    </div>
  );
}
