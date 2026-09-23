"use client";

import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  type ColumnDef,
  DataTable,
  sortableHeader,
} from "@/components/ui/data-table";
import { DatePicker } from "@/components/ui/date-picker";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DEFAULT_PAGE_SIZE } from "@/hooks/use-pagination";
import {
  type AppSettings,
  type EventPeriod,
  type EventRecord,
  getAppSettings,
  listEvents,
  listTrackedCompanies,
} from "@/lib/api";
import {
  daysUntil,
  formatDate,
  formatTicker,
  parseWindows,
} from "@/lib/utils";

type RowStatus =
  | "Alerting"
  | "Scheduled"
  | "Reported"
  | "Not tracked"
  | "No date";

const STATUS_VARIANT: Record<
  RowStatus,
  "success" | "neutral" | "accent" | "warning"
> = {
  Alerting: "success",
  Scheduled: "accent",
  Reported: "neutral",
  "Not tracked": "neutral",
  "No date": "neutral",
};

export default function EarningsPage() {
  const [rows, setRows] = useState<EventRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  // (exchange|symbol) keys for every tracked company — drives whether
  // an event-table row is actually alertable (the backend only fires
  // Telegram for matched tracked tickers; an untracked row in the
  // global calendar is never alertable no matter how close the date).
  const [trackedKeys, setTrackedKeys] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  const [period, setPeriod] = useState<EventPeriod>("upcoming");
  const [trackedOnly, setTrackedOnly] = useState(true);
  const [fromDate, setFromDate] = useState<string>("");
  const [toDate, setToDate] = useState<string>("");

  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

  // Reset to page 0 whenever a filter changes — otherwise pageIndex
  // could point past the new (smaller) page count.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPageIndex(0);
  }, [period, trackedOnly, fromDate, toDate, pageSize]);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [s, ev, tracked] = await Promise.all([
        getAppSettings(),
        listEvents("earnings", {
          relevant: trackedOnly,
          period,
          dateFrom: fromDate || undefined,
          dateTo: toDate || undefined,
          limit: pageSize,
          offset: pageIndex * pageSize,
        }),
        listTrackedCompanies(),
      ]);
      setSettings(s);
      setRows(ev.rows);
      setTotal(ev.total);
      setTrackedKeys(
        new Set(
          tracked.map(
            (tc) =>
              `${(tc.exchange || "").toUpperCase()}|${(tc.symbol || "").toUpperCase()}`
          )
        )
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load");
    }
  }, [trackedOnly, period, fromDate, toDate, pageIndex, pageSize]);

  useEffect(() => {
    // Initial fetch — external-system sync, the intended use of an effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  const windows = useMemo(
    () => (settings ? parseWindows(settings.earnings_alert_windows_days) : []),
    [settings]
  );
  const maxWindow = windows.length > 0 ? Math.max(...windows) : 0;

  const rowStatus = useCallback(
    (e: EventRecord): RowStatus => {
      const d = daysUntil(e.event_date);
      if (d === null) {
        return "No date";
      }
      if (d < 0) {
        return "Reported";
      }
      // Backend only fires Telegram alerts for matched tracked tickers;
      // untracked global-calendar rows are never alertable.
      const key = `${(e.exchange || "").toUpperCase()}|${(e.symbol || "").toUpperCase()}`;
      if (!trackedKeys.has(key)) {
        return "Not tracked";
      }
      if (d <= maxWindow) {
        return "Alerting";
      }
      return "Scheduled";
    },
    [maxWindow, trackedKeys]
  );

  const alertingCount = rows.filter(
    (e) => rowStatus(e) === "Alerting"
  ).length;

  const columns = useMemo<ColumnDef<EventRecord, unknown>[]>(
    () => [
      {
        accessorKey: "symbol",
        header: "Symbol",
        cell: ({ row }) => {
          const p = (row.original.payload ?? {}) as Record<string, unknown>;
          return (
            <span className="font-mono">
              {formatTicker(
                (p.code as string | undefined) ?? row.original.symbol
              )}
            </span>
          );
        },
      },
      {
        accessorKey: "event_date",
        header: sortableHeader<EventRecord>("Report date"),
        cell: ({ row }) => formatDate(row.original.event_date),
      },
      {
        id: "status",
        header: "Status",
        cell: ({ row }) => {
          const st = rowStatus(row.original);
          return <Badge variant={STATUS_VARIANT[st]}>{st}</Badge>;
        },
      },
      {
        id: "timing",
        header: "Timing",
        cell: ({ row }) => {
          const p = (row.original.payload ?? {}) as Record<string, unknown>;
          return (
            <span className="text-ink-muted">
              {String(p.before_after_market ?? "—")}
            </span>
          );
        },
      },
      {
        id: "estimate",
        header: "Consensus EPS",
        cell: ({ row }) => {
          const p = (row.original.payload ?? {}) as Record<string, unknown>;
          return (
            <span className="tabular-nums">
              {p.estimate == null ? "—" : String(p.estimate)}
            </span>
          );
        },
      },
    ],
    [rowStatus]
  );

  const pageCount = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-semibold text-xl tracking-tight">Earnings</h1>
          <p className="text-ink-muted text-sm">
            Upcoming earnings for your tracked companies. Toggle “Tracked only”
            off to see the full EODHD calendar.
          </p>
        </div>
        <Button onClick={load} size="sm" variant="secondary">
          <RefreshCw className="h-3.5 w-3.5" />
          Refresh
        </Button>
      </div>

      {error && <p className="text-destructive text-sm">{error}</p>}

      <div className="space-y-2">
        <h2 className="font-semibold text-sm">
          Earnings{" "}
          <span className="text-ink-muted">
            ({total.toLocaleString()} matching · {alertingCount} alerting on this page)
          </span>
        </h2>
        {windows.length > 0 && (
          <p className="text-ink-muted text-xs">
            Telegram alerts fire only for <b>tracked</b> companies when the
            report date enters a configured window ({windows.join(", ")} days),
            once per window. Untracked rows in the global calendar show as
            “Not tracked” and never alert.
          </p>
        )}
        <DataTable
          columns={columns}
          data={rows}
          emptyMessage={
            total === 0
              ? "No earnings match this filter."
              : "No earnings on this page."
          }
          minWidth={640}
          serverPagination={{
            pageIndex,
            pageSize,
            pageCount,
            total,
            onChange: ({ pageIndex: pi, pageSize: ps }) => {
              setPageIndex(pi);
              setPageSize(ps);
            },
          }}
          toolbar={
            <>
              <Tabs
                onValueChange={(v) => setPeriod(v as EventPeriod)}
                value={period}
              >
                <TabsList>
                  <TabsTrigger value="all">All</TabsTrigger>
                  <TabsTrigger value="upcoming">Upcoming</TabsTrigger>
                  <TabsTrigger value="past">Past</TabsTrigger>
                </TabsList>
              </Tabs>
              <label className="flex items-center gap-2 text-ink-muted text-xs">
                <Checkbox
                  checked={trackedOnly}
                  onCheckedChange={(v) => setTrackedOnly(v === true)}
                />
                Tracked only
              </label>
              <span className="text-ink-muted text-xs">From</span>
              <DatePicker onChange={setFromDate} value={fromDate} />
              <span className="text-ink-muted text-xs">To</span>
              <DatePicker onChange={setToDate} value={toDate} />
            </>
          }
        />
      </div>
    </div>
  );
}
