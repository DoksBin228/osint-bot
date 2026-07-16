import os
import re
import json
import logging
from datetime import datetime, date
from typing import Dict, List
import telebot
from telebot import types
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask

# === КОНФИГ ===
TOKEN = os.environ.get("TOKEN", "8983546242:AAHVHOfgflOeR4uZoZlbsmdVvjUIF-oiWbQ")
ADMIN_PASSWORD = "20120212"
DATABASE_URL = os.environ.get("DATABASE_URL")

# === ПРОВЕРКА: БЕЗ БАЗЫ НЕ ЗАПУСКАЕМСЯ ===
if not DATABASE_URL:
    raise Exception("postgresql://osint_db_gizl_user:lPeFFhrhAtq5DTPE27tBO8mbDnVQe53u@dpg-d9c6ruurnols73dtmk6g-a/osint_db_gizl")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ADMINS = []

# === ПРОСТАЯ БАЗА БЕЗ REDIS ===
class SimpleDB:
    def __init__(self):
        self.conn = psycopg2.connect(DATABASE_URL)
        self.conn.autocommit = True
        self.init_tables()
        logger.info("✅ PostgreSQL подключена")
    
    def init_tables(self):
        with self.conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS people (
                    id SERIAL PRIMARY KEY,
                    full_name TEXT,
                    phone TEXT,
                    email TEXT,
                    address TEXT,
                    social_links JSONB DEFAULT '{}',
                    risk_score FLOAT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS user_limits (
                    user_id BIGINT PRIMARY KEY,
                    request_count INTEGER DEFAULT 0,
                    last_reset DATE DEFAULT CURRENT_DATE
                );
            """)
            logger.info("✅ Таблицы созданы")
    
    def save_person(self, data: Dict):
        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO people (full_name, phone, email, address, social_links, risk_score)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                data.get('full_name'),
                data.get('phone'),
                data.get('email'),
                data.get('address'),
                json.dumps(data.get('social_links', {})),
                data.get('risk_score', 0)
            ))
    
    def search_people(self, query: str) -> List[Dict]:
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM people 
                WHERE full_name ILIKE %s OR phone ILIKE %s OR email ILIKE %s
                ORDER BY risk_score DESC
            """, (f'%{query}%', f'%{query}%', f'%{query}%'))
            return cur.fetchall()
    
    def get_user_limits(self, user_id: int):
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM user_limits WHERE user_id = %s", (user_id,))
            return cur.fetchone()
    
    def reset_user_limits(self, user_id: int):
        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_limits (user_id, request_count, last_reset)
                VALUES (%s, 0, CURRENT_DATE)
                ON CONFLICT (user_id) DO UPDATE SET
                    request_count = 0,
                    last_reset = CURRENT_DATE
            """, (user_id,))
    
    def increment_request(self, user_id: int):
        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_limits (user_id, request_count, last_reset)
                VALUES (%s, 1, CURRENT_DATE)
                ON CONFLICT (user_id) DO UPDATE SET
                    request_count = user_limits.request_count + 1
            """, (user_id,))

db = SimpleDB()

# === ЛИМИТЫ ===
def check_limit(user_id: int):
    today = date.today()
    data = db.get_user_limits(user_id)
    if not data or data['last_reset'] != today:
        db.reset_user_limits(user_id)
        return True, 4
    used = data['request_count']
    if used >= 4:
        return False, 0
    return True, 4 - used

# === ОФОРМЛЕНИЕ ===
def header(title):
    return f"╔══════════════════════════════════════╗\n║  🔥 WORTEX {title} 🔥               ║\n╚══════════════════════════════════════╝"

def footer():
    return "╔══════════════════════════════════════╗\n║  🩸 WORTEX OSINT — Твой код         ║\n╚══════════════════════════════════════╝"

# === БОТ ===
@bot.message_handler(commands=['start'])
def start(message):
    db.reset_user_limits(message.from_user.id)
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = ["🔍 Поиск", "📱 Telegram", "💀 Утечки", "📊 Граф", "📈 Риск", "🔄 Обновить лимит", "📋 Мои данные", "🔐 Админ-панель", "❓ Помощь"]
    markup.add(*[types.KeyboardButton(b) for b in buttons])
    bot.send_message(message.chat.id, f"🔴🔥 WORTEX OSINT v8.0 🔥🔴\n⚡ ПОИСК + ТЕЛЕГРАМ + УТЕЧКИ\n🩸 Твой код — твоя сила\n\n{header('ГЛАВНОЕ МЕНЮ')}\n\n╔══════════════════════════════════════╗\n║  🔴 ЛИМИТ: 4 ЗАПРОСА В ДЕНЬ        ║\n║  ✅ Доступно: 4 из 4               ║\n║  🔄 Обновляется в 00:00             ║\n╚══════════════════════════════════════╝", reply_markup=markup)

