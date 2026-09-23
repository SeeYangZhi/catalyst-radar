# Catalyst Radar Backend

FastAPI + SQLModel + async SQLAlchemy + Alembic + Celery backend.

See [../docs/references/commands.md](../docs/references/commands.md) for commands.

```bash
cp .env.example .env
uv sync
uv run alembic upgrade head
uv run uvicorn catalyst_radar.main:app --reload
```
