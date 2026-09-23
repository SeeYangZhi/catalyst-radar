export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

const TOKEN_KEY = "cr_token";

export function getToken(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_KEY);
}

export interface CurrentUser {
  created_at: string;
  email: string;
  full_name: string | null;
  id: number;
  is_active: boolean;
  role: string;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const res = await fetch(`${API_URL}${path}`, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    // Token expired or revoked — clear it and bounce to login so the
    // user isn't stuck on a page that silently no-ops every action.
    if (
      res.status === 401 &&
      typeof window !== "undefined" &&
      window.location.pathname !== "/login"
    ) {
      clearToken();
      window.location.href = "/login";
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) {
    return undefined as T;
  }
  return (await res.json()) as T;
}

export async function login(email: string, password: string): Promise<string> {
  const form = new URLSearchParams();
  form.set("username", email);
  form.set("password", password);

  const res = await fetch(`${API_URL}/auth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: form,
  });
  if (!res.ok) {
    let detail = "Login failed";
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  const data = (await res.json()) as { access_token: string };
  return data.access_token;
}

export function getMe(): Promise<CurrentUser> {
  return apiFetch<CurrentUser>("/me");
}

export interface AppSettings {
  alert_timezone: string;
  app_name: string;
  catalyst_autosend_min_confidence: number;
  catalyst_autosend_min_importance: string;
  catalyst_dedup_llm_judge_enabled: boolean;
  catalyst_drop_undated_news: boolean;
  catalyst_eastmoney_news_enabled: boolean;
  catalyst_max_items_per_run: number;
  catalyst_min_confidence: number;
  catalyst_min_importance: string;
  catalyst_mops_news_enabled: boolean;
  catalyst_news_max_age_days: number;
  catalyst_repeat_alert_window_hours: number;
  catalyst_websearch_all_markets: boolean;
  catalyst_websearch_enabled: boolean;
  catalyst_websearch_gap_exchanges: string;
  catalyst_websearch_lookback_days: number;
  daily_digest_hour: number;
  earnings_alert_windows_days: string;
  environment: string;
  eodhd_company_reference_enabled: boolean;
  eodhd_company_reference_exchanges: string;
  eodhd_ipo_enabled_countries: string;
  eodhd_sync_interval_minutes: number;
  catalyst_sync_interval_minutes: number;
  hk_ipo_source_aastocks_enabled: boolean;
  ipo_alert_windows_days: string;
  ipo_exchange_filter: string;
  ipo_exclude_etfs_trusts: boolean;
  ipo_industry_keywords: string;
  ipo_min_deal_size_usd: number;
  registration_enabled: boolean;
  restart_required_keys: string[];
  telegram_alerts_enabled: boolean;
  telegram_polling_enabled: boolean;
  weekly_digest_day: string;
  weekly_digest_hour: number;
}

export function getAppSettings(): Promise<AppSettings> {
  return apiFetch<AppSettings>("/settings");
}

export function updateAppSettings(
  body: Record<string, string | number | boolean>
): Promise<AppSettings> {
  return apiFetch<AppSettings>("/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export interface SettingsExportDoc {
  schema_version: number;
  overrides: Record<string, unknown>;
}

export interface SettingsImportResult {
  applied: number;
}

export function exportAppSettings(): Promise<SettingsExportDoc> {
  return apiFetch<SettingsExportDoc>("/settings/export");
}

export function importAppSettings(
  doc: SettingsExportDoc
): Promise<SettingsImportResult> {
  return apiFetch<SettingsImportResult>("/settings/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(doc),
  });
}

export interface EventRecord {
  company_name: string | null;
  country: string | null;
  created_at: string;
  event_date: string | null;
  event_type: string;
  exchange: string | null;
  id: number;
  payload: Record<string, unknown> | null;
  source_url: string | null;
  status: string;
  symbol: string | null;
  title: string | null;
}

export interface PaginatedEvents {
  rows: EventRecord[];
  total: number;
  limit: number;
  offset: number;
}

export type EventPeriod = "all" | "upcoming" | "past";

export function listEvents(
  eventType?: string,
  opts: {
    relevant?: boolean;
    limit?: number;
    offset?: number;
    period?: EventPeriod;
    dateFrom?: string; // ISO yyyy-mm-dd
    dateTo?: string; // ISO yyyy-mm-dd
  } = {}
): Promise<PaginatedEvents> {
  const qs = new URLSearchParams();
  if (eventType) {
    qs.set("event_type", eventType);
  }
  if (opts.relevant) {
    qs.set("relevant", "true");
  }
  if (opts.limit) {
    qs.set("limit", String(opts.limit));
  }
  if (opts.offset) {
    qs.set("offset", String(opts.offset));
  }
  if (opts.period && opts.period !== "all") {
    qs.set("period", opts.period);
  }
  if (opts.dateFrom) {
    qs.set("date_from", opts.dateFrom);
  }
  if (opts.dateTo) {
    qs.set("date_to", opts.dateTo);
  }
  const s = qs.toString();
  return apiFetch<PaginatedEvents>(`/events${s ? `?${s}` : ""}`);
}

export function listReviewCatalysts(): Promise<EventRecord[]> {
  return apiFetch<EventRecord[]>("/events/review");
}

export function listIgnoredCatalysts(
  limit = 500,
  offset = 0
): Promise<PaginatedEvents> {
  return apiFetch<PaginatedEvents>(
    `/events/ignored?limit=${limit}&offset=${offset}`
  );
}

export interface IpoExchangeRow {
  alerting: boolean;
  count: number;
  country: string | null;
  exchange: string | null;
  source: string | null;
}

export interface IpoCoverage {
  enabled_countries: string[];
  exchange_country_map: Record<string, string>;
  exchanges: IpoExchangeRow[];
}

export function getIpoCoverage(): Promise<IpoCoverage> {
  return apiFetch<IpoCoverage>("/events/ipo/coverage");
}

export type DecisionAction =
  | "send"
  | "ignore"
  | "promote"
  | "restore_to_review"
  | "restore_to_ignored";

export interface DecideResponse {
  event: EventRecord;
  notification_id: number | null;
}

export function decideEvent(
  eventId: number,
  action: DecisionAction
): Promise<DecideResponse> {
  return apiFetch<DecideResponse>(`/events/${eventId}/decide`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
}

export type FeedbackLabel =
  | "useful"
  | "not_useful"
  | "false_positive"
  | "false_negative";

export function submitEventFeedback(
  eventId: number,
  label: FeedbackLabel
): Promise<EventRecord> {
  return apiFetch<EventRecord>(`/events/${eventId}/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label }),
  });
}

