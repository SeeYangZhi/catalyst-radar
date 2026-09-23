"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Plus, Search as SearchIcon } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { type ColumnDef, DataTable } from "@/components/ui/data-table";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import {
  type CompanyReference,
  getAppSettings,
  searchCompanies,
  trackCompany,
} from "@/lib/api";
import { formatTicker, hasNewsCoverageGap } from "@/lib/utils";

const SearchSchema = z.object({
  q: z.string().trim().min(1, "Enter a ticker or company name"),
  country: z.string().trim().max(8, "Too long"),
  exchange: z.string().trim().max(8, "Too long"),
});
type SearchValues = z.infer<typeof SearchSchema>;

const ManualAddSchema = z.object({
  symbol: z.string().trim().min(1, "Required").max(64, "Too long"),
  exchange: z.string().trim().min(1, "Required").max(32, "Too long"),
  country: z.string().trim().max(8, "Too long").optional(),
  company_name: z.string().trim().min(1, "Required").max(256, "Too long"),
});
type ManualAddValues = z.infer<typeof ManualAddSchema>;

export default function CompanySearchPage() {
  const [results, setResults] = useState<CompanyReference[]>([]);
  const [source, setSource] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tracked, setTracked] = useState<Record<string, string>>({});
  // Taiwan listings only show the "no news feed" badge when the MOPS
  // material-information sweep is disabled. Default true = badge
  // hidden until settings load — MOPS is on by default.
  const [mopsEnabled, setMopsEnabled] = useState(true);

  useEffect(() => {
    getAppSettings()
      .then((s) => setMopsEnabled(s.catalyst_mops_news_enabled))
      .catch(() => undefined); // badge stays hidden on a failed load
  }, []);

  const form = useForm<SearchValues>({
    resolver: zodResolver(SearchSchema),
    defaultValues: { q: "", country: "", exchange: "" },
  });
  const { isSubmitting } = form.formState;

  const [manualState, setManualState] = useState<
    { kind: "ok"; symbol: string } | { kind: "err"; message: string } | null
  >(null);
  const manualForm = useForm<ManualAddValues>({
    resolver: zodResolver(ManualAddSchema),
    defaultValues: { symbol: "", exchange: "", country: "", company_name: "" },
  });
  const { isSubmitting: isAdding } = manualForm.formState;

  async function onManualAdd(values: ManualAddValues) {
    setManualState(null);
    try {
      const tc = await trackCompany({
        symbol: values.symbol,
        exchange: values.exchange,
        country: values.country || null,
        company_name: values.company_name,
      });
      setManualState({ kind: "ok", symbol: tc.symbol });
      manualForm.reset();
    } catch (err) {
      setManualState({
        kind: "err",
        message: err instanceof Error ? err.message : "Add failed",
      });
    }
  }

  async function onSubmit(values: SearchValues) {
    setError(null);
    // Drop badges from the previous query so a stale "tracked" pill
    // doesn't bleed onto a new result set the user is interpreting fresh.
    setTracked({});
    try {
      const res = await searchCompanies(values);
      setResults(res.results);
      setSource(res.source);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Search failed");
    }
  }

  async function onTrack(c: CompanyReference) {
    const key = `${c.exchange}:${c.symbol}`;
    try {
      await trackCompany(
        c.id
          ? { company_reference_id: c.id }
          : {
              symbol: c.symbol,
              exchange: c.exchange,
              country: c.country,
              company_name: c.company_name,
            }
      );
      setTracked((t) => ({ ...t, [key]: "tracked" }));
    } catch (err) {
      setTracked((t) => ({
        ...t,
        [key]: err instanceof Error ? err.message : "failed",
      }));
    }
  }

  const columns = useMemo<ColumnDef<CompanyReference, unknown>[]>(
    () => [
      {
        accessorKey: "symbol",
        header: "Symbol",
        cell: ({ row }) => (
          <span className="font-mono">{formatTicker(row.original.symbol)}</span>
        ),
      },
      {
        accessorKey: "company_name",
        header: "Company",
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
        accessorKey: "country",
        header: "Country",
        cell: ({ row }) => (
          <span className="text-ink-muted">{row.original.country ?? "—"}</span>
        ),
      },
      {
        id: "action",
        header: "",
        enableHiding: false,
        cell: ({ row }) => {
          const c = row.original;
          const key = `${c.exchange}:${c.symbol}`;
          const state = tracked[key];
          if (state === "tracked") {
            return <Badge variant="success">tracked</Badge>;
          }
          if (state) {
            return <span className="text-destructive text-xs">{state}</span>;
          }
          return (
            <Button onClick={() => onTrack(c)} size="sm" variant="secondary">
              <Plus className="h-3.5 w-3.5" />
              Track
            </Button>
          );
        },
      },
    ],
    [tracked, mopsEnabled]
  );

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <div>
        <h1 className="font-semibold text-xl tracking-tight">Company Search</h1>
        <p className="text-ink-muted text-sm">
          Search the preloaded universe. Falls back to EODHD search when the
          local reference misses and an API key is set.
        </p>
      </div>

      <Form {...form}>
        <form
          className="flex flex-wrap items-start gap-2"
          onSubmit={form.handleSubmit(onSubmit)}
        >
          <FormField
            control={form.control}
            name="q"
            render={({ field }) => (
              <FormItem className="w-full max-w-xs">
                <FormLabel className="sr-only">Query</FormLabel>
                <FormControl>
                  <Input placeholder="Ticker or company name" {...field} />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
          <FormField
            control={form.control}
            name="country"
            render={({ field }) => (
              <FormItem className="w-[160px]">
                <FormLabel className="sr-only">Country</FormLabel>
                <FormControl>
                  <Input placeholder="Country (US, HK, KR)" {...field} />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
          <FormField
            control={form.control}
            name="exchange"
            render={({ field }) => (
              <FormItem className="w-[180px]">
                <FormLabel className="sr-only">Exchange</FormLabel>
                <FormControl>
                  <Input placeholder="Exchange (US, HK, KO, KQ)" {...field} />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
          <Button disabled={isSubmitting} type="submit">
            <SearchIcon className="h-4 w-4" />
            {isSubmitting ? "Searching…" : "Search"}
          </Button>
        </form>
      </Form>

      {error && <p className="text-destructive text-sm">{error}</p>}

      {source && (
        <div className="flex items-center gap-2 text-ink-muted text-xs">
          <span>Source</span>
          <Badge variant={source === "reference" ? "neutral" : "accent"}>
            {source}
          </Badge>
          <span>· {results.length} results</span>
        </div>
      )}

      <DataTable
        columns={columns}
        data={results}
        emptyMessage="No results. Run a search above."
        minWidth={640}
      />

      <div className="space-y-3 border-border border-t pt-5">
        <div>
          <h2 className="font-semibold text-lg tracking-tight">Add manually</h2>
          <p className="text-ink-muted text-sm">
            For names the search universe doesn&apos;t cover — pre-IPO Chinese
            companies (CSRC reservation codes like{" "}
            <span className="font-mono">A25310</span>), unlisted private firms,
            or rare exchanges. Pre-IPO rows skip the EODHD news path and are
            covered by web_search instead.
          </p>
        </div>
        <Form {...manualForm}>
          <form
            className="flex flex-wrap items-start gap-2"
            onSubmit={manualForm.handleSubmit(onManualAdd)}
          >
            <FormField
              control={manualForm.control}
              name="symbol"
              render={({ field }) => (
                <FormItem className="w-[160px]">
                  <FormLabel className="sr-only">Symbol</FormLabel>
                  <FormControl>
                    <Input placeholder="Symbol (e.g. A25310)" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={manualForm.control}
              name="exchange"
              render={({ field }) => (
                <FormItem className="w-[140px]">
                  <FormLabel className="sr-only">Exchange</FormLabel>
                  <FormControl>
                    <Input placeholder="Exchange (SSE)" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={manualForm.control}
              name="country"
              render={({ field }) => (
                <FormItem className="w-[120px]">
                  <FormLabel className="sr-only">Country</FormLabel>
                  <FormControl>
                    <Input placeholder="Country (CN)" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={manualForm.control}
              name="company_name"
              render={({ field }) => (
                <FormItem className="w-full max-w-xs">
                  <FormLabel className="sr-only">Company name</FormLabel>
                  <FormControl>
                    <Input placeholder="Company name" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <Button disabled={isAdding} type="submit" variant="secondary">
              <Plus className="h-4 w-4" />
              {isAdding ? "Adding…" : "Add"}
            </Button>
          </form>
        </Form>
        {manualState?.kind === "ok" && (
          <div className="flex items-center gap-2 text-sm">
            <Badge variant="success">tracked</Badge>
            <span className="font-mono">{manualState.symbol}</span>
          </div>
        )}
        {manualState?.kind === "err" && (
          <p className="text-destructive text-sm">{manualState.message}</p>
        )}
      </div>
    </div>
  );
}
