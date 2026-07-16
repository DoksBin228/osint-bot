import os
import re
import json
import time
import asyncio
import hashlib
import logging
from datetime import datetime, date
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor

import telebot
from telebot import types
import psycopg2
import redis
from psycopg2.extras import RealDictCursor, Json
import requests
from bs4 import BeautifulSoup
from telethon import TelegramClient, errors
from flask import Flask

# === КОНФИГ ===
TOKEN = os.environ.get("TOKEN", "8983546242:AAHVHOfgflOeR4uZoZlbsmdVvjUIF-oiWbQ")
ADMIN_PASSWORD = "20120212"
DATABASE_URL = os.environ.get("DATABASE_URL")
REDIS_URL = os.environ.get("REDIS_URL")
API_ID = os.environ.get("API_ID", "123456")
API_HASH = os.environ.get("API_HASH", "your_api_hash")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ADMINS = []

# === БАЗА ДАННЫХ (ПРАВИЛЬНОЕ ПОДКЛЮЧЕНИЕ) ===
class Database:
    def __init__(self):
        # ПОДКЛЮЧЕНИЕ ЧЕРЕЗ ПЕРЕМЕННУЮ
        self.pg_conn = psycopg2.connect(DATABASE_URL)
        self.pg_conn.autocommit = True
        self.redis = redis.from_url(REDIS_URL, decode_responses=True)
        self.init_tables()
    
    def init_tables(self):
        with self.pg_conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS people (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    full_name TEXT,
                    phone TEXT,
                    email TEXT,
                    address TEXT,
                    social_links JSONB DEFAULT '{}',
                    work TEXT,
                    breaches JSONB DEFAULT '[]',
                    risk_score FLOAT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(full_name, phone)
                );
                
                CREATE TABLE IF NOT EXISTS search_history (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id BIGINT,
                    query TEXT,
                    results JSONB DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS user_limits (
                    user_id BIGINT PRIMARY KEY,
                    request_count INTEGER DEFAULT 0,
                    last_reset DATE DEFAULT CURRENT_DATE
                );
            """)
    
    def save_person(self, data: Dict):
        with self.pg_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO people (full_name, phone, email, address, social_links, work, breaches, risk_score)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (full_name, phone) DO UPDATE SET
                    social_links = people.social_links || EXCLUDED.social_links,
                    breaches = people.breaches || EXCLUDED.breaches,
                    risk_score = GREATEST(people.risk_score, EXCLUDED.risk_score)
            """, (
                data.get('full_name'),
                data.get('phone'),
                data.get('email'),
                data.get('address'),
                json.dumps(data.get('social_links', {})),
                data.get('work'),
                json.dumps(data.get('breaches', [])),
                data.get('risk_score', 0)
            ))
    
    def search_people(self, query: str) -> List[Dict]:
        with self.pg_conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM people 
                WHERE full_name ILIKE %s 
                   OR phone ILIKE %s 
                   OR email ILIKE %s
                ORDER BY risk_score DESC
            """, (f'%{query}%', f'%{query}%', f'%{query}%'))
            return cur.fetchall()
    
    def get_user_limits(self, user_id: int):
        with self.pg_conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM user_limits WHERE user_id = %s", (user_id,))
            return cur.fetchone()
    
    def reset_user_limits(self, user_id: int):
        with self.pg_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_limits (user_id, request_count, last_reset)
                VALUES (%s, 0, CURRENT_DATE)
                ON CONFLICT (user_id) DO UPDATE SET
                    request_count = 0,
                    last_reset = CURRENT_DATE
            """, (user_id,))
    
    def increment_request(self, user_id: int):
        with self.pg_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_limits (user_id, request_count, last_reset)
                VALUES (%s, 1, CURRENT_DATE)
                ON CONFLICT (user_id) DO UPDATE SET
                    request_count = user_limits.request_count + 1
            """, (user_id,))

db = Database()

# === ЛИМИТЫ ===
def check_limit(user_id: int) -> tuple:
    today = date.today()
    limit_data = db.get_user_limits(user_id)
    
    if not limit_data:
        db.reset_user_limits(user_id)
        return True, 4
    
    if limit_data['last_reset'] != today:
        db.reset_user_limits(user_id)
        return True, 4
    
    used = limit_data['request_count']
    remaining = 4 - used
    
    if used >= 4:
        return False, 0
    
    return True, remaining

# === ПОИСКОВИК ===
class FullSearch:
    def search_all(self, query: str) -> Dict:
        results = {
            'full_name': '',
            'phone': '',
            'email': '',
            'address': '',
            'social_links': {},
            'work': '',
            'breaches': [],
            'risk_score': 0
        }
        
        if query.startswith('+') or re.match(r'^[\d\s\-+()]+$', query):
            results['phone'] = query
        elif '@' in query:
            results['email'] = query
        else:
            results['full_name'] = query
        
        results['risk_score'] = self.calculate_risk(results)
        return results
    
    def calculate_risk(self, data: Dict) -> float:
        risk = 0
        social_count = len(data.get('social_links', {}))
        if social_count > 5:
            risk += 30
        elif social_count > 3:
            risk += 20
        elif social_count > 0:
            risk += 10
        if data.get('breaches'):
            risk += 40
        if data.get('address'):
            risk += 15
        return min(100, risk)

searcher = FullSearch()

# === ОФОРМЛЕНИЕ ===
def header(title):
    return f"""
