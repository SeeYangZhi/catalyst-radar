import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatTicker(value: string | null | undefined): string {
  if (value == null) {
    return "—";
  }
  const s = value.trim();
  if (!s) {
    return "—";
  }
  // Strip any leading $ first then add exactly one — keeps the function
  // idempotent even when callers double-up (e.g. embedding $ in a
  // template literal AND passing through formatTicker), and tolerates
  // legacy payloads where an upstream LLM leaked "$" into the ticker.
  const bare = s.replace(/^\$+/, "");
  return bare ? `$${bare}` : "—";
}

// All user-facing dates render dd/mm/yyyy regardless of locale. Accepts
// ISO datetimes ("2026-05-27T00:00:00Z"), bare yyyy-mm-dd, or Date objects.
// Bare yyyy-mm-dd is parsed as a calendar date (no tz drift); ISO strings
// are read in local time.
export function formatDate(value: string | Date | null | undefined): string {
  if (value == null || value === "") {
    return "—";
  }
  let d: Date;
  if (value instanceof Date) {
    d = value;
  } else if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const [y, m, day] = value.split("-").map(Number);
    d = new Date(y, m - 1, day);
  } else {
    d = new Date(value);
  }
  if (Number.isNaN(d.getTime())) {
    return "—";
  }
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()}`;
}

// Exchanges where EODHD's /news endpoint returns no items (verified
// live: 6451.TW / 6451.TWO → 0 items). These listings are normally
// covered by the MOPS material-information sweep (their authoritative
// primary source); the coverage gap only reopens when that sweep is
// disabled, leaving web_search as the sole news path.
const NEWS_GAP_EXCHANGES = new Set(["TW", "TWO", "TWSE", "TPEX"]);

export function hasNewsCoverageGap(
  exchange: string | null | undefined,
  mopsNewsEnabled = true
): boolean {
  return (
    !mopsNewsEnabled &&
    !!exchange &&
    NEWS_GAP_EXCHANGES.has(exchange.trim().toUpperCase())
  );
}

// Parse an "14,7,1"-style alert-window spec into day offsets (>= 0).
export function parseWindows(spec: string): number[] {
  return spec
    .split(",")
    .map((s) => Number(s.trim()))
    .filter((n) => Number.isFinite(n) && n >= 0);
}

// Whole-day delta from today to `iso`, compared by calendar day in local tz —
// matches the backend's _pick_window local-day delta, so a "days until" badge
// agrees with what the alert pipeline would do now. null if no date.
export function daysUntil(iso: string | null): number | null {
  if (!iso) {
    return null;
  }
  const target = new Date(iso);
  const tDay = new Date(
    target.getFullYear(),
    target.getMonth(),
    target.getDate()
  );
  const now = new Date();
  const nDay = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((tDay.getTime() - nDay.getTime()) / 86_400_000);
}

export function formatDateTime(
  value: string | Date | null | undefined
): string {
  if (value == null || value === "") {
    return "—";
  }
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) {
    return "—";
  }
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
