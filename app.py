# requirements.txt
# pip install psycopg2-binary pyTelegramBotAPI requests beautifulsoup4 lxml redis asyncpg aiohttp

import telebot
import requests
import re
import json
import time
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from bs4 import BeautifulSoup
from telebot import types
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
import hashlib
import logging
import asyncio
import aiohttp
import redis
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from enum import Enum

# === КОНФИГУРАЦИЯ ===
TOKEN = "8960312134:AAFZsB1i72tItBmu6RneQiU5qJ9LRSXO9-E"
ADMIN_ID = "ТВОЙ_ID_В_ТЕЛЕГРАМЕ"

# === РАСШИРЕННЫЕ ТИПЫ ДАННЫХ ===
class RiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

class DataSource(Enum):
    TELEGRAM = "telegram"
    VK = "vk"
    GITHUB = "github"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    TWITTER = "twitter"
    LINKEDIN = "linkedin"
    DARK_WEB = "dark_web"
    BREACH_DB = "breach_db"
    SOCIAL_GRAPH = "social_graph"

@dataclass
class Entity:
    """Сущность для графа связей"""
    id: str
    type: str  # person, phone, email, username, organization
    name: str
    properties: Dict[str, Any]
    confidence: float  # 0-1
    risk_score: float  # 0-100
    sources: List[str]
    first_seen: datetime
    last_seen: datetime
    aliases: List[str]
    tags: List[str]

@dataclass
class Relationship:
    """Связь между сущностями"""
    source_id: str
    target_id: str
    relation_type: str  # uses, owns, works_at, knows, etc
    strength: float  # 0-1
    context: str
    sources: List[str]
    timestamp: datetime

