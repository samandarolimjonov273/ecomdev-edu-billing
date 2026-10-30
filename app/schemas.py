from datetime import date
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict


class StudentBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=120, examples=["Алексей Смирнов"])
    phone: str = Field(..., min_length=5, max_length=30, examples=["+998 90 123 45 67"])
    group_name: str = Field(..., min_length=1, max_length=60, examples=["Python Backend #14"])
    monthly_fee: Decimal = Field(..., gt=0, decimal_places=2, examples=[1200000.00])


class StudentCreate(StudentBase):
    pass


class StudentUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    phone: Optional[str] = Field(None, min_length=5, max_length=30)
    group_name: Optional[str] = Field(None, min_length=1, max_length=60)
    monthly_fee: Optional[Decimal] = Field(None, gt=0)


class StudentResponse(StudentBase):
    id: int
    model_config = ConfigDict(from_attributes=True)


class PaymentCreate(BaseModel):
    student_id: int = Field(..., gt=0, examples=[1])
    amount: Decimal = Field(..., gt=0, decimal_places=2, examples=[500000.00])
    payment_date: date = Field(default_factory=date.today, examples=["2026-10-29"])
    payment_method: str = Field(default="Наличные", max_length=30, examples=["Payme"])
    comment: Optional[str] = Field(default=None, max_length=255, examples=["Оплата за 1-ю половину месяца"])


class PaymentResponse(BaseModel):
    id: int
    student_id: int
    amount: Decimal
    payment_date: date
    payment_method: str
    comment: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class DebtorItem(BaseModel):
    student_id: int
    name: str
    phone: str
    group_name: str
    monthly_fee: Decimal
    paid_amount: Decimal
    debt_amount: Decimal


class DebtorsReportResponse(BaseModel):
    month: str
    total_debtors: int
    total_debt_sum: Decimal
    debtors: list[DebtorItem]