export interface BulkDecideResponse {
  ok: number[];
  failed: { id: number; reason: string }[];
}

export function bulkDecideEvents(
  ids: number[],
  action: DecisionAction
): Promise<BulkDecideResponse> {
  return apiFetch<BulkDecideResponse>("/events/decide", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids, action }),
  });
}

export interface UrlProbeStats {
  dropped: number;
  dropped_status: Record<string, number>;
  kept: number;
  kept_non_2xx: Record<string, number>;
}

export interface SourceRun {
  error_count: number;
  finished_at: string | null;
  id: number;
  item_count: number;
  last_error: string | null;
  source_name: string;
  started_at: string;
  status: string;
  summary: { url_probes?: UrlProbeStats } | null;
}

export function listSourceRuns(): Promise<SourceRun[]> {
  return apiFetch<SourceRun[]>("/source-runs");
}

export interface DashboardSummary {
  catalysts_in_review: number;
  earnings_events: number;
  ipo_events: number;
  last_source_run: {
    source_name: string;
    status: string;
    started_at: string;
    item_count: number;
  } | null;
  telegram_chats: number;
  tracked_companies: number;
}

export function getDashboardSummary(): Promise<DashboardSummary> {
  return apiFetch<DashboardSummary>("/dashboard/summary");
}

export interface CompanyReference {
  company_name: string;
  country: string | null;
  currency: string | null;
  exchange: string;
  id: number;
  industry: string | null;
  isin: string | null;
  sector: string | null;
  source: string;
  symbol: string;
}

export interface TrackedCompany {
  aliases: string[];
  company_name: string;
  company_reference_id: number | null;
  country: string | null;
  created_at: string;
  entity_id: number | null;
  exchange: string;
  id: number;
  is_active: boolean;
  is_parent_only: boolean;
  sector: string | null;
  source: string;
  symbol: string;
  themes: string[];
}

// ── Entity layer ──────────────────────────────────────────────────

export interface CompanyEntity {
  id: number;
  canonical_name: string;
  country: string | null;
  summary: string | null;
  source: string;
  created_at: string;
  updated_at: string;
}

export type RelationshipKind =
  | "parent_of"
  | "joint_venture"
  | "major_shareholder";

export interface EntityRelationship {
  id: number;
  from_entity_id: number;
  to_entity_id: number;
  kind: RelationshipKind;
  notes: string | null;
  source: string;
  created_at: string;
}

export interface PreflightEntity {
  canonical_name: string;
  country: string;
  ticker: string;
  exchange: string;
  summary: string;
  confidence: number;
}

export interface RelationshipSuggestion {
  id: number;
  source: "preflight" | "backfill";
  tracked_company_id: number | null;
  payload: {
    entity: PreflightEntity;
    parents: PreflightEntity[];
    joint_venture_partners: PreflightEntity[];
    major_shareholders: PreflightEntity[];
    sources: { name: string; url: string }[];
    notes: string;
    model: string | null;
  };
  status: "pending" | "accepted" | "rejected";
  decided_at: string | null;
  notes: string | null;
  created_at: string;
}

export function listEntities(): Promise<CompanyEntity[]> {
  return apiFetch<CompanyEntity[]>("/entities");
}

export function listEntityRelationships(
  entityId?: number
): Promise<EntityRelationship[]> {
  const qs = entityId !== undefined ? `?entity_id=${entityId}` : "";
  return apiFetch<EntityRelationship[]>(`/entities/relationships${qs}`);
}

