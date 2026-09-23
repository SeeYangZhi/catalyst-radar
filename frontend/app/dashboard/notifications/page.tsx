"use client";

import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  type ColumnDef,
  DataTable,
  sortableHeader,
} from "@/components/ui/data-table";
import { listNotifications, type NotificationRecord } from "@/lib/api";
import { formatDateTime } from "@/lib/utils";

const STATUS_VARIANT: Record<
  string,
  "success" | "danger" | "warning" | "neutral"
> = {
  sent: "success",
  failed: "danger",
  skipped: "neutral",
  pending: "warning",
};

export default function NotificationsPage() {
  const [rows, setRows] = useState<NotificationRecord[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setRows(await listNotifications());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load");
    }
  }, []);

  useEffect(() => {
    // Initial fetch — external-system sync, the intended use of an effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  const columns = useMemo<ColumnDef<NotificationRecord, unknown>[]>(
    () => [
      {
        accessorKey: "status",
        header: "Status",
        cell: ({ row }) => (
          <Badge variant={STATUS_VARIANT[row.original.status] ?? "neutral"}>
            {row.original.status}
          </Badge>
        ),
      },
      {
        accessorKey: "channel",
        header: "Channel",
        cell: ({ row }) => (
          <span className="text-ink-muted">{row.original.channel}</span>
        ),
      },
      {
        accessorKey: "reminder_window",
        header: "Window",
        cell: ({ row }) => (
          <span className="text-ink-muted">
            {row.original.reminder_window ?? "—"}
          </span>
        ),
      },
      {
        accessorKey: "attempts",
        header: "Attempts",
        cell: ({ row }) => (
          <span className="tabular-nums">{row.original.attempts}</span>
        ),
      },
      {
        id: "detail",
        header: "Detail",
        // Error / skip-reason strings can be long; cap and wrap/clamp so they
        // don't stretch the table. Full text on hover.
        cell: ({ row }) => {
          const detail = row.original.error ?? row.original.skip_reason ?? null;
          return (
            <div
              className="line-clamp-2 max-w-[360px] whitespace-normal text-ink-muted"
              title={detail ?? undefined}
            >
              {detail ?? "—"}
            </div>
          );
        },
      },
      {
        accessorKey: "created_at",
        header: sortableHeader<NotificationRecord>("Created"),
        cell: ({ row }) => (
          <span className="text-ink-muted">
            {formatDateTime(row.original.created_at)}
          </span>
        ),
      },
    ],
    []
  );

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-semibold text-xl tracking-tight">
            Notifications
          </h1>
          <p className="text-ink-muted text-sm">
            Delivery log — every sent, skipped, or failed alert is recorded.
          </p>
        </div>
        <Button onClick={load} size="sm" variant="secondary">
          <RefreshCw className="h-3.5 w-3.5" />
          Refresh
        </Button>
      </div>

      {error && <p className="text-destructive text-sm">{error}</p>}

      <DataTable
        columns={columns}
        data={rows}
        emptyMessage="No notifications yet."
        initialSorting={[{ id: "created_at", desc: true }]}
        minWidth={640}
      />
    </div>
  );
}
