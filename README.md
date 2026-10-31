# Ecomdev EduBilling — мини-учёт оплат учебного центра

REST API (FastAPI + SQLAlchemy) + **Telegram-бот** (вариант А) + простая веб-панель для кассира.
БД: SQLite по умолчанию, PostgreSQL через Docker Compose.

## Что реализовано (по пунктам задания)

**Часть 1. API** (интерактивная документация: `/docs`)

| Пункт | Эндпоинт |
|---|---|
| Ученики: создать | `POST /students` |
| Ученики: список | `GET /students` (`?group=`, `?include_archived=true`) |
| Ученики: изменить | `PATCH /students/{id}` (`PUT` — то же, поля необязательные) |
| Ученики: удалить | `DELETE /students/{id}` → **архивация** (soft delete), история оплат сохраняется |
| Оплата: добавить | `POST /payments` (заголовок `Idempotency-Key` защищает от дублей) |
| Оплаты: список по ученику | `GET /students/{id}/payments` или `GET /payments?student_id=` |
| Должники за месяц | `GET /debtors?month=YYYY-MM` (`&include_arrears=true` — добавить тех, у кого долг только за прошлые месяцы) |

Дополнительно: отмена оплаты `POST /payments/{id}/void` (с причиной), CSV `GET /debtors/export`, журнал аудита `GET /audit-logs` и проверка его хеш-цепочки `GET /audit-logs/verify`.

**Часть 2. Telegram-бот:** `/debt` — должники текущего месяца, `/pay <id ученика> <сумма>` — внести оплату. Команды доступны только ID из `TELEGRAM_ADMIN_IDS`; свой ID показывает `/myid`.

### Как считаются должники

Ученик — должник за месяц, если оплаты в этом месяце меньше его месячной суммы.
`debt_amount` — сколько ещё должен **за этот месяц**. Переплата прошлых месяцев (аванс) зачитывается и уменьшает долг.
Долги прошлых месяцев не смешиваются с `debt_amount`, а отдаются отдельно: `prior_arrears`, `total_outstanding` (в отчёте — `total_outstanding_sum`).
Чтобы увидеть и тех, кто закрыл текущий месяц, но должен за прошлые, добавьте `include_arrears=true`.

Пример: тариф 1000, в сентябре оплачено 0, в октябре 400.
Отчёт за октябрь: `paid_amount=400`, `debt_amount=600`, `prior_arrears=1000`, `total_outstanding=1600`.

Прочие правила: до месяца зачисления долг не начисляется, а оплата с датой раньше месяца зачисления отклоняется (422); «сегодня» и границы месяцев считаются по часовому поясу `APP_TIMEZONE` (по умолчанию `Asia/Tashkent`), а не по времени сервера; смена тарифа действует с указанного месяца и не искажает прошлые (история тарифов); отменённые оплаты не учитываются; суммы хранятся как `Decimal`, не `float`.

## Запуск с нуля

Нужен Python 3.11+ (проверено на 3.12).

```bash
git clone <URL-репозитория> edu-billing && cd edu-billing
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # значения по умолчанию подходят для локального запуска
python migrate.py             # создаёт схему БД
python seed.py                # демо-данные: 6 учеников, 4 должника
uvicorn app.main:app --reload
```

Откройте http://127.0.0.1:8000 (веб-панель; ключ входа — `API_SECRET_KEY` из `.env`) или http://127.0.0.1:8000/docs.

### Примеры запросов

Все эндпоинты требуют заголовок `X-API-Key` со значением `API_SECRET_KEY`:

```bash
KEY="dev-only-change-me-0123456789abcdefghijkl"   # из .env

curl -H "X-API-Key: $KEY" "http://127.0.0.1:8000/debtors?month=2026-10"

curl -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"name":"Иван Петров","phone":"+998 90 123 45 67","group_name":"Python #1","monthly_fee":1000000}' \
  http://127.0.0.1:8000/students

curl -H "X-API-Key: $KEY" -H "Content-Type: application/json" -H "Idempotency-Key: pay-001" \
  -d '{"student_id":1,"amount":500000}' http://127.0.0.1:8000/payments

curl -H "X-API-Key: $KEY" http://127.0.0.1:8000/students/1/payments
```

### Telegram-бот

1. Создайте бота у @BotFather, впишите токен в `TELEGRAM_BOT_TOKEN` (`.env`).
2. Отправьте боту `/myid`, впишите свой числовой ID в `TELEGRAM_ADMIN_IDS` (несколько — через запятую).
3. Запустите: `python -m bot.telegram_bot`. Без `TELEGRAM_ADMIN_IDS` платёжные команды закрыты (fail-closed).

### Docker (PostgreSQL + API + бот)

```bash
cp .env.example .env     # задайте POSTGRES_PASSWORD и API_SECRET_KEY (≥ 32 символов)
docker compose up --build
```

Compose запускает приложение в `APP_ENV=production`: нужен случайный `API_SECRET_KEY` (значение-заглушка из `.env.example` приложение не примет) и `COOKIE_SECURE=1`. Для входа в панель по обычному `http://localhost` в Safari используйте локальный запуск без Docker. Миграции применяются при старте контейнера `web`. Бот в Compose — отдельный профиль и по умолчанию не стартует (чтобы не перезапускаться без токена). С ботом: задайте `TELEGRAM_BOT_TOKEN` и `TELEGRAM_ADMIN_IDS` и выполните `docker compose --profile bot up --build`.

### Тесты

```bash
pip install -r requirements-dev.txt
python -m pytest -q       # 34 теста на SQLite, не зависят от вашего .env

# + сквозной тест на настоящем PostgreSQL (в CI включён автоматически)
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/test_db python -m pytest -q tests/test_postgres.py
```

CI (GitHub Actions) гоняет тесты на SQLite и PostgreSQL 16 и проверяет миграции и бэкап.

## Что улучшил бы, будь ещё неделя 

1. **Пагинация и поиск** в `GET /students`, `/payments`, `/debtors` (сейчас отдаётся всё) + индексы под реальные объёмы.
2. **Настоящие роли** (админ / кассир / только чтение) с пользователями в БД вместо одного общего ключа.
3. **Блокировка журнала аудита для SQLite** (в PostgreSQL запись в цепочку уже сериализована advisory-lock) и append-only права на таблицу.
4. **Исправление тарифа задним числом** отдельной операцией с записью в аудит (сейчас запрещено; ошибки чинятся отменой оплаты).
5. **Фронтенд без CDN**: собрать Tailwind заранее, убрать `unsafe-inline` из CSP, добавить e2e-тесты на телефонных размерах.
6. **Бот и уведомления**: inline-кнопки «внести оплату», напоминания должникам, часовой пояс Ташкента для границ месяца, вебхук вместо long polling.
