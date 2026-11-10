from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.db.database import init_db
from app.routers import ingest_router, staging_router, products_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle event to initialize database tables on startup."""
    init_db()
    yield


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "Production-grade Catalog Ingestion POC using TypeSafe AI's JEV System One decision engine. "
        "Intelligently maps supplier spreadsheets, normalizes units/currencies, validates records, "
        "and stages products with idempotent approval."
    ),
    lifespan=lifespan,
)

# Enable CORS for local testing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register route controllers
app.include_router(ingest_router)
app.include_router(staging_router)
app.include_router(products_router)


@app.get("/", tags=["Root"])
def root():
    return {
        "message": f"Welcome to {settings.APP_NAME}",
        "documentation": "/docs",
        "health": "/api/v1/ingest/health",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)

