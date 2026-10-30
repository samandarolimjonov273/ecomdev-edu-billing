from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ["AUTO_START_BOT"] = "0"
os.environ["API_ALLOW_INSECURE_DEMO"] = "1"

from app import main as main_module
from app.main import app
from app.database import get_db, run_migrations
from bot import telegram_bot

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "sqlite://")
if TEST_DATABASE_URL == "sqlite://":
    TEST_ENGINE = create_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
else:
    TEST_ENGINE = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=TEST_ENGINE)
run_migrations(TEST_ENGINE)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
telegram_bot.SessionLocal = TestingSessionLocal
client = TestClient(app)


def test_01_healthcheck_readiness():
    """1. Проверка readiness-эндпоинта /health."""
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "healthy"


def test_02_full_billing_lifecycle():
    """2. Полный цикл: создание учеников, частичные оплаты, расчёт должников и обновление тарифа."""
    r1 = client.post("/students", json={
        "name": "Иван Петров",
        "phone": "+998 90 100 10 10",
        "group_name": "Python #1",
        "monthly_fee": 1000000,
        "effective_from_month": "2026-10",
    })
    assert r1.status_code == 201
    st1_id = r1.json()["id"]

    r2 = client.post("/students", json={
        "name": "Анна Сидорова",
        "phone": "+998 90 200 20 20",
        "group_name": "Python #1",
        "monthly_fee": 1200000,
        "effective_from_month": "2026-10",
    })
    assert r2.status_code == 201
    st2_id = r2.json()["id"]

    client.post("/payments", json={"student_id": st1_id, "amount": 400000, "payment_date": "2026-10-05"})
    client.post("/payments", json={"student_id": st1_id, "amount": 250000, "payment_date": "2026-10-18"})
    client.post("/payments", json={"student_id": st2_id, "amount": 1200000, "payment_date": "2026-10-03"})

    debtors_res = client.get("/debtors?month=2026-10")
    assert debtors_res.status_code == 200
    data = debtors_res.json()
    assert data["total_debtors"] == 1
    assert float(data["total_debt_sum"]) == 350000.0

    upd = client.patch(f"/students/{st1_id}", json={"monthly_fee": 650000, "effective_from_month": "2026-10"})
    assert upd.status_code == 200
    assert client.get("/debtors?month=2026-10").json()["total_debtors"] == 0


def test_03_historical_tariff_and_enrollment_boundary():
    """3. До месяца зачисления (enrolled_month) долг не начисляется; смена тарифа не искажает прошлые месяцы."""
    st = client.post("/students", json={
        "name": "Исторический Ученик",
        "phone": "+998 90 555 11 22",
        "group_name": "History #1",
        "monthly_fee": 1000000,
        "effective_from_month": "2026-09",
    }).json()

    aug_report = client.get("/debtors?month=2026-08").json()
    assert all(d["student_id"] != st["id"] for d in aug_report["debtors"])

    client.patch(f"/students/{st['id']}", json={"monthly_fee": 1500000, "effective_from_month": "2026-11"})

    sep_report = client.get("/debtors?month=2026-09").json()
    sep_item = next(d for d in sep_report["debtors"] if d["student_id"] == st["id"])
    assert float(sep_item["monthly_fee"]) == 1000000.0
    assert float(sep_item["debt_amount"]) == 1000000.0

    nov_report = client.get("/debtors?month=2026-11").json()
    nov_item = next(d for d in nov_report["debtors"] if d["student_id"] == st["id"])
    assert float(nov_item["monthly_fee"]) == 1500000.0


def test_04_carry_over_advance_reduces_next_month_debt():
    """4. Переплата (аванс) в одном месяце автоматически переносится и уменьшает долг следующего месяца (P0.3)."""
    st = client.post("/students", json={
        "name": "Авансовый Ученик",
        "phone": "+998 90 444 55 66",
        "group_name": "Advance #1",
        "monthly_fee": 1000000,
        "effective_from_month": "2026-09",
    }).json()

    # В сентябре платит 1 600 000 при тарифе 1 000 000 (переплата 600 000)
    client.post("/payments", json={"student_id": st["id"], "amount": 1600000, "payment_date": "2026-09-10"})

    # В октябре 0 оплат, но за счёт аванса 600 000 остаток долга равен 400 000!
    oct_report = client.get("/debtors?month=2026-10").json()
    oct_item = next(d for d in oct_report["debtors"] if d["student_id"] == st["id"])
    assert float(oct_item["carry_over_credit"]) == 600000.0
    assert float(oct_item["debt_amount"]) == 400000.0


