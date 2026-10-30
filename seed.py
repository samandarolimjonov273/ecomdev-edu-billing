from datetime import date
from decimal import Decimal
from app.database import SessionLocal, engine
from app import models

models.Base.metadata.create_all(bind=engine)


def seed_demo_data():
    db = SessionLocal()
    try:
        if db.query(models.Student).count() > 0:
            print("ℹ️ База уже содержит данные, пропуск seed.")
            return

        today = date.today()
        first_day_current_month = date(today.year, today.month, 1)

        students_data = [
            models.Student(name="Тимур Каримов", phone="+998 90 111 22 33", group_name="Python Backend #14", monthly_fee=Decimal("1200000.00")),
            models.Student(name="Азиза Рустамова", phone="+998 93 222 33 44", group_name="Python Backend #14", monthly_fee=Decimal("1200000.00")),
            models.Student(name="Даврон Ахмедов", phone="+998 97 333 44 55", group_name="Node.js Fullstack #8", monthly_fee=Decimal("1500000.00")),
            models.Student(name="Малика Юсупова", phone="+998 99 444 55 66", group_name="Node.js Fullstack #8", monthly_fee=Decimal("1500000.00")),
            models.Student(name="Сардор Алиев", phone="+998 90 555 66 77", group_name="QA Automation #5", monthly_fee=Decimal("1000000.00")),
            models.Student(name="Елена Смирнова", phone="+998 91 666 77 88", group_name="UI/UX Design #11", monthly_fee=Decimal("1100000.00")),
        ]
        db.add_all(students_data)
        db.commit()

        payments_data = [
            models.Payment(student_id=1, amount=Decimal("700000.00"), payment_date=first_day_current_month, payment_method="Payme", comment="Частичная оплата"),
            models.Payment(student_id=2, amount=Decimal("1200000.00"), payment_date=first_day_current_month, payment_method="Click", comment="Полная оплата за месяц"),
            models.Payment(student_id=3, amount=Decimal("500000.00"), payment_date=first_day_current_month, payment_method="Наличные", comment="Аванс в кассу"),
            models.Payment(student_id=3, amount=Decimal("400000.00"), payment_date=today, payment_method="Карта", comment="Доплата через терминал"),
            models.Payment(student_id=5, amount=Decimal("300000.00"), payment_date=today, payment_method="Uzum", comment="Первый взнос"),
            models.Payment(student_id=6, amount=Decimal("1100000.00"), payment_date=today, payment_method="Карта", comment="Закрыт полностью"),
        ]
        db.add_all(payments_data)
        db.commit()
        print("✅ Демо-данные успешно загружены (6 учеников, 4 должника в текущем месяце).")
    finally:
        db.close()


if __name__ == "__main__":
    seed_demo_data()