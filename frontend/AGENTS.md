# frontend/ — Next.js dashboard

<!-- BEGIN:nextjs-agent-rules -->
## This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

## Purpose

Single-admin dashboard for Catalyst Radar. Lists tracked companies + events + notifications, surfaces alert review state, exposes runtime config edits.

## Ownership

Everything under `frontend/`. Backend API contracts (the shape of `/api/v1/*` responses) are owned by `../backend/AGENTS.md`; this app is a typed client that mirrors those shapes in `lib/api.ts`.

## Local Contracts

### Layout

- `app/` — Next.js app-router pages
  - `app/dashboard/` — authenticated UI (companies, events, settings, search…)
  - `app/login/` — public login flow
  - `app/layout.tsx` / `globals.css` — root shell + Tailwind base
- `components/` — shared UI; shadcn primitives under `components/ui/`
- `hooks/` — React hooks (auth, dashboard data)
- `lib/api.ts` — typed fetchers for `/api/v1/*` (one function per endpoint)
- `lib/utils.ts` — display helpers (formatTicker, etc.)
- `tests/` — vitest + Testing Library suites (`*.test.ts(x)`); `tests/setup.ts` registers jest-dom matchers + RTL cleanup; `vitest.config.ts` mirrors tsconfig's `@/*` alias
- `eslint.config.mjs` — Next-default config
- `components.json` — shadcn registry config (do not hand-edit)
- `.env.production` — committed `NEXT_PUBLIC_*` build-time values (relative `/api/v1` for the nginx-fronted prod stack)
- `Dockerfile` — optional `NEXT_PUBLIC_API_URL` build arg overrides `.env.production` (the dev compose stack uses it because it has no nginx)

### shadcn-first

Before writing a custom UI primitive, check the shadcn registry (via MCP if available, else the website). Existing primitives live under `components/ui/`. Add new shadcn components via the shadcn CLI; they install into `components/ui/`.

### Form pattern

`react-hook-form` + `zod` + `@hookform/resolvers/zod`. Wrap in shadcn's `<Form>` / `<FormField>` / `<FormItem>` / `<FormControl>` / `<FormMessage>` so validation messages render consistently. See `app/dashboard/search/page.tsx` for the canonical two-form example (search + manual-add).

### API client

`lib/api.ts` re-exports `apiFetch<T>(path, init)` and one typed function per endpoint. New endpoint? Add the function with explicit request/response types — never `any`. Auth is a bearer token in `localStorage` (`cr_token`); `apiFetch` injects the `Authorization` header and, on a 401 off the login page, clears the token and redirects to `/login`. This contract is pinned by `tests/api.test.ts`.

### Icons

`lucide-react` only for icon buttons. Don't mix icon libraries.

### Styling

Follow `../DESIGN.md` (tokens, typography, component conventions; dark-only theme). Tailwind CSS. Project palette uses `text-ink-muted`, `text-destructive`, `bg-*` etc. — defined in `globals.css`. Don't invent ad-hoc colors. shadcn `Badge` has `variant="neutral" | "accent" | "success" | "warning" | "danger"` for status pills (see `components/ui/badge.tsx`).

### Cards

UI cards for repeated items, tables, panels. No nested cards. The dashboard is operational, not a marketing page — favor compact, scan-friendly layouts.

### No secrets

Frontend code ships to the user's browser. Never embed API keys, tokens, or any credential here. All third-party calls go through the backend.

## Work Guidance

- New page: add `app/dashboard/<route>/page.tsx`. Auth gate is in the dashboard layout — pages don't re-check.
- New form: zod schema + `useForm` + `<Form>` wrapper + state for success/error feedback. Match the existing two-state pattern (`{ kind: "ok" | "err", ... }`).
- New API call: add to `lib/api.ts` with explicit types; consume in the page via `useEffect` or a hook in `hooks/`.
- Adding a shadcn primitive: `bunx shadcn@latest add <name>` (Bun, not npx). Commit `components/ui/<name>.tsx` + any `components.json` change.
- Before any Next.js feature work: re-read `node_modules/next/dist/docs/` for the relevant API. Don't rely on training-data knowledge of older Next versions.

## Verification

- `cd frontend && bun run typecheck` — strict tsc, no emit.
- `cd frontend && bun run lint` — eslint with the Next config.
- `cd frontend && bun run test` — vitest (jsdom + Testing Library); CI runs it alongside typecheck + lint. Stub `fetch` with `vi.stubGlobal`; never hit the real API. Prefer testing presentational components with plain props; components needing Next router context aren't worth mocking.
- Visual: run `bun dev` against a local API (or the dev compose stack) and load `/dashboard` in a browser.

## Child DOX Index

(none)
