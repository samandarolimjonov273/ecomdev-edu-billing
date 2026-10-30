# рџ’ј Ecomdev EduBilling вЂ” РЎРёСЃС‚РµРјР° СѓС‡С‘С‚Р° РѕРїР»Р°С‚ СѓС‡РµР±РЅРѕРіРѕ С†РµРЅС‚СЂР°

![CI Status](https://github.com/samandarolimjonov273/ecomdev-edu-billing/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)

РўРµСЃС‚РѕРІРѕРµ Р·Р°РґР°РЅРёРµ (Р­С‚Р°Рї 3) РЅР° РїРѕР·РёС†РёСЋ СЂР°Р·СЂР°Р±РѕС‚С‡РёРєР° РІ **Ecomdev**.
Р’ РїСЂРѕРµРєС‚Рµ СЂРµР°Р»РёР·РѕРІР°РЅС‹ **РѕР±Р° РІР°СЂРёР°РЅС‚Р° РёРЅС‚РµСЂС„РµР№СЃР°** РёР· Р§Р°СЃС‚Рё 2 (Р°РґР°РїС‚РёРІРЅС‹Р№ Mobile-First РІРµР±-РёРЅС‚РµСЂС„РµР№СЃ РєР°СЃСЃРёСЂР° + Telegram-Р±РѕС‚), РїРѕРґРєР»СЋС‡С‘РЅРЅС‹Рµ Рє РµРґРёРЅРѕРјСѓ СЃР»РѕСЋ Р±РёР·РЅРµСЃ-Р»РѕРіРёРєРё Рё Р‘Р”.

---

## рџ–ј РџСЂРµРІСЊСЋ РёРЅС‚РµСЂС„РµР№СЃРѕРІ

### 1. Р’РµР±-РёРЅС‚РµСЂС„РµР№СЃ РєР°СЃСЃРёСЂР° (Р’Р°СЂРёР°РЅС‚ Р‘ вЂ” Mobile-First FinTech UI)
![Р’РµР±-РёРЅС‚РµСЂС„РµР№СЃ РєР°СЃСЃРёСЂР°](docs/dashboard.png)

### 2. Telegram-Р±РѕС‚ (Р’Р°СЂРёР°РЅС‚ Рђ вЂ” `/debt` Рё `/pay <id> <СЃСѓРјРјР°>`)
![Telegram-Р±РѕС‚](docs/bot.png)

---

## рџљЂ Р‘С‹СЃС‚СЂС‹Р№ Р·Р°РїСѓСЃРє РїСЂРѕРµРєС‚Р° СЃ РЅСѓР»СЏ (Р§Р°СЃС‚СЊ 3.1)

### Р’Р°СЂРёР°РЅС‚ 1: Р›РѕРєР°Р»СЊРЅС‹Р№ Р·Р°РїСѓСЃРє (Python)
```bash
git clone [https://github.com/samandarolimjonov273/ecomdev-edu-billing.git](https://github.com/samandarolimjonov273/ecomdev-edu-billing.git)
cd ecomdev-edu-billing

python -m pip install -r requirements.txt
python seed.py
python -m uvicorn app.main:app --reload

