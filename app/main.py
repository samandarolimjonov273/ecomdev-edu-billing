import re
from datetime import date
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import models, schemas, crud
from app.database import engine, get_db

# Автоматическое создание таблиц при старте
models.Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Ecomdev EduBilling API",
    description="Мини-система учёта оплат и контроля задолженностей учебного центра.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MONTH_REGEX = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


# ==================== 1. УЧЕНИКИ (STUDENTS CRUD) ====================
@app.post(
    "/students",
    response_model=schemas.StudentResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["1. Ученики (Students)"],
    summary="Создать нового ученика",
)
def create_student_endpoint(payload: schemas.StudentCreate, db: Session = Depends(get_db)):
    return crud.create_student(db, payload)


@app.get(
    "/students",
    response_model=list[schemas.StudentResponse],
    tags=["1. Ученики (Students)"],
    summary="Список всех учеников (с опциональной фильтрацией по группе)",
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
    summary="Частично изменить данные ученика",
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
    summary="Добавить новую оплату",
)
def create_payment_endpoint(payload: schemas.PaymentCreate, db: Session = Depends(get_db)):
    student = crud.get_student(db, payload.student_id)
    if not student:
        raise HTTPException(status_code=404, detail=f"Ученик с ID {payload.student_id} не найден")
    return crud.create_payment(db, payload)


@app.get(
    "/students/{student_id}/payments",
    response_model=list[schemas.PaymentResponse],
    tags=["2. Оплаты (Payments)"],
    summary="Список всех оплат конкретного ученика",
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
)
def list_payments_by_query(
    student_id: int = Query(..., gt=0, description="ID ученика"),
    db: Session = Depends(get_db),
):
    student = crud.get_student(db, student_id)
    if not student:
        raise HTTPException(status_code=404, detail=f"Ученик с ID {student_id} не найден")
    return crud.get_student_payments(db, student_id)


# ==================== 3. ДОЛЖНИКИ (DEBTORS) ====================
@app.get(
    "/debtors",
    response_model=schemas.DebtorsReportResponse,
    tags=["3. Аналитика и Должники (Debtors)"],
    summary="Запрос «должники за месяц» (кто заплатил меньше месячной суммы и сколько должен)",
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


# ==================== 4. ВЕБ-ИНТЕРФЕЙС (FRONTEND) ====================
STATIC_INDEX = Path(__file__).parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def serve_web_dashboard():
    return FileResponse(STATIC_INDEX)
