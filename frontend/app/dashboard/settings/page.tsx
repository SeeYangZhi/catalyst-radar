"use client";

import { Download, RefreshCw, Save, Upload } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  type AppSettings,
  exportAppSettings,
  getAppSettings,
  importAppSettings,
  type SettingsExportDoc,
  updateAppSettings,
} from "@/lib/api";

type Field = { key: keyof AppSettings; label: string; kind: "bool" | "text" };

const SECTIONS: { title: string; hint: string; fields: Field[] }[] = [
  {
    title: "Delivery",
    hint: "Telegram behaviour. Takes effect immediately.",
    fields: [
      {
        key: "telegram_alerts_enabled",
        label: "Telegram alerts",
        kind: "bool",
      },
      {
        key: "telegram_polling_enabled",
        label: "Telegram command polling",
        kind: "bool",
      },
      { key: "registration_enabled", label: "Web registration", kind: "bool" },
    ],
  },
  {
    title: "Catalyst gate",
    hint: "Balanced gate + hybrid auto-send thresholds. Immediate.",
    fields: [
      { key: "catalyst_min_importance", label: "Min importance", kind: "text" },
      { key: "catalyst_min_confidence", label: "Min confidence", kind: "text" },
      {
        key: "catalyst_autosend_min_importance",
        label: "Auto-send min importance",
        kind: "text",
      },
      {
        key: "catalyst_autosend_min_confidence",
        label: "Auto-send min confidence",
        kind: "text",
      },
      {
        key: "catalyst_max_items_per_run",
        label: "Max LLM items / run",
        kind: "text",
      },
    ],
  },
  {
    title: "Catalyst sources",
    hint: "Which news paths feed the catalyst pipeline. Immediate.",
    fields: [
      {
        key: "catalyst_websearch_enabled",
        label: "Web_search news sweep",
        kind: "bool",
      },
      {
        key: "catalyst_websearch_all_markets",
        label: "Web_search: all tracked markets",
        kind: "bool",
      },
      {
        key: "catalyst_websearch_gap_exchanges",
        label: "Web_search gap exchanges",
        kind: "text",
      },
      {
        key: "catalyst_websearch_lookback_days",
        label: "Web_search lookback (days)",
        kind: "text",
      },
      {
        key: "catalyst_eastmoney_news_enabled",
        label: "Eastmoney CN news sweep",
        kind: "bool",
      },
      {
        key: "catalyst_mops_news_enabled",
        label: "MOPS Taiwan announcements sweep",
        kind: "bool",
      },
    ],
  },
  {
    title: "Dedup & noise",
    hint: "Duplicate-alert suppression. Immediate.",
    fields: [
      {
        key: "catalyst_dedup_llm_judge_enabled",
        label: "LLM same-story judge",
        kind: "bool",
      },
      {
        key: "catalyst_repeat_alert_window_hours",
        label: "Repeat-alert window (hours, 0 = off)",
        kind: "text",
      },
      {
        key: "catalyst_news_max_age_days",
        label: "Max news age (days)",
        kind: "text",
      },
      {
        key: "catalyst_drop_undated_news",
        label: "Drop undated news",
        kind: "bool",
      },
    ],
  },
  {
    title: "Coverage",
    hint: "Which markets/windows generate alerts. Immediate.",
    fields: [
      {
        key: "hk_ipo_source_aastocks_enabled",
        label: "HK IPO via AAStocks",
        kind: "bool",
      },
      {
        key: "eodhd_ipo_enabled_countries",
        label: "IPO countries",
        kind: "text",
      },
      {
        key: "eodhd_company_reference_enabled",
        label: "EODHD company reference sync",
        kind: "bool",
      },
      {
        key: "eodhd_company_reference_exchanges",
        label: "Reference exchanges",
        kind: "text",
      },
      {
        key: "earnings_alert_windows_days",
        label: "Earnings windows (days)",
        kind: "text",
      },
      {
        key: "ipo_alert_windows_days",
        label: "IPO windows (days)",
        kind: "text",
      },
      { key: "alert_timezone", label: "Alert timezone", kind: "text" },
    ],
  },
  {
    title: "IPO Filters",
    hint: "Narrow which IPOs generate alerts. Immediate. Empty = no filter.",
    fields: [
      {
        key: "ipo_exclude_etfs_trusts",
        label: "Exclude ETFs / Trusts / Funds",
        kind: "bool",
      },
      {
        key: "ipo_industry_keywords",
        label: "Industry keywords",
        kind: "text",
      },
      {
        key: "ipo_min_deal_size_usd",
        label: "Min deal size (USD)",
        kind: "text",
      },
      {
        key: "ipo_exchange_filter",
        label: "Exchange filter",
        kind: "text",
      },
    ],
  },
  {
    title: "Schedule",
    hint: "Read by the scheduler at startup — changes apply after a beat/worker restart.",
    fields: [
      {
        key: "eodhd_sync_interval_minutes",
        label: "EODHD calendar interval (min)",
        kind: "text",
      },
      {
        key: "catalyst_sync_interval_minutes",
        label: "Catalyst news interval (min)",
        kind: "text",
      },
      { key: "daily_digest_hour", label: "Daily digest hour", kind: "text" },
      { key: "weekly_digest_day", label: "Weekly digest day", kind: "text" },
      { key: "weekly_digest_hour", label: "Weekly digest hour", kind: "text" },
    ],
  },
];

