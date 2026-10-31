import re
from datetime import date
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from app.timeutil import today

MONTH_PATTERN = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
MAX_MONEY_AMOUNT = Decimal("1000000000.00")  # 1 млрд сум — верхний предел одной операции
ALLOWED_PAYMENT_METHODS = {"Наличные", "Карта", "Click", "Payme", "Uzum", "Telegram Bot", "Банковский перевод"}


def _strip_dangerous_tags(val: Optional[str]) -> Optional[str]:
    if val is None:
        return None
    cleaned = val.strip()
    if "<" in cleaned or ">" in cleaned:
        raise ValueError("Поле не может содержать HTML-теги (< или >)")
    return cleaned


class StudentBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=120, examples=["Алексей Смирнов"])
    phone: str = Field(..., min_length=5, max_length=30, examples=["+998 90 123 45 67"])
    group_name: str = Field(..., min_length=1, max_length=60, examples=["Python Backend #14"])
    monthly_fee: Decimal = Field(..., gt=0, le=MAX_MONEY_AMOUNT, decimal_places=2, examples=[1200000.00])

    @field_validator("name", "phone", "group_name")
    @classmethod
    def validate_no_html(cls, v: str) -> str:
        return _strip_dangerous_tags(v)


class StudentCreate(StudentBase):
    effective_from_month: Optional[str] = Field(
        default=None,
        description="Месяц зачисления и начала действия тарифа (YYYY-MM).",
    )

    @field_validator("effective_from_month")
    @classmethod
    def validate_month(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = v.strip()
        match = MONTH_PATTERN.match(cleaned)
        if not match or int(match.group(1)) < 1:
            raise ValueError("Используйте корректный формат YYYY-MM")
        return cleaned


class StudentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, min_length=2, max_length=120)
    phone: Optional[str] = Field(None, min_length=5, max_length=30)
    group_name: Optional[str] = Field(None, min_length=1, max_length=60)
    monthly_fee: Optional[Decimal] = Field(None, gt=0, le=MAX_MONEY_AMOUNT, decimal_places=2)
    effective_from_month: Optional[str] = Field(default=None)

    @field_validator("name", "phone", "group_name")
    @classmethod
    def validate_no_html(cls, v: Optional[str]) -> Optional[str]:
        return _strip_dangerous_tags(v)

    @field_validator("effective_from_month")
    @classmethod
    def validate_month(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = v.strip()
        match = MONTH_PATTERN.match(cleaned)
        if not match or int(match.group(1)) < 1:
            raise ValueError("Используйте корректный формат YYYY-MM")
        return cleaned

    @model_validator(mode="after")
    def effective_month_requires_fee(self):
        if self.effective_from_month is not None and self.monthly_fee is None:
            raise ValueError("effective_from_month можно указать только вместе с monthly_fee")
        return self


class StudentResponse(StudentBase):
    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: int
    enrolled_month: Optional[str] = None
    history_verified_from: Optional[str] = None
    is_archived: bool = False


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    student_id: int = Field(..., gt=0, examples=[1])
    amount: Decimal = Field(..., gt=0, le=MAX_MONEY_AMOUNT, decimal_places=2, examples=[500000.00])
    payment_date: date = Field(default_factory=today, examples=["2026-10-30"])
    payment_method: str = Field(default="Наличные", max_length=30, examples=["Payme"])
    comment: Optional[str] = Field(default=None, max_length=255)
    idempotency_key: Optional[str] = Field(default=None, max_length=64)

    @field_validator("payment_method")
    @classmethod
    def validate_method(cls, v: str) -> str:
        cleaned = _strip_dangerous_tags(v) or "Наличные"
        if cleaned not in ALLOWED_PAYMENT_METHODS:
            raise ValueError(f"Недопустимый способ оплаты. Разрешены: {', '.join(sorted(ALLOWED_PAYMENT_METHODS))}")
        return cleaned

    @field_validator("comment")
    @classmethod
    def validate_comment(cls, v: Optional[str]) -> Optional[str]:
        return _strip_dangerous_tags(v)

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Idempotency-Key не может быть пустым")
        return cleaned


class PaymentVoidRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 3:
            raise ValueError("Укажите причину отмены (не менее 3 символов)")
        return cleaned


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: int
    student_id: int
    amount: Decimal
    payment_date: date
    payment_method: str
    comment: Optional[str] = None
    idempotency_key: Optional[str] = None
    tariff_snapshot: Optional[Decimal] = None
    is_voided: bool = False
    voided_at: Optional[str] = None
    voided_by: Optional[str] = None
    void_reason: Optional[str] = None


class DebtorItem(BaseModel):
    student_id: int
    name: str
    phone: str
    group_name: str
    monthly_fee: Decimal
    paid_amount: Decimal
    carry_over_credit: Decimal = Decimal("0.00")
    prior_arrears: Decimal = Decimal("0.00")
    debt_amount: Decimal  # сколько ученик ещё должен ЗА ЭТОТ месяц
    total_outstanding: Decimal = Decimal("0.00")  # всего к оплате, включая долги прошлых месяцев
    history_verified: bool = True


class DebtorsReportResponse(BaseModel):
    month: str
    status: str = "ok"
    history_verified: bool = True
    total_debtors: int
    total_debt_sum: Decimal  # сумма долгов именно за выбранный месяц
    total_outstanding_sum: Decimal = Decimal("0.00")  # сумма к оплате с учётом прошлых месяцев
    unverified_debt_sum: Decimal = Decimal("0.00")
    debtors: list[DebtorItem]


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: str
    entity_id: int
    action: str
    actor: str
    details: Optional[str] = None
    created_at: str


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str = Field(..., min_length=1, max_length=128)