import asyncio
import html
import os
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx
from dotenv import load_dotenv

from app.database import SessionLocal
from app import crud, schemas

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


def is_authorized(user_id: int | None) -> bool:
    """Если задан список TELEGRAM_ADMIN_IDS в .env, разрешает команды только указанным ID."""
    raw = os.getenv("TELEGRAM_ADMIN_IDS", "").strip()
    if not raw:
        return True
    allowed = {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}
    return (user_id in allowed) if allowed else True


def fmt_sum(val: Decimal | float | int) -> str:
    return f"{int(round(float(val))):,}".replace(",", " ") + " сум"


def handle_start() -> str:
    return (
        "💼 <b>Ecomdev EduBilling Bot</b>\n\n"
        "Доступные команды:\n"
        "• <code>/debt</code> — список должников текущего месяца\n"
        "• <code>/pay &lt;id ученика&gt; &lt;сумма&gt;</code> — внести оплату\n"
        "  <i>Пример:</i> <code>/pay 4 500000</code>"
    )


def handle_debt() -> str:
    today = date.today()
    db = SessionLocal()
    try:
        report = crud.get_debtors_for_month(db, year=today.year, month=today.month)
        if report.total_debtors == 0:
            return f"🎉 За <b>{report.month}</b> задолженностей нет!"

        lines = [
            f"📊 <b>Должники за {report.month}</b>",
            f"Всего должников: <b>{report.total_debtors}</b> | Сумма: <b>{fmt_sum(report.total_debt_sum)}</b>\n",
        ]
        for idx, d in enumerate(report.debtors, start=1):
            safe_name = html.escape(d.name)
            safe_group = html.escape(d.group_name)
            safe_phone = html.escape(d.phone)
            lines.append(
                f"{idx}. <b>{safe_name}</b> (ID: <code>{d.student_id}</code>)\n"
                f"   Группа: {safe_group} | 📞 {safe_phone}\n"
                f"   Оплачено: {fmt_sum(d.paid_amount)} из {fmt_sum(d.monthly_fee)}\n"
                f"   🔴 Долг: <b>{fmt_sum(d.debt_amount)}</b>\n"
                f"   Быстрая оплата: <code>/pay {d.student_id} {int(d.debt_amount)}</code>\n"
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
        if amount <= 0:
            raise ValueError
    except (ValueError, InvalidOperation):
        return "❌ ID ученика должен быть целым числом, а сумма — положительным числом."

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
                payment_date=date.today(),
                payment_method="Telegram Bot",
                comment=f"Внесено через Telegram (@{username})",
                idempotency_key=idem_key,
            ),
            student=student,
        )
        today = date.today()
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
    finally:
        db.close()


async def send_message(client: httpx.AsyncClient, chat_id: int, text: str):
    await client.post(
        f"{API_URL}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
    )


async def run_bot():
    if not BOT_TOKEN:
        print("⚠️ Ошибка: TELEGRAM_BOT_TOKEN не найден в файле .env")
        return

    offset = 0
    async with httpx.AsyncClient(timeout=35.0) as client:
        me = (await client.get(f"{API_URL}/getMe")).json()
        if not me.get("ok"):
            print("❌ Неверный токен бота:", me)
            return
        bot_username = me["result"].get("username")
        print(f"🤖 Telegram-бот @{bot_username} успешно запущен! Ожидание команд /debt и /pay...")

        while True:
            try:
                resp = await client.get(
                    f"{API_URL}/getUpdates",
                    params={"offset": offset, "timeout": 25},
                )
                data = resp.json()
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

                    if not is_authorized(user_id):
                        await send_message(client, chat_id, "⛔ У вас нет прав доступа к финансовому боту.")
                        continue

                    cmd = text.split()[0].split("@")[0].lower()
                    if cmd in ("/start", "/help"):
                        reply = handle_start()
                    elif cmd == "/debt":
                        reply = handle_debt()
                    elif cmd == "/pay":
                        reply = handle_pay(text, username, update_id=upd_id)
                    else:
                        reply = "Используйте команды <code>/debt</code> или <code>/pay &lt;id&gt; &lt;сумма&gt;</code>"

                    await send_message(client, chat_id, reply)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Повтор подключения через 2 сек ({e})...")
                await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(run_bot())