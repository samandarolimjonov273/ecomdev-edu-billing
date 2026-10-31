import asyncio
import html
import logging
import os
import time
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx
from dotenv import load_dotenv

from sqlalchemy import or_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.database import SessionLocal
from app import crud, models, schemas
from app.config import settings
from app.timeutil import today as tz_today

load_dotenv()
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else "https://api.telegram.org/bot"
INSTANCE_ID = f"bot-{uuid.uuid4().hex[:12]}"
LOCK_KEY = "telegram_polling_leader"
LOCK_TTL_SECONDS = 120


def get_allowed_admin_ids() -> set[int]:
    raw = (
        os.getenv("TELEGRAM_ADMIN_IDS", "").strip()
        or os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
        or settings.telegram_admin_ids()
    )
    if not raw:
        return set()
    tokens = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
    allowed: set[int] = set()
    for token in tokens:
        if token.isdigit():
            allowed.add(int(token))
    return allowed


def is_authorized(user_id: int | None) -> bool:
    """
    Строгая fail-closed авторизация для финансовых команд (/debt и /pay).
    Если список разрешённых ID пуст или бот не настроен, доступ закрыт.
    """
    allowed = get_allowed_admin_ids()
    if not allowed or user_id is None:
        return False
    return user_id in allowed


def acquire_or_renew_polling_lock(owner_id: str = INSTANCE_ID, ttl: int = LOCK_TTL_SECONDS) -> bool:
    """
    Распределённая блокировка (lease/heartbeat) в БД:
    гарантирует, что только один экземпляр бота ведёт polling в один момент времени (P0.5).
    """
    now = int(time.time())
    db = SessionLocal()
    try:
        lock_values = {"lock_key": LOCK_KEY, "owner_id": owner_id, "expires_at": now + ttl}
        can_acquire = or_(
            models.BotPollingLock.expires_at <= now,
            models.BotPollingLock.owner_id == owner_id,
        )
        dialect = db.get_bind().dialect.name
        if dialect == "sqlite":
            statement = sqlite_insert(models.BotPollingLock).values(**lock_values)
        elif dialect == "postgresql":
            statement = pg_insert(models.BotPollingLock).values(**lock_values)
        else:
            raise RuntimeError(f"Unsupported database for Telegram polling lock: {dialect}")
        statement = statement.on_conflict_do_update(
            index_elements=[models.BotPollingLock.lock_key],
            set_={"owner_id": owner_id, "expires_at": now + ttl},
            where=can_acquire,
        )
        # RETURNING надёжнее rowcount: драйверы PostgreSQL/SQLite по-разному считают
        # затронутые строки у INSERT ... ON CONFLICT. Строка вернулась — блокировка наша.
        result = db.execute(statement.returning(models.BotPollingLock.owner_id))
        acquired = result.first() is not None
        db.commit()
        return acquired
    except Exception:
        db.rollback()
        logger.exception("Could not acquire/renew Telegram polling lock")
        return False
    finally:
        db.close()


