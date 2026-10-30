import calendar
from datetime import date
from decimal import Decimal
from sqlalchemy import func, and_
from sqlalchemy.orm import Session
from app import models, schemas


# --- УЧЕНИКИ (CRUD) ---
def create_student(db: Session, data: schemas.StudentCreate) -> models.Student:
    student = models.Student(**data.model_dump())
    db.add(student)
    db.commit()
    db.refresh(student)
    return student


def get_students(db: Session, group_name: str | None = None) -> list[models.Student]:
    query = db.query(models.Student)
    if group_name:
        query = query.filter(models.Student.group_name == group_name)
    return query.order_by(models.Student.id.asc()).all()


def get_student(db: Session, student_id: int) -> models.Student | None:
    return db.query(models.Student).filter(models.Student.id == student_id).first()


def update_student(db: Session, student: models.Student, data: schemas.StudentUpdate) -> models.Student:
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(student, field, value)
    db.commit()
    db.refresh(student)
    return student


def delete_student(db: Session, student: models.Student) -> None:
    db.delete(student)
    db.commit()


# --- ОПЛАТЫ ---
def create_payment(db: Session, data: schemas.PaymentCreate) -> models.Payment:
    payment = models.Payment(**data.model_dump())
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


def get_student_payments(db: Session, student_id: int) -> list[models.Payment]:
    return (
        db.query(models.Payment)
        .filter(models.Payment.student_id == student_id)
        .order_by(models.Payment.payment_date.desc(), models.Payment.id.desc())
        .all()
    )


# --- АНАЛИТИКА И ДОЛЖНИКИ ЗА МЕСЯЦ (БЕЗ ПРОБЛЕМЫ N+1) ---
def get_debtors_for_month(db: Session, year: int, month: int) -> schemas.DebtorsReportResponse:
    start_date = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end_date = date(year, month, last_day)

    paid_sum_expr = func.coalesce(func.sum(models.Payment.amount), 0)

    # Единый SQL-запрос с LEFT OUTER JOIN и фильтрацией дат внутри ON-условия
    rows = (
        db.query(
            models.Student,
            paid_sum_expr.label("paid_amount"),
        )
        .outerjoin(
            models.Payment,
            and_(
                models.Payment.student_id == models.Student.id,
                models.Payment.payment_date >= start_date,
                models.Payment.payment_date <= end_date,
            ),
        )
        .group_by(models.Student.id)
        .order_by(models.Student.id.asc())
        .all()
    )

    debtors_list: list[schemas.DebtorItem] = []
    total_debt_sum = Decimal("0.00")

    for student, paid_raw in rows:
        monthly_fee = Decimal(str(student.monthly_fee)).quantize(Decimal("0.01"))
        paid_amount = Decimal(str(paid_raw)).quantize(Decimal("0.01"))

        if paid_amount < monthly_fee:
            debt = (monthly_fee - paid_amount).quantize(Decimal("0.01"))
            total_debt_sum += debt
            debtors_list.append(
                schemas.DebtorItem(
                    student_id=student.id,
                    name=student.name,
                    phone=student.phone,
                    group_name=student.group_name,
                    monthly_fee=monthly_fee,
                    paid_amount=paid_amount,
                    debt_amount=debt,
                )
            )

    # Сортируем от самого крупного долга к меньшему для удобства менеджера
    debtors_list.sort(key=lambda x: x.debt_amount, reverse=True)

    return schemas.DebtorsReportResponse(
        month=f"{year:04d}-{month:02d}",
        total_debtors=len(debtors_list),
        total_debt_sum=total_debt_sum,
        debtors=debtors_list,
    )