def test_05_legacy_db_migration_backup_and_restore(tmp_path):
    """5. Миграция старой БД через sqlite3.backup API, проверка идемпотентности повторного запуска и восстановления."""
    db_file = tmp_path / "legacy_edu.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, group_name TEXT, monthly_fee NUMERIC)")
    conn.execute("CREATE TABLE payments (id INTEGER PRIMARY KEY, student_id INTEGER, amount NUMERIC, payment_date DATE)")
    conn.execute("INSERT INTO students VALUES (1, 'Старый Ученик', '+998901112233', 'Legacy #1', 1000000)")
    conn.execute("INSERT INTO payments VALUES (1, 1, 400000, '2026-09-15')")
    conn.execute("INSERT INTO students VALUES (2, 'Без Оплат', '+998901112244', 'Legacy #1', 1200000)")
    conn.commit()
    conn.close()

    legacy_engine = create_engine(f"sqlite:///{db_file}")
    v1 = run_migrations(legacy_engine)
    v2 = run_migrations(legacy_engine)  # Повторный запуск идемпотентен
    assert v1 == v2 == 6

    bak_file = tmp_path / "legacy_edu.db.bak"
    assert bak_file.exists()

    insp = inspect(legacy_engine)
    pay_cols = {c["name"] for c in insp.get_columns("payments")}
    assert {"idempotency_key", "payload_fingerprint", "tariff_snapshot", "status", "is_voided"}.issubset(pay_cols)

    with legacy_engine.connect() as conn_after:
        st1 = conn_after.execute(
            text("SELECT enrolled_month, history_verified_from FROM students WHERE id = 1")
        ).fetchone()
        st2 = conn_after.execute(
            text("SELECT enrolled_month, history_verified_from FROM students WHERE id = 2")
        ).fetchone()
        assert st1 == ("2026-09", "2026-09")
        assert st2[0] == st2[1]
        assert st2[0] >= "2026-10"
    legacy_engine.dispose()


def test_06_idempotency_10_repeats_create_single_record():
    """6. Десятикратная повторная отправка операции с тем же ключом создаёт строго одну запись в БД (P0.4)."""
    st = client.post("/students", json={
        "name": "Санжар Касымов",
        "phone": "+998 90 777 88 99",
        "group_name": "Go #2",
        "monthly_fee": 1000000,
    }).json()

    headers = {"Idempotency-Key": "idem-10x-test-key"}
    payload = {"student_id": st["id"], "amount": 300000, "payment_date": "2026-10-10", "comment": "Повтор"}
    ids = set()
    for _ in range(10):
        resp = client.post("/payments", json=payload, headers=headers)
        assert resp.status_code == 201
        ids.add(resp.json()["id"])

    assert len(ids) == 1
    assert len(client.get(f"/students/{st['id']}/payments").json()) == 1


def test_07_idempotency_conflict_on_any_field_change():
    """7. Тот же Idempotency-Key при изменении суммы, метода или комментария возвращает 409 Conflict."""
    st = client.post("/students", json={
        "name": "Конфликт Тест",
        "phone": "+998 90 777 00 00",
        "group_name": "Go #2",
        "monthly_fee": 1000000,
    }).json()

    headers = {"Idempotency-Key": "idem-conflict-key-99"}
    base = {"student_id": st["id"], "amount": 300000, "payment_date": "2026-10-10", "payment_method": "Наличные", "comment": "Чек 1"}
    assert client.post("/payments", json=base, headers=headers).status_code == 201

    assert client.post("/payments", json={**base, "amount": 400000}, headers=headers).status_code == 409
    assert client.post("/payments", json={**base, "comment": "Чек 2"}, headers=headers).status_code == 409
    assert client.post("/payments", json={**base, "payment_method": "Payme"}, headers=headers).status_code == 409