export function deleteRelationship(id: number): Promise<void> {
  return apiFetch<void>(`/entities/relationships/${id}`, {
    method: "DELETE",
  });
}

export function listRelationshipSuggestions(
  status: "pending" | "accepted" | "rejected" = "pending"
): Promise<RelationshipSuggestion[]> {
  return apiFetch<RelationshipSuggestion[]>(
    `/entities/suggestions?status=${status}`
  );
}

export interface SuggestionDecisionResult {
  status: "accepted" | "rejected";
  applied: {
    kind: RelationshipKind;
    relationship_id: number;
    entity_id: number;
    entity_name: string;
    auto_tracked: boolean;
    tracked_company_id: number | null;
  }[];
  auto_tracked: {
    entity_name: string;
    tracked_company_id: number | null;
  }[];
}

export function decideRelationshipSuggestion(
  id: number,
  body: {
    decision: "accept" | "reject";
    accepted_keys?: string[];
    auto_track?: boolean;
    notes?: string | null;
  }
): Promise<SuggestionDecisionResult> {
  return apiFetch<SuggestionDecisionResult>(
    `/entities/suggestions/${id}/decision`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }
  );
}

export interface CompanySearchResponse {
  count: number;
  results: CompanyReference[];
  source: string;
}

export function searchCompanies(params: {
  q?: string;
  country?: string;
  exchange?: string;
}): Promise<CompanySearchResponse> {
  const qs = new URLSearchParams();
  if (params.q) {
    qs.set("q", params.q);
  }
  if (params.country) {
    qs.set("country", params.country);
  }
  if (params.exchange) {
    qs.set("exchange", params.exchange);
  }
  return apiFetch<CompanySearchResponse>(`/companies/search?${qs.toString()}`);
}

export function listTrackedCompanies(): Promise<TrackedCompany[]> {
  return apiFetch<TrackedCompany[]>("/companies/tracked");
}

export function trackCompany(body: {
  company_reference_id?: number;
  symbol?: string;
  exchange?: string;
  country?: string | null;
  company_name?: string;
  sector?: string | null;
  themes?: string[];
}): Promise<TrackedCompany> {
  return apiFetch<TrackedCompany>("/companies/tracked", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function untrackCompany(id: number, purge = false): Promise<void> {
  return apiFetch<void>(`/companies/tracked/${id}?purge=${purge}`, {
    method: "DELETE",
  });
}

export type SourceKind =
  | "rss"
  | "ir_press"
  | "blog"
  | "sec"
  | "hkex"
  | "twitter";
export type FetchStrategy = "auto" | "static" | "browser" | "agent";

export interface CompanySource {
  consecutive_failures: number;
  created_at: string;
  fetch_strategy: string;
  id: number;
  is_active: boolean;
  kind: string;
  label: string | null;
  last_error: string | null;
  last_fetched_at: string | null;
  last_item_at: string | null;
  last_verified_at: string | null;
  needs_review: boolean;
  source: string;
  status: string;
  tracked_company_id: number;
  url: string;
}

export function listCompanySources(
  trackedId: number,
  includeInactive = false
): Promise<CompanySource[]> {
  return apiFetch<CompanySource[]>(
    `/companies/tracked/${trackedId}/sources?include_inactive=${includeInactive}`
  );
}

export function addCompanySource(
  trackedId: number,
  body: {
    kind: SourceKind;
    url: string;
    label?: string | null;
    fetch_strategy?: FetchStrategy;
  }
): Promise<CompanySource> {
  return apiFetch<CompanySource>(`/companies/tracked/${trackedId}/sources`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function updateCompanySource(
  sourceId: number,
  body: Partial<{
    kind: SourceKind;
    url: string;
    label: string | null;
    fetch_strategy: FetchStrategy;
    status: string;
    is_active: boolean;
    needs_review: boolean;
  }>
): Promise<CompanySource> {
  return apiFetch<CompanySource>(`/companies/sources/${sourceId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function deleteCompanySource(sourceId: number): Promise<void> {
  return apiFetch<void>(`/companies/sources/${sourceId}`, { method: "DELETE" });
}

export interface DiscoverySummary {
  created: number;
  discovered: number;
  provider: string;
  status: string;
}

export function discoverCompanySources(
  trackedId: number
): Promise<DiscoverySummary> {
  return apiFetch<DiscoverySummary>(
    `/companies/tracked/${trackedId}/discover-sources`,
    {
      method: "POST",
    }
  );
}

export interface NotificationRecord {
  attempts: number;
  channel: string;
  chat_id: string | null;
  created_at: string;
  error: string | null;
  event_id: number | null;
  id: number;
  reminder_window: string | null;
  sent_at: string | null;
  skip_reason: string | null;
  status: string;
}

export function listNotifications(
  status?: string
): Promise<NotificationRecord[]> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  return apiFetch<NotificationRecord[]>(`/notifications${qs}`);
}

