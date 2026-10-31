import asyncio
import csv
import hashlib
import hmac
import io
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import Cookie, FastAPI, Depends, Header, HTTPException, Query, Request, Response, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models, schemas, crud
from app.config import settings
from app.database import get_db
from app.timeutil import current_month

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "edubilling_session"
CSRF_COOKIE_NAME = "edubilling_csrf"
SESSION_TTL_SECONDS = 86400
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 60

FORMULA_PREFIXES = ("=", "+", "-", "@")

_CSP_COMMON = "base-uri 'self'; frame-ancestors 'none'; object-src 'none'; form-action 'self'"
# JSON-ответы API: ничего не грузим.
CSP_API = f"default-src 'self'; {_CSP_COMMON}"
# Дашборд: Tailwind CDN + inline-скрипт/стили самой страницы (в планах: убрать CDN, см. README).
CSP_DASHBOARD = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
    f"style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; {_CSP_COMMON}"
)
# Swagger UI / ReDoc берут ассеты с jsDelivr.
CSP_DOCS = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.jsdelivr.net; "
    f"worker-src 'self' blob:; connect-src 'self'; {_CSP_COMMON}"
)


def is_insecure_demo_allowed() -> bool:
    """Поддерживает как новый API_ALLOW_INSECURE_DEMO, так и старый ALLOW_INSECURE_DEMO."""
    return settings.is_demo_allowed()


def sanitize_csv_cell(val: object) -> str:
    """Защищает ячейку CSV от Spreadsheet Formula Injection (P1.8)."""
    s = str(val if val is not None else "")
    probe = s.lstrip(" \t\r\n\0\x0b\x0c")
    if probe.startswith(FORMULA_PREFIXES):
        return "'" + s
    return s


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_runtime_config()
    if not settings.api_secret_key():
        demo_enabled = is_insecure_demo_allowed()
        if not demo_enabled:
            raise RuntimeError("API_SECRET_KEY is required unless explicit insecure development mode is enabled")
    bot_task: Optional[asyncio.Task] = None
    if os.getenv("TELEGRAM_BOT_TOKEN") and settings.auto_start_bot():
        from bot.telegram_bot import run_bot
        bot_task = asyncio.create_task(run_bot())
    yield
    if bot_task:
        bot_task.cancel()


app = FastAPI(
    title="Ecomdev EduBilling API",
    description="Система финансового учёта оплат и контроля задолженностей учебного центра.",
    version="2.0.0",
    lifespan=lifespan,
)

allowed_origins = [
    o.strip()
    for o in settings.cors_origins().split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
    path = request.url.path
    if path in ("/docs", "/redoc"):
        csp = CSP_DOCS
    elif path == "/":
        csp = CSP_DASHBOARD
    else:
        csp = CSP_API
    response.headers.setdefault("Content-Security-Policy", csp)
    return response

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_access(
    request: Request,
    x_api_key: Optional[str] = Security(api_key_header),
    x_csrf_token: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
    session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: Session = Depends(get_db),
) -> str:
    """
    Проверяет авторизацию и возвращает идентификатор актора для аудита.
    """
    required_key = settings.api_secret_key()
    if not required_key:
        demo_enabled = is_insecure_demo_allowed()
        if demo_enabled:
            return "demo-operator"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_SECRET_KEY is not configured",
        )

    if x_api_key and hmac.compare_digest(x_api_key, required_key):
        return "api-key-client"

    now = int(time.time())
    if session_id:
        session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        sess = db.get(models.WebSession, session_hash)
        if sess and sess.expires_at > now:
            if request.method in ("POST", "PUT", "PATCH", "DELETE") and not request.url.path.endswith("/auth/logout"):
                supplied_hash = hashlib.sha256((x_csrf_token or "").encode("utf-8")).hexdigest()
                if not x_csrf_token or not hmac.compare_digest(supplied_hash, sess.csrf_hash):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Отсутствует или недействителен X-CSRF-Token",
                    )
            return "web-cashier"
        if sess:
            db.delete(sess)
            db.commit()

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Требуется авторизация (сессия кассира или заголовок X-API-Key)",
    )


MONTH_REGEX = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


# ==================== 0. HEALTHCHECK И АВТОРИЗАЦИЯ ====================
@app.get("/health", tags=["0. Система и Авторизация"], summary="Проверка готовности сервиса и БД (Readiness Probe)")
def healthcheck_endpoint(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "healthy", "database": "ok"}