def release_polling_lock(owner_id: str = INSTANCE_ID) -> None:
    db = SessionLocal()
    try:
        db.execute(
            update(models.BotPollingLock)
            .where(
                models.BotPollingLock.lock_key == LOCK_KEY,
                models.BotPollingLock.owner_id == owner_id,
            )
            .values(expires_at=0)
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not release Telegram polling lock")
    finally:
        db.close()


def fmt_sum(val: Decimal | float | int) -> str:
    return f"{int(round(float(val))):,}".replace(",", " ") + " сум"


def handle_start(user_id: int | None = None) -> str:
    if not is_authorized(user_id):
        uid_info = f"\nВаш Telegram ID: <code>{user_id}</code>" if user_id else ""
        return "⛔ Доступ к финансовым командам закрыт администратором." + uid_info
    uid_info = f"\nВаш Telegram ID: <code>{user_id}</code>" if user_id else ""
    return (
        "💼 <b>Ecomdev EduBilling Bot</b>\n\n"
        "Доступные команды (только для авторизованных администраторов):\n"
        "• <code>/debt</code> — список должников текущего месяца\n"
        "• <code>/pay &lt;id ученика&gt; &lt;сумма&gt;</code> — внести оплату\n"
        f"  <i>Пример:</i> <code>/pay 4 500000</code>{uid_info}"
    )


def handle_debt() -> str:
    today = tz_today()
    db = SessionLocal()
    try:
        report = crud.get_debtors_for_month(db, year=today.year, month=today.month)
        if report.total_debtors == 0:
            return f"🎉 За <b>{report.month}</b> задолженностей нет!"

        lines = [
            f"📊 <b>Должники за {report.month}</b>",
            f"Всего должников: <b>{report.total_debtors}</b> | Сумма: <b>{fmt_sum(report.total_debt_sum)}</b>\n",
        ]
        if report.unverified_debt_sum > 0:
            lines.insert(
                1,
                f"⚠️ История требует сверки: <b>{fmt_sum(report.unverified_debt_sum)}</b>\n",
            )
        for idx, d in enumerate(report.debtors, start=1):
            safe_name = html.escape(d.name)
            safe_group = html.escape(d.group_name)
            safe_phone = html.escape(d.phone)
            details = (
                f"{idx}. <b>{safe_name}</b> (ID: <code>{d.student_id}</code>)\n"
                f"   Группа: {safe_group} | 📞 {safe_phone}\n"
                f"   Оплачено: {fmt_sum(d.paid_amount)} из {fmt_sum(d.monthly_fee)}\n"
            )
            if d.prior_arrears > 0:
                details += f"   Долг прошлых месяцев: {fmt_sum(d.prior_arrears)}\n"
            if not d.history_verified:
                details += "   ⚠️ Исторические данные требуют сверки\n"
            lines.append(
                details
                + f"   🔴 Долг: <b>{fmt_sum(d.debt_amount)}</b>\n"
                + f"   Быстрая оплата: <code>/pay {d.student_id} {int(d.debt_amount)}</code>\n"
            )
        return "\n".join(lines)
    finally:
        db.close()


def handle_pay(text: str, username: str, update_id: int | None = None) -> str:
    parts = text.strip().split()
    if len(parts) < 3:
        return (
            "⚠️ <b>Формат команды:</b> <code>/pay &lt;id ученика&gt; &lt;сумма&gt;</code>\n"
            "Пример: <code>/pay 4 500000</code>"
        )

    try:
        student_id = int(parts[1])
        amount = Decimal(parts[2])
        if not amount.is_finite() or amount <= 0 or amount > schemas.MAX_MONEY_AMOUNT:
            raise ValueError
        if amount.as_tuple().exponent < -2:
            return "❌ Сумма может иметь не более 2 знаков после запятой."
    except (ValueError, InvalidOperation):
        return "❌ Некорректные параметры: укажите валидный ID ученика и положительную сумму."

    db = SessionLocal()
    try:
        student = crud.get_student(db, student_id)
        if not student:
            return f"❌ Ученик с ID <code>{student_id}</code> не найден."

        idem_key = f"tg-{update_id}" if update_id is not None else None
        payment = crud.create_payment(
            db,
            schemas.PaymentCreate(
                student_id=student_id,
                amount=amount,
                payment_date=tz_today(),
                payment_method="Telegram Bot",
                comment=f"Внесено через Telegram (@{username})",
                idempotency_key=idem_key,
            ),
            student=student,
            actor=f"tg:{username}",
        )
        today = tz_today()
        report = crud.get_debtors_for_month(db, year=today.year, month=today.month)
        remaining = next(
            (d.debt_amount for d in report.debtors if d.student_id == student_id),
            Decimal("0.00"),
        )

        safe_name = html.escape(student.name)
        return (
            f"✅ <b>Оплата успешно принята!</b>\n\n"
            f"👤 Ученик: <b>{safe_name}</b> (ID {student.id})\n"
            f"💰 Сумма: <b>{fmt_sum(payment.amount)}</b>\n"
            f"📅 Дата: {payment.payment_date.strftime('%d.%m.%Y')}\n"
            f"📌 Остаток долга за {report.month}: <b>{fmt_sum(remaining)}</b>"
        )
    except crud.IdempotencyConflictError as exc:
        return f"⚠️ Конфликт идемпотентности: {html.escape(str(exc))}"
    except crud.PaymentBeforeEnrollmentError as exc:
        return f"❌ {html.escape(str(exc))}"
    except ValueError as exc:  # включая pydantic.ValidationError
        logger.warning("Rejected /pay input: %s", exc)
        return "❌ Некорректные данные платежа. Пример: <code>/pay 4 500000</code>"
    finally:
        db.close()


async def send_message(client: httpx.AsyncClient, chat_id: int, text: str):
    response = await client.post(
        f"{API_URL}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {result}")


async def run_bot():
    if not BOT_TOKEN:
        print("⚠️ Ошибка: TELEGRAM_BOT_TOKEN не найден в .env. Бот не запущен.")
        return

    allowed = get_allowed_admin_ids()
    if not allowed:
        logger.error("Telegram bot is disabled: TELEGRAM_ADMIN_IDS/TELEGRAM_ALLOWED_USER_IDS is empty; no user can pay via the bot")
        print("⚠️ Ошибка: TELEGRAM_ADMIN_IDS/TELEGRAM_ALLOWED_USER_IDS не задан. Нельзя разрешить оплату через Telegram Bot.")
        return

    if not acquire_or_renew_polling_lock():
        logger.error("Another Telegram bot instance owns the polling lease; exiting")
        return

    try:
        offset = 0
        async with httpx.AsyncClient(timeout=35.0) as client:
            me_response = await client.get(f"{API_URL}/getMe")
            me_response.raise_for_status()
            me = me_response.json()
            if not me.get("ok"):
                raise RuntimeError("Telegram getMe rejected the configured bot token")
            bot_username = me["result"].get("username")
            logger.info("Telegram bot @%s started (lock owner: %s)", bot_username, INSTANCE_ID)

            while True:
                try:
                    if not acquire_or_renew_polling_lock():
                        logger.error("Lost Telegram polling lease; stopping this instance")
                        break

                    response = await client.get(
                        f"{API_URL}/getUpdates",
                        params={"offset": offset, "timeout": 20},
                    )
                    response.raise_for_status()
                    data = response.json()
                    if not data.get("ok"):
                        raise RuntimeError(f"Telegram getUpdates failed: {data}")
                    for upd in data.get("result", []):
                        upd_id = upd["update_id"]
                        offset = upd_id + 1
                        msg = upd.get("message") or {}
                        chat_id = msg.get("chat", {}).get("id")
                        text = (msg.get("text") or "").strip()
                        from_user = msg.get("from") or {}
                        user_id = from_user.get("id")
                        username = from_user.get("username") or str(user_id or "user")

                        if not chat_id or not text:
                            continue

                        cmd = text.split()[0].split("@")[0].lower()
                        if cmd in ("/start", "/help", "/myid"):
                            await send_message(client, chat_id, handle_start(user_id))
                            continue

                        if not is_authorized(user_id):
                            await send_message(
                                client,
                                chat_id,
                                "⛔ Доступ закрыт. Администратор должен настроить TELEGRAM_ADMIN_IDS.",
                            )
                            continue

                        if cmd == "/debt":
                            reply = handle_debt()
                        elif cmd == "/pay":
                            reply = handle_pay(text, username, update_id=upd_id)
                        else:
                            reply = "Используйте команды <code>/debt</code> или <code>/pay &lt;id&gt; &lt;сумма&gt;</code>"

                        await send_message(client, chat_id, reply)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    message = str(exc)
                    if BOT_TOKEN:
                        message = message.replace(BOT_TOKEN, "[REDACTED]")
                    logger.error(
                        "Telegram polling iteration failed (%s): %s; retrying",
                        type(exc).__name__,
                        message,
                    )
                    await asyncio.sleep(2)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = str(exc)
        if BOT_TOKEN:
            message = message.replace(BOT_TOKEN, "[REDACTED]")
        logger.error(
            "Telegram bot stopped (%s): %s",
            type(exc).__name__,
            message,
        )
    finally:
        release_polling_lock()


if __name__ == "__main__":
    asyncio.run(run_bot())