from .ingest import router as ingest_router
from .staging import router as staging_router
from .products import router as products_router

__all__ = ["ingest_router", "staging_router", "products_router"]

