from datetime import date, datetime, timezone
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from app.database import Base


class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False, index=True)
    phone = Column(String(30), nullable=False)
    group_name = Column(String(60), nullable=False, index=True)
    monthly_fee = Column(Numeric(12, 2), nullable=False)
    enrolled_month = Column(
        String(7),
        nullable=False,
        default=lambda: date.today().strftime("%Y-%m"),
        index=True,
    )
    history_verified_from = Column(
        String(7),
        nullable=False,
        default=lambda: date.today().strftime("%Y-%m"),
    )
    is_archived = Column(Boolean, nullable=False, default=False, index=True)
    archived_at = Column(String(40), nullable=True)
    archived_month = Column(String(7), nullable=True, index=True)

    payments = relationship(
        "Payment",
        back_populates="student",
        passive_deletes=True,
        order_by="desc(Payment.payment_date), desc(Payment.id)",
    )
    tariff_history = relationship(
        "StudentTariffHistory",
        back_populates="student",
        cascade="all, delete-orphan",
        order_by="desc(StudentTariffHistory.effective_from_month), desc(StudentTariffHistory.id)",
    )


class StudentTariffHistory(Base):
    __tablename__ = "student_tariff_history"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True)
    effective_from_month = Column(String(7), nullable=False, index=True)
    monthly_fee = Column(Numeric(12, 2), nullable=False)

    student = relationship("Student", back_populates="tariff_history")

    __table_args__ = (
        UniqueConstraint("student_id", "effective_from_month", name="uq_student_tariff_month"),
    )


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    # RESTRICT защищает финансовую историю от физического удаления при удалении ученика
    student_id = Column(Integer, ForeignKey("students.id", ondelete="RESTRICT"), nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    payment_date = Column(Date, nullable=False, default=date.today, index=True)
    payment_method = Column(String(30), nullable=False, default="Наличные")
    comment = Column(String(255), nullable=True)
    status = Column(String(20), nullable=False, default="COMPLETED", index=True)
    idempotency_key = Column(String(64), unique=True, nullable=True, index=True)
    payload_fingerprint = Column(String(128), nullable=True)
    tariff_snapshot = Column(Numeric(12, 2), nullable=True)
    is_voided = Column(Boolean, nullable=False, default=False, index=True)
    voided_at = Column(String(40), nullable=True)
    voided_by = Column(String(80), nullable=True)
    void_reason = Column(String(500), nullable=True)

    student = relationship("Student", back_populates="payments")


class AuditLog(Base):
    """Неизменяемый журнал финансового и административного аудита (P1.6)."""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    entity_type = Column(String(40), nullable=False, index=True)
    entity_id = Column(Integer, nullable=False, index=True)
    action = Column(String(40), nullable=False, index=True)
    actor = Column(String(80), nullable=False, default="system")
    details = Column(String(500), nullable=True)
    previous_hash = Column(String(128), nullable=True, index=True)
    payload_hash = Column(String(128), nullable=True, index=True)
    created_at = Column(
        String(40),
        nullable=False,
        default=lambda: datetime.now(timezone.utc).isoformat(),
    )


class BotPollingLock(Base):
    """Распределённая блокировка (lease) для предотвращения параллельного polling одного токена бота (P0.5)."""
    __tablename__ = "bot_locks"

    lock_key = Column(String(64), primary_key=True)
    owner_id = Column(String(64), nullable=False)
    expires_at = Column(Integer, nullable=False)


class WebSession(Base):
    __tablename__ = "web_sessions"

    session_hash = Column(String(64), primary_key=True)
    csrf_hash = Column(String(64), nullable=False)
    expires_at = Column(Integer, nullable=False, index=True)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    client_key = Column(String(64), primary_key=True)
    window_started_at = Column(Integer, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)


LoginRateLimit = LoginAttempt


Index("ix_payments_student_date", Payment.student_id, Payment.payment_date)