@app.get("/auth/status", tags=["0. Система и Авторизация"], summary="Статус сессии кассира и CSRF-токен")
def auth_status_endpoint(
    session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    csrf_cookie: Optional[str] = Cookie(default=None, alias=CSRF_COOKIE_NAME),
    db: Session = Depends(get_db),
):
    required_key = settings.api_secret_key()
    if not required_key:
        demo_enabled = is_insecure_demo_allowed()
        return {
            "auth_required": not demo_enabled,
            "authenticated": demo_enabled,
            "csrf_token": None,
        }

    now = int(time.time())
    session_hash = hashlib.sha256((session_id or "").encode("utf-8")).hexdigest()
    sess = db.get(models.WebSession, session_hash) if session_id else None
    if sess and sess.expires_at > now:
        csrf_hash = hashlib.sha256((csrf_cookie or "").encode("utf-8")).hexdigest()
        valid_csrf = csrf_cookie if hmac.compare_digest(csrf_hash, sess.csrf_hash) else None
        return {"auth_required": True, "authenticated": True, "csrf_token": valid_csrf}
    return {"auth_required": True, "authenticated": False, "csrf_token": None}


@app.post("/auth/login", tags=["0. Система и Авторизация"], summary="Вход кассира (HttpOnly Session + CSRF + Rate Limit)")
def auth_login_endpoint(
    payload: schemas.LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    client_ip = request.client.host if request.client else "local"
    now = time.time()
    now_int = int(now)
    client_key = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()
    rate_limit = db.get(models.LoginAttempt, client_key)
    if rate_limit and now_int - rate_limit.window_started_at < LOGIN_WINDOW_SECONDS:
        attempts = rate_limit.attempts
    else:
        attempts = 0
    if attempts >= MAX_LOGIN_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много попыток входа. Повторите через минуту.",
        )

    required_key = settings.api_secret_key()
    if not required_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_SECRET_KEY is not configured",
        )
    if not hmac.compare_digest(payload.api_key.strip(), required_key):
        rate_result = db.execute(
            text(
                """
                INSERT INTO login_attempts (client_key, window_started_at, attempts)
                VALUES (:key, :now, 1)
                ON CONFLICT (client_key) DO UPDATE SET
                    window_started_at = CASE
                        WHEN login_attempts.window_started_at <= :cutoff THEN :now
                        ELSE login_attempts.window_started_at
                    END,
                    attempts = CASE
                        WHEN login_attempts.window_started_at <= :cutoff THEN 1
                        ELSE login_attempts.attempts + 1
                    END
                RETURNING attempts
                """
            ),
            {"key": client_key, "now": now_int, "cutoff": now_int - LOGIN_WINDOW_SECONDS},
        )
        failed_attempts = rate_result.scalar_one()
        db.query(models.LoginAttempt).filter(
            models.LoginAttempt.window_started_at < now_int - 86400
        ).delete(synchronize_session=False)
        db.commit()
        if failed_attempts > MAX_LOGIN_ATTEMPTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Слишком много попыток входа. Повторите через минуту.",
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный ключ доступа")

    db.query(models.WebSession).filter(models.WebSession.expires_at <= now_int).delete(
        synchronize_session=False
    )
    db.query(models.LoginAttempt).filter(
        models.LoginAttempt.window_started_at < now_int - 86400
    ).delete(synchronize_session=False)
    db.execute(
        text(
            """
            INSERT INTO login_attempts (client_key, window_started_at, attempts)
            VALUES (:key, :now, 0)
            ON CONFLICT (client_key) DO UPDATE SET
                window_started_at = :now,
                attempts = 0
            """
        ),
        {"key": client_key, "now": now_int},
    )
    session_id = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    db.add(
        models.WebSession(
            session_hash=hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
            csrf_hash=hashlib.sha256(csrf_token.encode("utf-8")).hexdigest(),
            expires_at=now_int + SESSION_TTL_SECONDS,
        )
    )
    db.commit()

    secure_cookie = settings.cookie_secure()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        secure=secure_cookie,
        samesite="strict",
        max_age=SESSION_TTL_SECONDS,
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=secure_cookie,
        samesite="strict",
        max_age=SESSION_TTL_SECONDS,
    )
    return {"status": "ok", "csrf_token": csrf_token}


@app.post("/auth/logout", tags=["0. Система и Авторизация"], summary="Выход кассира (отзыв сессии на сервере)")
def auth_logout_endpoint(
    response: Response,
    session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    actor: str = Depends(verify_access),
    db: Session = Depends(get_db),
):
    if session_id:
        session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        sess = db.get(models.WebSession, session_hash)
        if sess:
            db.delete(sess)
            db.commit()
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    response.delete_cookie(key=CSRF_COOKIE_NAME)
    return {"status": "logged_out"}


