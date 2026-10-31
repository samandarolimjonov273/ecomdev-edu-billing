import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.timeutil import current_month
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

DATABASE_URL = settings.database_url()

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

LATEST_SCHEMA_VERSION = 6
_MIGRATION_LOCK = threading.RLock()


@contextmanager
def _migration_database_lock(target_engine):
    if target_engine.dialect.name != "postgresql":
        yield
        return

    conn = target_engine.connect()
    lock_id = 761204819034
    try:
        conn.execute(text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": lock_id})
        yield
    finally:
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id})
        finally:
            conn.close()


def create_safe_sqlite_backup(target_engine, *, overwrite: bool = False) -> Path | None:
    """
    Создаёт консистентный онлайн-бэкап SQLite через нативный sqlite3.Connection.backup() API
    и проверяет целостность базы через PRAGMA integrity_check перед миграцией.
    """
    if target_engine.url.get_backend_name() != "sqlite":
        return None
    raw_path = target_engine.url.database
    if not raw_path or raw_path == ":memory:":
        return None

    db_path = Path(raw_path)
    if not db_path.exists() or db_path.stat().st_size == 0:
        return None

    backup_path = db_path.with_suffix(db_path.suffix + ".bak")
    if backup_path.exists() and not overwrite:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = db_path.with_suffix(db_path.suffix + f".{timestamp}.bak")
    src_conn = sqlite3.connect(str(db_path))
    try:
        integrity = src_conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0].lower() != "ok":
            raise RuntimeError(f"База данных повреждена (integrity_check={integrity})")
        dst_conn = sqlite3.connect(str(backup_path))
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    return backup_path


