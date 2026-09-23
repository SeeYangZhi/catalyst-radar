Catalyst Radar — Next.js dashboard.

## Local dev

```bash
bun install
bun run dev
```

Open http://localhost:3000. The dev server expects the backend API on
`http://localhost:8000` (set `NEXT_PUBLIC_API_URL` in `.env.local` to override).

## Checks

```bash
bun run typecheck
bun run lint
bun run test
```

## Production

Built and served behind nginx via `docker-compose.prod.yml`. `NEXT_PUBLIC_*`
values are inlined at build time from the committed `.env.production`
(relative `/api/v1`, so hostname-independent). The dev compose stack passes an
absolute `NEXT_PUBLIC_API_URL` build arg instead because it has no nginx. See
`../deploy/README.md` for an example deployment and `../DESIGN.md` for UI
conventions.