╔══════════════════════════════════════╗
║  🔥 WORTEX {title} 🔥               ║
╚══════════════════════════════════════╝
    """

def footer():
    return """
╔══════════════════════════════════════╗
║  🩸 WORTEX OSINT — Твой код         ║
╚══════════════════════════════════════╝
    """

# === БОТ ===
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    db.reset_user_limits(user_id)
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        "🔍 Поиск", "📱 Telegram", "💀 Утечки",
        "📊 Граф", "📈 Риск", "🔄 Обновить лимит",
        "📋 Мои данные", "🔐 Админ-панель", "❓ Помощь"
    ]
    markup.add(*[types.KeyboardButton(b) for b in buttons])
    
    bot.send_message(
        message.chat.id,
        f"""
🔴🔥 WORTEX OSINT v8.0 🔥🔴
⚡ ПОИСК + ТЕЛЕГРАМ + УТЕЧКИ
🩸 Твой код — твоя сила

{header('ГЛАВНОЕ МЕНЮ')}

╔══════════════════════════════════════╗
║  🔴 ЛИМИТ: 4 ЗАПРОСА В ДЕНЬ        ║
║  ✅ Доступно: 4 из 4               ║
║  🔄 Обновляется в 00:00             ║
╚══════════════════════════════════════╝
        """,
        reply_markup=markup
    )

# === ПОИСК (ОСНОВНОЙ) ===
@bot.message_handler(func=lambda msg: msg.text == "🔍 Поиск")
def search_prompt(message):
    user_id = message.from_user.id
    can_proceed, remaining = check_limit(user_id)
    
    if not can_proceed:
        bot.send_message(
            message.chat.id,
            f"""
{header('ЛИМИТ ИСЧЕРПАН')}

❌ Вы использовали все 4 запроса на сегодня

⏳ Следующие запросы будут доступны:
🔄 ЗАВТРА В 00:00

Нажмите кнопку:
📌 «🔄 Обновить лимит» в главном меню

{footer()}
            """
        )
        return
    
    msg = bot.send_message(
        message.chat.id,
        f"""
{header('ПОИСК')}

🔴 Введите ФИО, телефон или email:

📱 +79991234567
👤 Иван Петров
📧 ivan@mail.com

✅ Осталось запросов: {remaining}

{footer()}
        """
    )
    bot.register_next_step_handler(msg, lambda m: process_search(m, user_id))

def process_search(message, user_id):
    query = message.text.strip()
    
    db.increment_request(user_id)
    remaining = check_limit(user_id)[1]
    
    msg = bot.send_message(message.chat.id, "🔄 Ищу...")
    
    results = searcher.search_all(query)
    db.save_person(results)
    
    response = f"""
{header('РЕЗУЛЬТАТ')}

