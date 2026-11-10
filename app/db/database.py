from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from app.core.config import settings

# SQLite requires 'check_same_thread: False' for multi-threaded FastAPI workers.
# Server databases (PostgreSQL, MySQL) get pool_pre_ping so connections dropped by the server
# (e.g. MySQL's wait_timeout) are detected and replaced instead of failing the next request.
is_sqlite = settings.DATABASE_URL.startswith("sqlite")
engine_kwargs = {"connect_args": {"check_same_thread": False}} if is_sqlite else {"pool_pre_ping": True}

engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    **engine_kwargs,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that provides a database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables in the database."""
    import app.models  # noqa: F401  (registers the ORM models on Base.metadata)

    Base.metadata.create_all(bind=engine)