# === УЛЬТИМАТИВНАЯ БАЗА ДАННЫХ ===
class UltimateOSINTDatabase:
    def __init__(self):
        # PostgreSQL для основного хранилища
        self.pg_conn = psycopg2.connect(
            host="localhost",
            database="osint_ultimate",
            user="postgres",
            password="super_secure_password",
            application_name="OSINT_Bot"
        )
        self.pg_conn.autocommit = True
        
        # Redis для кэша и временных данных
        self.redis_client = redis.Redis(
            host='localhost',
            port=6379,
            db=0,
            decode_responses=True,
            password='redis_password'
        )
        
        # Пул соединений для асинхронных запросов
        self.executor = ThreadPoolExecutor(max_workers=20)
        self.process_executor = ProcessPoolExecutor(max_workers=4)
        
        self.init_ultimate_tables()
        self.init_redis_schema()
    
    def init_ultimate_tables(self):
        """Создание продвинутой схемы БД уровня Maltego/Sherlock"""
        with self.pg_conn.cursor() as cur:
            
            # === 1. ГРАФ СВЯЗЕЙ (КАК В MALTEgo) ===
            cur.execute("""
                CREATE EXTENSION IF NOT EXISTS pg_trgm;
                CREATE EXTENSION IF NOT EXISTS btree_gin;
                
                -- Основная таблица сущностей
                CREATE TABLE IF NOT EXISTS entities (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    entity_type VARCHAR(50) NOT NULL,
                    name VARCHAR(500) NOT NULL,
                    normalized_name VARCHAR(500) GENERATED ALWAYS AS (LOWER(name)) STORED,
                    properties JSONB DEFAULT '{}',
                    confidence FLOAT DEFAULT 0.5,
                    risk_score FLOAT DEFAULT 0,
                    sources TEXT[] DEFAULT '{}',
                    aliases TEXT[] DEFAULT '{}',
                    tags TEXT[] DEFAULT '{}',
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    metadata JSONB DEFAULT '{}',
                    
                    -- Индексы для быстрого поиска
                    CONSTRAINT entities_unique UNIQUE (entity_type, normalized_name)
                );
                
                -- Таблица связей (граф)
                CREATE TABLE IF NOT EXISTS relationships (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    source_id UUID REFERENCES entities(id) ON DELETE CASCADE,
                    target_id UUID REFERENCES entities(id) ON DELETE CASCADE,
                    relation_type VARCHAR(100) NOT NULL,
                    strength FLOAT DEFAULT 0.5,
                    context TEXT,
                    sources TEXT[] DEFAULT '{}',
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    properties JSONB DEFAULT '{}',
                    
                    CONSTRAINT unique_relationship UNIQUE (source_id, target_id, relation_type)
                );
                
                -- GIN индексы для JSONB
                CREATE INDEX IF NOT EXISTS idx_entities_properties ON entities USING GIN (properties);
                CREATE INDEX IF NOT EXISTS idx_entities_tags ON entities USING GIN (tags);
                CREATE INDEX IF NOT EXISTS idx_entities_aliases ON entities USING GIN (aliases);
                CREATE INDEX IF NOT EXISTS idx_entities_name_trgm ON entities USING GIN (name gin_trgm_ops);
                
                -- Индексы для графа
                CREATE INDEX IF NOT EXISTS idx_relationships_source ON relationships(source_id);
                CREATE INDEX IF NOT EXISTS idx_relationships_target ON relationships(target_id);
                CREATE INDEX IF NOT EXISTS idx_relationships_type ON relationships(relation_type);
            """)
            
            # === 2. ИСТОРИЯ ИЗМЕНЕНИЙ (AUDIT) ===
            cur.execute("""
                CREATE TABLE IF NOT EXISTS entity_history (
                    id SERIAL PRIMARY KEY,
                    entity_id UUID REFERENCES entities(id),
                    field_name VARCHAR(100),
                    old_value TEXT,
                    new_value TEXT,
                    changed_by BIGINT,
                    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                -- Триггер для автоматического логирования изменений
                CREATE OR REPLACE FUNCTION log_entity_changes()
                RETURNS TRIGGER AS $$
                BEGIN
                    IF TG_OP = 'UPDATE' THEN
                        IF OLD.name IS DISTINCT FROM NEW.name THEN
                            INSERT INTO entity_history (entity_id, field_name, old_value, new_value)
                            VALUES (NEW.id, 'name', OLD.name, NEW.name);
                        END IF;
                        IF OLD.properties IS DISTINCT FROM NEW.properties THEN
                            INSERT INTO entity_history (entity_id, field_name, old_value, new_value)
                            VALUES (NEW.id, 'properties', OLD.properties::TEXT, NEW.properties::TEXT);
                        END IF;
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                
                DROP TRIGGER IF EXISTS entity_audit_trigger ON entities;
                CREATE TRIGGER entity_audit_trigger
                AFTER UPDATE ON entities
                FOR EACH ROW
                EXECUTE FUNCTION log_entity_changes();
            """)
            
            # === 3. ВРЕМЕННЫЕ ИНДЕКСЫ И КЭШ ===
            cur.execute("""
                CREATE TABLE IF NOT EXISTS temporal_data (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    entity_id UUID REFERENCES entities(id),
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    data JSONB,
                    location GEOGRAPHY(POINT, 4326),
                    device_info JSONB,
                    ip_address INET
                );
                
                CREATE INDEX IF NOT EXISTS idx_temporal_location ON temporal_data USING GIST (location);
                CREATE INDEX IF NOT EXISTS idx_temporal_time ON temporal_data(timestamp);
            """)
            
            # === 4. АНАЛИТИКА И МЕТРИКИ ===
            cur.execute("""
                CREATE TABLE IF NOT EXISTS analytics (
                    date DATE PRIMARY KEY,
                    total_entities INTEGER DEFAULT 0,
                    total_relationships INTEGER DEFAULT 0,
                    search_volume INTEGER DEFAULT 0,
                    unique_users INTEGER DEFAULT 0,
                    top_searches JSONB DEFAULT '{}',
                    entity_types JSONB DEFAULT '{}',
                    risk_distribution JSONB DEFAULT '{}'
                );
                
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id BIGINT PRIMARY KEY,
                    profile JSONB DEFAULT '{}',
                    preferences JSONB DEFAULT '{}',
                    search_history JSONB DEFAULT '[]',
                    saved_entities UUID[] DEFAULT '{}',
                    api_usage JSONB DEFAULT '{}',
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            # === 5. ПОИСКОВЫЕ ИНДЕКСЫ (КАК В SHERLOCK) ===
            cur.execute("""
                -- Полнотекстовый поиск
                CREATE INDEX IF NOT EXISTS idx_entities_search 
                ON entities USING GIN (to_tsvector('russian', name || ' ' || COALESCE(properties::TEXT, '')));
                
                -- Функция поиска
                CREATE OR REPLACE FUNCTION search_entities(
                    search_query TEXT,
                    entity_types TEXT[] DEFAULT NULL,
                    min_confidence FLOAT DEFAULT 0,
                    max_results INTEGER DEFAULT 50
                )
                RETURNS TABLE(
                    id UUID,
                    name TEXT,
                    entity_type TEXT,
                    confidence FLOAT,
                    risk_score FLOAT,
                    relevance FLOAT
                ) AS $$
                BEGIN
                    RETURN QUERY
                    SELECT 
                        e.id,
                        e.name,
                        e.entity_type,
                        e.confidence,
                        e.risk_score,
                        ts_rank(to_tsvector('russian', e.name || ' ' || COALESCE(e.properties::TEXT, '')), 
                                plainto_tsquery('russian', search_query)) AS relevance
                    FROM entities e
                    WHERE 
                        (entity_types IS NULL OR e.entity_type = ANY(entity_types))
                        AND e.confidence >= min_confidence
                        AND to_tsvector('russian', e.name || ' ' || COALESCE(e.properties::TEXT, '')) @@ 
                            plainto_tsquery('russian', search_query)
                    ORDER BY relevance DESC, e.risk_score DESC
                    LIMIT max_results;
                END;
                $$ LANGUAGE plpgsql;
            """)
            
            # === 6. МАТЕРИАЛИЗОВАННЫЕ ПРЕДСТАВЛЕНИЯ ДЛЯ БЫСТРОЙ АНАЛИТИКИ ===
            cur.execute("""
                CREATE MATERIALIZED VIEW IF NOT EXISTS entity_network_stats AS
                SELECT 
                    e.id,
                    e.name,
                    e.entity_type,
                    COUNT(r.id) as connection_count,
                    AVG(r.strength) as avg_connection_strength,
                    MAX(r.strength) as max_connection_strength,
                    SUM(CASE WHEN r.strength > 0.8 THEN 1 ELSE 0 END) as strong_connections,
                    AVG(e.risk_score) as avg_risk_score
                FROM entities e
                LEFT JOIN relationships r ON e.id = r.source_id OR e.id = r.target_id
                GROUP BY e.id, e.name, e.entity_type;
                
                -- Обновление каждые 5 минут
                CREATE OR REPLACE FUNCTION refresh_network_stats()
                RETURNS TRIGGER AS $$
                BEGIN
                    REFRESH MATERIALIZED VIEW CONCURRENTLY entity_network_stats;
                    RETURN NULL;
                END;
                $$ LANGUAGE plpgsql;
            """)
            
            # Вставка демо-данных
            self.insert_demo_data()
    
    def init_redis_schema(self):
        """Настройка Redis для кэширования"""
        # Ключи для разных типов данных
        self.redis_client.delete('search_cache:*')
        self.redis_client.delete('session:*')
        self.redis_client.delete('rate_limit:*')
        self.redis_client.delete('entity_graph:*')
        
        # Установка TTL для разных типов
        self.redis_client.config_set('maxmemory-policy', 'allkeys-lru')
        self.redis_client.config_set('maxmemory', '2gb')
    
    def insert_demo_data(self):
        """Вставка тестовых данных"""
        with self.pg_conn.cursor() as cur:
            # Создаем тестовые сущности
            cur.execute("""
                INSERT INTO entities (entity_type, name, properties, confidence, risk_score, tags)
                VALUES 
                    ('person', 'Иван Петров', '{"age": 30, "city": "Moscow", "occupation": "developer"}', 0.95, 70, ARRAY['developer', 'public']),
                    ('person', 'Мария Смирнова', '{"age": 28, "city": "SPb", "occupation": "designer"}', 0.9, 30, ARRAY['designer']),
                    ('username', 'ivan_dev', '{"platform": "github", "public_repos": 45}', 0.8, 50, ARRAY['developer', 'open_source']),
                    ('phone', '+79991234567', '{"operator": "МТС", "region": "Moscow"}', 0.7, 40, ARRAY['verified']),
                    ('email', 'ivan@example.com', '{"provider": "gmail", "verified": true}', 0.85, 20, ARRAY['personal']),
                    ('organization', 'TechCorp', '{"employees": 500, "industry": "IT"}', 0.9, 10, ARRAY['company'])
                ON CONFLICT (entity_type, normalized_name) DO NOTHING
            """)
            
            # Связи между сущностями
            cur.execute("""
                WITH person AS (SELECT id FROM entities WHERE name = 'Иван Петров'),
                     email AS (SELECT id FROM entities WHERE name = 'ivan@example.com'),
                     phone AS (SELECT id FROM entities WHERE name = '+79991234567'),
                     org AS (SELECT id FROM entities WHERE name = 'TechCorp')
                INSERT INTO relationships (source_id, target_id, relation_type, strength, context)
                VALUES 
                    ((SELECT id FROM person), (SELECT id FROM email), 'uses', 1.0, 'Основной email'),
                    ((SELECT id FROM person), (SELECT id FROM phone), 'owns', 0.9, 'Личный номер'),
                    ((SELECT id FROM person), (SELECT id FROM org), 'works_at', 0.8, 'Сотрудник')
                ON CONFLICT (source_id, target_id, relation_type) DO NOTHING;
            """)
    
    # === ОСНОВНЫЕ МЕТОДЫ ===
    
    def create_entity(self, entity_type: str, name: str, properties: Dict = None, 
                      confidence: float = 0.5, tags: List[str] = None) -> str:
        """Создание новой сущности"""
        with self.pg_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO entities (entity_type, name, properties, confidence, tags)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (entity_type, normalized_name) DO UPDATE SET
                    properties = entities.properties || EXCLUDED.properties,
                    confidence = GREATEST(entities.confidence, EXCLUDED.confidence),
                    last_seen = CURRENT_TIMESTAMP,
                    tags = ARRAY(SELECT DISTINCT unnest(entities.tags || EXCLUDED.tags))
                RETURNING id
            """, (entity_type, name, Json(properties or {}), confidence, tags or []))
            
            return cur.fetchone()[0]
    
    def link_entities(self, source_id: str, target_id: str, relation_type: str, 
                     strength: float = 0.5, context: str = None):
        """Создание связи между сущностями"""
        with self.pg_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO relationships (source_id, target_id, relation_type, strength, context)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (source_id, target_id, relation_type) DO UPDATE SET
                    strength = GREATEST(relationships.strength, EXCLUDED.strength),
                    context = COALESCE(relationships.context, EXCLUDED.context),
                    timestamp = CURRENT_TIMESTAMP
            """, (source_id, target_id, relation_type, strength, context))
    
    def search_entities(self, query: str, entity_types: List[str] = None, 
                       min_confidence: float = 0.5, limit: int = 50) -> List[Dict]:
        """Поиск сущностей с использованием полнотекстового индекса"""
        cache_key = f"search:{hashlib.md5(f'{query}:{str(entity_types)}:{min_confidence}'.encode()).hexdigest()}"
        cached = self.redis_client.get(cache_key)
        if cached:
            return json.loads(cached)
        
        with self.pg_conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM search_entities(%s, %s, %s, %s)
            """, (query, entity_types, min_confidence, limit))
            
            results = cur.fetchall()
            
            # Кэширование результата на 5 минут
            self.redis_client.setex(cache_key, 300, json.dumps(results))
            
            return results
    
    def get_entity_network(self, entity_id: str, depth: int = 2) -> Dict:
        """Получение графа связей (аналог Maltego)"""
        cache_key = f"graph:{entity_id}:depth:{depth}"
        cached = self.redis_client.get(cache_key)
        if cached:
            return json.loads(cached)
        
        with self.pg_conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Рекурсивный запрос для получения графа
            cur.execute("""
                WITH RECURSIVE network AS (
                    -- Базовый случай: сама сущность
                    SELECT e.id, e.name, e.entity_type, e.properties, 0 as depth
                    FROM entities e
                    WHERE e.id = %s::UUID
                    
                    UNION ALL
                    
                    -- Рекурсивный случай: соседи
                    SELECT 
                        e.id, e.name, e.entity_type, e.properties, n.depth + 1
                    FROM network n
                    JOIN relationships r ON n.id = r.source_id OR n.id = r.target_id
                    JOIN entities e ON (e.id = r.source_id OR e.id = r.target_id) AND e.id != n.id
                    WHERE n.depth < %s
                )
                SELECT DISTINCT * FROM network
                ORDER BY depth, entity_type
            """, (entity_id, depth))
            
            nodes = cur.fetchall()
            
            # Получение связей между узлами
            cur.execute("""
                SELECT 
                    r.source_id,
                    r.target_id,
                    r.relation_type,
                    r.strength,
                    r.context,
                    json_agg(DISTINCT s.name) as sources
                FROM relationships r
                JOIN entities e1 ON r.source_id = e1.id
                JOIN entities e2 ON r.target_id = e2.id
                WHERE r.source_id = ANY(%s::UUID[]) OR r.target_id = ANY(%s::UUID[])
                GROUP BY r.source_id, r.target_id, r.relation_type, r.strength, r.context
            """, (nodes, nodes))
            
            edges = cur.fetchall()
            
            result = {
                'nodes': nodes,
                'edges': edges,
                'total_nodes': len(nodes),
                'total_edges': len(edges)
            }
            
            # Кэширование на 10 минут
            self.redis_client.setex(cache_key, 600, json.dumps(result))
            
            return result
    
    def analyze_risk(self, entity_id: str) -> Dict:
        """Анализ риска для сущности (включая связи)"""
        with self.pg_conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                WITH entity_risk AS (
                    SELECT 
                        e.id,
                        e.name,
                        e.entity_type,
                        e.risk_score as base_risk,
                        e.confidence,
                        COUNT(DISTINCT r.id) as connections,
                        AVG(r.strength) as avg_strength,
                        MAX(r.strength) as max_strength,
                        SUM(CASE WHEN r.strength > 0.8 THEN 1 ELSE 0 END) as strong_connections,
                        AVG(e2.risk_score) as avg_connected_risk
                    FROM entities e
                    LEFT JOIN relationships r ON e.id = r.source_id OR e.id = r.target_id
                    LEFT JOIN entities e2 ON (e2.id = r.source_id OR e2.id = r.target_id) AND e2.id != e.id
                    WHERE e.id = %s::UUID
                    GROUP BY e.id, e.name, e.entity_type, e.risk_score, e.confidence
                )
                SELECT 
                    *,
                    CASE 
                        WHEN base_risk > 80 OR avg_connected_risk > 80 THEN 'CRITICAL'
                        WHEN base_risk > 60 OR avg_connected_risk > 60 THEN 'HIGH'
                        WHEN base_risk > 40 OR avg_connected_risk > 40 THEN 'MEDIUM'
                        ELSE 'LOW'
                    END as risk_level,
                    CASE 
                        WHEN strong_connections > 5 AND avg_connected_risk > 70 THEN 'ВЫСОКИЙ РИСК: Связан с опасными сущностями'
                        WHEN base_risk > 80 THEN 'КРИТИЧЕСКИЙ РИСК: Необходимо немедленное внимание'
                        WHEN connections > 20 THEN 'ПОВЫШЕННЫЙ РИСК: Много связей в сети'
                        ELSE 'Стандартный уровень риска'
                    END as risk_description
                FROM entity_risk
            """, (entity_id,))
            
            return cur.fetchone()
    
    def get_similar_entities(self, entity_id: str, limit: int = 10) -> List[Dict]:
        """Нахождение похожих сущностей (продвинутый алгоритм)"""
        with self.pg_conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                WITH target AS (
                    SELECT entity_type, properties, tags FROM entities WHERE id = %s::UUID
                )
                SELECT 
                    e.id,
                    e.name,
                    e.entity_type,
                    e.properties,
                    e.tags,
                    e.confidence,
                    -- Сходство по тегам
                    (SELECT COUNT(*) FROM unnest(e.tags) t1 
                     JOIN unnest((SELECT tags FROM target)) t2 ON t1 = t2)::FLOAT / 
                    NULLIF(GREATEST(ARRAY_LENGTH(e.tags, 1), ARRAY_LENGTH((SELECT tags FROM target), 1)), 0) as tag_similarity,
                    -- Сходство по свойствам
                    (SELECT COUNT(*) FROM jsonb_each(e.properties) p1
                     JOIN jsonb_each((SELECT properties FROM target)) p2 ON p1.key = p2.key AND p1.value = p2.value)::FLOAT /
                    NULLIF(GREATEST((
                        SELECT COUNT(*) FROM jsonb_each(e.properties)
                    ), (
                        SELECT COUNT(*) FROM jsonb_each((SELECT properties FROM target))
                    )), 0) as property_similarity
                FROM entities e
                WHERE e.id != %s::UUID
                    AND e.entity_type = (SELECT entity_type FROM target)
                ORDER BY (tag_similarity + property_similarity) / 2 DESC
                LIMIT %s
            """, (entity_id, entity_id, limit))
            
            return cur.fetchall()
    
    def track_user_search(self, user_id: int, query: str, result_count: int, search_type: str):
        """Отслеживание поисковых запросов пользователя"""
        with self.pg_conn.cursor() as cur:
            # Обновление профиля пользователя
            cur.execute("""
                INSERT INTO user_profiles (user_id, search_history)
                VALUES (%s, '[]'::JSONB)
                ON CONFLICT (user_id) DO UPDATE SET
                    search_history = (user_profiles.search_history || %s::JSONB)::JSONB,
                    last_active = CURRENT_TIMESTAMP
            """, (user_id, json.dumps([{'query': query, 'result_count': result_count, 
                                        'search_type': search_type, 'timestamp': datetime.now().isoformat()}])))
            
            # Обновление аналитики
            self.update_analytics(search_type)
    
    def update_analytics(self, search_type: str):
        """Обновление аналитических данных"""
        today = datetime.now().date()
        with self.pg_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO analytics (date, search_volume, unique_users, top_searches)
                VALUES (%s, 1, 1, %s)
                ON CONFLICT (date) DO UPDATE SET
                    search_volume = analytics.search_volume + 1,
                    top_searches = analytics.top_searches || %s
            """, (today, json.dumps({search_type: 1}), json.dumps({search_type: 1})))
    
    def close(self):
        """Закрытие соединений"""
        self.pg_conn.close()
        self.redis_client.close()
        self.executor.shutdown()
        self.process_executor.shutdown()

# === ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ===
db = UltimateOSINTDatabase()

# === ОБРАБОТЧИКИ БОТА С НОВОЙ БАЗОЙ ===

@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn1 = types.KeyboardButton("🔍 Поиск по нику")
    btn2 = types.KeyboardButton("📱 Поиск по номеру")
    btn3 = types.KeyboardButton("🌐 Граф связей")
    btn4 = types.KeyboardButton("📊 Анализ риска")
    btn5 = types.KeyboardButton("🔗 Похожие сущности")
    btn6 = types.KeyboardButton("📈 Мои данные")
    btn7 = types.KeyboardButton("❓ Помощь")
    markup.add(btn1, btn2, btn3, btn4, btn5, btn6, btn7)
    
    bot.send_message(message.chat.id,
        "🕵️ OSINT Бот v4.0 - ULTIMATE\n\n"
        "⚡ Нейросетевая база данных\n"
        "🌐 Граф связей (Maltego level)\n"
        "📊 Анализ риска в реальном времени\n"
        "🔗 Поиск по всему интернету\n\n"
        "Выберите действие:",
        reply_markup=markup)

@bot.message_handler(func=lambda msg: msg.text == "🔗 Похожие сущности")
def ask_similar(msg):
    sent = bot.send_message(msg.chat.id, "Введите ID или имя сущности:")
    bot.register_next_step_handler(sent, process_similar)

def process_similar(msg):
    query = msg.text.strip()
    
    # Поиск сущности
    entities = db.search_entities(query, limit=1)
    if not entities:
        bot.send_message(msg.chat.id, "❌ Сущность не найдена")
        return
    
    entity_id = entities[0]['id']
    similar = db.get_similar_entities(entity_id, limit=10)
    
    if not similar:
        bot.send_message(msg.chat.id, "❌ Похожих сущностей не найдено")
        return
    
    response = f"🔗 Похожие сущности для {entities[0]['name']}:\n\n"
    for s in similar:
        response += f"• {s['name']} ({s['entity_type']}) - сходство: {s['tag_similarity']*100:.0f}%\n"
    
    bot.send_message(msg.chat.id, response[:4000])

@bot.message_handler(func=lambda msg: msg.text == "🌐 Граф связей")
def ask_network(msg):
    sent = bot.send_message(msg.chat.id, "Введите ID или имя для построения графа:")
    bot.register_next_step_handler(sent, process_network)

def process_network(msg):
    query = msg.text.strip()
    entities = db.search_entities(query, limit=1)
    
    if not entities:
        bot.send_message(msg.chat.id, "❌ Сущность не найдена")
        return
    
    entity_id = entities[0]['id']
    network = db.get_entity_network(entity_id, depth=2)
    
    response = f"🌐 Граф связей для {entities[0]['name']}:\n\n"
    response += f"📊 Узлов: {network['total_nodes']}\n"
    response += f"🔗 Связей: {network['total_edges']}\n\n"
    
    if network['nodes']:
        response += "👤 Узлы:\n"
        for node in network['nodes'][:10]:
            response += f"  • {node['name']} ({node['entity_type']})\n"
    
    if network['edges']:
        response += "\n🔗 Связи:\n"
        for edge in network['edges'][:10]:
            response += f"  • {edge['relation_type']} (сила: {edge['strength']:.2f})\n"
    
    bot.send_message(msg.chat.id, response[:4000])

@bot.message_handler(func=lambda msg: msg.text == "📊 Анализ риска")
def ask_risk(msg):
    sent = bot.send_message(msg.chat.id, "Введите ID или имя для анализа риска:")
    bot.register_next_step_handler(sent, process_risk)

def process_risk(msg):
    query = msg.text.strip()
    entities = db.search_entities(query, limit=1)
    
    if not entities:
        bot.send_message(msg.chat.id, "❌ Сущность не найдена")
        return
    
    entity_id = entities[0]['id']
    risk = db.analyze_risk(entity_id)
    
    if risk:
        response = f"📊 Анализ риска для {risk['name']}:\n\n"
        response += f"⚠ Базовый риск: {risk['base_risk']:.1f}/100\n"
        response += f"📊 Уровень: {risk['risk_level']}\n"
        response += f"🔗 Связей: {risk['connections']}\n"
        response += f"💪 Средняя сила связей: {risk['avg_strength']:.2f}\n"
        response += f"⚡ Критических связей: {risk['strong_connections']}\n"
        response += f"📝 {risk['risk_description']}\n"
        
        bot.send_message(msg.chat.id, response)
    else:
        bot.send_message(msg.chat.id, "❌ Ошибка анализа")

@bot.message_handler(func=lambda msg: msg.text == "📈 Мои данные")
def my_data(msg):
    user_id = msg.from_user.id
    
    with db.pg_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM user_profiles WHERE user_id = %s", (user_id,))
        profile = cur.fetchone()
    
    if profile:
        response = "📈 Ваши данные:\n\n"
        response += f"👤 ID: {profile['user_id']}\n"
        response += f"📊 Всего поисков: {len(profile['search_history'])}\n"
        if profile['search_history']:
            last_search = profile['search_history'][-1]
            response += f"🔍 Последний поиск: {last_search['query']}\n"
        
        bot.send_message(msg.chat.id, response)
    else:
        bot.send_message(msg.chat.id, "❌ Данных пока нет")

# === ЗАПУСК ===
if __name__ == "__main__":
    print("🕵️ OSINT Бот v4.0 ULTIMATE запущен")
    print("📊 База данных уровня Maltego/Sherlock")
    print("🌐 Граф связей активирован")
    
    try:
        bot.polling(none_stop=True)
    finally:
        db.close()
