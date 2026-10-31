"""Единая «текущая дата» приложения — по часовому поясу учебного центра, а не сервера (UTC)."""
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Tashkent").strip() or "Asia/Tashkent"
TZ = ZoneInfo(APP_TIMEZONE)


def today() -> date:
    return datetime.now(TZ).date()


def current_month() -> str:
    return today().strftime("%Y-%m")