👤 ФИО: {results.get('full_name', 'Не найдено')}
📱 Телефон: {results.get('phone', 'Не найдено')}
📧 Email: {results.get('email', 'Не найдено')}
🏠 Адрес: {results.get('address', 'Не найдено')}
⚠️ Риск: {results.get('risk_score', 0):.1f}/100

✅ Осталось запросов: {remaining}

{footer()}
    """
    bot.edit_message_text(response, message.chat.id, msg.message_id)

# === TELEGRAM ===
@bot.message_handler(func=lambda msg: msg.text == "📱 Telegram")
def telegram_prompt(message):
    user_id = message.from_user.id
    can_proceed, remaining = check_limit(user_id)
    
    if not can_proceed:
        bot.send_message(
            message.chat.id,
            f"""
{header('ЛИМИТ ИСЧЕРПАН')}

❌ Вы использовали все 4 запроса на сегодня
⏳ Ждите 00:00

{footer()}
            """
        )
        return
    
    msg = bot.send_message(
        message.chat.id,
        f"""
{header('TELEGRAM ПОИСК')}

📱 Введите номер или @username:
Пример: +79991234567 или @username

✅ Осталось запросов: {remaining}

{footer()}
        """
    )
    bot.register_next_step_handler(msg, lambda m: process_telegram(m, user_id))

def process_telegram(message, user_id):
    db.increment_request(user_id)
    remaining = check_limit(user_id)[1]
    query = message.text.strip()
    
    bot.send_message(
        message.chat.id,
        f"""
{header('TELEGRAM РЕЗУЛЬТАТ')}

👤 Имя: Иван Петров
👾 Юзернейм: @{query.replace('@', '')}
📱 Телефон: +79991234567
🆔 ID: 123456789

✅ Осталось запросов: {remaining}

ℹ️ Полная версия в разработке

{footer()}
        """
    )

# === УТЕЧКИ ===
@bot.message_handler(func=lambda msg: msg.text == "💀 Утечки")
def breach_prompt(message):
    user_id = message.from_user.id
    can_proceed, remaining = check_limit(user_id)
    
    if not can_proceed:
        bot.send_message(
            message.chat.id,
            f"""
{header('ЛИМИТ ИСЧЕРПАН')}

❌ Вы использовали все 4 запроса на сегодня
⏳ Ждите 00:00

{footer()}
            """
        )
        return
    
    msg = bot.send_message(
        message.chat.id,
        f"""
{header('ПРОВЕРКА УТЕЧЕК')}

💀 Введите email для проверки:
Пример: ivan@mail.com

✅ Осталось запросов: {remaining}

{footer()}
        """
    )
    bot.register_next_step_handler(msg, lambda m: process_breach(m, user_id))

def process_breach(message, user_id):
    db.increment_request(user_id)
    remaining = check_limit(user_id)[1]
    email = message.text.strip()
    
    bot.send_message(
        message.chat.id,
        f"""
{header('РЕЗУЛЬТАТ УТЕЧЕК')}

📧 Email: {email}
💀 Найдено утечек: 0

✅ Осталось запросов: {remaining}

ℹ️ Полная версия в разработке

{footer()}
        """
    )

# === ГРАФ ===
@bot.message_handler(func=lambda msg: msg.text == "📊 Граф")
def graph_prompt(message):
    bot.send_message(
        message.chat.id,
        f"""
{header('ГРАФ СВЯЗЕЙ')}

📊 Функция в разработке
ℹ️ Будет показывать связи между людьми

{footer()}
        """
    )

# === РИСК ===
@bot.message_handler(func=lambda msg: msg.text == "📈 Риск")
def risk_prompt(message):
    user_id = message.from_user.id
    can_proceed, remaining = check_limit(user_id)
    
    if not can_proceed:
        bot.send_message(
            message.chat.id,
            f"""
{header('ЛИМИТ ИСЧЕРПАН')}

❌ Вы использовали все 4 запроса на сегодня
⏳ Ждите 00:00

{footer()}
            """
        )
        return
    
    msg = bot.send_message(
        message.chat.id,
        f"""
{header('АНАЛИЗ РИСКА')}

📈 Введите имя для анализа:
Пример: Иван Петров

✅ Осталось запросов: {remaining}

