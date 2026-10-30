# Ecomdev EduBilling

Mini billing system for an education center with:
- student management
- payment tracking
- debt calculation by month
- Telegram bot assistant for admin operations

## Stack
- Python 3.11+
- FastAPI
- SQLAlchemy
- SQLite (default for local development)
- PostgreSQL support via Docker Compose
- Telegram Bot API

## Part 1. API
The project implements the required financial accounting functionality:

### Students
- create student
- list students
- update student
- archive student

### Payments
- add payment for a student
- list payments by student
- payment history is preserved even after archive

### Debtors
- `GET /debtors?month=YYYY-MM`
- returns students who paid less than their monthly fee and the remaining debt

## Part 2. Interface
This project implements the Telegram bot option.

### Commands
- `/debt` — shows debtors for the current month
- `/pay <student_id> <amount>` — adds a payment for a student

## Part 3. README
### Quick start

#### 1. Clone repository
```bash
git clone https://github.com/<your-user>/<your-repo>.git
cd <your-repo>
2. Create environment file
cp .env.example .env
Then update .env:
APP_ENV=development
API_SECRET_KEY=your_super_long_random_secret_here
COOKIE_SECURE=0
TELEGRAM_BOT_TOKEN=
TELEGRAM_ADMIN_IDS=
AUTO_START_BOT=0
CORS_ORIGINS=http://127.0.0.1:8000,http://localhost:8000
Important:

.env is local-only and must not be committed
API_SECRET_KEY must be set in production
TELEGRAM_ADMIN_IDS must contain valid Telegram user IDs before bot payment commands are enabled
3. Install dependencies
python -m pip install -r requirements.txt
4. Run migrations
python migrate.py
5. Seed demo data
python seed.py
6. Start API
python -m uvicorn app.main:app --reload
Open:

http://127.0.0.1:8000/docs
http://127.0.0.1:8000
7. Start Telegram bot
python -m bot.telegram_bot
Production notes
Do not commit .env
Use a strong API_SECRET_KEY
Set COOKIE_SECURE=1 behind HTTPS or a trusted reverse proxy
Telegram bot is fail-closed: if no admin IDs are configured, payment commands are disabled
For real deployment, use a proper secret manager instead of only .env
3–5 improvements if I had another week
Full RBAC with separate roles for admin, cashier, and viewer
Real secret manager integration for AWS/GCP/Azure
Stronger audit immutability and tamper-evidence for financial logs
Deployment guardrails, backup verification, and staging/production checks
CI/CD pipeline for migration validation and automated test coverage
Part 4. AI usage summary
I used GitHub Copilot together with targeted prompts and direct test trace analysis to debug the project. The most effective prompts were short and specific, such as: “fix the root cause of the failing FastAPI tests with minimal patch”, “explain why startup failed because of env mismatch and show the exact fix”, and “add production-safe config validation without breaking the API contract”. AI helped with scaffolding, explaining runtime issues, and suggesting safer patterns, but the final validation and the business-logic fixes were reviewed and corrected manually. This was especially important for migration behavior, debt calculation, and bot authorization rules. In total, AI accelerated the development, but the final correctness was confirmed by the pytest suite and direct code review.

