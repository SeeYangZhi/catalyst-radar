## Summary

What does this change and why? Link the issue if there is one (`Closes #...`).

## How was it verified?

<!-- Commands run, manual checks, screenshots for UI changes. -->

## Checklist

- [ ] Small and focused on one change
- [ ] Backend: `uv run ruff check .` and `uv run pytest -q` pass
- [ ] Frontend: `bun run typecheck`, `bun run lint`, `bun run test` pass
- [ ] Tests added / updated (no live network; synthetic, real-shaped fixtures)
- [ ] Schema change has an Alembic migration (revision id ≤ 32 chars)
- [ ] New settings added to `backend/.env.example` / `config.py` and documented
- [ ] Nearest `AGENTS.md` / docs updated if a contract changed
- [ ] No secrets, personal identifiers, or verbatim third-party data committed
