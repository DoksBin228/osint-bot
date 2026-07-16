import os
import re
import json
import time
import asyncio
import hashlib
import logging
import socket
import socks
from datetime import datetime
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

# === БАЗА ДАННЫХ ===
class Database:
    def __init__(self):
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
                
                CREATE TABLE IF NOT EXISTS breaches (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    email TEXT,
                    password TEXT,
                    source TEXT,
                    leaked_at TIMESTAMP,
                    found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS search_history (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id BIGINT,
                    query TEXT,
                    results JSONB DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

db = Database()

# === ПОИСКОВИК ===
class FullSearch:
    def __init__(self):
        self.social_platforms = [
            'telegram', 'vk', 'instagram', 'twitter', 
            'facebook', 'linkedin', 'github'
        ]
    
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
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        "🔍 Поиск", "📱 Telegram", "💀 Утечки",
        "📊 Граф", "📈 Риск", "📋 Мои данные",
        "🔐 Админ-панель", "❓ Помощь"
    ]
    markup.add(*[types.KeyboardButton(b) for b in buttons])
    
    bot.send_message(
        message.chat.id,
        f"""
🔴🔥 WORTEX OSINT v8.0 🔥🔴
⚡ ПОИСК + ТЕЛЕГРАМ + УТЕЧКИ
🩸 Твой код — твоя сила

{header('ГЛАВНОЕ МЕНЮ')}
        """,
        reply_markup=markup
    )

@bot.message_handler(func=lambda msg: msg.text == "🔍 Поиск")
def search_prompt(message):
    msg = bot.send_message(message.chat.id, "🔴 Введите ФИО, телефон или email:")
    bot.register_next_step_handler(msg, process_search)

def process_search(message):
    query = message.text.strip()
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

{footer()}
    """
    bot.edit_message_text(response, message.chat.id, msg.message_id)

@bot.message_handler(func=lambda msg: msg.text == "📱 Telegram")
def telegram_prompt(message):
    msg = bot.send_message(message.chat.id, "📱 Введите номер или @username:")
    bot.register_next_step_handler(msg, process_telegram)

def process_telegram(message):
    query = message.text.strip()
    bot.send_message(message.chat.id, f"📱 Поиск в Telegram...\nℹ️ В разработке")

@bot.message_handler(func=lambda msg: msg.text == "💀 Утечки")
def breach_prompt(message):
    bot.send_message(message.chat.id, "💀 Проверка утечек...\nℹ️ В разработке")

@bot.message_handler(func=lambda msg: msg.text == "📊 Граф")
def graph_prompt(message):
    bot.send_message(message.chat.id, "📊 Граф связей\nℹ️ В разработке")

@bot.message_handler(func=lambda msg: msg.text == "📈 Риск")
def risk_prompt(message):
    msg = bot.send_message(message.chat.id, "📈 Введите имя:")
    bot.register_next_step_handler(msg, process_risk)

def process_risk(message):
    query = message.text.strip()
    results = db.search_people(query)
    if results:
        response = f"{header('РИСК')}\n\n"
        for r in results[:3]:
            response += f"👤 {r['full_name']} — {r['risk_score']:.1f}/100\n"
        bot.send_message(message.chat.id, response)
    else:
        bot.send_message(message.chat.id, "❌ Не найдено")

@bot.message_handler(func=lambda msg: msg.text == "📋 Мои данные")
def my_data(message):
    bot.send_message(message.chat.id, f"👤 Ваш ID: {message.from_user.id}")

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
    bot.send_message(message.chat.id, f"{header('АДМИН')}\n\nВыберите:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_'))
def admin_callback(call):
    if call.from_user.id not in ADMINS:
        bot.answer_callback_query(call.id, "❌ Доступ запрещён!")
        return
    if call.data == "admin_stats":
        with db.pg_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM people")
            count = cur.fetchone()[0]
        bot.send_message(call.message.chat.id, f"📊 Людей в базе: {count}")

@bot.message_handler(func=lambda msg: msg.text == "❓ Помощь")
def help_msg(message):
    bot.send_message(message.chat.id, """
📖 WORTEX OSINT

🔍 Поиск — ищет ФИО/номер/email
📱 Telegram — поиск в Telegram
💀 Утечки — проверка email
📊 Граф — связи между людьми
📈 Риск — оценка опасности
🔐 Админ-панель — пароль 20120212
    """)

@app.route('/')
def home():
    return "🔥 WORTEX OSINT v8.0 Running", 200

if __name__ == "__main__":
    import threading
    threading.Thread(target=bot.infinity_polling).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))