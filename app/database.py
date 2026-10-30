import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# По умолчанию используем SQLite для быстрого старта без настройки внешних серверов,
# но архитектура полностью готова к PostgreSQL через переменную окружения DATABASE_URL.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./edu_billing.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Dependency для получения сессии БД в эндпоинтах FastAPI."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
