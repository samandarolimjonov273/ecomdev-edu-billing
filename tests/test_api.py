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
    # 1. Создание двух учеников
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

    # 2. Внесение оплат за октябрь 2026:
    client.post("/payments", json={"student_id": st1_id, "amount": 400000, "payment_date": "2026-10-05"})
    client.post("/payments", json={"student_id": st1_id, "amount": 250000, "payment_date": "2026-10-18"})
    client.post("/payments", json={"student_id": st2_id, "amount": 1200000, "payment_date": "2026-10-03"})

    # 3. Проверка списка оплат по ученику
    st1_payments = client.get(f"/students/{st1_id}/payments")
    assert st1_payments.status_code == 200
    assert len(st1_payments.json()) == 2

    # 4. Проверка эндпоинта должников за 2026-10
    debtors_res = client.get("/debtors?month=2026-10")
    assert debtors_res.status_code == 200
    data = debtors_res.json()
    assert data["total_debtors"] == 1
    assert float(data["total_debt_sum"]) == 350000.0
    assert data["debtors"][0]["student_id"] == st1_id
    assert float(data["debtors"][0]["paid_amount"]) == 650000.0
    assert float(data["debtors"][0]["debt_amount"]) == 350000.0

    # 5. Изменение тарифа ученика
    upd = client.patch(f"/students/{st1_id}", json={"monthly_fee": 650000})
    assert upd.status_code == 200

    debtors_after = client.get("/debtors?month=2026-10").json()
    assert debtors_after["total_debtors"] == 0
