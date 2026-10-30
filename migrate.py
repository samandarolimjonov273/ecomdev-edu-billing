import argparse
import sqlite3
from pathlib import Path
from sqlalchemy import text
from app.database import engine, run_migrations, create_safe_sqlite_backup


def restore_sqlite_from_backup(db_path: Path, backup_path: Path) -> None:
    """Атомарное восстановление базы SQLite из резервной копии через нативный sqlite3.backup API."""
    if not backup_path.exists():
        raise FileNotFoundError(f"Файл резервной копии не найден: {backup_path}")

    src_conn = sqlite3.connect(str(backup_path))
    try:
        chk = src_conn.execute("PRAGMA integrity_check").fetchone()
        if not chk or chk[0].lower() != "ok":
            raise RuntimeError(f"Резервная копия повреждена: {chk}")
        dst_conn = sqlite3.connect(str(db_path))
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def verify_database_state(target_engine=engine) -> dict:
    """Проверяет целостность БД, отсутствие следов ошибочной миграции v7 и сверяет контрольные суммы."""
    with target_engine.connect() as conn:
        versions = [
            r[0] for r in conn.execute(text("SELECT version FROM schema_migrations ORDER BY version")).fetchall()
        ]
        if 7 in versions:
            raise RuntimeError(
                "ОБНАРУЖЕНА ОШИБОЧНАЯ МИГРАЦИЯ v7! База содержит искажённые исторические даты. "
                "Восстановите базу из чистой резервной копии (python migrate.py --restore) или пересоздайте из первичного источника."
            )
        students_count = conn.execute(text("SELECT COUNT(*) FROM students")).scalar() or 0
        payments_count = conn.execute(text("SELECT COUNT(*) FROM payments WHERE is_voided = false")).scalar() or 0
        total_paid = conn.execute(text("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE is_voided = false")).scalar() or 0

    return {
        "versions": versions,
        "students_count": int(students_count),
        "payments_count": int(payments_count),
        "total_paid": float(total_paid),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Управление миграциями, бэкапом и восстановлением БД.")
    parser.add_argument("--backup", action="store_true", help="Создать резервную копию БД перед операцией")
    parser.add_argument("--restore", type=str, default=None, help="Путь к .bak файлу для восстановления БД")
    parser.add_argument("--verify", action="store_true", help="Проверить целостность данных и отсутствие v7")
    args = parser.parse_args()

    if args.restore:
        raw_db = engine.url.database
        if not raw_db or raw_db == ":memory:":
            raise RuntimeError("Восстановление поддерживается только для файловой БД SQLite.")
        engine.dispose()
        restore_sqlite_from_backup(Path(raw_db), Path(args.restore))
        state = verify_database_state(engine)
        print(f"✅ БД успешно восстановлена из {args.restore}. Состояние: {state}")
    else:
        if args.backup:
            bak = create_safe_sqlite_backup(engine)
            print(f"📦 Резервная копия создана: {bak}")
        ver = run_migrations(engine)
        state = verify_database_state(engine)
        print(f"✅ Схема БД мигрирована и проверена (v{ver}). Сверка данных: {state}")