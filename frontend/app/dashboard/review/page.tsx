"use client";

import { formatDistanceToNowStrict } from "date-fns";
import { ArrowUp, ChevronDown, RefreshCw, Send, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  type ColumnDef,
  DataTable,
  sortableHeader,
} from "@/components/ui/data-table";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import {
  bulkDecideEvents,
  decideEvent,
  type DecisionAction,
  type EventRecord,
  type FeedbackLabel,
  listIgnoredCatalysts,
  listReviewCatalysts,
  submitEventFeedback,
} from "@/lib/api";
import { formatTicker } from "@/lib/utils";

type Tab = "review" | "ignored";

function importanceRank(imp: string | undefined): number {
  // Sortable ranking: H > M > L > unknown — so descending sort surfaces
  // High first by default, which is what triage wants.
  if (imp === "H" || imp === "high") return 3;
  if (imp === "M" || imp === "medium") return 2;
  if (imp === "L" || imp === "low") return 1;
  return 0;
}

function classification(e: EventRecord): Record<string, unknown> {
  return (e.payload?.classification as Record<string, unknown>) ?? {};
}

function ignoreReason(e: EventRecord): string {
  const p = e.payload ?? {};
  const c = classification(e);
  return (
    (p.ignore_reason as string | undefined) ??
    (c.ignore_reason as string | undefined) ??
    "—"
  );
}

const FEEDBACK_OPTIONS: { value: FeedbackLabel; label: string }[] = [
  { value: "useful", label: "Useful" },
  { value: "not_useful", label: "Not useful" },
  { value: "false_positive", label: "False positive" },
  { value: "false_negative", label: "False negative" },
];

function feedbackOf(e: EventRecord): FeedbackLabel | undefined {
  const fb = e.payload?.feedback;
  return FEEDBACK_OPTIONS.some((o) => o.value === fb)
    ? (fb as FeedbackLabel)
    : undefined;
}

const IGNORED_PAGE_SIZE = 500;

