"""Тесты, привязанные к пунктам тестового задания (часть 1 и 2)."""
import os

from fastapi.testclient import TestClient

from tests import test_api  # переиспользуем изолированную in-memory БД и override get_db
from app.main import app
from bot import telegram_bot

client = TestClient(app)


def _student(name, fee, month="2026-09"):
    r = client.post("/students", json={
        "name": name, "phone": "+998 90 000 00 00", "group_name": "Spec", "monthly_fee": fee,
        "effective_from_month": month,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _pay(student_id, amount, day):
    r = client.post("/payments", json={"student_id": student_id, "amount": amount, "payment_date": day})
    assert r.status_code == 201, r.text


def _item(report, sid):
    return next((d for d in report["debtors"] if d["student_id"] == sid), None)


def test_students_crud():
    sid = _student("Crud Тест", 500000)
    assert any(s["id"] == sid for s in client.get("/students").json())
    upd = client.put(f"/students/{sid}", json={"group_name": "Новая группа"})
    assert upd.status_code == 200 and upd.json()["group_name"] == "Новая группа"
    assert client.delete(f"/students/{sid}").status_code == 204
    assert all(s["id"] != sid for s in client.get("/students").json())


def test_payments_added_and_listed_per_student():
    sid = _student("Платежи Тест", 500000)
    _pay(sid, 100000, "2026-10-02")
    _pay(sid, 50000, "2026-10-09")
    items = client.get(f"/students/{sid}/payments").json()
    assert [float(p["amount"]) for p in items] == [50000.0, 100000.0]  # свежие сверху


def test_debtors_is_literal_per_month_and_ignores_past_months_in_debt_amount():
    sid = _student("Только октябрь", 1000, "2026-09")
    _pay(sid, 1000, "2026-10-05")  # октябрь оплачен полностью, сентябрь не оплачен
    assert _item(client.get("/debtors?month=2026-10").json(), sid) is None
    sep = _item(client.get("/debtors?month=2026-09").json(), sid)
    assert float(sep["debt_amount"]) == 1000.0


def test_debtors_partial_payment_and_prior_arrears_reported_separately():
    sid = _student("Частично", 1000, "2026-09")
    _pay(sid, 400, "2026-10-05")
    item = _item(client.get("/debtors?month=2026-10").json(), sid)
    assert float(item["paid_amount"]) == 400.0
    assert float(item["debt_amount"]) == 600.0          # долг ЗА октябрь
    assert float(item["prior_arrears"]) == 1000.0       # сентябрь отдельно
    assert float(item["total_outstanding"]) == 1600.0


def test_voided_payment_does_not_count():
    sid = _student("Отмена", 1000, "2026-10")
    r = client.post("/payments", json={"student_id": sid, "amount": 1000, "payment_date": "2026-10-03"})
    assert _item(client.get("/debtors?month=2026-10").json(), sid) is None
    client.post(f"/payments/{r.json()['id']}/void", json={"reason": "ошибка кассира"})
    assert float(_item(client.get("/debtors?month=2026-10").json(), sid)["debt_amount"]) == 1000.0


def test_audit_chain_verifies_and_detects_tampering():
    _student("Аудит", 1000)
    assert client.get("/audit-logs/verify").json()["ok"] is True
    from sqlalchemy import text
    with test_api.TEST_ENGINE.begin() as conn:
        original = conn.execute(text("SELECT id, details FROM audit_logs ORDER BY id LIMIT 1")).one()
        conn.execute(text("UPDATE audit_logs SET details = 'подделка' WHERE id = :i"), {"i": original.id})
    try:
        assert client.get("/audit-logs/verify").json()["ok"] is False
    finally:  # общая in-memory БД: возвращаем как было, чтобы не влиять на другие тесты
        with test_api.TEST_ENGINE.begin() as conn:
            conn.execute(text("UPDATE audit_logs SET details = :d WHERE id = :i"), {"d": original.details, "i": original.id})
    assert client.get("/audit-logs/verify").json()["ok"] is True


def test_csp_allows_dashboard_and_swagger_assets():
    dash = client.get("/").headers["content-security-policy"]
    assert "https://cdn.tailwindcss.com" in dash and "'unsafe-inline'" in dash
    docs = client.get("/docs").headers["content-security-policy"]
    assert "https://cdn.jsdelivr.net" in docs
    assert "script-src" not in client.get("/health").headers["content-security-policy"]


def test_bot_pay_rejects_bad_amounts_with_a_reply():
    sid = _student("Бот", 1000, "2026-10")
    for bad in ("100.555", "-5", "abc", "NaN", "Infinity", "0"):
        reply = telegram_bot.handle_pay(f"/pay {sid} {bad}", "tester", update_id=None)
        assert reply.startswith("❌"), (bad, reply)
    ok = telegram_bot.handle_pay(f"/pay {sid} 250.50", "tester", update_id=None)
    assert ok.startswith("✅")


def test_bot_pay_unknown_student():
    assert "не найден" in telegram_bot.handle_pay("/pay 999999 1000", "tester", update_id=None)


def test_payment_before_enrollment_month_is_rejected():
    sid = _student("Поздний набор", 1000, "2026-10")
    r = client.post("/payments", json={"student_id": sid, "amount": 1000, "payment_date": "2026-09-15"})
    assert r.status_code == 422
    assert "зачисления" in r.json()["detail"]


def test_bot_rejects_nothing_extra_but_api_accepts_enrollment_month_payment():
    sid = _student("Граница", 1000, "2026-10")
    r = client.post("/payments", json={"student_id": sid, "amount": 1000, "payment_date": "2026-10-01"})
    assert r.status_code == 201


def test_include_arrears_lists_students_with_only_old_debt():
    sid = _student("Старый долг", 1000, "2026-09")
    _pay(sid, 1000, "2026-10-05")  # октябрь закрыт, сентябрь — нет
    default = client.get("/debtors?month=2026-10").json()
    assert _item(default, sid) is None
    full = client.get("/debtors?month=2026-10&include_arrears=true").json()
    item = _item(full, sid)
    assert float(item["debt_amount"]) == 0.0 and float(item["total_outstanding"]) == 1000.0
    assert float(full["total_outstanding_sum"]) >= 1000.0


def test_today_uses_tashkent_timezone():
    from datetime import datetime, timezone
    from app import timeutil
    assert timeutil.APP_TIMEZONE == "Asia/Tashkent"
    # Toshkent = UTC+5: разница с UTC-датой не больше одного дня, и текущий месяц — строка YYYY-MM
    assert abs((timeutil.today() - datetime.now(timezone.utc).date()).days) <= 1
    assert len(timeutil.current_month()) == 7


def test_production_config_rejects_dev_placeholder_and_insecure_cookie(monkeypatch):
    import pytest
    from app.config import Settings
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("API_ALLOW_INSECURE_DEMO", "0")
    monkeypatch.setenv("API_SECRET_KEY", "dev-only-change-me-0123456789abcdefghijkl")
    monkeypatch.setenv("COOKIE_SECURE", "1")
    with pytest.raises(RuntimeError):
        Settings.validate_runtime_config()
    monkeypatch.setenv("API_SECRET_KEY", "x7Kq9" * 8)
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:8000")
    with pytest.raises(RuntimeError):
        Settings.validate_runtime_config()
    monkeypatch.setenv("COOKIE_SECURE", "1")
    Settings.validate_runtime_config()
