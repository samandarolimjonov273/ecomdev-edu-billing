from datetime import date
from sqlalchemy import Column, Integer, String, Numeric, Date, ForeignKey, Index
from sqlalchemy.orm import relationship
from app.database import Base


class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False, index=True)
    phone = Column(String(30), nullable=False)
    group_name = Column(String(60), nullable=False, index=True)
    monthly_fee = Column(Numeric(12, 2), nullable=False)

    payments = relationship(
        "Payment",
        back_populates="student",
        cascade="all, delete-orphan",
        order_by="desc(Payment.payment_date), desc(Payment.id)",
    )


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    payment_date = Column(Date, nullable=False, default=date.today, index=True)
    payment_method = Column(String(30), nullable=False, default="Наличные")
    comment = Column(String(255), nullable=True)
    idempotency_key = Column(String(64), unique=True, nullable=True, index=True)
    tariff_snapshot = Column(Numeric(12, 2), nullable=True)

    student = relationship("Student", back_populates="payments")


Index("ix_payments_student_date", Payment.student_id, Payment.payment_date)