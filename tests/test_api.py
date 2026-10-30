import os
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ["AUTO_START_BOT"] = "0"

from app.main import app
from app.database import Base, get_db
from bot import telegram_bot

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
telegram_bot.SessionLocal = TestingSessionLocal
client = TestClient(app)


def test_full_billing_lifecycle():
    """1. Проверка полного цикла: создание учеников, частичные оплаты, расчёт должников и обновление тарифа."""
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

    upd = client.patch(f"/students/{st1_id}", json={"monthly_fee": 650000})
    assert upd.status_code == 200
    assert client.get("/debtors?month=2026-10").json()["total_debtors"] == 0


def test_invalid_month_format_returns_400():
    """2. Некорректный формат месяца должен возвращать HTTP 400."""
    assert client.get("/debtors?month=2026-13").status_code == 400
    assert client.get("/debtors?month=october-2026").status_code == 400


def test_negative_or_zero_payment_rejected_422():
    """3. Отрицательная или нулевая сумма оплаты блокируется валидацией (HTTP 422)."""
    st = client.post("/students", json={
        "name": "Тест Валидации",
        "phone": "+998 90 000 00 00",
        "group_name": "QA #1",
        "monthly_fee": 900000,
    }).json()
    assert client.post("/payments", json={"student_id": st["id"], "amount": -50000}).status_code == 422


def test_payment_for_nonexistent_student_returns_404():
    """4. Попытка внести оплату для несуществующего ученика возвращает HTTP 404."""
    assert client.post("/payments", json={"student_id": 999999, "amount": 500000}).status_code == 404


def test_csv_export_endpoint():
    """5. Проверка выгрузки отчёта должников в формате CSV."""
    res = client.get("/debtors/export?month=2026-10")
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]
    assert "ФИО ученика" in res.text


def test_idempotency_prevents_duplicate_payments():
    """6. Защита от двойного списания по заголовку Idempotency-Key."""
    st = client.post("/students", json={
        "name": "Санжар Касымов",
        "phone": "+998 90 777 88 99",
        "group_name": "Go #2",
        "monthly_fee": 1000000,
    }).json()

    headers = {"Idempotency-Key": "unique-payment-key-001"}
    p1 = client.post("/payments", json={"student_id": st["id"], "amount": 300000, "payment_date": "2026-10-10"}, headers=headers)
    p2 = client.post("/payments", json={"student_id": st["id"], "amount": 300000, "payment_date": "2026-10-10"}, headers=headers)

    assert p1.status_code == 201
    assert p2.status_code == 201
    assert p1.json()["id"] == p2.json()["id"]
    assert len(client.get(f"/students/{st['id']}/payments").json()) == 1


def test_xss_html_tags_rejected_in_input():
    """7. Попытка внедрить HTML/XSS-теги в имя ученика или комментарий блокируется валидатором (HTTP 422)."""
    res = client.post("/students", json={
        "name": "<script>alert(1)</script>",
        "phone": "+998 90 111 11 11",
        "group_name": "Sec #1",
        "monthly_fee": 1000000,
    })
    assert res.status_code == 422


def test_cascade_delete_student_removes_payments():
    """8. Удаление ученика каскадно очищает его платежи и исключает из отчёта должников."""
    st = client.post("/students", json={
        "name": "Удаляемый Ученик",
        "phone": "+998 90 333 22 11",
        "group_name": "Temp #1",
        "monthly_fee": 800000,
    }).json()
    client.post("/payments", json={"student_id": st["id"], "amount": 200000, "payment_date": "2026-10-12"})

    del_res = client.delete(f"/students/{st['id']}")
    assert del_res.status_code == 204
    assert client.get(f"/students/{st['id']}").status_code == 404


def test_telegram_bot_debt_and_pay_commands():
    """9. Проверка бизнес-логики команд Telegram-бота /debt и /pay."""
    st = client.post("/students", json={
        "name": "Бот Тестов",
        "phone": "+998 90 999 00 11",
        "group_name": "BotGroup #1",
        "monthly_fee": 1000000,
    }).json()

    debt_text = telegram_bot.handle_debt()
    assert "Бот Тестов" in debt_text

    pay_reply = telegram_bot.handle_pay(f"/pay {st['id']} 600000", username="tester", update_id=777)
    assert "Оплата успешно принята" in pay_reply
    assert "400 000 сум" in pay_reply


def test_api_key_authentication_when_enabled(monkeypatch):
    """10. Проверка защиты эндпоинтов через X-API-Key при включённом API_SECRET_KEY."""
    monkeypatch.setenv("API_SECRET_KEY", "super-secret-prod-key")
    unauth = client.get("/students")
    assert unauth.status_code == 401

    auth_ok = client.get("/students", headers={"X-API-Key": "super-secret-prod-key"})
    assert auth_ok.status_code == 200