import calendar
import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, extract, func, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app import models, schemas
from app.timeutil import current_month


class PaymentBeforeEnrollmentError(ValueError):
    """Оплата датирована раньше месяца зачисления: такая оплата не попала бы в расчёт долга."""


class IdempotencyConflictError(Exception):
    """Выбрасывается, если Idempotency-Key повторно использован с другими параметрами платежа."""
    pass


class PaymentAlreadyVoidedError(Exception):
    """Raised when an already voided payment is voided again."""


class PaymentNotFoundError(Exception):
    """Raised when a payment targeted for voiding does not exist."""


def log_audit(
    db: Session,
    entity_type: str,
    entity_id: int,
    action: str,
    actor: str = "system",
    details: str | None = None,
) -> models.AuditLog:
    created_at = datetime.now(timezone.utc).isoformat()
    details_norm = details or ""

    previous_hash = None
    try:
        if db.get_bind().dialect.name == "postgresql":
            # Сериализуем запись в цепочку: параллельные транзакции не получат один и тот же previous_hash.
            db.execute(text("SELECT pg_advisory_xact_lock(727001)"))
        previous = db.execute(
            text(
                "SELECT payload_hash FROM audit_logs ORDER BY id DESC LIMIT 1"
            )
        ).scalar()
        previous_hash = previous
    except Exception:
        previous_hash = None

    payload = "|".join([
        created_at,
        entity_type,
        str(entity_id),
        action,
        actor,
        details_norm,
        previous_hash or "",
    ])
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    entry = models.AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor=actor,
        details=details_norm,
        previous_hash=previous_hash,
        payload_hash=payload_hash,
        created_at=created_at,
    )
    db.add(entry)
    return entry


def _build_payment_fingerprint(data: schemas.PaymentCreate) -> str:
    amt = Decimal(str(data.amount)).quantize(Decimal("0.01"))
    comment_norm = " ".join((data.comment or "").strip().split())
    method_norm = (data.payment_method or "Наличные").strip()
    raw = f"{data.student_id}|{amt}|{data.payment_date.isoformat()}|{method_norm}|{comment_norm}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_student(db: Session, data: schemas.StudentCreate, actor: str = "api") -> models.Student:
    start_month = data.effective_from_month or current_month()
    dump = data.model_dump(exclude={"effective_from_month"})
    dump["enrolled_month"] = start_month
    dump["history_verified_from"] = start_month

    student = models.Student(**dump)
    db.add(student)
    db.flush()

    db.add(
        models.StudentTariffHistory(
            student_id=student.id,
            effective_from_month=start_month,
            monthly_fee=student.monthly_fee,
        )
    )
    log_audit(
        db,
        entity_type="student",
        entity_id=student.id,
        action="CREATE",
        actor=actor,
        details=f"Создан ученик {student.name}, тариф {student.monthly_fee} с {start_month}",
    )
    db.commit()
    db.refresh(student)
    return student


def get_students(
    db: Session, group_name: str | None = None, include_archived: bool = False
) -> list[models.Student]:
    query = db.query(models.Student)
    if not include_archived:
        query = query.filter(models.Student.is_archived.is_(False))
    if group_name:
        query = query.filter(models.Student.group_name == group_name)
    return query.order_by(models.Student.id.asc()).all()


def get_student(
    db: Session, student_id: int, include_archived: bool = False
) -> models.Student | None:
    query = db.query(models.Student).filter(models.Student.id == student_id)
    if not include_archived:
        query = query.filter(models.Student.is_archived.is_(False))
    return query.first()


def update_student(
    db: Session, student: models.Student, data: schemas.StudentUpdate, actor: str = "api"
) -> models.Student:
    update_data = data.model_dump(exclude_unset=True)
    eff_month = update_data.pop("effective_from_month", None)
    new_fee = update_data.get("monthly_fee")

    for field, value in update_data.items():
        setattr(student, field, value)

    if new_fee is not None:
        now_month = current_month()
        target_month = eff_month or now_month
        if target_month < now_month:
            raise ValueError("Нельзя менять тариф задним числом; укажите текущий или будущий месяц")
        if target_month < student.enrolled_month:
            raise ValueError("Месяц действия тарифа не может быть раньше зачисления ученика")

        existing_hist = (
            db.query(models.StudentTariffHistory)
            .filter(
                models.StudentTariffHistory.student_id == student.id,
                models.StudentTariffHistory.effective_from_month == target_month,
            )
            .first()
        )
        if existing_hist:
            existing_hist.monthly_fee = new_fee
        else:
            db.add(
                models.StudentTariffHistory(
                    student_id=student.id,
                    effective_from_month=target_month,
                    monthly_fee=new_fee,
                )
            )
        current_hist = (
            db.query(models.StudentTariffHistory)
            .filter(
                models.StudentTariffHistory.student_id == student.id,
                models.StudentTariffHistory.effective_from_month <= now_month,
            )
            .order_by(
                models.StudentTariffHistory.effective_from_month.desc(),
                models.StudentTariffHistory.id.desc(),
            )
            .first()
        )
        if current_hist:
            student.monthly_fee = current_hist.monthly_fee

    log_audit(
        db,
        entity_type="student",
        entity_id=student.id,
        action="UPDATE",
        actor=actor,
        details=f"Обновлены данные ученика (тариф={student.monthly_fee})",
    )
    db.commit()
    db.refresh(student)
    return student