export default function ReviewPage() {
  const [tab, setTab] = useState<Tab>("review");
  const [rows, setRows] = useState<EventRecord[]>([]);
  const [ignored, setIgnored] = useState<EventRecord[]>([]);
  // Server-side total so the tab badge and "load more" reflect the full
  // count, not just what we've fetched into memory.
  const [ignoredTotal, setIgnoredTotal] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setSelected(new Set());
    try {
      const [r, ig] = await Promise.all([
        listReviewCatalysts(),
        listIgnoredCatalysts(IGNORED_PAGE_SIZE),
      ]);
      setRows(r);
      setIgnored(ig.rows);
      setIgnoredTotal(ig.total);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadMoreIgnored = useCallback(async () => {
    setLoadingMore(true);
    try {
      const next = await listIgnoredCatalysts(IGNORED_PAGE_SIZE, ignored.length);
      setIgnored((cur) => [...cur, ...next.rows]);
      setIgnoredTotal(next.total);
    } catch (e) {
      toast.error(
        `Load more failed: ${e instanceof Error ? e.message : "unknown"}`
      );
    } finally {
      setLoadingMore(false);
    }
  }, [ignored.length]);

  useEffect(() => {
    // Initial data fetch — external-system sync, the intended use of an effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  // Switching tabs clears selection in the onClick — a leftover Review
  // selection would silently apply Send/Ignore semantics on the Ignored tab.
  const switchTab = useCallback((t: Tab) => {
    setTab(t);
    setSelected(new Set());
  }, []);

  const activeRows = tab === "review" ? rows : ignored;
  const setActive = tab === "review" ? setRows : setIgnored;

  const decide = useCallback(
    async (
      event: EventRecord,
      action: DecisionAction,
      undoAction: DecisionAction,
      verb: string
    ) => {
      // Optimistic dismiss — drop the row immediately so the queue stays
      // responsive. Snapshot the current source list so undo / rollback
      // can re-insert it without a refetch.
      const fromTab: Tab = tab;
      const sourceList = fromTab === "review" ? rows : ignored;
      const setList = fromTab === "review" ? setRows : setIgnored;
      const snapshot = sourceList;
      setList(sourceList.filter((r) => r.id !== event.id));
      setSelected((s) => {
        const next = new Set(s);
        next.delete(event.id);
        return next;
      });

      try {
        await decideEvent(event.id, action);
        toast(`${verb} ${event.company_name ?? formatTicker(event.symbol)}`, {
          duration: 5000,
          action: {
            label: "Undo",
            onClick: () => {
              setList(snapshot);
              void decideEvent(event.id, undoAction).catch((e) => {
                toast.error(
                  `Undo failed: ${e instanceof Error ? e.message : "unknown"}`
                );
              });
            },
          },
        });
      } catch (e) {
        setList(snapshot); // rollback
        toast.error(
          `${verb} failed: ${e instanceof Error ? e.message : "unknown"}`
        );
      }
    },
    [tab, rows, ignored]
  );

  const send = useCallback(
    (e: EventRecord) => decide(e, "send", "restore_to_review", "Sent"),
    [decide]
  );
  const ignore = useCallback(
    (e: EventRecord) => decide(e, "ignore", "restore_to_review", "Ignored"),
    [decide]
  );
  const promote = useCallback(
    (e: EventRecord) => decide(e, "promote", "restore_to_ignored", "Promoted"),
    [decide]
  );

  const setFeedback = useCallback(
    async (event: EventRecord, label: FeedbackLabel) => {
      // Optimistic: paint the new label into the row's payload so the
      // dropdown reflects it immediately; the server persists it into
      // payload.feedback, so a reload shows the same state.
      const setList = tab === "review" ? setRows : setIgnored;
      const apply =
        (fb: unknown) =>
        (cur: EventRecord[]): EventRecord[] =>
          cur.map((r) => {
            if (r.id !== event.id) {
              return r;
            }
            // No prior label → remove the key entirely on rollback rather
            // than writing `feedback: undefined` into the payload.
            const payload = { ...(r.payload ?? {}) };
            if (fb === undefined) {
              delete payload.feedback;
            } else {
              payload.feedback = fb;
            }
            return { ...r, payload };
          });
      const previous = event.payload?.feedback;
      setList(apply(label));
      try {
        await submitEventFeedback(event.id, label);
      } catch (e) {
        setList(apply(previous)); // rollback
        toast.error(
          `Feedback failed: ${e instanceof Error ? e.message : "unknown"}`
        );
      }
    },
    [tab]
  );

  const bulkDecide = useCallback(
    async (action: DecisionAction, verb: string) => {
      const ids = Array.from(selected);
      if (ids.length === 0) {
        return;
      }
      const snapshot = activeRows;
      // Optimistic: remove all selected rows.
      setActive(activeRows.filter((r) => !selected.has(r.id)));
      setSelected(new Set());
      try {
        const res = await bulkDecideEvents(ids, action);
        if (res.failed.length > 0) {
          // Restore failed rows from snapshot so the UI is consistent.
          const failedIds = new Set(res.failed.map((f) => f.id));
          setActive((cur) => {
            const failedRows = snapshot.filter((r) => failedIds.has(r.id));
            return [...failedRows, ...cur];
          });
          toast.warning(
            `${verb} ${res.ok.length}/${ids.length} — ${res.failed.length} failed`
          );
        } else {
          toast(`${verb} ${res.ok.length}`);
        }
      } catch (e) {
        setActive(snapshot);
        toast.error(
          `Bulk ${verb.toLowerCase()} failed: ${e instanceof Error ? e.message : "unknown"}`
        );
      }
    },
    [selected, activeRows, setActive]
  );

  const columns = useMemo<ColumnDef<EventRecord, unknown>[]>(() => {
    const select: ColumnDef<EventRecord, unknown> = {
      id: "select",
      enableHiding: false,
      header: () => {
        const allChecked =
          activeRows.length > 0 && selected.size === activeRows.length;
        const someChecked = selected.size > 0 && !allChecked;
        return (
          <Checkbox
            aria-label="Select all"
            checked={allChecked || (someChecked ? "indeterminate" : false)}
            onCheckedChange={(v) => {
              if (v) {
                setSelected(new Set(activeRows.map((r) => r.id)));
              } else {
                setSelected(new Set());
              }
            }}
          />
        );
      },
      cell: ({ row }) => {
        const id = row.original.id;
        return (
          <Checkbox
            aria-label={`Select ${row.original.company_name ?? id}`}
            checked={selected.has(id)}
            onCheckedChange={(v) =>
              setSelected((cur) => {
                const next = new Set(cur);
                if (v) {
                  next.add(id);
                } else {
                  next.delete(id);
                }
                return next;
              })
            }
          />
        );
      },
    };

    const company: ColumnDef<EventRecord, unknown> = {
      accessorKey: "company_name",
      header: sortableHeader<EventRecord>("Company"),
      cell: ({ row }) => (
        <div className="flex max-w-[220px] flex-col whitespace-normal">
          <span className="font-semibold text-sm">
            {row.original.company_name ?? formatTicker(row.original.symbol)}
          </span>
          {row.original.symbol && row.original.company_name && (
            <span className="font-mono text-ink-muted text-xs">
              {formatTicker(row.original.symbol)}
            </span>
          )}
        </div>
      ),
    };

    const type: ColumnDef<EventRecord, unknown> = {
      id: "type",
      header: "Type",
      cell: ({ row }) => {
        const c = classification(row.original);
        return (
          <Badge variant="accent">
            {String(c.event_subtype ?? "catalyst")}
          </Badge>
        );
      },
    };

    const headline: ColumnDef<EventRecord, unknown> = {
      id: "headline",
      header: "Headline",
      cell: ({ row }) => {
        const c = classification(row.original);
        const why = String(c.why_it_matters ?? "");
        // TableCell defaults to whitespace-nowrap, which would stretch this
        // column to fit the full headline on one line and blow out the table
        // width. Cap it and let the text wrap (title clamped to 2 lines).
        return (
          <div className="flex max-w-[480px] flex-col gap-0.5 whitespace-normal">
            <span className="line-clamp-2 text-sm leading-snug">
              {row.original.title ?? "—"}
            </span>
            {why && (
              <span className="line-clamp-1 text-ink-muted text-xs">{why}</span>
            )}
            {row.original.source_url && (
              <a
                className="text-accent text-xs hover:underline"
                href={row.original.source_url}
                onClick={(e) => e.stopPropagation()}
                rel="noreferrer"
                target="_blank"
              >
                source
              </a>
            )}
          </div>
        );
      },
    };

    const importance: ColumnDef<EventRecord, unknown> = {
      id: "importance",
      header: sortableHeader<EventRecord>("Imp"),
      // accessor used by the sort comparator only; the cell renders the
      // human-readable badge below.
      accessorFn: (e) =>
        importanceRank(
          (classification(e).importance as string | undefined) ?? undefined
        ),
      cell: ({ row }) => {
        const c = classification(row.original);
        const imp = c.importance as string | undefined;
        const conf = c.confidence as number | string | undefined;
        if (!imp) {
          return <span className="text-ink-muted text-xs">—</span>;
        }
        return (
          <Badge variant="neutral">
            {String(imp)} · {String(conf ?? "?")}
          </Badge>
        );
      },
    };

    const reason: ColumnDef<EventRecord, unknown> = {
      id: "reason",
      header: "Reason",
      cell: ({ row }) => (
        <Badge variant="neutral">{ignoreReason(row.original)}</Badge>
      ),
    };

    const age: ColumnDef<EventRecord, unknown> = {
      accessorKey: "created_at",
      header: sortableHeader<EventRecord>("Age"),
      cell: ({ row }) => (
        <span className="whitespace-nowrap text-ink-muted text-xs">
          {formatDistanceToNowStrict(new Date(row.original.created_at), {
            addSuffix: false,
          })}
        </span>
      ),
    };

    const feedback: ColumnDef<EventRecord, unknown> = {
      id: "feedback",
      header: "Feedback",
      cell: ({ row }) => {
        const current = feedbackOf(row.original);
        const currentLabel = FEEDBACK_OPTIONS.find(
          (o) => o.value === current
        )?.label;
        return (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                className={current ? "" : "text-ink-muted"}
                size="sm"
                variant="ghost"
              >
                {currentLabel ?? "Rate"}
                <ChevronDown className="h-3.5 w-3.5" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuRadioGroup
                onValueChange={(v) =>
                  void setFeedback(row.original, v as FeedbackLabel)
                }
                value={current ?? ""}
              >
                {FEEDBACK_OPTIONS.map((o) => (
                  <DropdownMenuRadioItem key={o.value} value={o.value}>
                    {o.label}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        );
      },
    };

    const actions: ColumnDef<EventRecord, unknown> =
      tab === "review"
        ? {
            id: "actions",
            enableHiding: false,
            header: "",
            cell: ({ row }) => (
              <div className="flex shrink-0 justify-end gap-1">
                <Button onClick={() => send(row.original)} size="sm">
                  <Send className="h-3.5 w-3.5" />
                  Send
                </Button>
                <Button
                  onClick={() => ignore(row.original)}
                  size="sm"
                  variant="ghost"
                >
                  <X className="h-3.5 w-3.5" />
                  Ignore
                </Button>
              </div>
            ),
          }
        : {
            id: "actions",
            enableHiding: false,
            header: "",
            cell: ({ row }) => (
              <div className="flex shrink-0 justify-end">
                <Button onClick={() => promote(row.original)} size="sm">
                  <ArrowUp className="h-3.5 w-3.5" />
                  Promote
                </Button>
              </div>
            ),
          };

    if (tab === "review") {
      return [select, company, type, headline, importance, feedback, age, actions];
    }
    return [select, company, type, headline, reason, feedback, age, actions];
  }, [activeRows, selected, tab, send, ignore, promote, setFeedback]);

  const oldestAge =
    activeRows.length === 0
      ? null
      : formatDistanceToNowStrict(
          new Date(
            Math.min(...activeRows.map((r) => new Date(r.created_at).getTime()))
          ),
          { addSuffix: false }
        );

  return (
    <div className="mx-auto max-w-6xl space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-semibold text-xl tracking-tight">
            Catalyst Review
          </h1>
          <p className="text-ink-muted text-sm">
            {tab === "review"
              ? "Classified catalysts below the auto-send bar. Send to fire Telegram; ignore to dismiss."
              : "Catalysts dropped by the prefilter. Promote anything that should have alerted."}
          </p>
        </div>
        <Button onClick={load} size="sm" variant="secondary">
          <RefreshCw className="h-3.5 w-3.5" />
          Refresh
        </Button>
      </div>

      <div className="flex items-center gap-1 border-border border-b">
        {(["review", "ignored"] as Tab[]).map((t) => {
          // Ignored uses the server total so the badge reflects the full
          // queue even when only the first page is loaded.
          const count = t === "review" ? rows.length : ignoredTotal;
          return (
            <button
              className={`-mb-px border-b-2 px-3 py-1.5 text-sm capitalize ${
                tab === t
                  ? "border-accent text-ink"
                  : "border-transparent text-ink-muted hover:text-ink"
              }`}
              key={t}
              onClick={() => switchTab(t)}
              type="button"
            >
              {t} ({count})
            </button>
          );
        })}
        {oldestAge && (
          <span className="ml-auto text-ink-muted text-xs">
            oldest {oldestAge}
          </span>
        )}
      </div>

      {error && <p className="text-destructive text-sm">{error}</p>}

      {selected.size > 0 && (
        <div className="flex items-center justify-between rounded-[12px] border border-border bg-surface-1 p-3">
          <span className="text-sm">
            {selected.size} selected
            <button
              className="ml-2 text-ink-muted text-xs hover:text-ink"
              onClick={() => setSelected(new Set())}
              type="button"
            >
              clear
            </button>
          </span>
          <div className="flex gap-2">
            {tab === "review" ? (
              <>
                <Button
                  onClick={() => bulkDecide("send", "Sent")}
                  size="sm"
                >
                  <Send className="h-3.5 w-3.5" />
                  Send all
                </Button>
                <Button
                  onClick={() => bulkDecide("ignore", "Ignored")}
                  size="sm"
                  variant="secondary"
                >
                  Ignore all
                </Button>
              </>
            ) : (
              <Button onClick={() => bulkDecide("promote", "Promoted")} size="sm">
                <ArrowUp className="h-3.5 w-3.5" />
                Promote all
              </Button>
            )}
          </div>
        </div>
      )}

      {loading ? (
        <div className="space-y-2" role="status" aria-label="Loading review queue">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
        </div>
      ) : (
        <>
          <DataTable
            columns={columns}
            data={activeRows}
            emptyMessage={
              tab === "review"
                ? "Review queue is empty."
                : "No ignored catalysts yet."
            }
            initialSorting={
              tab === "review"
                ? [{ id: "importance", desc: true }]
                : [{ id: "created_at", desc: true }]
            }
            minWidth={900}
          />
          {tab === "ignored" && ignored.length < ignoredTotal && (
            <div className="flex items-center justify-center gap-3 pt-2 text-ink-muted text-xs">
              <span>
                Showing {ignored.length} of {ignoredTotal}
              </span>
              <Button
                disabled={loadingMore}
                onClick={loadMoreIgnored}
                size="sm"
                variant="secondary"
              >
                {loadingMore ? "Loading…" : "Load more"}
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
