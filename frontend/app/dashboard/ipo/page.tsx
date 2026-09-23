"use client";

import { RefreshCw, Save } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  type ColumnDef,
  DataTable,
  sortableHeader,
} from "@/components/ui/data-table";
import { DatePicker } from "@/components/ui/date-picker";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { DEFAULT_PAGE_SIZE } from "@/hooks/use-pagination";
import {
  type EventPeriod,
  type EventRecord,
  getAppSettings,
  getIpoCoverage,
  type IpoCoverage,
  listEvents,
  updateAppSettings,
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
  | "Listed"
  | "Country off"
  | "Unmapped"
  | "No date";

const STATUS_VARIANT: Record<
  RowStatus,
  "success" | "neutral" | "warning" | "accent"
> = {
  Alerting: "success",
  Scheduled: "accent",
  Listed: "neutral",
  "Country off": "neutral",
  Unmapped: "warning",
  "No date": "neutral",
};

function fmtNum(v: unknown): string {
  const n = typeof v === "string" ? Number(v) : (v as number);
  if (v == null || v === "" || Number.isNaN(n)) {
    return "—";
  }
  return n.toLocaleString();
}

function str(v: unknown): string {
  return v == null || v === "" ? "—" : String(v);
}

type CoverageRow = NonNullable<IpoCoverage["exchanges"]>[number];

const coverageColumns: ColumnDef<CoverageRow, unknown>[] = [
  {
    accessorKey: "exchange",
    header: "Exchange",
    cell: ({ row }) => (
      <span className="font-mono text-xs">
        {row.original.exchange ?? "∅ (none)"}
      </span>
    ),
  },
  {
    accessorKey: "source",
    header: "Source",
    cell: ({ row }) => (
      <span className="font-mono text-xs text-ink-muted">
        {row.original.source ?? "—"}
      </span>
    ),
  },
  {
    accessorKey: "country",
    header: "Country",
    cell: ({ row }) =>
      row.original.country ? (
        <Badge variant="accent">{row.original.country}</Badge>
      ) : (
        <Badge variant="warning">unmapped</Badge>
      ),
  },
  {
    accessorKey: "count",
    header: sortableHeader<CoverageRow>("Events"),
    cell: ({ row }) => (
      <span className="text-ink-muted">{row.original.count}</span>
    ),
  },
  {
    id: "status",
    header: "Status",
    cell: ({ row }) => {
      const x = row.original;
      if (!x.country) {
        return <Badge variant="warning">unmapped</Badge>;
      }
      return x.alerting ? (
        <Badge variant="success">alerting</Badge>
      ) : (
        <Badge variant="neutral">country off</Badge>
      );
    },
  },
];

export default function IpoFiltersPage() {
  const [coverage, setCoverage] = useState<IpoCoverage | null>(null);
  const [enabled, setEnabled] = useState<string[]>([]);
  const [windows, setWindows] = useState<number[]>([]);
  const [rows, setRows] = useState<EventRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mappingOpen, setMappingOpen] = useState(false);

  // Date filter UI state
  const [period, setPeriod] = useState<EventPeriod>("all");
  const [fromDate, setFromDate] = useState<string>("");
  const [toDate, setToDate] = useState<string>("");

  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

  // Reset to page 0 when filters change so pageIndex can't outrun pageCount.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPageIndex(0);
  }, [period, fromDate, toDate, pageSize]);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [s, ipos, cov] = await Promise.all([
        getAppSettings(),
        listEvents("ipo", {
          period,
          dateFrom: fromDate || undefined,
          dateTo: toDate || undefined,
          limit: pageSize,
          offset: pageIndex * pageSize,
        }),
        getIpoCoverage(),
      ]);
      setEnabled(
        s.eodhd_ipo_enabled_countries
          .split(",")
          .map((c) => c.trim())
          .filter(Boolean)
      );
      setWindows(parseWindows(s.ipo_alert_windows_days));
      setRows(ipos.rows);
      setTotal(ipos.total);
      setCoverage(cov);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load");
    }
  }, [period, fromDate, toDate, pageIndex, pageSize]);

  useEffect(() => {
    // Initial fetch — external-system sync, the intended use of an effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  // Every country that the exchange map can resolve to, plus whatever is
  // currently enabled — so the toggles always cover the real surface.
  const allCountries = useMemo(() => {
    const set = new Set<string>(enabled);
    if (coverage) {
      for (const c of Object.values(coverage.exchange_country_map)) {
        set.add(c);
      }
    }
    return [...set].sort();
  }, [coverage, enabled]);

  const mapForExchange = useCallback(
    (exchange: string | null): string | null => {
      if (!(exchange && coverage)) {
        return null;
      }
      return coverage.exchange_country_map[exchange.toUpperCase()] ?? null;
    },
    [coverage]
  );

  const maxWindow = windows.length > 0 ? Math.max(...windows) : 0;

  const rowStatus = useCallback(
    (e: EventRecord): RowStatus => {
      const country = mapForExchange(e.exchange);
      if (!country) {
        return "Unmapped";
      }
      if (!enabled.includes(country)) {
        return "Country off";
      }
      if (!e.event_date) {
        return "No date";
      }
      const d = daysUntil(e.event_date);
      if (d === null) {
        return "No date";
      }
      if (d < 0) {
        return "Listed";
      }
      // Mirrors backend _pick_window: 0 <= days_until <= max_window.
      if (d <= maxWindow) {
        return "Alerting";
      }
      return "Scheduled";
    },
    [enabled, mapForExchange, maxWindow]
  );

  async function save() {
    setSaving(true);
    setMsg(null);
    setError(null);
    try {
      await updateAppSettings({
        eodhd_ipo_enabled_countries: enabled.join(","),
      });
      setMsg("Saved.");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  const alertingCount = rows.filter(
    (e) => rowStatus(e) === "Alerting"
  ).length;

  const pageCount = Math.max(1, Math.ceil(total / pageSize));

  const eventColumns = useMemo<ColumnDef<EventRecord, unknown>[]>(
    () => [
      {
        accessorKey: "company_name",
        header: sortableHeader<EventRecord>("Company"),
        cell: ({ row }) => {
          const e = row.original;
          const prof = (e.payload?.profile ?? {}) as {
            description?: string;
          };
          // Cap the cell width and force descriptions to wrap. The shadcn
          // TableCell defaults to whitespace-nowrap, which would otherwise
          // stretch the column to fit the full description on one line.
          return (
            <div className="w-[280px] max-w-[320px] whitespace-normal">
              {e.source_url ? (
                <a
                  className="text-accent hover:underline"
                  href={e.source_url}
                  rel="noreferrer"
                  target="_blank"
                >
                  {e.company_name ?? formatTicker(e.symbol)}
                </a>
              ) : (
                (e.company_name ?? formatTicker(e.symbol))
              )}
              {prof.description && (
                <p className="mt-0.5 text-ink-muted text-xs leading-snug">
                  {prof.description}
                </p>
              )}
            </div>
          );
        },
      },
      {
        accessorKey: "symbol",
        header: "Symbol",
        cell: ({ row }) => (
          <span className="font-mono text-xs">
            {formatTicker(row.original.symbol)}
          </span>
        ),
      },
      {
        accessorKey: "exchange",
        header: "Exchange",
        cell: ({ row }) => (
          <span className="font-mono text-ink-muted text-xs">
            {str(row.original.exchange)}
          </span>
        ),
      },
      {
        id: "country",
        header: "Country",
        cell: ({ row }) => {
          const country = mapForExchange(row.original.exchange);
          return country ? (
            <Badge variant="accent">{country}</Badge>
          ) : (
            <Badge variant="warning">?</Badge>
          );
        },
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
        id: "range",
        header: "Offer / range",
        cell: ({ row }) => {
          const p = (row.original.payload ?? {}) as Record<string, unknown>;
          const cur = str(p.currency);
          const range =
            p.offer_price != null && p.offer_price !== ""
              ? `${cur} ${fmtNum(p.offer_price)}`
              : p.price_from || p.price_to
                ? `${cur} ${fmtNum(p.price_from)}–${fmtNum(p.price_to)}`
                : "—";
          return <span className="text-ink-muted">{range}</span>;
        },
      },
      {
        id: "shares",
        header: "Shares",
        cell: ({ row }) => {
          const p = (row.original.payload ?? {}) as Record<string, unknown>;
          return <span className="text-ink-muted">{fmtNum(p.shares)}</span>;
        },
      },
      {
        id: "deal_type",
        header: "Deal",
        cell: ({ row }) => {
          const p = (row.original.payload ?? {}) as Record<string, unknown>;
          return <span className="text-ink-muted">{str(p.deal_type)}</span>;
        },
      },
      {
        id: "filed",
        header: "Filed",
        cell: ({ row }) => {
          const e = row.original;
          const p = (e.payload ?? {}) as Record<string, unknown>;
          const prof = (p.profile ?? {}) as { filing_date?: unknown };
          // EODHD's "Expected" rows mirror start_date into filing_date/
          // amended_date. Prefer the real S-1 date from EDGAR enrichment
          // (profile.filing_date); fall back to payload.filing_date only
          // when it differs from the listing date.
          const profFd =
            typeof prof.filing_date === "string"
              ? prof.filing_date.slice(0, 10)
              : null;
          const rawFd =
            typeof p.filing_date === "string"
              ? p.filing_date.slice(0, 10)
              : null;
          const ld = e.event_date ? e.event_date.slice(0, 10) : null;
          const out = profFd ?? (rawFd && rawFd !== ld ? rawFd : null);
          return (
            <span className="text-ink-muted">
              {out ? formatDate(out) : "—"}
            </span>
          );
        },
      },
      {
        accessorKey: "event_date",
        header: sortableHeader<EventRecord>("Listing"),
        cell: ({ row }) => (
          <span className="text-ink-muted">
            {formatDate(row.original.event_date)}
          </span>
        ),
      },
    ],
    [mapForExchange, rowStatus]
  );

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="font-semibold text-xl tracking-tight">
            IPO Coverage & Filters
          </h1>
          <p className="text-ink-muted text-sm">
            EODHD reports exchanges as free text (e.g.{" "}
            <code className="text-ink">HKSE</code>,{" "}
            <code className="text-ink">Shanghai</code>). Each is mapped to a
            country; only enabled countries generate alerts.
          </p>
        </div>
        <Button onClick={load} size="sm" variant="secondary">
          <RefreshCw className="h-3.5 w-3.5" />
          Reload
        </Button>
      </div>

      {error && <p className="text-destructive text-sm">{error}</p>}

      {/* Enabled countries */}
      <div className="rounded-[15px] border border-border bg-card p-5">
        <div className="mb-3">
          <h2 className="font-semibold text-sm">Enabled countries</h2>
          <p className="text-ink-muted text-xs">
            An IPO alerts only if its mapped country is on and it has a listing
            date inside a reminder window.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <ToggleGroup
            onValueChange={(v) => setEnabled(v)}
            size="sm"
            type="multiple"
            value={enabled}
            variant="outline"
          >
            {allCountries.map((c) => (
              <ToggleGroupItem aria-label={c} key={c} value={c}>
                {c}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
          <Button disabled={saving} onClick={save} size="sm">
            <Save className="h-3.5 w-3.5" />
            {saving ? "Saving…" : "Save"}
          </Button>
          {msg && <span className="text-success text-xs">{msg}</span>}
        </div>
      </div>

      {/* Coverage by exchange */}
      <div className="rounded-[15px] border border-border bg-card p-5">
        <div className="mb-3">
          <h2 className="font-semibold text-sm">Coverage by exchange</h2>
          <p className="text-ink-muted text-xs">
            What EODHD actually returned, how it maps, and whether it alerts.
          </p>
        </div>
        <DataTable
          columns={coverageColumns}
          data={coverage?.exchanges ?? []}
          emptyMessage="No IPO events yet. Run the IPO sync job."
          enableColumnVisibility={false}
          pageSize={10}
        />

        {coverage && (
          <Collapsible
            className="mt-4"
            onOpenChange={setMappingOpen}
            open={mappingOpen}
          >
            <CollapsibleTrigger asChild>
              <button className="cursor-pointer text-ink-muted text-xs hover:underline">
                Full exchange → country mapping (
                {Object.keys(coverage.exchange_country_map).length} entries)
              </button>
            </CollapsibleTrigger>
            <CollapsibleContent className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-3 md:grid-cols-4">
              {Object.entries(coverage.exchange_country_map).map(([ex, c]) => (
                <div
                  className="flex items-center justify-between text-xs"
                  key={ex}
                >
                  <span className="font-mono">{ex}</span>
                  <span className="text-ink-muted">{c}</span>
                </div>
              ))}
            </CollapsibleContent>
          </Collapsible>
        )}
      </div>

      {/* IPO events — detailed */}
      <div className="space-y-2">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <h2 className="font-semibold text-sm">
            IPO events{" "}
            <span className="text-ink-muted">
              ({total.toLocaleString()} matching · {alertingCount} alerting on this page)
            </span>
          </h2>
        </div>
        {windows.length > 0 && (
          <p className="text-ink-muted text-xs">
            Alerting = listing within configured windows ({windows.join(", ")}{" "}
            days). Past listings show as “Listed”; further-out ones as
            “Scheduled”.
          </p>
        )}
        <DataTable
          columns={eventColumns}
          data={rows}
          emptyMessage={
            total === 0
              ? "No IPOs match this filter."
              : "No IPOs on this page."
          }
          minWidth={1040}
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