def delete_student(db: Session, student: models.Student, actor: str = "api") -> None:
    """
    Выполняет безопасное архивирование (Soft Delete) ученика,
    сохраняя все его исторические финансовые транзакции для бухгалтерского аудита.
    """
    student.is_archived = True
    student.archived_at = datetime.now(timezone.utc).isoformat()
    student.archived_month = current_month()
    log_audit(
        db,
        entity_type="student",
        entity_id=student.id,
        action="ARCHIVE",
        actor=actor,
        details=f"Ученик {student.name} переведён в архив (история оплат сохранена)",
    )
    db.commit()


def get_effective_tariff_for_month(db: Session, student: models.Student, target_ym: str) -> Decimal:
    hist = (
        db.query(models.StudentTariffHistory)
        .filter(
            models.StudentTariffHistory.student_id == student.id,
            models.StudentTariffHistory.effective_from_month <= target_ym,
        )
        .order_by(
            models.StudentTariffHistory.effective_from_month.desc(),
            models.StudentTariffHistory.id.desc(),
        )
        .first()
    )
    fee = hist.monthly_fee if hist else student.monthly_fee
    return Decimal(str(fee)).quantize(Decimal("0.01"))


def get_payment_by_idempotency_key(db: Session, key: str) -> models.Payment | None:
    return db.query(models.Payment).filter(models.Payment.idempotency_key == key).first()


def create_payment(
    db: Session,
    data: schemas.PaymentCreate,
    student: models.Student | None = None,
    actor: str = "api",
) -> models.Payment:
    fingerprint = _build_payment_fingerprint(data)

    if data.idempotency_key:
        existing = get_payment_by_idempotency_key(db, data.idempotency_key)
        if existing:
            if existing.payload_fingerprint != fingerprint:
                raise IdempotencyConflictError(
                    "Ключ идемпотентности уже использован с другими параметрами платежа"
                )
            return existing

    if student is None:
        student = get_student(db, data.student_id)

    pay_ym = data.payment_date.strftime("%Y-%m")
    if student and pay_ym < student.enrolled_month:
        raise PaymentBeforeEnrollmentError(
            f"Дата оплаты ({data.payment_date}) раньше месяца зачисления ученика ({student.enrolled_month}). "
            "Укажите корректную дату или измените месяц зачисления."
        )
    effective_fee = get_effective_tariff_for_month(db, student, pay_ym) if student else None

    payload = data.model_dump()
    payload["payload_fingerprint"] = fingerprint
    payload["tariff_snapshot"] = effective_fee
    payload["status"] = "COMPLETED"
    payment = models.Payment(**payload)
    db.add(payment)

    try:
        db.flush()
        log_audit(
            db,
            entity_type="payment",
            entity_id=payment.id,
            action="CREATE",
            actor=actor,
            details=f"Оплата {payment.amount} сум для student_id={payment.student_id} ({payment.payment_method})",
        )
        db.commit()
        db.refresh(payment)
        return payment
    except IntegrityError:
        db.rollback()
        if data.idempotency_key:
            existing = get_payment_by_idempotency_key(db, data.idempotency_key)
            if existing:
                if existing.payload_fingerprint != fingerprint:
                    raise IdempotencyConflictError(
                        "Ключ идемпотентности уже использован с другими параметрами платежа"
                    )
                return existing
        raise


