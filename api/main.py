# api/main.py

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes.health  import router as health_router
from api.routes.process import router as process_router
from api.routes.document import router as document_router
from api.routes.ingest import router as ingest_router

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
# CORS — restrict in production
# ---------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten before production
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------
# Routers
# ---------------------------------

app.include_router(health_router,  tags=["Health"])
app.include_router(process_router, tags=["Processing"])
app.include_router(document_router, tags=["Documents"])
app.include_router(ingest_router, tags=["Pipeline"])

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
