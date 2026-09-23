# Catalyst Radar Design Guide

The dashboard is an operational tool for scanning events, not a marketing
site. Favor compact, dense, scan-friendly layouts over decoration. This guide
describes what the frontend implements; `frontend/app/globals.css` is the
source of truth for tokens.

## Theme

- **Dark only.** There is no theme switcher and no light palette. All tokens
  are defined once on `:root` in `globals.css` and mapped to Tailwind colors
  via `@theme inline`. The `dark:` variant exists for shadcn compatibility
  (`@custom-variant dark`) but pages should not rely on it.
- Hierarchy comes from surface lift (canvas → surface-1 → surface-2) and
  hairline borders, not from shadows or saturated fills.

## Color tokens

| CSS variable | Value | Tailwind names | Use |
|---|---|---|---|
| `--canvas` | `#090909` | `bg-background` | Page background |
| `--surface-1` | `#141414` | `bg-card`, `bg-muted`, `bg-secondary`, `bg-surface-1` | Cards, inputs, sidebar |
| `--surface-2` | `#1c1c1c` | `bg-popover`, `bg-surface-2` | Popovers, selected pills, neutral badges |
| `--hairline` | `#262626` | `border-border`, `border-input` | Default borders |
| `--hairline-soft` | `#1a1a1a` | `border-hairline-soft` | Row dividers inside cards |
| `--ink` | `#ffffff` | `text-foreground`, `text-ink`, `bg-primary` | Primary text, primary button fill |
| `--ink-muted` | `#999999` | `text-muted-foreground`, `text-ink-muted` | Secondary text, labels, icons |
| `--accent-blue` | `#0099ff` | `text-accent`, `bg-accent`, `ring` | Signal color: focus rings, links, accent badges |
| `--success` | `#22c55e` | `text-success` | Healthy / sent / succeeded |
| `--warning` | `#ff7a3d` | `text-warning` | Degraded / pending / attention |
| `--danger` | `#ff5577` | `text-destructive` | Failed / errors / destructive actions |

Rules:

- Use tokens only; do not introduce ad-hoc hex values or Tailwind palette
  colors (`text-red-500`, etc.).
- Accent blue is a signal, not a surface. Don't use it as a large fill;
  selected and "on" states use `bg-surface-2` + `text-ink` instead.
- Status colors appear as tinted pills (`bg-<status>/15 text-<status>`) via
  `Badge`, not as solid blocks.

## Typography

- Font stack: `Inter`, then the system UI stack (`--font-sans`). Inter is not
  bundled; the system fallback is acceptable. Monospace: `--font-mono`
  (system mono stack).
- Body: `letter-spacing: -0.01em`, antialiased, OpenType features
  `cv11`, `ss03`.
- Scale actually in use:
  - Page title: `text-xl font-semibold tracking-tight`
  - Page subtitle / body: `text-sm`, muted copy `text-sm text-ink-muted`
  - Card title: `text-sm font-semibold tracking-tight` (`CardTitle`)
  - Dense labels, table meta, descriptions: `text-xs`
  - Stat tiles: `text-2xl font-semibold tabular-nums`
  - Eyebrow labels: `text-xs font-medium uppercase tracking-wide text-ink-muted`
- Numbers that line up (counts, prices, dates in tables) use `tabular-nums`.
  Tickers, symbols and ids use `font-mono text-xs`.

## Shape

| Token | Value | Used by |
|---|---|---|
| `--radius-sm` | 6px | Small elements, calendar day cells |
| `--radius-md` | 10px | Inputs, select triggers (`rounded-[10px]`) |
| `--radius-lg` | 15px | Cards (`rounded-[15px]`) |
| pill | `rounded-full` | Buttons, badges, tabs, toggles |

Buttons, badges, tab lists, tab triggers and toggles are pills. Icon-only
buttons become circles.

## Components

shadcn/ui (`new-york` style, `neutral` base, CSS variables) is the primitive
layer. Check the shadcn registry before writing a custom primitive, and add
new ones with `bunx shadcn@latest add <name>` into `components/ui/`. Local
adjustments already made to the shadcn defaults:

- **Button** (`components/ui/button.tsx`): pill radius on every variant.
  `default` is white-on-dark (`bg-primary`), `outline` / `ghost` for secondary
  actions, `destructive` for deletes. Sizes `xs`, `sm`, `default`, `lg` and
  `icon*`.
- **Badge** (`components/ui/badge.tsx`): variants `neutral | accent | success |
  warning | danger`, 11px medium text, tinted background. Use for status.
- **Card** (`components/ui/card.tsx`): `bg-card`, hairline border, 15px radius,
  compact padding (`px-5`). Use for panels, stat tiles and repeated items.
- **Input / Select**: `bg-surface-1`, hairline border, 10px radius, muted
  placeholder, accent focus ring.
- **Tabs / Toggle / ToggleGroup**: segmented-control look. Transparent track,
  selected item `bg-surface-2 text-ink`, inactive `text-ink-muted`.
- **Calendar / DatePicker**: range and today cells use `bg-surface-2`; the
  selected day uses `bg-primary`.
- **DataTable** (`components/ui/data-table.tsx`, TanStack Table) with
  `TablePagination`: the standard for any list longer than a handful of rows.
  Sortable headers via `sortableHeader`, optional column visibility menu,
  expandable sub-rows.
- **Feedback**: `sonner` toasts (`toast.error`, `toast.success`) bottom-right
  for action results; `Skeleton` for loading; inline
  `<p className="text-destructive text-sm">` for page-level errors.
- **Dialogs**: `AlertDialog` for destructive confirmation, `Sheet` for side
  panels, `Popover` / `DropdownMenu` for compact menus, `Tooltip` for icon
  hints.

## Icons

`lucide-react` only, at `h-4 w-4` (or `size-4`) in buttons, nav and card
headers, colored `text-ink-muted` unless they carry status. Icon-only buttons
need an `aria-label`.

## Layout

- **App shell** (`components/app-shell.tsx`): shadcn `Sidebar` (collapsible to
  icons) with lucide nav icons, a 56px (`h-14`) header with the sidebar
  trigger, and a scrolling `main` with `p-4 sm:p-5`.
- **Page container**: `mx-auto max-w-6xl space-y-6` for wide pages (dashboard,
  tables), `max-w-5xl` / `max-w-3xl` for forms and settings.
- **Page header**: title + one-line muted description, then content.
- **Grids**: stat tiles `grid grid-cols-2 gap-3 lg:grid-cols-4`; panels
  `grid gap-3 lg:grid-cols-2`. Gaps stay small (`gap-3`).
- **Cards, not nested cards.** Use a card per panel or repeated item; inside a
  card, separate rows with `border-b border-hairline-soft` dividers instead of
  inner cards.
- **Responsive**: every page must work at phone width. Collapse grids to one or
  two columns, keep tables horizontally scrollable inside their container, and
  hide secondary header text below `sm`.

## Forms

`react-hook-form` + `zod` wrapped in shadcn `Form` / `FormField` /
`FormItem` / `FormControl` / `FormMessage`, so validation messages render
consistently. Show the result of a submit with a toast or an inline
success / error line.

## Accessibility

- Visible focus: accent ring (`focus-visible:ring-[3px] ring-ring/50` on
  buttons, `ring-2 ring-ring` on inputs). Never remove focus styles.
- `aria-current="page"` on the active nav link, `aria-label` on icon-only
  controls.
- Text contrast: body text is white on near-black; muted text (`#999`) is for
  secondary information only.