def void_payment(
    db: Session,
    payment_id: int,
    reason: str,
    actor: str,
) -> models.Payment:
    now = datetime.now(timezone.utc).isoformat()
    result = (
        db.query(models.Payment)
        .filter(
            models.Payment.id == payment_id,
            models.Payment.is_voided.is_(False),
        )
        .update(
            {
                models.Payment.is_voided: True,
                models.Payment.voided_at: now,
                models.Payment.voided_by: actor,
                models.Payment.void_reason: reason,
            },
            synchronize_session=False,
        )
    )
    if result != 1:
        db.rollback()
        if db.query(models.Payment.id).filter(models.Payment.id == payment_id).first():
            raise PaymentAlreadyVoidedError(f"Платёж {payment_id} уже отменён")
        raise PaymentNotFoundError(f"Платёж {payment_id} не найден")

    db.execute(
        models.Payment.__table__.update()
        .where(models.Payment.id == payment_id)
        .values(status="VOIDED", is_voided=True, voided_at=now, voided_by=actor, void_reason=reason)
    )

    log_audit(
        db,
        entity_type="payment",
        entity_id=payment_id,
        action="VOID",
        actor=actor,
        details=reason,
    )
    db.commit()
    db.expire_all()
    payment = db.get(models.Payment, payment_id)
    if payment is None:
        raise RuntimeError(f"Платёж {payment_id} исчез после успешной отмены")
    return payment


def get_student_payments(
    db: Session, student_id: int, include_archived_student: bool = True
) -> list[models.Payment]:
    return (
        db.query(models.Payment)
        .filter(models.Payment.student_id == student_id)
        .order_by(models.Payment.payment_date.desc(), models.Payment.id.desc())
        .all()
    )