export default function AlertSettingsPage() {
  const [s, setS] = useState<AppSettings | null>(null);
  const [draft, setDraft] = useState<Record<string, string | boolean>>({});
  const [restartKeys, setRestartKeys] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [importing, setImporting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getAppSettings();
      setS(data);
      setRestartKeys(data.restart_required_keys ?? []);
      const d: Record<string, string | boolean> = {};
      for (const sec of SECTIONS) {
        for (const f of sec.fields) {
          const v = data[f.key] as unknown;
          d[f.key as string] = f.kind === "bool" ? Boolean(v) : String(v);
        }
      }
      setDraft(d);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Initial fetch — external-system sync, the intended use of an effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  async function save() {
    setSaving(true);
    setMsg(null);
    setError(null);
    try {
      await updateAppSettings(
        draft as Record<string, string | number | boolean>
      );
      setMsg("Saved. Schedule changes need a scheduler restart.");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function exportConfig() {
    setMsg(null);
    setError(null);
    try {
      const doc = await exportAppSettings();
      const blob = new Blob([JSON.stringify(doc, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `catalyst-radar-settings-${new Date()
        .toISOString()
        .slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
      setMsg(`Exported ${Object.keys(doc.overrides).length} override(s).`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Export failed");
    }
  }

  async function importConfig(file: File) {
    setMsg(null);
    setError(null);
    setImporting(true);
    try {
      let doc: SettingsExportDoc;
      try {
        doc = JSON.parse(await file.text()) as SettingsExportDoc;
      } catch {
        throw new Error("Not a valid JSON file");
      }
      if (
        doc?.schema_version !== 1 ||
        typeof doc?.overrides !== "object" ||
        doc.overrides === null
      ) {
        throw new Error(
          "Not a settings export (expected schema_version: 1 + overrides)"
        );
      }
      const res = await importAppSettings(doc);
      setMsg(`Imported ${res.applied} override(s).`);
      await load();
    } catch (e) {
      // The backend 422 detail names the offending keys verbatim.
      setError(e instanceof Error ? e.message : "Import failed");
    } finally {
      setImporting(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-semibold text-xl tracking-tight">
            Alert Settings
          </h1>
          <p className="text-ink-muted text-sm">
            Runtime configuration ({s?.environment}). Persisted to the DB and
            applied without redeploy, except schedule keys.
          </p>
        </div>
        <Button onClick={load} size="sm" variant="secondary">
          <RefreshCw className="h-3.5 w-3.5" />
          Reload
        </Button>
      </div>

      {error && <p className="text-destructive text-sm">{error}</p>}
      {loading || !s ? (
        <p className="text-ink-muted text-sm">Loading…</p>
      ) : (
        <>
          {SECTIONS.map((sec) => (
            <div
              className="rounded-[15px] border border-border bg-card p-5"
              key={sec.title}
            >
              <div className="mb-3">
                <h2 className="font-semibold text-sm">{sec.title}</h2>
                <p className="text-ink-muted text-xs">{sec.hint}</p>
              </div>
              <div className="space-y-3">
                {sec.fields.map((f) => {
                  const k = f.key as string;
                  const restart = restartKeys.includes(k);
                  return (
                    <div
                      className="flex flex-col gap-1.5 sm:flex-row sm:items-center sm:justify-between"
                      key={k}
                    >
                      <span className="flex items-center gap-2 text-sm">
                        {f.label}
                        {restart && <Badge variant="warning">restart</Badge>}
                      </span>
                      {f.kind === "bool" ? (
                        <Switch
                          checked={Boolean(draft[k])}
                          onCheckedChange={(v) =>
                            setDraft((d) => ({ ...d, [k]: v }))
                          }
                        />
                      ) : (
                        <Input
                          className="sm:max-w-[220px]"
                          onChange={(e) =>
                            setDraft((d) => ({ ...d, [k]: e.target.value }))
                          }
                          value={String(draft[k] ?? "")}
                        />
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          ))}

          <div className="rounded-[15px] border border-border bg-card p-5">
            <div className="mb-3">
              <h2 className="font-semibold text-sm">Backup</h2>
              <p className="text-ink-muted text-xs">
                Export the saved overrides as JSON, or restore them after a DB
                migration. Import is all-or-nothing.
              </p>
            </div>
            <div className="flex items-center gap-3">
              <Button onClick={exportConfig} size="sm" variant="secondary">
                <Download className="h-3.5 w-3.5" />
                Export config
              </Button>
              <Button
                disabled={importing}
                onClick={() => fileInputRef.current?.click()}
                size="sm"
                variant="secondary"
              >
                <Upload className="h-3.5 w-3.5" />
                {importing ? "Importing…" : "Import config"}
              </Button>
              <input
                accept="application/json,.json"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  e.target.value = "";
                  if (file) {
                    void importConfig(file);
                  }
                }}
                ref={fileInputRef}
                type="file"
              />
            </div>
          </div>

          <div className="flex items-center gap-3">
            <Button disabled={saving} onClick={save}>
              <Save className="h-4 w-4" />
              {saving ? "Saving…" : "Save settings"}
            </Button>
            {msg && <span className="text-success text-xs">{msg}</span>}
          </div>
        </>
      )}
    </div>
  );
}
