from datetime import date
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict, field_validator


def _strip_dangerous_tags(val: Optional[str]) -> Optional[str]:
    if val is None:
        return None
    cleaned = val.strip()
    if "<" in cleaned or ">" in cleaned:
        raise ValueError("Поле не может содержать HTML-теги (< или >)")
    return cleaned


class StudentBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=120, examples=["Алексей Смирнов"])
    phone: str = Field(..., min_length=5, max_length=30, examples=["+998 90 123 45 67"])
    group_name: str = Field(..., min_length=1, max_length=60, examples=["Python Backend #14"])
    monthly_fee: Decimal = Field(..., gt=0, decimal_places=2, examples=[1200000.00])

    @field_validator("name", "phone", "group_name")
    @classmethod
    def validate_no_html(cls, v: str) -> str:
        return _strip_dangerous_tags(v)


class StudentCreate(StudentBase):
    pass


class StudentUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    phone: Optional[str] = Field(None, min_length=5, max_length=30)
    group_name: Optional[str] = Field(None, min_length=1, max_length=60)
    monthly_fee: Optional[Decimal] = Field(None, gt=0)

    @field_validator("name", "phone", "group_name")
    @classmethod
    def validate_no_html(cls, v: Optional[str]) -> Optional[str]:
        return _strip_dangerous_tags(v)


class StudentResponse(StudentBase):
    id: int
    model_config = ConfigDict(from_attributes=True)


class PaymentCreate(BaseModel):
    student_id: int = Field(..., gt=0, examples=[1])
    amount: Decimal = Field(..., gt=0, decimal_places=2, examples=[500000.00])
    payment_date: date = Field(default_factory=date.today, examples=["2026-10-30"])
    payment_method: str = Field(default="Наличные", max_length=30, examples=["Payme"])
    comment: Optional[str] = Field(default=None, max_length=255)
    idempotency_key: Optional[str] = Field(default=None, max_length=64, description="Уникальный ключ для защиты от дублей")

    @field_validator("payment_method", "comment")
    @classmethod
    def validate_no_html(cls, v: Optional[str]) -> Optional[str]:
        return _strip_dangerous_tags(v)


class PaymentResponse(BaseModel):
    id: int
    student_id: int
    amount: Decimal
    payment_date: date
    payment_method: str
    comment: Optional[str] = None
    idempotency_key: Optional[str] = None
    tariff_snapshot: Optional[Decimal] = None
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