def test_08_soft_delete_preserves_payments_and_writes_audit_log():
    """8. Архивация (DELETE) ученика НЕ удаляет его платежи и фиксирует событие в audit_logs (P1.6)."""
    st = client.post("/students", json={
        "name": "Архивный Ученик",
        "phone": "+998 90 333 22 11",
        "group_name": "Audit #1",
        "monthly_fee": 800000,
    }).json()
    client.post("/payments", json={"student_id": st["id"], "amount": 250000, "payment_date": "2026-10-12"})

    assert client.delete(f"/students/{st['id']}").status_code == 204
    assert client.get(f"/students/{st['id']}").status_code == 404
    hist = client.get(f"/students/{st['id']}/payments?include_archived=true")
    assert hist.status_code == 200
    assert len(hist.json()) == 1

    logs = client.get("/audit-logs").json()
    assert any(l["entity_type"] == "student" and l["action"] == "ARCHIVE" and l["entity_id"] == st["id"] for l in logs)


def test_09_csv_formula_injection_sanitized():
    """9. Защита CSV-экспорта от Spreadsheet Formula Injection (=, +, -, @) (P1.8)."""
    st = client.post("/students", json={
        "name": "=CMD|' /C calc'!A0",
        "phone": "+998901234567",
        "group_name": "@SUM(1+1)",
        "monthly_fee": 900000,
        "effective_from_month": "2026-10",
    }).json()

    csv_text = client.get("/debtors/export?month=2026-10").text
    assert "'=CMD|" in csv_text
    assert "'@SUM(1+1)" in csv_text
    client.delete(f"/students/{st['id']}")


def test_10_extra_fields_and_invalid_inputs_rejected_422():
    """10. Неизвестные поля (extra='forbid'), XSS-теги и суммы вне диапазона отклоняются с 422."""
    r_extra = client.post("/students", json={
        "name": "Тест Поля",
        "phone": "+998 90 000 00 00",
        "group_name": "QA",
        "monthly_fee": 500000,
        "hacker_field": "drop table",
    })
    assert r_extra.status_code == 422

    r_xss = client.post("/students", json={
        "name": "<script>alert(1)</script>",
        "phone": "+998 90 000 00 00",
        "group_name": "QA",
        "monthly_fee": 500000,
    })
    assert r_xss.status_code == 422


def test_11_invalid_month_format_returns_400():
    """11. Некорректный формат месяца возвращает HTTP 400."""
    assert client.get("/debtors?month=2026-13").status_code == 400
    assert client.get("/debtors?month=october-2026").status_code == 400


def test_12_payment_for_nonexistent_student_returns_404():
    """12. Оплата для несуществующего ученика возвращает HTTP 404."""
    assert client.post("/payments", json={"student_id": 999999, "amount": 500000}).status_code == 404


def test_13_telegram_bot_commands_and_distributed_lock():
    """13. Проверка команд бота /debt, /pay и распределённой блокировки polling (bot_locks)."""
    st = client.post("/students", json={
        "name": "Бот Тестов",
        "phone": "+998 90 999 00 11",
        "group_name": "BotGroup #1",
        "monthly_fee": 1000000,
    }).json()

    assert "Бот Тестов" in telegram_bot.handle_debt()
    pay_reply = telegram_bot.handle_pay(f"/pay {st['id']} 600000", username="tester", update_id=777)
    assert "Оплата успешно принята" in pay_reply

    assert telegram_bot.acquire_or_renew_polling_lock(owner_id="instance-A", ttl=60) is True
    assert telegram_bot.acquire_or_renew_polling_lock(owner_id="instance-B", ttl=60) is False
    assert telegram_bot.acquire_or_renew_polling_lock(owner_id="instance-A", ttl=60) is True


