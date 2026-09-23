FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Headless Chromium for the AAStocks HK IPO adapter (Phase 9.5a), in its
# own layer ABOVE the pyproject COPY so dependency changes don't
# re-trigger the (slow, hundreds-of-MB) Chromium + apt install. This
# layer is keyed only on the version pinned here, which MUST match
# playwright in uv.lock (enforced by test_dockerfile_playwright_pin);
# the throwaway pip install is removed — the venv's playwright (same
# version) finds the browsers in /root/.cache/ms-playwright.
RUN pip install --no-cache-dir playwright==1.59.0 \
 && playwright install --with-deps chromium \
 && pip uninstall -y playwright

COPY pyproject.toml README.md ./
RUN uv sync --no-install-project --no-dev

COPY . .
RUN uv sync --no-dev

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "catalyst_radar.main:app", "--host", "0.0.0.0", "--port", "8000"]
