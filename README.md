# 🕵️ OSINT Bot v3.0 ULTIMATE

Мощный OSINT Telegram бот с полноценной базой данных.

## 🌟 Возможности

- 🔍 **Поиск** по базе данных (с кэшированием)
- 📊 **Добавление** сущностей (person, phone, email, username)
- 🌐 **Граф связей** (анализ связей между сущностями)
- 📊 **Анализ риска** (оценка опасности сущности)
- 📈 **Статистика** системы
- ⚡ **Кэширование** через Redis
- 🗄️ **Постоянное хранение** в PostgreSQL

## 🛠️ Технологии

- Python 3.11
- pyTelegramBotAPI
- Flask (для Render)
- PostgreSQL 15
- Redis
- Gunicorn

## 🚀 Деплой на Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)

Нажмите кнопку выше или используйте render.yaml для автоматического деплоя.

## 📋 Команды

| Команда | Описание |
|---------|----------|
| `/start` | Главное меню |
| `🔍 Поиск` | Поиск сущностей |
| `📊 Добавить` | Добавление новой сущности |
| `🌐 Граф связей` | Просмотр связей |
| `📊 Анализ риска` | Анализ опасности |
| `📈 Статистика` | Общая статистика |

## 🔧 Переменные окружения

- `TOKEN` - Токен Telegram бота
- `DATABASE_URL` - URL PostgreSQL (создается автоматически)
- `REDIS_URL` - URL Redis (создается автоматически)

## 📦 Установка локально

```bash
git clone https://github.com/ваш_аккаунт/osint-bot
cd osint-bot
pip install -r requirements.txt
python app.py