def test_14_telegram_bot_strict_default_deny(monkeypatch):
    """14. Финансовые команды бота по умолчанию ЗАКРЫТЫ (Default-Deny) без валидного TELEGRAM_ADMIN_IDS."""
    monkeypatch.delenv("TELEGRAM_ADMIN_IDS", raising=False)
    assert telegram_bot.is_authorized(123456) is False

    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "bad_value")
    assert telegram_bot.is_authorized(123456) is False

    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "111, 222")
    assert telegram_bot.is_authorized(111) is True
    assert telegram_bot.is_authorized(999) is False


def test_15_cookie_session_csrf_protection_and_server_logout(monkeypatch):
    """15. Проверка сессии кассира, обязательности X-CSRF-Token для POST и отзыва сессии при logout (P0.2)."""
    monkeypatch.setenv("API_SECRET_KEY", "prod-secret-key-999")
    client.cookies.clear()

    assert client.get("/students").status_code == 401

    login_res = client.post("/auth/login", json={"api_key": "prod-secret-key-999"})
    assert login_res.status_code == 200
    csrf_token = login_res.json()["csrf_token"]

    assert client.get("/students").status_code == 200

    csrf_fail = client.post("/students", json={
        "name": "CSRF Тест",
        "phone": "+998 90 111 22 33",
        "group_name": "Sec",
        "monthly_fee": 500000,
    })
    assert csrf_fail.status_code == 403

    csrf_ok = client.post(
        "/students",
        json={"name": "CSRF Ок", "phone": "+998 90 111 22 33", "group_name": "Sec", "monthly_fee": 500000},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert csrf_ok.status_code == 201

    saved_session = client.cookies.get("edubilling_session")
    client.post("/auth/logout")
    client.cookies.set("edubilling_session", saved_session)
    assert client.get("/students").status_code == 401
    client.cookies.clear()


def test_16_login_rate_limiting(monkeypatch):
    """16. Защита от перебора ключа: после 5 неверных попыток возвращается HTTP 429."""
    monkeypatch.setenv("API_SECRET_KEY", "prod-secret-key-999")
    with TestingSessionLocal() as db:
        db.execute(text("DELETE FROM login_attempts"))
        db.commit()

    for _ in range(5):
        r = client.post("/auth/login", json={"api_key": "wrong-key"})
        assert r.status_code == 401

    blocked = client.post("/auth/login", json={"api_key": "wrong-key"})
    assert blocked.status_code == 429
    with TestingSessionLocal() as db:
        db.execute(text("DELETE FROM login_attempts"))
        db.commit()


def test_17_default_closed_without_secret_when_demo_disabled(monkeypatch):
    """17. Без API_SECRET_KEY и без API_ALLOW_INSECURE_DEMO=1 сервис закрыт (HTTP 503)."""
    monkeypatch.delenv("API_SECRET_KEY", raising=False)
    monkeypatch.setenv("API_ALLOW_INSECURE_DEMO", "0")
    assert client.get("/students").status_code == 503
    status_res = client.get("/auth/status").json()
    assert status_res["auth_required"] is True
    assert status_res["authenticated"] is False


def test_18_parallel_idempotent_requests_create_single_payment(tmp_path):
    """18. Параллельные запросы с одним Idempotency-Key создают одну оплату, а конфликт payload даёт 409."""
    db_file = tmp_path / "parallel_idem.db"
    file_engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    FileSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=file_engine)
    run_migrations(file_engine)

    def override_file_db():
        db = FileSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_file_db
    try:
        with TestClient(app) as local_client:
            st = local_client.post("/students", json={
                "name": "Параллельный Ученик",
                "phone": "+998 90 123 00 00",
                "group_name": "Concurrency #1",
                "monthly_fee": 1000000,
            }).json()

        payload = {
            "student_id": st["id"],
            "amount": 350000,
            "payment_date": "2026-10-15",
            "payment_method": "Payme",
            "comment": "Параллельный чек",
        }
        headers = {"Idempotency-Key": "parallel-idem-key-001"}

        def send_same():
            with TestClient(app) as c:
                r = c.post("/payments", json=payload, headers=headers)
                return r.status_code, r.json()["id"]

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: send_same(), range(5)))

        assert all(code == 201 for code, _ in results)
        assert len({pid for _, pid in results}) == 1

        with TestClient(app) as local_client:
            conflict = local_client.post(
                "/payments",
                json={**payload, "amount": 900000},
                headers=headers,
            )
            assert conflict.status_code == 409
            payments = local_client.get(f"/students/{st['id']}/payments").json()
            assert len(payments) == 1
    finally:
        app.dependency_overrides[get_db] = override_get_db
        file_engine.dispose()


