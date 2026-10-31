"""Сквозная проверка на настоящем PostgreSQL. Запускается, только если задан TEST_DATABASE_URL (в CI задан)."""
import os
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import crud, models, schemas
from app.database import run_migrations

URL = os.getenv("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.startswith("postgresql"), reason="TEST_DATABASE_URL не задан")


def test_fresh_postgres_migrates_and_computes_debtors():
    engine = create_engine(URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    run_migrations(engine)  # раньше падало: boolean = integer

    db = sessionmaker(bind=engine)()
    try:
        st = crud.create_student(db, schemas.StudentCreate(
            name="PG Тест", phone="12345", group_name="PG", monthly_fee=1000, effective_from_month="2026-10"))
        crud.create_payment(db, schemas.PaymentCreate(student_id=st.id, amount=400, payment_date="2026-10-05"), student=st)
        report = crud.get_debtors_for_month(db, 2026, 10)
        assert [(d.student_id, d.debt_amount) for d in report.debtors] == [(st.id, Decimal("600.00"))]
        assert crud.verify_audit_chain(db)["ok"] is True
    finally:
        db.close()
        engine.dispose()
