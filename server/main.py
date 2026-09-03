"""
FastAPI entrypoint.

Boot sequence (lifespan):
  1. Validate DB can be reached + create tables
  2. Ensure ES256 keypairs exist (generate if absent)
  3. Mount MCP server at /mcp
  4. Start reconciler background task
  5. Yield (app is live)
  6. On shutdown: stop reconciler
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.config import settings
from server.db.session import create_tables
from server.mandate.issuer import ensure_keypairs


# ── Lifespan ──────────────────────────────────────────────────────────────────

# Windows defaults stdout to cp1252 when it is not a terminal, so a redirected
# or containerised start died on a tick character in the banner below — the
# server refusing to boot because of a decoration in its own startup message.
# Reconfiguring is cheap and keeps the banner readable where UTF-8 is available.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):          # not a real stream
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Phase 1: DB
    create_tables()

    # Phase 2: Keypairs
    ensure_keypairs()

    # Phase 3: MCP server (mounted below after app creation)
    # Phase 4: Reconciler
    #
    # This used to be wrapped in `except ImportError: pass`, which made a
    # broken reconciler indistinguishable from a working one — the single most
    # expensive kind of silence, because the symptom (sessions stuck live)
    # looks identical either way. Startup failures are now recorded and
    # surfaced on /health, so "is the reconciler running?" is a question with
    # an answer rather than a guess.
    reconciler_task: asyncio.Task | None = None
    try:
        from server.payments.reconciler import run_reconciler, sweep

        # Sweep once at boot. Without this a server started against a database
        # holding old sessions shows them as live until the first interval
        # elapses, which is exactly when someone is looking at the dashboard.
        try:
            swept = sweep()
            app.state.reconciler_boot_sweep = swept
        except Exception as exc:                      # noqa: BLE001
            app.state.reconciler_boot_sweep = {"error": str(exc)}
            print(f"[tollgate] ! boot sweep failed: {exc}")

        reconciler_task = asyncio.create_task(run_reconciler())
        app.state.reconciler_task = reconciler_task
        app.state.reconciler_started = True
        app.state.reconciler_error = None
    except Exception as exc:                          # noqa: BLE001
        app.state.reconciler_started = False
        app.state.reconciler_error = f"{type(exc).__name__}: {exc}"
        print(
            f"\n[tollgate] !! RECONCILER FAILED TO START: {exc}"
            f"\n   Stalled sessions will NOT be swept to STALE.\n"
        )

    boot_sweep = getattr(app.state, "reconciler_boot_sweep", None)
    print(
        f"\n[tollgate] ✓ Server ready"
        f"\n  Razorpay key: {settings.RAZORPAY_KEY_ID[:20]}..."
        f"\n  DB: {settings.DATABASE_URL}"
        f"\n  Stub mode: {settings.STUB_MODE}"
        f"\n  Tamper endpoint: {'ENABLED' if settings.ALLOW_TAMPER else 'disabled'}"
        f"\n  Reconciler: "
        f"{'running' if getattr(app.state, 'reconciler_started', False) else 'NOT RUNNING'}"
        f" (every {settings.RECONCILER_INTERVAL_SECONDS}s,"
        f" stale after {settings.STALE_SESSION_TIMEOUT_SECONDS}s)"
        f"\n  Boot sweep: {boot_sweep}\n"
    )

    yield

    # Shutdown
    if reconciler_task and not reconciler_task.done():
        reconciler_task.cancel()
        try:
            await reconciler_task
        except asyncio.CancelledError:
            pass


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Tollgate",
    description=(
        "Governed agentic-commerce rail. "
        "Every money action is explainable, bounded, and gated."
    ),
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],    # dashboard is served separately; tighten in prod
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routes (registered lazily so imports don't blow up before DB init) ─────────

def _register_routes() -> None:
    try:
        from server.api.routes import router
        app.include_router(router)
    except ImportError:
        pass  # routes not yet built (Phase 8)

_register_routes()


# ── MCP mount (Phase 6) ────────────────────────────────────────────────────────

def _mount_mcp() -> None:
    try:
        from server.mcp.server import mcp_asgi_app
        app.mount(settings.MCP_MOUNT_PATH, mcp_asgi_app)
    except ImportError:
        pass

_mount_mcp()


# ── Core endpoints ─────────────────────────────────────────────────────────────

def _llm_descriptor() -> dict:
    """Which model would actually be called, and by whom."""
    try:
        from server.agents.llm import resolve

        cfg = resolve()
        return {
            "configured": True,
            "provider": cfg.provider,
            "model": cfg.model,
            "base_url": cfg.base_url,
            "timeout_seconds": settings.LLM_TIMEOUT_SECONDS,
        }
    except Exception as exc:                          # noqa: BLE001
        return {"configured": False, "reason": str(exc)[:160]}


@app.get("/health", tags=["meta"])
def health():
    """
    Liveness plus the two facts that are otherwise unobservable: whether the
    background reconciler actually started, and what it swept at boot.
    """
    task: asyncio.Task | None = getattr(app.state, "reconciler_task", None)
    return {
        "status": "ok",
        "razorpay_key_prefix": settings.RAZORPAY_KEY_ID[:12],
        "stub_mode": settings.STUB_MODE,
        # The dashboard needs this before a click, not after: it pre-opens a
        # tab inside the approve gesture, and only the live path ever returns a
        # payment link to put in it. Without knowing the mode up front, every
        # approval in synthetic mode would flash a tab open and shut.
        "payments_mode": settings.PAYMENTS_MODE,
        "tamper_enabled": settings.ALLOW_TAMPER,
        # Named so the banner can say exactly what is running, rather than
        # leaving a viewer to infer whether a model is involved at all.
        "llm": _llm_descriptor(),
        "reconciler": {
            "started": getattr(app.state, "reconciler_started", False),
            "alive": bool(task and not task.done()),
            "error": getattr(app.state, "reconciler_error", None),
            "interval_seconds": settings.RECONCILER_INTERVAL_SECONDS,
            "stale_after_seconds": settings.STALE_SESSION_TIMEOUT_SECONDS,
            "boot_sweep": getattr(app.state, "reconciler_boot_sweep", None),
        },
    }


# ── Dashboard static mount (Phase 9) ───────────────────────────────────────────
#
# Mounted LAST so that every API route above wins the path match. The whole
# demo — API, MCP surface and dashboard — then runs from this one process.
#
# `dashboard/dist` is produced by `npm run build` in dashboard/. If it hasn't
# been built, / returns a short instruction instead of a 404 that looks like a
# broken server.

def _mount_dashboard() -> None:
    from pathlib import Path

    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    dist = Path(__file__).parent.parent / "dashboard" / "dist"

    if not (dist / "index.html").exists():
        @app.get("/", include_in_schema=False)
        def dashboard_not_built() -> HTMLResponse:
            return HTMLResponse(
                "<pre style='font:14px ui-monospace,monospace;padding:24px'>"
                "tollgate dashboard is not built.\n\n"
                "  cd dashboard &amp;&amp; npm install &amp;&amp; npm run build\n\n"
                "then reload this page. The API is already live at /docs."
                "</pre>",
                status_code=503,
            )
        return

    # html=True serves index.html at "/" and falls back to it for unknown
    # paths, which keeps the mount working if routing is ever added.
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="dashboard")

_mount_dashboard()


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Tollgate server")
    parser.add_argument("--stub", action="store_true", help="Use fixture responses instead of live LLM calls")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    if args.stub:
        import os
        os.environ["STUB_MODE"] = "true"
        # Re-create settings with updated env
        settings.STUB_MODE = True

    uvicorn.run(
        "server.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