{footer()}
        """
    )
    bot.register_next_step_handler(msg, lambda m: process_risk(m, user_id))

def process_risk(message, user_id):
    db.increment_request(user_id)
    remaining = check_limit(user_id)[1]
    query = message.text.strip()
    
    results = db.search_people(query)
    
    if results:
        response = f"{header('РЕЗУЛЬТАТ РИСКА')}\n\n"
        for r in results[:3]:
            response += f"👤 {r['full_name']} — {r['risk_score']:.1f}/100\n"
        response += f"\n✅ Осталось запросов: {remaining}\n"
        response += footer()
    else:
        response = f"❌ Не найдено\n\n✅ Осталось запросов: {remaining}"
    
    bot.send_message(message.chat.id, response)

# === ОБНОВИТЬ ЛИМИТ ===
@bot.message_handler(func=lambda msg: msg.text == "🔄 Обновить лимит")
def reset_limit(message):
    user_id = message.from_user.id
    db.reset_user_limits(user_id)
    
    bot.send_message(
        message.chat.id,
        f"""
{header('ЛИМИТ ОБНОВЛЁН')}

✅ Ваши запросы сброшены!
📊 Доступно: 4 из 4

🔄 Теперь вы можете снова искать

{footer()}
        """
    )

# === МОИ ДАННЫЕ ===
@bot.message_handler(func=lambda msg: msg.text == "📋 Мои данные")
def my_data(message):
    user_id = message.from_user.id
    limit_data = db.get_user_limits(user_id)
    
    if limit_data:
        used = limit_data['request_count']
        remaining = 4 - used
        if remaining < 0:
            remaining = 0
    else:
        used = 0
        remaining = 4
    
    bot.send_message(
        message.chat.id,
        f"""
{header('МОИ ДАННЫЕ')}

👤 ID: {user_id}
📊 Использовано запросов: {used}/4
✅ Осталось: {remaining}

🔄 Обновление в 00:00

{footer()}
        """
    )

# === АДМИН-ПАНЕЛЬ ===
@bot.message_handler(func=lambda msg: msg.text == "🔐 Админ-панель")
def admin_login(message):
    msg = bot.send_message(message.chat.id, "🔐 Введите пароль:")
    bot.register_next_step_handler(msg, check_admin)

def check_admin(message):
    if message.text == ADMIN_PASSWORD:
        ADMINS.append(message.from_user.id)
        show_admin(message)
    else:
        bot.send_message(message.chat.id, "❌ Неверный пароль!")

def show_admin(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"))
    bot.send_message(
        message.chat.id,
        f"{header('АДМИН')}\n\nВыберите:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_'))
def admin_callback(call):
    if call.from_user.id not in ADMINS:
        bot.answer_callback_query(call.id, "❌ Доступ запрещён!")
        return
    
    if call.data == "admin_stats":
        with db.pg_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM people")
            people = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM user_limits")
            users = cur.fetchone()[0]
        
        bot.send_message(
            call.message.chat.id,
            f"{header('СТАТИСТИКА')}\n\n"
            f"👤 Людей в базе: {people}\n"
            f"👥 Всего пользователей: {users}\n"
            f"{footer()}"
        )

# === ПОМОЩЬ ===
@bot.message_handler(func=lambda msg: msg.text == "❓ Помощь")
def help_msg(message):
    bot.send_message(
        message.chat.id,
        f"""
{header('ПОМОЩЬ')}

🔍 Поиск — ищет ФИО/номер/email (4/день)
📱 Telegram — поиск в Telegram (4/день)
💀 Утечки — проверка email (4/день)
📊 Граф — связи между людьми
📈 Риск — оценка опасности (4/день)
🔄 Обновить лимит — сброс запросов
📋 Мои данные — статистика
🔐 Админ-панель — пароль 20120212

📌 ЛИМИТ: 4 ЗАПРОСА В ДЕНЬ
⏳ Обновление в 00:00

{footer()}
        """
    )

@app.route('/')
def home():
    return "🔥 WORTEX OSINT v8.0 Running", 200

if __name__ == "__main__":
    import threading
    threading.Thread(target=bot.infinity_polling).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
    