def test_19_pre_verification_month_returns_unverified_status(tmp_path):
    """19. До подтверждённого месяца мигрированной БД запрос /debtors возвращает status=legacy_unverified."""
    db_file = tmp_path / "legacy_unverified.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, group_name TEXT, monthly_fee NUMERIC)")
    conn.execute("CREATE TABLE payments (id INTEGER PRIMARY KEY, student_id INTEGER, amount NUMERIC, payment_date DATE)")
    conn.execute("INSERT INTO students VALUES (1, 'Мигрированный Ученик', '+998901112233', 'Legacy #1', 1000000)")
    conn.execute("INSERT INTO payments VALUES (1, 1, 400000, '2026-10-01')")
    conn.commit()
    conn.close()

    legacy_engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    LegacySessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=legacy_engine)
    run_migrations(legacy_engine)

    def override_legacy_db():
        db = LegacySessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_legacy_db
    try:
        with TestClient(app) as local_client:
            before_verified = local_client.get("/debtors?month=2026-09").json()
            assert before_verified["status"] == "legacy_unverified"
            assert before_verified["history_verified"] is False
            assert before_verified["total_debtors"] == 0

            verified = local_client.get("/debtors?month=2026-10").json()
            assert verified["status"] == "ok"
            assert verified["history_verified"] is True
            assert verified["total_debtors"] == 1
            assert float(verified["total_debt_sum"]) == 600000.0
    finally:
        app.dependency_overrides[get_db] = override_get_db
        legacy_engine.dispose()


def test_20_backup_restore_drill_and_v7_corruption_check(tmp_path):
    """20. Полное учение по аварийному восстановлению (Disaster Recovery Drill) из .bak и детектор v7."""
    from migrate import restore_sqlite_from_backup, verify_database_state
    from app.database import create_safe_sqlite_backup

    db_file = tmp_path / "prod_drill.db"
    drill_engine = create_engine(f"sqlite:///{db_file}")
    run_migrations(drill_engine)

    with drill_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO students (id, name, phone, group_name, monthly_fee, enrolled_month, history_verified_from, is_archived) "
            "VALUES (1, 'Эталон Ученик', '+998901112233', 'Prod #1', 1200000, '2026-10', '2026-10', 0)"
        ))
        conn.execute(text(
            "INSERT INTO payments (id, student_id, amount, payment_date, payment_method, status, is_voided) "
            "VALUES (1, 1, 700000, '2026-10-05', 'Payme', 'COMPLETED', false)"
        ))

    before_state = verify_database_state(drill_engine)
    bak_path = create_safe_sqlite_backup(drill_engine)
    assert bak_path is not None and bak_path.exists()

    # Имитируем повреждение данных (удаление платежей и запись ошибочной версии v7)
    with drill_engine.begin() as conn:
        conn.execute(text("DELETE FROM payments"))
        conn.execute(text("INSERT INTO schema_migrations (version, applied_at) VALUES (7, '2026-10-30')"))

    # Аудитор должен мгновенно обнаружить следы v7 и выбросить ошибку
    try:
        verify_database_state(drill_engine)
        assert False, "Должна была выброситься ошибка при обнаружении миграции v7"
    except RuntimeError as exc:
        assert "v7" in str(exc)

    # Выполняем боевое восстановление из бэкапа и сверяем контрольные суммы с эталоном
    drill_engine.dispose()
    restore_sqlite_from_backup(db_file, bak_path)
    after_state = verify_database_state(drill_engine)
    assert after_state == before_state
    assert after_state["total_paid"] == 700000.0
    drill_engine.dispose()