def run_migrations(target_engine=engine) -> int:
    """
    Applies explicitly versioned migrations for SQLite and PostgreSQL.
    The web process must not run this implicitly; invoke migrate.py before deployment.
    """
    import app.models  # noqa: F401

    with _MIGRATION_LOCK, _migration_database_lock(target_engine):
        inspector = inspect(target_engine)
        existing_tables = set(inspector.get_table_names())
        current_version = 0
        if "schema_migrations" in existing_tables:
            with target_engine.connect() as conn:
                current_version = conn.execute(
                    text("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
                ).scalar_one()

        if current_version < LATEST_SCHEMA_VERSION:
            create_safe_sqlite_backup(target_engine)

        with target_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at VARCHAR(40) NOT NULL
                    )
                    """
                )
            )

        # Create new tables only. Existing tables are altered by numbered steps below.
        Base.metadata.create_all(bind=target_engine)

        for version in range(current_version + 1, LATEST_SCHEMA_VERSION + 1):
            inspector = inspect(target_engine)
            tables = set(inspector.get_table_names())
            now_ym = current_month()

            with target_engine.begin() as conn:
                if version == 1 and "students" in tables:
                    student_cols = {c["name"] for c in inspector.get_columns("students")}
                    if "enrolled_month" not in student_cols:
                        conn.execute(text("ALTER TABLE students ADD COLUMN enrolled_month VARCHAR(7)"))
                        conn.execute(
                            text(
                                """
                                UPDATE students
                                SET enrolled_month = COALESCE(
                                    (SELECT SUBSTR(CAST(MIN(p.payment_date) AS VARCHAR(10)), 1, 7)
                                     FROM payments p WHERE p.student_id = students.id),
                                    :now_ym
                                )
                                WHERE enrolled_month IS NULL
                                """
                            ),
                            {"now_ym": now_ym},
                        )
                    if "is_archived" not in student_cols:
                        conn.execute(
                            text("ALTER TABLE students ADD COLUMN is_archived BOOLEAN NOT NULL DEFAULT FALSE")
                        )
                    if "archived_at" not in student_cols:
                        conn.execute(text("ALTER TABLE students ADD COLUMN archived_at VARCHAR(40)"))
                    if "history_verified_from" not in student_cols:
                        conn.execute(text("ALTER TABLE students ADD COLUMN history_verified_from VARCHAR(7)"))
                        conn.execute(
                            text(
                                """
                                UPDATE students
                                SET history_verified_from = COALESCE(enrolled_month, :now_ym)
                                WHERE history_verified_from IS NULL
                                """
                            ),
                            {"now_ym": now_ym},
                        )

                if version == 2 and "payments" in tables:
                    payment_cols = {c["name"] for c in inspector.get_columns("payments")}
                    if "payment_method" not in payment_cols:
                        conn.execute(
                            text(
                                "ALTER TABLE payments ADD COLUMN payment_method VARCHAR(30) NOT NULL DEFAULT 'Наличные'"
                            )
                        )
                    if "comment" not in payment_cols:
                        conn.execute(text("ALTER TABLE payments ADD COLUMN comment VARCHAR(255)"))
                    if "idempotency_key" not in payment_cols:
                        conn.execute(text("ALTER TABLE payments ADD COLUMN idempotency_key VARCHAR(64)"))
                    if "payload_fingerprint" not in payment_cols:
                        conn.execute(text("ALTER TABLE payments ADD COLUMN payload_fingerprint VARCHAR(128)"))
                    if "tariff_snapshot" not in payment_cols:
                        conn.execute(text("ALTER TABLE payments ADD COLUMN tariff_snapshot NUMERIC(12, 2)"))
                    if "is_voided" not in payment_cols:
                        conn.execute(
                            text("ALTER TABLE payments ADD COLUMN is_voided BOOLEAN NOT NULL DEFAULT FALSE")
                        )
                    conn.execute(
                        text(
                            "CREATE UNIQUE INDEX IF NOT EXISTS ix_payments_idempotency_key "
                            "ON payments (idempotency_key)"
                        )
                    )

                if version == 3 and "students" in tables and "student_tariff_history" in tables:
                    student_cols = {c["name"] for c in inspector.get_columns("students")}
                    if "enrolled_month" in student_cols and "history_verified_from" in student_cols:
                        conn.execute(
                            text(
                                """
                                INSERT INTO student_tariff_history
                                    (student_id, effective_from_month, monthly_fee)
                                SELECT s.id, s.enrolled_month, s.monthly_fee
                                FROM students s
                                WHERE NOT EXISTS (
                                    SELECT 1 FROM student_tariff_history h
                                    WHERE h.student_id = s.id
                                )
                                """
                            )
                        )

                if version == 4 and "students" in tables and "student_tariff_history" in tables:
                    conn.execute(
                        text(
                            """
                            UPDATE students
                            SET history_verified_from = :now_ym
                            WHERE history_verified_from IS NULL
                            """
                        ),
                        {"now_ym": now_ym},
                    )

                if version == 5 and "students" in tables:
                    student_cols = {c["name"] for c in inspect(conn).get_columns("students")}
                    if "archived_month" not in student_cols:
                        conn.execute(text("ALTER TABLE students ADD COLUMN archived_month VARCHAR(7)"))
                    conn.execute(
                        text(
                            """
                            UPDATE students
                            SET archived_month = SUBSTR(archived_at, 1, 7)
                            WHERE is_archived = TRUE AND archived_month IS NULL AND archived_at IS NOT NULL
                            """
                        )
                    )

                if version == 6 and "payments" in tables:
                    payment_cols = {c["name"] for c in inspect(conn).get_columns("payments")}
                    if "status" not in payment_cols:
                        conn.execute(
                            text("ALTER TABLE payments ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'COMPLETED'")
                        )
                    if "voided_at" not in payment_cols:
                        conn.execute(text("ALTER TABLE payments ADD COLUMN voided_at VARCHAR(40)"))
                    if "voided_by" not in payment_cols:
                        conn.execute(text("ALTER TABLE payments ADD COLUMN voided_by VARCHAR(80)"))
                    if "void_reason" not in payment_cols:
                        conn.execute(text("ALTER TABLE payments ADD COLUMN void_reason VARCHAR(500)"))
                    conn.execute(
                        text(
                            "UPDATE payments SET status = CASE WHEN is_voided IS TRUE THEN 'VOIDED' ELSE 'COMPLETED' END "
                            "WHERE status IS NULL OR status = ''"
                        )
                    )

                conn.execute(
                    text(
                        "INSERT INTO schema_migrations (version, applied_at) "
                        "VALUES (:version, :applied_at)"
                    ),
                    {
                        "version": version,
                        "applied_at": datetime.now(timezone.utc).isoformat(),
                    },
                )

        return LATEST_SCHEMA_VERSION



def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()