# ==================== 1. УЧЕНИКИ (STUDENTS CRUD) ====================
@app.post(
    "/students",
    response_model=schemas.StudentResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["1. Ученики (Students)"],
    summary="Создать нового ученика",
)
def create_student_endpoint(
    payload: schemas.StudentCreate,
    actor: str = Depends(verify_access),
    db: Session = Depends(get_db),
):
    return crud.create_student(db, payload, actor=actor)


@app.get(
    "/students",
    response_model=list[schemas.StudentResponse],
    tags=["1. Ученики (Students)"],
    summary="Список активных учеников (с опциональной фильтрацией по группе)",
    dependencies=[Depends(verify_access)],
)
def list_students_endpoint(
    group: str | None = Query(None, description="Фильтр по названию группы"),
    include_archived: bool = Query(False, description="Включить архивных учеников"),
    db: Session = Depends(get_db),
):
    return crud.get_students(db, group_name=group, include_archived=include_archived)


@app.get(
    "/students/{student_id}",
    response_model=schemas.StudentResponse,
    tags=["1. Ученики (Students)"],
    summary="Получить данные ученика по ID",
    dependencies=[Depends(verify_access)],
)
def get_student_endpoint(student_id: int, db: Session = Depends(get_db)):
    student = crud.get_student(db, student_id)
    if not student:
        raise HTTPException(status_code=404, detail=f"Ученик с ID {student_id} не найден")
    return student


@app.put(
    "/students/{student_id}",
    response_model=schemas.StudentResponse,
    tags=["1. Ученики (Students)"],
    summary="Изменить данные ученика",
)
@app.patch(
    "/students/{student_id}",
    response_model=schemas.StudentResponse,
    tags=["1. Ученики (Students)"],
    summary="Частично изменить данные ученика (с поддержкой effective_from_month)",
)
def update_student_endpoint(
    student_id: int,
    payload: schemas.StudentUpdate,
    actor: str = Depends(verify_access),
    db: Session = Depends(get_db),
):
    student = crud.get_student(db, student_id)
    if not student:
        raise HTTPException(status_code=404, detail=f"Ученик с ID {student_id} не найден")
    try:
        return crud.update_student(db, student, payload, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@app.delete(
    "/students/{student_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["1. Ученики (Students)"],
    summary="Архивировать ученика (Soft Delete без потери финансовой истории оплат)",
)
def delete_student_endpoint(
    student_id: int,
    actor: str = Depends(verify_access),
    db: Session = Depends(get_db),
):
    student = crud.get_student(db, student_id)
    if not student:
        raise HTTPException(status_code=404, detail=f"Ученик с ID {student_id} не найден")
    crud.delete_student(db, student, actor=actor)
    return None


# ==================== 2. ОПЛАТЫ (PAYMENTS) ====================
@app.post(
    "/payments",
    response_model=schemas.PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["2. Оплаты (Payments)"],
    summary="Добавить новую оплату (с поддержкой Idempotency-Key и защитой от конфликтов)",
)
def create_payment_endpoint(
    payload: schemas.PaymentCreate,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    actor: str = Depends(verify_access),
    db: Session = Depends(get_db),
):
    student = crud.get_student(db, payload.student_id)
    if not student:
        raise HTTPException(status_code=404, detail=f"Ученик с ID {payload.student_id} не найден")
    if idempotency_key is not None:
        normalized_key = idempotency_key.strip()
        if not normalized_key or len(normalized_key) > 64:
            raise HTTPException(status_code=422, detail="Idempotency-Key должен содержать от 1 до 64 символов")
        if payload.idempotency_key and payload.idempotency_key != normalized_key:
            raise HTTPException(status_code=422, detail="Заголовок и тело содержат разные Idempotency-Key")
        payload.idempotency_key = normalized_key
    try:
        return crud.create_payment(db, payload, student=student, actor=actor)
    except crud.IdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except crud.PaymentBeforeEnrollmentError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@app.post(
    "/payments/{payment_id}/void",
    response_model=schemas.PaymentResponse,
    tags=["2. Оплаты (Payments)"],
    summary="Отменить оплату с причиной и записью в аудите",
)
def void_payment_endpoint(
    payment_id: int,
    payload: schemas.PaymentVoidRequest,
    actor: str = Depends(verify_access),
    db: Session = Depends(get_db),
):
    try:
        return crud.void_payment(db, payment_id, payload.reason, actor=actor)
    except crud.PaymentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except crud.PaymentAlreadyVoidedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@app.get(
    "/students/{student_id}/payments",
    response_model=list[schemas.PaymentResponse],
    tags=["2. Оплаты (Payments)"],
    summary="Список всех оплат конкретного ученика (включая архивных для аудита)",
    dependencies=[Depends(verify_access)],
)
def list_student_payments_endpoint(
    student_id: int,
    include_archived: bool = Query(True, description="Разрешить просмотр истории архивированного ученика"),
    db: Session = Depends(get_db),
):
    student = crud.get_student(db, student_id, include_archived=include_archived)
    if not student:
        raise HTTPException(status_code=404, detail=f"Ученик с ID {student_id} не найден")
    return crud.get_student_payments(db, student_id)


@app.get(
    "/payments",
    response_model=list[schemas.PaymentResponse],
    tags=["2. Оплаты (Payments)"],
    summary="Список оплат по ID ученика (альтернативный query-параметр)",
    dependencies=[Depends(verify_access)],
)
def list_payments_by_query(
    student_id: int = Query(..., gt=0, description="ID ученика"),
    db: Session = Depends(get_db),
):
    student = crud.get_student(db, student_id, include_archived=True)
    if not student:
        raise HTTPException(status_code=404, detail=f"Ученик с ID {student_id} не найден")
    return crud.get_student_payments(db, student_id)


# ==================== 3. ДОЛЖНИКИ, АУДИТ И ЭКСПОРТ ====================
@app.get(
    "/debtors",
    response_model=schemas.DebtorsReportResponse,
    tags=["3. Аналитика и Должники (Debtors)"],
    summary="Запрос «должники за месяц» (с учётом исторического тарифа и переноса переплат)",
    dependencies=[Depends(verify_access)],
)
def get_debtors_endpoint(
    month: str = Query(
        default_factory=lambda: current_month(),
        description="Месяц в формате YYYY-MM (например, 2026-10)",
    ),
    include_arrears: bool = Query(
        default=False,
        description="true — добавить в отчёт и тех, у кого долг только за прошлые месяцы",
    ),
    db: Session = Depends(get_db),
):
    match = MONTH_REGEX.match(month.strip())
    if not match or int(match.group(1)) < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неверный формат месяца. Используйте строгий формат YYYY-MM (например, 2026-10).",
        )
    year, month_num = int(match.group(1)), int(match.group(2))
    return crud.get_debtors_for_month(db, year=year, month=month_num, include_arrears=include_arrears)


