"use client";

import { Sparkles, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
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
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Table, TableBody, TableCell, TableRow } from "@/components/ui/table";
import {
  addCompanySource,
  type CompanySource,
  deleteCompanySource,
  discoverCompanySources,
  type FetchStrategy,
  listCompanySources,
  type SourceKind,
  updateCompanySource,
} from "@/lib/api";

const KINDS: SourceKind[] = [
  "rss",
  "ir_press",
  "blog",
  "sec",
  "hkex",
  "twitter",
];
const STRATEGIES: FetchStrategy[] = ["auto", "static", "browser", "agent"];

const STATUS_VARIANT: Record<
  string,
  "success" | "danger" | "warning" | "neutral"
> = {
  active: "success",
  broken: "danger",
  structure_changed: "warning",
  disabled: "neutral",
};

export function CompanySourcesPanel({ trackedId }: { trackedId: number }) {
  const [rows, setRows] = useState<CompanySource[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [kind, setKind] = useState<SourceKind>("rss");
  const [url, setUrl] = useState("");
  const [label, setLabel] = useState("");
  const [strategy, setStrategy] = useState<FetchStrategy>("auto");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setRows(await listCompanySources(trackedId, true));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load sources");
    } finally {
      setLoading(false);
    }
  }, [trackedId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  async function add() {
    if (!url.trim()) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await addCompanySource(trackedId, {
        kind,
        url: url.trim(),
        label: label.trim() || null,
        fetch_strategy: strategy,
      });
      setUrl("");
      setLabel("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Add failed");
    } finally {
      setBusy(false);
    }
  }

  async function discover() {
    setBusy(true);
    setError(null);
    try {
      const s = await discoverCompanySources(trackedId);
      if (s.created === 0) {
        setError(`Discovery ran (${s.provider}): no new sources found.`);
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Discovery failed");
    } finally {
      setBusy(false);
    }
  }

  async function toggleActive(s: CompanySource) {
    try {
      await updateCompanySource(s.id, { is_active: !s.is_active });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Update failed");
    }
  }

  async function remove(s: CompanySource) {
    try {
      await deleteCompanySource(s.id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    }
  }

  return (
    <div className="space-y-3 bg-surface-1/40 px-4 py-3">
      <div className="flex items-center justify-between">
        <span className="font-medium text-ink-muted text-xs uppercase tracking-wide">
          Primary sources
        </span>
        <Button
          disabled={busy}
          onClick={discover}
          size="sm"
          variant="secondary"
        >
          <Sparkles className="h-3.5 w-3.5" />
          Discover
        </Button>
      </div>

      {error && <p className="text-sm text-warning">{error}</p>}

      {loading ? (
        <p className="text-ink-muted text-sm">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-ink-muted text-sm">
          No sources yet. Add a feed/URL below or run Discover.
        </p>
      ) : (
        <Table>
          <TableBody>
            {rows.map((s) => (
              <TableRow key={s.id}>
                <TableCell className="py-1.5 pr-2">
                  <Badge variant="neutral">{s.kind}</Badge>
                </TableCell>
                <TableCell className="py-1.5 pr-2">
                  {/* Cap + truncate so a long URL doesn't stretch the table;
                      badges stay visible (shrink-0). */}
                  <div className="flex max-w-[460px] items-center gap-2">
                    <a
                      className="min-w-0 truncate text-accent hover:underline"
                      href={s.url}
                      rel="noreferrer"
                      target="_blank"
                    >
                      {s.label || s.url}
                    </a>
                    {s.needs_review && (
                      <Badge className="shrink-0" variant="warning">
                        review
                      </Badge>
                    )}
                    {!s.is_active && (
                      <Badge className="shrink-0" variant="neutral">
                        inactive
                      </Badge>
                    )}
                  </div>
                </TableCell>
                <TableCell className="py-1.5 pr-2">
                  <Badge variant={STATUS_VARIANT[s.status] ?? "neutral"}>
                    {s.status}
                  </Badge>
                </TableCell>
                <TableCell className="py-1.5 pr-2 text-ink-muted">
                  {s.source}
                </TableCell>
                <TableCell className="py-1.5">
                  <div className="flex justify-end gap-1">
                    <Button
                      onClick={() => toggleActive(s)}
                      size="sm"
                      variant="ghost"
                    >
                      {s.is_active ? "Disable" : "Enable"}
                    </Button>
                    <AlertDialog>
                      <AlertDialogTrigger asChild>
                        <Button size="sm" variant="ghost">
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </AlertDialogTrigger>
                      <AlertDialogContent>
                        <AlertDialogHeader>
                          <AlertDialogTitle>Delete source?</AlertDialogTitle>
                          <AlertDialogDescription>
                            Permanently remove{" "}
                            <span className="font-mono text-xs">{s.url}</span>{" "}
                            from this company.
                          </AlertDialogDescription>
                        </AlertDialogHeader>
                        <AlertDialogFooter>
                          <AlertDialogCancel>Cancel</AlertDialogCancel>
                          <AlertDialogAction onClick={() => remove(s)}>
                            Delete
                          </AlertDialogAction>
                        </AlertDialogFooter>
                      </AlertDialogContent>
                    </AlertDialog>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <div className="flex flex-wrap items-center gap-2 pt-1">
        <Select onValueChange={(v) => setKind(v as SourceKind)} value={kind}>
          <SelectTrigger className="h-9 w-full sm:w-32">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {KINDS.map((k) => (
              <SelectItem key={k} value={k}>
                {k}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Input
          className="w-full sm:w-72"
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://company.com/ir/rss"
          value={url}
        />
        <Input
          className="w-full sm:w-40"
          onChange={(e) => setLabel(e.target.value)}
          placeholder="label (optional)"
          value={label}
        />
        <Select
          onValueChange={(v) => setStrategy(v as FetchStrategy)}
          value={strategy}
        >
          <SelectTrigger className="h-9 w-full sm:w-32">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {STRATEGIES.map((s) => (
              <SelectItem key={s} value={s}>
                {s}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button disabled={busy || !url.trim()} onClick={add} size="sm">
          Add source
        </Button>
      </div>
    </div>
  );
}
