import calendar
from datetime import date
from decimal import Decimal
from sqlalchemy import func, and_
from sqlalchemy.orm import Session
from app import models, schemas


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


def get_payment_by_idempotency_key(db: Session, key: str) -> models.Payment | None:
    return db.query(models.Payment).filter(models.Payment.idempotency_key == key).first()


def create_payment(
    db: Session, data: schemas.PaymentCreate, student: models.Student | None = None
) -> models.Payment:
    if data.idempotency_key:
        existing = get_payment_by_idempotency_key(db, data.idempotency_key)
        if existing:
            return existing

    if student is None:
        student = get_student(db, data.student_id)

    payload = data.model_dump()
    payload["tariff_snapshot"] = student.monthly_fee if student else None
    payment = models.Payment(**payload)
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


def get_debtors_for_month(db: Session, year: int, month: int) -> schemas.DebtorsReportResponse:
    start_date = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end_date = date(year, month, last_day)

    paid_sum_expr = func.coalesce(func.sum(models.Payment.amount), 0)
    snapshot_fee_expr = func.max(models.Payment.tariff_snapshot)

    rows = (
        db.query(
            models.Student,
            paid_sum_expr.label("paid_amount"),
            snapshot_fee_expr.label("historical_fee"),
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
    current_ym = date.today().strftime("%Y-%m")
    target_ym = f"{year:04d}-{month:02d}"

    for student, paid_raw, hist_fee in rows:
        # Для прошлых месяцев используем сохранённый исторический тариф (если были платежи),
        # а для текущего месяца — актуальный тариф ученика.
        effective_fee_raw = (
            hist_fee if (hist_fee is not None and target_ym < current_ym) else student.monthly_fee
        )
        monthly_fee = Decimal(str(effective_fee_raw)).quantize(Decimal("0.01"))
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

    debtors_list.sort(key=lambda x: x.debt_amount, reverse=True)

    return schemas.DebtorsReportResponse(
        month=target_ym,
        total_debtors=len(debtors_list),
        total_debt_sum=total_debt_sum,
        debtors=debtors_list,
    )