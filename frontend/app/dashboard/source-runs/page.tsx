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
import { listSourceRuns, type SourceRun } from "@/lib/api";
import { formatDateTime } from "@/lib/utils";

const VARIANT: Record<string, "success" | "danger" | "warning" | "neutral"> = {
  success: "success",
  failed: "danger",
  rate_limited: "warning",
  empty: "warning",
  running: "neutral",
  skipped: "neutral",
};

export default function SourceRunsPage() {
  const [rows, setRows] = useState<SourceRun[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setRows(await listSourceRuns());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    }
  }, []);

  useEffect(() => {
    // Initial fetch — external-system sync, the intended use of an effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  const columns = useMemo<ColumnDef<SourceRun, unknown>[]>(
    () => [
      {
        accessorKey: "source_name",
        header: sortableHeader<SourceRun>("Source"),
        cell: ({ row }) => (
          <span className="font-mono text-xs">{row.original.source_name}</span>
        ),
      },
      {
        accessorKey: "status",
        header: "Status",
        cell: ({ row }) => (
          <Badge variant={VARIANT[row.original.status] ?? "neutral"}>
            {row.original.status}
          </Badge>
        ),
      },
      {
        accessorKey: "item_count",
        header: sortableHeader<SourceRun>("Items"),
        cell: ({ row }) => (
          <span className="tabular-nums">{row.original.item_count}</span>
        ),
      },
      {
        id: "url_probes",
        header: "URL probes",
        cell: ({ row }) => {
          const probes = row.original.summary?.url_probes;
          if (!probes) {
            return <span className="text-ink-muted">—</span>;
          }
          const breakdown = (counts: Record<string, number>) =>
            Object.entries(counts)
              .map(([code, n]) => `${code}×${n}`)
              .join(", ");
          const keptDetail = breakdown(probes.kept_non_2xx);
          const droppedDetail = breakdown(probes.dropped_status);
          return (
            <span className="text-xs tabular-nums">
              {probes.kept} kept{keptDetail ? ` (${keptDetail})` : ""} ·{" "}
              {probes.dropped} dropped
              {droppedDetail ? ` (${droppedDetail})` : ""}
            </span>
          );
        },
      },
      {
        accessorKey: "started_at",
        header: sortableHeader<SourceRun>("Started"),
        cell: ({ row }) => (
          <span className="text-ink-muted">
            {formatDateTime(row.original.started_at)}
          </span>
        ),
      },
      {
        id: "last_error",
        header: "Last error",
        // Error strings can be very long; cap the column and wrap/clamp so a
        // single failing run doesn't blow out the table width. Full text on
        // hover.
        cell: ({ row }) => (
          <div
            className="line-clamp-2 max-w-[360px] whitespace-normal text-ink-muted"
            title={row.original.last_error ?? undefined}
          >
            {row.original.last_error ?? "—"}
          </div>
        ),
      },
    ],
    []
  );

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-semibold text-xl tracking-tight">Source Runs</h1>
          <p className="text-ink-muted text-sm">
            Ingestion health. A failed run records its error without overwriting
            prior good data.
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
        emptyMessage="No source runs yet."
        initialSorting={[{ id: "started_at", desc: true }]}
        minWidth={640}
      />
    </div>
  );
}
