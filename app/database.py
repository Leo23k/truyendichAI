from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import PROJECT_ROOT, settings


class Base(DeclarativeBase):
    pass


def _database_url() -> str:
    if settings.database_url.startswith("sqlite:///./"):
        relative_path = settings.database_url.removeprefix("sqlite:///./")
        return f"sqlite:///{PROJECT_ROOT / relative_path}"
    return settings.database_url


engine = create_engine(
    _database_url(),
    connect_args={"check_same_thread": False} if _database_url().startswith("sqlite") else {},
)


@event.listens_for(engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    if _database_url().startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def create_tables() -> None:
    from app import models  # noqa: F401

    settings.ensure_directories()
    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