def _iter_months(start_ym: str, end_ym: str) -> list[str]:
    sy, sm = map(int, start_ym.split("-"))
    ey, em = map(int, end_ym.split("-"))
    months: list[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        months.append(f"{y:04d}-{m:02d}")
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1
    return months


def get_debtors_for_month(
    db: Session, year: int, month: int, include_arrears: bool = False
) -> schemas.DebtorsReportResponse:
    """
    Рассчитывает задолженности за месяц YYYY-MM.
    Ученик — должник, если оплаты за месяц (+ аванс прошлых месяцев) меньше тарифа месяца;
    debt_amount = недостающая сумма за этот месяц. Долги прошлых месяцев не смешиваются
    с долгом месяца: они отдаются отдельно (prior_arrears, total_outstanding).
    Учитывается:
    1) Границы зачисления ученика (enrolled_month);
    2) Исторического тарифа на каждый месяц (StudentTariffHistory);
    3) Переноса аванса/переплаты (carry_over_credit) с предыдущих месяцев;
    4) Политики достоверности истории (history_verified).
    """
    target_ym = f"{year:04d}-{month:02d}"
    end_last_day = calendar.monthrange(year, month)[1]
    end_date = date(year, month, end_last_day)

    students = (
        db.query(models.Student)
        .filter(
            or_(
                models.Student.is_archived.is_(False),
                models.Student.archived_month >= target_ym,
            ),
            models.Student.enrolled_month <= target_ym,
        )
        .order_by(models.Student.id.asc())
        .all()
    )

    if not students:
        legacy_students = (
            db.query(models.Student)
            .filter(
                or_(
                    models.Student.is_archived.is_(False),
                    models.Student.archived_month >= target_ym,
                )
            )
            .all()
        )
        if legacy_students:
            earliest_verified = min(
                (st.history_verified_from or st.enrolled_month for st in legacy_students),
                default=target_ym,
            )
            if target_ym < earliest_verified:
                return schemas.DebtorsReportResponse(
                    month=target_ym,
                    status="legacy_unverified",
                    history_verified=False,
                    total_debtors=0,
                    total_debt_sum=Decimal("0.00"),
                    debtors=[],
                )
        return schemas.DebtorsReportResponse(
            month=target_ym,
            status="ok",
            history_verified=True,
            total_debtors=0,
            total_debt_sum=Decimal("0.00"),
            debtors=[],
        )

    student_ids = [s.id for s in students]

    all_tariffs = (
        db.query(models.StudentTariffHistory)
        .filter(
            models.StudentTariffHistory.student_id.in_(student_ids),
            models.StudentTariffHistory.effective_from_month <= target_ym,
        )
        .order_by(
            models.StudentTariffHistory.student_id.asc(),
            models.StudentTariffHistory.effective_from_month.asc(),
            models.StudentTariffHistory.id.asc(),
        )
        .all()
    )

    tariffs_by_student: dict[int, list[models.StudentTariffHistory]] = {}
    for t in all_tariffs:
        tariffs_by_student.setdefault(t.student_id, []).append(t)

    payment_year = extract("year", models.Payment.payment_date)
    payment_month = extract("month", models.Payment.payment_date)
    monthly_payments = (
        db.query(
            models.Payment.student_id,
            payment_year.label("year"),
            payment_month.label("month"),
            func.sum(models.Payment.amount).label("paid_amount"),
        )
        .filter(
            models.Payment.student_id.in_(student_ids),
            models.Payment.is_voided.is_(False),
            models.Payment.payment_date <= end_date,
        )
        .group_by(models.Payment.student_id, payment_year, payment_month)
        .all()
    )

    paid_by_student_month: dict[tuple[int, str], Decimal] = {}
    for student_id, year_value, month_value, amount in monthly_payments:
        ym = f"{int(year_value):04d}-{int(month_value):02d}"
        paid_by_student_month[(student_id, ym)] = Decimal(str(amount)).quantize(Decimal("0.01"))

    debtors_list: list[schemas.DebtorItem] = []
    total_debt_sum = Decimal("0.00")
    total_outstanding_sum = Decimal("0.00")
    unverified_debt_sum = Decimal("0.00")

    for st in students:
        st_tariffs = tariffs_by_student.get(st.id, [])

        def resolve_fee(ym: str) -> Decimal:
            applicable = [h.monthly_fee for h in st_tariffs if h.effective_from_month <= ym]
            raw_fee = applicable[-1] if applicable else st.monthly_fee
            return Decimal(str(raw_fee)).quantize(Decimal("0.01"))

        # A negative balance is prior arrears; a positive balance is carried credit.
        prior_balance = Decimal("0.00")
        months_chain = _iter_months(st.enrolled_month, target_ym)
        for prev_ym in months_chain[:-1]:
            prev_fee = resolve_fee(prev_ym)
            prev_paid = paid_by_student_month.get((st.id, prev_ym), Decimal("0.00"))
            prior_balance = (prior_balance + prev_fee - prev_paid).quantize(Decimal("0.01"))

        current_fee = resolve_fee(target_ym)
        current_paid = paid_by_student_month.get((st.id, target_ym), Decimal("0.00")).quantize(Decimal("0.01"))
        carry_credit = max(Decimal("0.00"), -prior_balance).quantize(Decimal("0.01"))
        prior_arrears = max(Decimal("0.00"), prior_balance).quantize(Decimal("0.01"))
        # Долг ЗА МЕСЯЦ: сколько не хватает до тарифа этого месяца (с учётом аванса прошлых месяцев).
        month_debt = max(Decimal("0.00"), current_fee - current_paid - carry_credit).quantize(Decimal("0.01"))
        # Весь остаток к оплате, включая долги прошлых месяцев (справочно).
        total_outstanding = max(Decimal("0.00"), prior_balance + current_fee - current_paid).quantize(Decimal("0.01"))

        if month_debt > 0 or (include_arrears and total_outstanding > 0):
            debt = month_debt
            total_debt_sum += debt
            total_outstanding_sum += total_outstanding
            verified_from = st.history_verified_from or st.enrolled_month
            history_verified = target_ym >= verified_from
            if not history_verified:
                unverified_debt_sum += debt
            debtors_list.append(
                schemas.DebtorItem(
                    student_id=st.id,
                    name=st.name,
                    phone=st.phone,
                    group_name=st.group_name,
                    monthly_fee=current_fee,
                    paid_amount=current_paid,
                    carry_over_credit=carry_credit,
                    prior_arrears=prior_arrears,
                    debt_amount=debt,
                    total_outstanding=total_outstanding,
                    history_verified=history_verified,
                )
            )

    debtors_list.sort(key=lambda x: x.debt_amount, reverse=True)

    earliest_verified = min(
        (st.history_verified_from or st.enrolled_month for st in students),
        default=target_ym,
    )
    status = "legacy_unverified" if target_ym < earliest_verified else "ok"

    return schemas.DebtorsReportResponse(
        month=target_ym,
        status=status,
        history_verified=(status == "ok"),
        total_debtors=len(debtors_list),
        total_debt_sum=total_debt_sum,
        total_outstanding_sum=total_outstanding_sum,
        unverified_debt_sum=unverified_debt_sum,
        debtors=debtors_list,
    )

def verify_audit_chain(db: Session) -> dict:
    """
    Пересчитывает хеш каждой записи аудита и сверяет цепочку previous_hash -> payload_hash.
    Обнаруживает изменение/удаление записей задним числом (но не защищает от них:
    для этого нужен append-only доступ на уровне БД).
    """
    rows = db.query(models.AuditLog).order_by(models.AuditLog.id.asc()).all()
    prev = None
    for row in rows:
        if (row.previous_hash or None) != prev:
            return {"ok": False, "checked": len(rows), "broken_at_id": row.id, "reason": "разрыв цепочки"}
        payload = "|".join([
            row.created_at, row.entity_type, str(row.entity_id), row.action,
            row.actor, row.details or "", row.previous_hash or "",
        ])
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != row.payload_hash:
            return {"ok": False, "checked": len(rows), "broken_at_id": row.id, "reason": "хеш не совпал"}
        prev = row.payload_hash
    return {"ok": True, "checked": len(rows), "broken_at_id": None, "reason": None}