# === ПОИСК ===
@bot.message_handler(func=lambda msg: msg.text == "🔍 Поиск")
def search_prompt(message):
    user_id = message.from_user.id
    can, rem = check_limit(user_id)
    if not can:
        bot.send_message(message.chat.id, f"{header('ЛИМИТ ИСЧЕРПАН')}\n\n❌ Вы использовали все 4 запроса на сегодня\n⏳ ЗАВТРА В 00:00\n\n{footer()}")
        return
    msg = bot.send_message(message.chat.id, f"{header('ПОИСК')}\n\n🔴 Введите ФИО, телефон или email:\n\n✅ Осталось запросов: {rem}\n\n{footer()}")
    bot.register_next_step_handler(msg, lambda m: process_search(m, user_id))

def process_search(message, user_id):
    query = message.text.strip()
    db.increment_request(user_id)
    rem = check_limit(user_id)[1]
    results = {
        'full_name': query if not query.startswith('+') and '@' not in query else '',
        'phone': query if query.startswith('+') else '',
        'email': query if '@' in query else '',
        'address': '',
        'social_links': {},
        'risk_score': 0
    }
    db.save_person(results)
    bot.send_message(message.chat.id, f"{header('РЕЗУЛЬТАТ')}\n\n👤 ФИО: {results['full_name'] or 'Не найдено'}\n📱 Телефон: {results['phone'] or 'Не найдено'}\n📧 Email: {results['email'] or 'Не найдено'}\n🏠 Адрес: Не найдено\n⚠️ Риск: {results['risk_score']:.1f}/100\n\n✅ Осталось запросов: {rem}\n\n{footer()}")

# === ДРУГИЕ КНОПКИ ===
@bot.message_handler(func=lambda msg: msg.text in ["📱 Telegram", "💀 Утечки", "📊 Граф", "📈 Риск"])
def placeholder(message):
    bot.send_message(message.chat.id, f"{header('В РАЗРАБОТКЕ')}\n\nℹ️ Функция появится в следующей версии\n\n{footer()}")

@bot.message_handler(func=lambda msg: msg.text == "🔄 Обновить лимит")
def reset_limit(message):
    db.reset_user_limits(message.from_user.id)
    bot.send_message(message.chat.id, f"{header('ЛИМИТ ОБНОВЛЁН')}\n\n✅ Доступно: 4 из 4\n\n{footer()}")

@bot.message_handler(func=lambda msg: msg.text == "📋 Мои данные")
def my_data(message):
    data = db.get_user_limits(message.from_user.id)
    used = data['request_count'] if data else 0
    bot.send_message(message.chat.id, f"{header('МОИ ДАННЫЕ')}\n\n👤 ID: {message.from_user.id}\n📊 Использовано: {used}/4\n✅ Осталось: {4 - used if used < 4 else 0}\n\n🔄 Обновление в 00:00\n\n{footer()}")

@bot.message_handler(func=lambda msg: msg.text == "🔐 Админ-панель")
def admin_login(message):
    msg = bot.send_message(message.chat.id, "🔐 Введите пароль:")
    bot.register_next_step_handler(msg, check_admin)

def check_admin(message):
    if message.text == ADMIN_PASSWORD:
        ADMINS.append(message.from_user.id)
        with db.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM people")
            people = cur.fetchone()[0]
        bot.send_message(message.chat.id, f"{header('АДМИН')}\n\n👤 Людей в базе: {people}\n\n{footer()}")
    else:
        bot.send_message(message.chat.id, "❌ Неверный пароль!")

@bot.message_handler(func=lambda msg: msg.text == "❓ Помощь")
def help_msg(message):
    bot.send_message(message.chat.id, f"{header('ПОМОЩЬ')}\n\n🔍 Поиск — ищет ФИО/номер/email (4/день)\n📱 Telegram — поиск в Telegram (4/день)\n💀 Утечки — проверка email (4/день)\n📊 Граф — связи между людьми\n📈 Риск — оценка опасности (4/день)\n🔄 Обновить лимит — сброс запросов\n📋 Мои данные — статистика\n🔐 Админ-панель — пароль 20120212\n\n📌 ЛИМИТ: 4 ЗАПРОСА В ДЕНЬ\n⏳ Обновление в 00:00\n\n{footer()}")

@app.route('/')
def home():
    return "🔥 WORTEX OSINT v8.0 Running", 200

if __name__ == "__main__":
    import threading
    threading.Thread(target=bot.infinity_polling).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))