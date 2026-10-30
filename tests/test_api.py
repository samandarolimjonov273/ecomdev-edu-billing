from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db

TEST_ENGINE = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=TEST_ENGINE)
Base.metadata.create_all(bind=TEST_ENGINE)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


def test_full_billing_lifecycle():
    """Проверка полного цикла: создание учеников, частичные оплаты, расчёт должников и обновление тарифа."""
    r1 = client.post("/students", json={
        "name": "Иван Петров",
        "phone": "+998 90 100 10 10",
        "group_name": "Python #1",
        "monthly_fee": 1000000,
    })
    assert r1.status_code == 201
    st1_id = r1.json()["id"]

    r2 = client.post("/students", json={
        "name": "Анна Сидорова",
        "phone": "+998 90 200 20 20",
        "group_name": "Python #1",
        "monthly_fee": 1200000,
    })
    assert r2.status_code == 201
    st2_id = r2.json()["id"]

    # Внесение оплат за октябрь 2026
    client.post("/payments", json={"student_id": st1_id, "amount": 400000, "payment_date": "2026-10-05"})
    client.post("/payments", json={"student_id": st1_id, "amount": 250000, "payment_date": "2026-10-18"})
    client.post("/payments", json={"student_id": st2_id, "amount": 1200000, "payment_date": "2026-10-03"})

    st1_payments = client.get(f"/students/{st1_id}/payments")
    assert st1_payments.status_code == 200
    assert len(st1_payments.json()) == 2

    debtors_res = client.get("/debtors?month=2026-10")
    assert debtors_res.status_code == 200
    data = debtors_res.json()
    assert data["total_debtors"] == 1
    assert float(data["total_debt_sum"]) == 350000.0
    assert data["debtors"][0]["student_id"] == st1_id

    # Изменение тарифа ученика и проверка исчезновения из списка должников
    upd = client.patch(f"/students/{st1_id}", json={"monthly_fee": 650000})
    assert upd.status_code == 200
    assert client.get("/debtors?month=2026-10").json()["total_debtors"] == 0


def test_invalid_month_format_returns_400():
    """Некорректный формат месяца должен возвращать HTTP 400."""
    res = client.get("/debtors?month=2026-13")
    assert res.status_code == 400
    res2 = client.get("/debtors?month=october-2026")
    assert res2.status_code == 400


def test_negative_or_zero_payment_rejected_422():
    """Отрицательная или нулевая сумма оплаты должна блокироваться валидацией Pydantic (HTTP 422)."""
    st = client.post("/students", json={
        "name": "Тест Валидации",
        "phone": "+998 90 000 00 00",
        "group_name": "QA #1",
        "monthly_fee": 900000,
    }).json()

    bad_pay = client.post("/payments", json={"student_id": st["id"], "amount": -50000})
    assert bad_pay.status_code == 422


def test_payment_for_nonexistent_student_returns_404():
    """Попытка внести оплату для несуществующего ученика возвращает HTTP 404."""
    res = client.post("/payments", json={"student_id": 999999, "amount": 500000})
    assert res.status_code == 404


def test_csv_export_endpoint():
    """Проверка выгрузки отчёта должников в формате CSV (UTF-8 BOM для Excel)."""
    res = client.get("/debtors/export?month=2026-10")
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]
    assert "ФИО ученика" in res.text