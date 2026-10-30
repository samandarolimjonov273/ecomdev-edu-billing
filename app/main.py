import asyncio
import csv
import io
import os
import re
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, Header, HTTPException, Query, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from app import models, schemas, crud
from app.database import engine, get_db

models.Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Автоматический фоновый запуск Telegram-бота вместе с FastAPI (если задан токен и AUTO_START_BOT=1)."""
    bot_task: Optional[asyncio.Task] = None
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("AUTO_START_BOT", "1") == "1":
        from bot.telegram_bot import run_bot
        bot_task = asyncio.create_task(run_bot())
    yield
    if bot_task:
        bot_task.cancel()


app = FastAPI(
    title="Ecomdev EduBilling API",
    description="Мини-система учёта оплат и контроля задолженностей учебного центра.",
    version="1.2.0",
    lifespan=lifespan,
)

allowed_origins = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(x_api_key: Optional[str] = Security(api_key_header)) -> None:
    """Опциональная защита API ключом: если в .env задан API_SECRET_KEY, требует заголовок X-API-Key."""
    required_key = os.getenv("API_SECRET_KEY", "").strip()
    if required_key and x_api_key != required_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный или отсутствующий X-API-Key",
        )


MONTH_REGEX = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


# ==================== 1. УЧЕНИКИ (STUDENTS CRUD) ====================
@app.post(
    "/students",
    response_model=schemas.StudentResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["1. Ученики (Students)"],
    summary="Создать нового ученика",
    dependencies=[Depends(verify_api_key)],
)
def create_student_endpoint(payload: schemas.StudentCreate, db: Session = Depends(get_db)):
    return crud.create_student(db, payload)


@app.get(
    "/students",
    response_model=list[schemas.StudentResponse],
    tags=["1. Ученики (Students)"],
    summary="Список всех учеников (с опциональной фильтрацией по группе)",
    dependencies=[Depends(verify_api_key)],
)
def list_students_endpoint(
    group: str | None = Query(None, description="Фильтр по названию группы"),
    db: Session = Depends(get_db),
):
    return crud.get_students(db, group_name=group)


@app.get(
    "/students/{student_id}",
    response_model=schemas.StudentResponse,
    tags=["1. Ученики (Students)"],
    summary="Получить данные ученика по ID",
    dependencies=[Depends(verify_api_key)],
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
    dependencies=[Depends(verify_api_key)],
)
@app.patch(
    "/students/{student_id}",
    response_model=schemas.StudentResponse,
    tags=["1. Ученики (Students)"],
    summary="Частично изменить данные ученика",
    dependencies=[Depends(verify_api_key)],
)
def update_student_endpoint(
    student_id: int, payload: schemas.StudentUpdate, db: Session = Depends(get_db)
):
    student = crud.get_student(db, student_id)
    if not student:
        raise HTTPException(status_code=404, detail=f"Ученик с ID {student_id} не найден")
    return crud.update_student(db, student, payload)


@app.delete(
    "/students/{student_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["1. Ученики (Students)"],
    summary="Удалить ученика и его историю оплат",
    dependencies=[Depends(verify_api_key)],
)
def delete_student_endpoint(student_id: int, db: Session = Depends(get_db)):
    student = crud.get_student(db, student_id)
    if not student:
        raise HTTPException(status_code=404, detail=f"Ученик с ID {student_id} не найден")
    crud.delete_student(db, student)
    return None


# ==================== 2. ОПЛАТЫ (PAYMENTS) ====================
@app.post(
    "/payments",
    response_model=schemas.PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["2. Оплаты (Payments)"],
    summary="Добавить новую оплату (с поддержкой Idempotency-Key)",
    dependencies=[Depends(verify_api_key)],
)
def create_payment_endpoint(
    payload: schemas.PaymentCreate,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    student = crud.get_student(db, payload.student_id)
    if not student:
        raise HTTPException(status_code=404, detail=f"Ученик с ID {payload.student_id} не найден")
    if idempotency_key and not payload.idempotency_key:
        payload.idempotency_key = idempotency_key.strip()[:64]
    return crud.create_payment(db, payload, student=student)


@app.get(
    "/students/{student_id}/payments",
    response_model=list[schemas.PaymentResponse],
    tags=["2. Оплаты (Payments)"],
    summary="Список всех оплат конкретного ученика",
    dependencies=[Depends(verify_api_key)],
)
def list_student_payments_endpoint(student_id: int, db: Session = Depends(get_db)):
    student = crud.get_student(db, student_id)
    if not student:
        raise HTTPException(status_code=404, detail=f"Ученик с ID {student_id} не найден")
    return crud.get_student_payments(db, student_id)


@app.get(
    "/payments",
    response_model=list[schemas.PaymentResponse],
    tags=["2. Оплаты (Payments)"],
    summary="Список оплат по ID ученика (альтернативный query-параметр)",
    dependencies=[Depends(verify_api_key)],
)
def list_payments_by_query(
    student_id: int = Query(..., gt=0, description="ID ученика"),
    db: Session = Depends(get_db),
):
    student = crud.get_student(db, student_id)
    if not student:
        raise HTTPException(status_code=404, detail=f"Ученик с ID {student_id} не найден")
    return crud.get_student_payments(db, student_id)


# ==================== 3. ДОЛЖНИКИ И ЭКСПОРТ (DEBTORS) ====================
@app.get(
    "/debtors",
    response_model=schemas.DebtorsReportResponse,
    tags=["3. Аналитика и Должники (Debtors)"],
    summary="Запрос «должники за месяц» (кто заплатил меньше месячной суммы и сколько должен)",
    dependencies=[Depends(verify_api_key)],
)
def get_debtors_endpoint(
    month: str = Query(
        default_factory=lambda: date.today().strftime("%Y-%m"),
        description="Месяц в формате YYYY-MM (например, 2026-10)",
        examples=["2026-10"],
    ),
    db: Session = Depends(get_db),
):
    match = MONTH_REGEX.match(month.strip())
    if not match:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неверный формат месяца. Используйте строгий формат YYYY-MM (например, 2026-10).",
        )
    year, month_num = int(match.group(1)), int(match.group(2))
    return crud.get_debtors_for_month(db, year=year, month=month_num)


@app.get(
    "/debtors/export",
    tags=["3. Аналитика и Должники (Debtors)"],
    summary="Скачать отчёт по должникам за месяц в формате CSV (совместим с MS Excel)",
    dependencies=[Depends(verify_api_key)],
)
def export_debtors_csv(
    month: str = Query(
        default_factory=lambda: date.today().strftime("%Y-%m"),
        description="Месяц в формате YYYY-MM",
    ),
    db: Session = Depends(get_db),
):
    match = MONTH_REGEX.match(month.strip())
    if not match:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неверный формат месяца. Используйте строгий формат YYYY-MM.",
        )
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
        "Оплачено (сум)",
        "Остаток долга (сум)",
    ])
    for d in report.debtors:
        writer.writerow([
            d.student_id,
            d.name,
            d.group_name,
            d.phone,
            d.monthly_fee,
            d.paid_amount,
            d.debt_amount,
        ])

    output.seek(0)
    filename = f"debtors_{report.month}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


STATIC_INDEX = Path(__file__).parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def serve_web_dashboard():
    return FileResponse(STATIC_INDEX)