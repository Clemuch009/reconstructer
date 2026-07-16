# api/main.py

import os
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from api.middleware.auth import RateLimitHeadersMiddleware
from api.routes.health    import router as health_router
from api.routes.process   import router as process_router
from api.routes.document  import router as document_router
from api.routes.ingest    import router as ingest_router
from api.routes.ingest_file import router as ingest_file_router
from api.routes.reconcile  import router as reconcile_router
from api.routes.dedupe     import router as dedupe_router
from api.auth.router      import router as auth_router
from api.routes.billing   import router as billing_router


# ---------------------------------
# App
# ---------------------------------

app = FastAPI(
    title="Text Reconstruction Engine",
    description=(
        "Deterministic, structure-aware document processing API. "
        "Segments, classifies, and reconstructs messy text into "
        "typed structured output for human, AI, and machine consumers."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)


# ---------------------------------
# Middleware — must be registered before routers
# ---------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten before production
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(RateLimitHeadersMiddleware)


# ---------------------------------
# Routers
# ---------------------------------

app.include_router(health_router,      tags=["Health"])
app.include_router(auth_router,        tags=["Auth"])
app.include_router(billing_router,     tags=["Billing"])
app.include_router(process_router,     tags=["Processing"])
app.include_router(document_router,    tags=["Documents"])
app.include_router(ingest_router,      tags=["Pipeline"])
app.include_router(ingest_file_router, tags=["File Ingestion"])
app.include_router(reconcile_router,   tags=["Reconciliation"])
app.include_router(dedupe_router,      tags=["Deduplication"])


# ---------------------------------
# Frontend routes
# ---------------------------------

def _resolve_frontend_dir() -> str:
    """Find the directory that actually holds the HTML pages. Different setups
    keep them in different places. Try known locations and use the first that
    exists and contains the pages."""
    here = os.path.dirname(__file__)          # .../api
    repo = os.path.dirname(here)              # repo root
    candidates = [
        os.path.join(here, "frontend", "public"),  # api/frontend/public/  ← actual
        os.path.join(here, "frontend"),            # api/frontend/
        os.path.join(repo, "public"),              # public/
    ]
    for d in candidates:
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "invoices.html")):
            return d
    for d in candidates:
        if os.path.isdir(d):
            return d
    return os.path.join(here, "frontend", "public")

FRONTEND_DIR = _resolve_frontend_dir()


@app.get("/console", include_in_schema=False)
async def developer_console():
    return FileResponse(os.path.join(FRONTEND_DIR, "console.html"))


@app.get("/invoices", include_in_schema=False)
async def invoice_workspace():
    # Serve the client workspace from the API itself, so the page and the
    # /process-text, /process-file, /reconcile endpoints share one origin.
    # Same-origin means API_BASE='' just works — no cross-server 501.
    return FileResponse(os.path.join(FRONTEND_DIR, "invoices.html"))


@app.get("/observer", include_in_schema=False)
async def enterprise_observer():
    return FileResponse(os.path.join(FRONTEND_DIR, "observer.html"))


@app.get("/signup", include_in_schema=False)
async def signup_page():
    return FileResponse(os.path.join(FRONTEND_DIR, "signup.html"))


@app.get("/dashboard", include_in_schema=False)
async def dashboard_page():
    return FileResponse(os.path.join(FRONTEND_DIR, "dashboard.html"))


# ---------------------------------
# Startup
# ---------------------------------

@app.on_event("startup")
async def startup() -> None:
    """
    Warm up engine singleton on startup.
    Prevents cold start latency on first request.
    """
    from api.dependencies import get_engine
    get_engine()
    print("[startup] Engine singleton initialized.")


# ---------------------------------
# Run
# ---------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        workers=1,
    )
