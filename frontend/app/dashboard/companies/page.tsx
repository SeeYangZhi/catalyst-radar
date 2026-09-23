"use client";

import { ChevronDown, ChevronRight, RefreshCw, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  type ColumnDef,
  DataTable,
  sortableHeader,
} from "@/components/ui/data-table";
import {
  getAppSettings,
  listTrackedCompanies,
  type TrackedCompany,
  untrackCompany,
} from "@/lib/api";
import { formatTicker, hasNewsCoverageGap } from "@/lib/utils";
import { CompanySourcesPanel } from "./company-sources";

export default function TrackedCompaniesPage() {
  const [rows, setRows] = useState<TrackedCompany[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  // Taiwan listings only show the "no news feed" badge when the MOPS
  // material-information sweep is disabled. Default true = badge
  // hidden until settings load — MOPS is on by default.
  const [mopsEnabled, setMopsEnabled] = useState(true);

  useEffect(() => {
    getAppSettings()
      .then((s) => setMopsEnabled(s.catalyst_mops_news_enabled))
      .catch(() => undefined); // badge stays hidden on a failed load
  }, []);

  const load = useCallback(async () => {
    setError(null);
    try {
      setRows(await listTrackedCompanies());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load");
    }
  }, []);

  useEffect(() => {
    // Initial data fetch — external-system sync, the intended use of an effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  const remove = useCallback(
    async (id: number, purge: boolean) => {
      try {
        await untrackCompany(id, purge);
        await load();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Remove failed");
      }
    },
    [load]
  );

  const columns = useMemo<ColumnDef<TrackedCompany, unknown>[]>(
    () => [
      {
        id: "expand",
        header: "",
        enableHiding: false,
        cell: ({ row }) => {
          const id = row.original.id;
          const open = expanded === id;
          return (
            <button
              aria-label={open ? "Collapse sources" : "Expand sources"}
              className="rounded-md p-1 text-ink-muted hover:bg-secondary"
              onClick={() => setExpanded(open ? null : id)}
              type="button"
            >
              {open ? (
                <ChevronDown className="h-4 w-4" />
              ) : (
                <ChevronRight className="h-4 w-4" />
              )}
            </button>
          );
        },
      },
      {
        accessorKey: "symbol",
        header: sortableHeader<TrackedCompany>("Symbol"),
        cell: ({ row }) => (
          <span className="font-mono">{formatTicker(row.original.symbol)}</span>
        ),
      },
      {
        accessorKey: "company_name",
        header: sortableHeader<TrackedCompany>("Company"),
        // Cap + wrap so a long company name doesn't stretch the table
        // (TableCell defaults to whitespace-nowrap).
        cell: ({ row }) => (
          <div className="max-w-[260px] whitespace-normal">
            {row.original.company_name}
          </div>
        ),
      },
      {
        accessorKey: "exchange",
        header: "Exchange",
        cell: ({ row }) => (
          <span className="inline-flex items-center gap-1.5 text-ink-muted">
            {row.original.exchange}
            {hasNewsCoverageGap(row.original.exchange, mopsEnabled) && (
              <Badge
                title="The MOPS announcements sweep is disabled and EODHD has no news coverage for Taiwan listings — catalyst alerts rely on the web_search sweep alone (Settings → MOPS Taiwan announcements)."
                variant="warning"
              >
                no news feed
              </Badge>
            )}
          </span>
        ),
      },
      {
        accessorKey: "source",
        header: "Source",
        cell: ({ row }) => (
          <Badge variant="neutral">{row.original.source}</Badge>
        ),
      },
      {
        id: "actions",
        header: "",
        enableHiding: false,
        cell: ({ row }) => (
          <div className="flex justify-end gap-2">
            <Button
              onClick={() => remove(row.original.id, false)}
              size="sm"
              variant="ghost"
            >
              Remove
            </Button>
            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button size="sm" variant="destructive">
                  <Trash2 className="h-3.5 w-3.5" />
                  Delete data
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>
                    Delete {row.original.company_name}?
                  </AlertDialogTitle>
                  <AlertDialogDescription>
                    This removes the tracked company and purges all locally
                    stored events, notifications, and source rows for it. This
                    cannot be undone.
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                  <AlertDialogAction
                    onClick={() => remove(row.original.id, true)}
                  >
                    Delete
                  </AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          </div>
        ),
      },
    ],
    [expanded, remove, mopsEnabled]
  );

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-semibold text-xl tracking-tight">
            Tracked Companies
          </h1>
          <p className="text-ink-muted text-sm">
            Active universe driving earnings and catalyst alerts.
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
        emptyMessage="No tracked companies yet. Add some from Company Search."
        minWidth={640}
        renderSubRow={(c) =>
          expanded === c.id ? <CompanySourcesPanel trackedId={c.id} /> : null
        }
      />
    </div>
  );
}