@app.get(
    "/debtors/export",
    tags=["3. Аналитика и Должники (Debtors)"],
    summary="Скачать защищённый от Formula Injection CSV-отчёт по должникам за месяц",
    dependencies=[Depends(verify_access)],
)
def export_debtors_csv(
    month: str = Query(default_factory=lambda: current_month()),
    db: Session = Depends(get_db),
):
    match = MONTH_REGEX.match(month.strip())
    if not match or int(match.group(1)) < 1:
        raise HTTPException(status_code=400, detail="Неверный формат месяца. Используйте YYYY-MM.")
    year, month_num = int(match.group(1)), int(match.group(2))
    report = crud.get_debtors_for_month(db, year=year, month=month_num)

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=";")
    writer.writerow([
        "ID ученика",
        "ФИО ученика",
        "Группа",
        "Телефон",
        "Месячный тариф (сум)",
        "Оплачено в месяце (сум)",
        "Зачтённый аванс (сум)",
        "Долг прошлых месяцев (сум)",
        "Остаток долга (сум)",
        "История подтверждена",
    ])
    for d in report.debtors:
        writer.writerow([
            d.student_id,
            sanitize_csv_cell(d.name),
            sanitize_csv_cell(d.group_name),
            sanitize_csv_cell(d.phone),
            d.monthly_fee,
            d.paid_amount,
            d.carry_over_credit,
            d.prior_arrears,
            d.debt_amount,
            "Да" if d.history_verified else "Мигрировано",
        ])

    output.seek(0)
    filename = f"debtors_{report.month}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get(
    "/audit-logs",
    response_model=list[schemas.AuditLogResponse],
    tags=["3. Аналитика и Должники (Debtors)"],
    summary="Журнал финансового аудита (последние 100 событий)",
    dependencies=[Depends(verify_access)],
)
def list_audit_logs_endpoint(db: Session = Depends(get_db)):
    return db.query(models.AuditLog).order_by(models.AuditLog.id.desc()).limit(100).all()


@app.get(
    "/audit-logs/verify",
    tags=["3. Аналитика и Должники (Debtors)"],
    summary="Проверить целостность хеш-цепочки журнала аудита",
    dependencies=[Depends(verify_access)],
)
def verify_audit_logs_endpoint(db: Session = Depends(get_db)):
    return crud.verify_audit_chain(db)


STATIC_INDEX = Path(__file__).parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def serve_web_dashboard():
    return FileResponse(STATIC_INDEX)