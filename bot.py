# -*- coding: utf-8 -*-
import os
import json
import sqlite3
import hashlib
import pickle
import re
import time
import logging
import random
import csv
import io
import asyncio
import requests
from datetime import datetime, timedelta
from collections import deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler

# ===== ЛОГИРОВАНИЕ =====
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ===== КОНФИГУРАЦИЯ =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
HF_TOKEN = os.getenv("HF_TOKEN")
HF_MODELS = os.getenv("HF_MODELS", "google/flan-t5-small")
HF_MODELS = [m.strip() for m in HF_MODELS.split(",") if m.strip()]
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", None)

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN не задан")

# ===== БАЗА ДАННЫХ =====
DB_PATH = "memory.db"
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS aggressors (
            id TEXT PRIMARY KEY,
            history TEXT,
            weak_points TEXT,
            style_used TEXT,
            success_count INTEGER DEFAULT 0,
            first_attack TEXT,
            last_attack TEXT,
            total_attacks INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS aliases (
            player_id TEXT,
            alias TEXT,
            UNIQUE(player_id, alias)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            user_id TEXT,
            action TEXT,
            target TEXT,
            reply TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS dialog_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            aggressor_id TEXT,
            message TEXT,
            timestamp TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS banned (
            id TEXT PRIMARY KEY,
            reason TEXT,
            timestamp TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            user_id TEXT PRIMARY KEY,
            target_id TEXT,
            target_name TEXT,
            history TEXT,
            mode TEXT,
            created_at TEXT,
            last_activity TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS weak_points (
            player_id TEXT,
            weak_point TEXT,
            count INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def log_action(user_id, action, target=None, reply=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO logs (timestamp, user_id, action, target, reply) VALUES (?, ?, ?, ?, ?)",
        (datetime.now().isoformat(), str(user_id), action, str(target), str(reply))
    )
    conn.commit()
    conn.close()
    logger.info(f"LOG: {user_id} -> {action} -> {target}")

def save_session_to_db(user_id, session):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "REPLACE INTO sessions (user_id, target_id, target_name, history, mode, created_at, last_activity) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(user_id), str(session.get("target_id", "")), str(session.get("target_name", "")),
         json.dumps(session.get("history", [])), str(session.get("mode", "логик")),
         session.get("created_at", datetime.now().isoformat()), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def load_session_from_db(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT target_id, target_name, history, mode, created_at FROM sessions WHERE user_id=?", (str(user_id),))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "target_id": row[0],
            "target_name": row[1],
            "history": json.loads(row[2]) if row[2] else [],
            "mode": row[3] if row[3] else "логик",
            "created_at": row[4] if row[4] else datetime.now().isoformat()
        }
    return None

# ===== КЭШ =====
CACHE_PATH = "cache.pkl"
try:
    with open(CACHE_PATH, "rb") as f:
        CACHE = pickle.load(f)
except:
    CACHE = {}

def save_cache():
    with open(CACHE_PATH, "wb") as f:
        pickle.dump(CACHE, f)

def get_cache_key(attack_text, agg_id):
    raw = f"{attack_text}_{agg_id}"
    return hashlib.md5(raw.encode()).hexdigest()

# ===== ОЧЕРЕДЬ ЗАПРОСОВ =====
request_queue = []
last_request_time = 0

def queue_request(prompt):
    global last_request_time
    request_queue.append(prompt)
    if len(request_queue) > 50:
        return "⚠️ Слишком много запросов. Попробуйте позже."
    current_time = time.time()
    if current_time - last_request_time < 2:
        time.sleep(2 - (current_time - last_request_time))
    last_request_time = time.time()
    return None

# ===== АНАЛИЗ ТОНА =====
AGGRESSION_MARKERS = ["мам", "дурак", "лох", "идиот", "тупой", "гандон", "дебил", "хуй", "пизда", "бля", "ебал", "шлюха", "сука", "блять"]
SARCASM_MARKERS = ["ну", "да", "конечно", "ага", "ясно", "понятно", "ладно", "как скажешь", "ты прав"]
PASSIVE_MARKERS = ["извини", "прости", "наверное", "возможно", "кажется", "может быть", "наверно"]

def analyze_tone(text):
    """Определяет тон сообщения: агрессивный, саркастичный, пассивный или нейтральный"""
    lower = text.lower()
    aggression_score = sum(1 for word in AGGRESSION_MARKERS if word in lower)
    sarcasm_score = sum(1 for word in SARCASM_MARKERS if word in lower)
    passive_score = sum(1 for word in PASSIVE_MARKERS if word in lower)

    if aggression_score > sarcasm_score and aggression_score > passive_score:
        return "aggressive"
    elif sarcasm_score > aggression_score and sarcasm_score > passive_score:
        return "sarcastic"
    elif passive_score > aggression_score and passive_score > sarcasm_score:
        return "passive"
    else:
        return "neutral"

# ===== ВЫЗОВ HF =====
def call_hf(prompt, model):
    try:
        url = f"https://api-inference.huggingface.co/models/{model}"
        headers = {"Authorization": f"Bearer {HF_TOKEN}"}
        payload = {
            "inputs": prompt,
            "parameters": {
                "max_new_tokens": 50,
                "temperature": 0.85,
                "do_sample": True
            }
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and "generated_text" in data[0]:
                full = data[0]["generated_text"]
                return full[len(prompt):].strip()
        else:
            logger.warning(f"HF model {model} returned {resp.status_code}")
            return None
    except Exception as e:
        logger.warning(f"HF model {model} failed: {e}")
        return None

def call_openrouter(prompt):
    if not OPENROUTER_KEY:
        return None
    try:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "openrouter/free",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 50,
            "temperature": 0.85
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            if "choices" in data and len(data["choices"]) > 0:
                return data["choices"][0]["message"]["content"].strip()
        else:
            logger.warning(f"OpenRouter returned {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        logger.warning(f"OpenRouter failed: {e}")
        return None

def call_hf_with_fallback(prompt):
    queue_status = queue_request(prompt)
    if queue_status:
        return queue_status
    for model in HF_MODELS:
        logger.info(f"Пробуем HF модель: {model}")
        reply = call_hf(prompt, model)
        if reply:
            logger.info(f"✅ Ответ от HF: {model}")
            return reply
        else:
            logger.warning(f"❌ HF модель {model} не ответила")
    if OPENROUTER_KEY:
        logger.info("Пробуем OpenRouter...")
        reply = call_openrouter(prompt)
        if reply:
            logger.info("✅ Ответ от OpenRouter")
            return reply
        else:
            logger.warning("❌ OpenRouter не ответил")
    logger.warning("Все модели не ответили. Ответ не будет отправлен.")
    return None

# ===== ИЗВЛЕЧЕНИЕ ИМЁН =====
STOP_WORDS = {"я", "ты", "он", "она", "оно", "мы", "вы", "они", "меня", "тебя", "себя",
              "слабость", "сила", "кда", "скилл", "глуп", "туп", "ум", "возраст", "старый", "молод",
              "fuck", "why", "because", "nigga", "man", "stuff", "body", "mother", "fucker",
              "твоей", "твоя", "твоё", "его", "её", "вашей", "ваше", "своей"}

def extract_aliases(text):
    patterns = [
        r'я\s+[\-–]\s*([A-Za-zА-ЯЁа-яё0-9_\-]+)',
        r'меня\s+зовут\s+([A-Za-zА-ЯЁа-яё0-9_\-]+)',
        r'это\s+([A-Za-zА-ЯЁа-яё0-9_\-]+)',
        r'я\s+([A-Za-zА-ЯЁа-яё0-9_\-]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            name = match.group(1)
            if name.lower() not in STOP_WORDS:
                return name
    return None

def find_mentioned_player(text):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT player_id, alias FROM aliases")
    rows = c.fetchall()
    conn.close()
    for player_id, alias in rows:
        if alias.lower() in text.lower():
            if alias.lower() not in STOP_WORDS:
                return player_id, alias
    return None, None

# ===== ХРАНЕНИЕ СОСТОЯНИЙ =====
user_sessions = {}
banned_players = {}
last_message_time = {}
session_data = {}
message_queue = {}  # user_id -> [messages]

# ===== КНОПКИ =====
def get_main_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("🎯 Сменить цель", callback_data="action_change_target"),
            InlineKeyboardButton("🎭 Режим", callback_data="action_mode")
        ],
        [
            InlineKeyboardButton("📊 Тон", callback_data="action_tone"),
            InlineKeyboardButton("📤 Экспорт", callback_data="action_export")
        ],
        [
            InlineKeyboardButton("⏹ Завершить сессию", callback_data="action_stop"),
            InlineKeyboardButton("❓ Помощь", callback_data="action_help")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_mode_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("🧠 Логик", callback_data="mode_logic"),
            InlineKeyboardButton("🪞 Зеркало", callback_data="mode_mirror")
        ],
        [
            InlineKeyboardButton("😏 Сарказм", callback_data="mode_sarcasm"),
            InlineKeyboardButton("🔥 Провокатор", callback_data="mode_provocator")
        ],
        [
            InlineKeyboardButton("🧐 Психолог", callback_data="mode_psychologist"),
            InlineKeyboardButton("🌀 Хаос", callback_data="mode_chaos")
        ],
        [
            InlineKeyboardButton("📊 Статистик", callback_data="mode_statistic"),
            InlineKeyboardButton("🔙 Назад", callback_data="action_back")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_sender_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("👤 От меня", callback_data="sender_me"),
            InlineKeyboardButton("👤 От него", callback_data="sender_him")
        ],
        [
            InlineKeyboardButton("❓ Не знаю", callback_data="sender_unknown")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_count_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("1️⃣ Вариант", callback_data="count_1"),
            InlineKeyboardButton("3️⃣ Варианта", callback_data="count_3"),
            InlineKeyboardButton("5️⃣ Вариантов", callback_data="count_5")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# ===== ОБРАБОТЧИК СООБЩЕНИЙ =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message

    if not message:
        return

    if str(user_id) in banned_players:
        return

    # Задержка
    last_time = last_message_time.get(user_id, 0)
    if time.time() - last_time < 3:
        await asyncio.sleep(3 - (time.time() - last_time))
    last_message_time[user_id] = time.time()

    text = None
    if message.text:
        text = message.text
    elif message.forward_from or message.forward_from_chat:
        if message.forward_from_message_id:
            try:
                forwarded_msg = await context.bot.forward_message(
                    chat_id=message.chat.id,
                    from_chat_id=message.forward_from_chat.id if message.forward_from_chat else message.chat.id,
                    message_id=message.forward_from_message_id
                )
                text = forwarded_msg.text
            except:
                text = "⚠️ Не удалось получить текст пересланного сообщения."
        else:
            text = "⚠️ Пересланное сообщение без текста."

    if not text:
        return

    # === Команды ===
    if text.startswith("/"):
        if text.lower().startswith("/stop"):
            await stop_session(update, context)
            return
        if text.lower().startswith("/target"):
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                target_name = parts[1].strip()
                session_data[user_id] = {
                    "target_id": target_name,
                    "target_name": target_name,
                    "history": [],
                    "mode": "логик",
                    "created_at": datetime.now().isoformat()
                }
                save_session_to_db(user_id, session_data[user_id])
                await update.message.reply_text(
                    f"🎯 Цель установлена: {target_name}\n\n"
                    "Теперь отправляйте сообщения, и я буду анализировать их.",
                    reply_markup=get_main_keyboard()
                )
            return

    # === Определяем тон ===
    tone = analyze_tone(text)
    context.user_data["current_tone"] = tone

    # === Если есть активная сессия ===
    if user_id in session_data:
        session = session_data[user_id]
        if not session.get("target_id"):
            await update.message.reply_text("👤 С кем я общаюсь? Напишите имя цели.")
            user_sessions[user_id] = {"step": "waiting_target"}
            return

        # === Уточнение отправителя ===
        if user_id not in context.user_data.get("sender_confirmed", {}):
            await update.message.reply_text(
                f"❓ Это сообщение от вас или от оппонента ({session.get('target_name')})?",
                reply_markup=get_sender_keyboard()
            )
            context.user_data["pending_message"] = text
            context.user_data["pending_tone"] = tone
            return

        # === Обработка сообщения ===
        # Склеиваем, если есть очередь
        if user_id in message_queue and message_queue[user_id]:
            message_queue[user_id].append(text)
            await update.message.reply_text(
                f"📝 Сообщение добавлено в очередь. Всего: {len(message_queue[user_id])}. Нажмите «Готово», чтобы проанализировать.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Готово", callback_data="action_process_queue")]])
            )
            return
        else:
            message_queue[user_id] = [text]
            await update.message.reply_text(
                "📝 Сообщение добавлено. Отправьте ещё или нажмите «Готово».",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Готово", callback_data="action_process_queue")]])
            )
            return

    # === Новая сессия ===
    await auto_detect_target(update, context, text)

async def auto_detect_target(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    potential_names = re.findall(r'\b([A-Za-zА-ЯЁа-яё0-9_\-]+)\b', text)
    candidates = [name for name in potential_names if name.lower() not in STOP_WORDS]
    if candidates:
        target_name = candidates[0]
        session_data[user_id] = {
            "target_id": target_name,
            "target_name": target_name,
            "history": [],
            "mode": "логик",
            "created_at": datetime.now().isoformat()
        }
        save_session_to_db(user_id, session_data[user_id])
        await update.message.reply_text(
            f"🎯 Я определил цель: {target_name}\n\n"
            "Это правильный игрок? (Если нет — используйте кнопку «Сменить цель»)",
            reply_markup=get_main_keyboard()
        )
    else:
        await update.message.reply_text(
            "❌ Не удалось определить цель.\n"
            "Напишите имя вручную, или я спрошу вас позже.",
            reply_markup=get_main_keyboard()
        )
        user_sessions[user_id] = {"step": "waiting_target"}

async def generate_response(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    session = session_data.get(user_id)
    if not session:
        return

    target_name = session.get("target_name")
    mode = session.get("mode", "логик")

    # Сохраняем в историю
    session["history"].append({"role": "user", "text": text, "timestamp": datetime.now().isoformat()})
    if len(session["history"]) > 20:
        session["history"] = session["history"][-20:]
    save_session_to_db(user_id, session)

    # Получаем тон
    tone = context.user_data.get("current_tone", "neutral")

    styles = {
        "логик": "логический анализ, разбор аргументов",
        "зеркало": "копирование стиля с переворотом смысла",
        "сарказм": "уничтожение через иронию",
        "провокатор": "вызов эмоций, провокация",
        "психолог": "анализ поведения с юмором",
        "хаос": "абсурдные, но логически связанные ответы",
        "статистик": "статистика по оппоненту"
    }

    # Получаем слабые места
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT weak_point FROM weak_points WHERE player_id=?", (target_name,))
    weak_rows = c.fetchall()
    conn.close()
    weak_points = [row[0] for row in weak_rows] if weak_rows else ["неизвестно"]

    prompt = (
        f"Ты — бот-ассистент по троллингу. Оппонент: {target_name}. "
        f"Сообщение: \"{text}\". Режим: {mode} ({styles.get(mode, mode)}). "
        f"Тон сообщения: {tone}. Слабые места оппонента: {', '.join(weak_points)}. "
        f"История диалога (последние 3 сообщения): {json.dumps(session['history'][-3:])}. "
        f"Сделай ответ максимально колким, логичным и неожиданным. "
        f"Твоя задача: сгенерировать 5 вариантов ответа (до 20 слов каждый), "
        f"которые уничтожат оппонента, используя его же логику и слабые места. "
        f"Варианты:"
    )

    reply = call_hf_with_fallback(prompt)
    if not reply:
        await update.message.reply_text("⚠️ Не удалось сгенерировать ответ. Попробуйте позже.")
        return

    options = reply.split("\n")
    options = [o.strip() for o in options if o.strip() and len(o.strip()) > 5]
    if len(options) < 3:
        options = options + ["Вариант 1: " + reply, "Вариант 2: " + reply, "Вариант 3: " + reply]
    options = options[:5]

    context.user_data["last_options"] = options
    context.user_data["last_prompt"] = prompt

    # Стратегии
    strategies = [
        "Сейчас лучше ответить холодно — он потеряет контроль.",
        "Используй сарказм — он не выдержит насмешки.",
        "Бей в логику — у него нет аргументов.",
        "Спровоцируй его на эмоции — он ошибётся.",
        "Ответь зеркально — он увидит себя со стороны.",
        "Используй абсурд — он не поймёт, как реагировать.",
        "Покажи статистику — это его слабое место."
    ]
    strategy = random.choice(strategies)

    # Кнопки для 5 вариантов
    buttons = []
    for i in range(5):
        buttons.append(InlineKeyboardButton(f"📝 Вариант {i+1}", callback_data=f"choose_{i}"))
    keyboard = [buttons]
    keyboard.append([
        InlineKeyboardButton("🔄 Ещё 5 вариантов", callback_data="action_more"),
        InlineKeyboardButton("🔙 Назад", callback_data="action_back")
    ])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"🎯 Цель: {target_name} | Режим: {mode}\n"
        f"📊 Тон: {tone}\n\n"
        f"**Варианты ответа:**\n"
        + "\n".join([f"{i+1}️⃣ {options[i]}" for i in range(len(options))]) +
        f"\n\n💡 Стратегия: {strategy}\n\n"
        f"Выберите вариант или сгенерируйте новые:",
        reply_markup=reply_markup
    )

def calculate_degree(text):
    score = 0
    offensive_words = ["мам", "дурак", "лох", "идиот", "тупой", "гандон", "дебил", "хуй", "пизда", "бля", "ебал", "шлюха", "сука", "блять"]
    for word in offensive_words:
        if word in text.lower():
            score += 2
    if len(text) > 50:
        score += 1
    if text.isupper():
        score += 1
    return min(10, score)

# ===== ОБРАБОТЧИК КНОПОК =====
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    # === Выбор варианта ===
    if data.startswith("choose_"):
        idx = int(data.replace("choose_", ""))
        options = context.user_data.get("last_options", [])
        if idx < len(options):
            await query.edit_message_text(f"✅ Выбран вариант {idx+1}:\n\n{options[idx]}")
        return

    # === Ещё 5 вариантов ===
    if data == "action_more":
        prompt = context.user_data.get("last_prompt", "")
        if prompt:
            reply = call_hf_with_fallback(prompt + " Дай ещё 5 вариантов (новых).")
            if reply:
                options = reply.split("\n")
                options = [o.strip() for o in options if o.strip() and len(o.strip()) > 5][:5]
                context.user_data["last_options"] = options
                buttons = []
                for i in range(5):
                    buttons.append(InlineKeyboardButton(f"📝 Вариант {i+1}", callback_data=f"choose_{i}"))
                keyboard = [buttons]
                keyboard.append([
                    InlineKeyboardButton("🔄 Ещё 5 вариантов", callback_data="action_more"),
                    InlineKeyboardButton("🔙 Назад", callback_data="action_back")
                ])
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    f"🎯 Новые варианты:\n\n"
                    + "\n".join([f"{i+1}️⃣ {options[i]}" for i in range(len(options))]) +
                    f"\n\nВыберите вариант:",
                    reply_markup=reply_markup
                )
        return

    # === Уточнение отправителя ===
    if data.startswith("sender_"):
        sender = data.replace("sender_", "")
        session = session_data.get(user_id)
        if not session:
            await query.edit_message_text("❌ Нет активной сессии.")
            return
        pending_text = context.user_data.get("pending_message")
        if not pending_text:
            await query.edit_message_text("❌ Нет сообщения для обработки.")
            return

        if sender == "him":
            # Сохраняем сообщение как от оппонента
            session["history"].append({"role": "opponent", "text": pending_text, "timestamp": datetime.now().isoformat()})
            await query.edit_message_text(f"✅ Сообщение от {session.get('target_name')} сохранено.")
            context.user_data["sender_confirmed"] = True
            # Генерируем ответ
            await generate_response(update, context, pending_text)
        elif sender == "me":
            session["history"].append({"role": "user", "text": pending_text, "timestamp": datetime.now().isoformat()})
            await query.edit_message_text("✅ Ваше сообщение сохранено как контекст.")
            context.user_data["sender_confirmed"] = True
        else:
            await query.edit_message_text("❌ Не удалось определить отправителя. Попробуйте ещё раз.")
        context.user_data.pop("pending_message", None)
        return

    # === Обработка очереди ===
    if data == "action_process_queue":
        if user_id not in message_queue or not message_queue[user_id]:
            await query.edit_message_text("❌ Очередь пуста.")
            return
        full_text = "\n".join(message_queue[user_id])
        message_queue[user_id] = []
        await query.edit_message_text("📝 Анализирую накопленные сообщения...")
        await generate_response(update, context, full_text)
        return

    # === Остальные действия ===
    if data == "action_change_target":
        await query.edit_message_text("📝 Введите имя цели вручную:\nНапишите: /target <имя>")
        return

    if data == "action_mode":
        await query.edit_message_text("🎭 Выберите режим троллинга:", reply_markup=get_mode_keyboard())
        return

    if data.startswith("mode_"):
        mode = data.replace("mode_", "")
        if user_id in session_data:
            session_data[user_id]["mode"] = mode
            save_session_to_db(user_id, session_data[user_id])
        mode_names = {
            "logic": "Логик",
            "mirror": "Зеркало",
            "sarcasm": "Сарказм",
            "provocator": "Провокатор",
            "psychologist": "Психолог",
            "chaos": "Хаос",
            "statistic": "Статистик"
        }
        mode_descriptions = {
            "logic": "Разбираю аргументы по косточкам, уничтожаю логикой.",
            "mirror": "Копирую стиль противника, переворачиваю смысл.",
            "sarcasm": "Уничтожаю через иронию и сарказм.",
            "provocator": "Провоцирую на эмоции, чтобы он ошибся.",
            "psychologist": "Анализирую поведение с юмором и колкостями.",
            "chaos": "Абсурдные, но логически связанные ответы.",
            "statistic": "Показываю статистику по оппоненту."
        }
        await query.edit_message_text(
            f"✅ Режим **{mode_names.get(mode, mode)}** выбран.\n"
            f"Описание: {mode_descriptions.get(mode, '')}\n\n"
            f"Продолжайте отправлять сообщения."
        )
        return

    if data == "action_tone":
        if user_id in session_data and session_data[user_id]["history"]:
            last_msg = session_data[user_id]["history"][-1]["text"]
            tone = analyze_tone(last_msg)
            await query.edit_message_text(f"📊 Тон последнего сообщения: **{tone}**")
        else:
            await query.edit_message_text("📊 Нет данных для анализа тона.")
        return

    if data == "action_export":
        if user_id in session_data:
            session = session_data[user_id]
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["Time", "Role", "Message"])
            for entry in session.get("history", []):
                writer.writerow([entry.get("timestamp", ""), entry.get("role", ""), entry.get("text", "")])
            await query.edit_message_text("📤 Экспортирую диалог...")
            await update.effective_message.reply_document(
                document=io.BytesIO(output.getvalue().encode()),
                filename=f"dialog_{session.get('target_name', 'unknown')}.csv",
                caption="📊 История диалога"
            )
        else:
            await query.edit_message_text("ℹ️ Нет активной сессии для экспорта.")
        return

    if data == "action_stop":
        await stop_session(update, context)
        return

    if data == "action_help":
        await query.edit_message_text(
            "👋 Я — бот-ассистент по троллингу.\n\n"
            "📌 Как я работаю:\n"
            "1. Отправьте сообщение оппонента (скопируйте из игры).\n"
            "2. Я определю цель или попрошу уточнить.\n"
            "3. Я анализирую тон и предлагаю 5 вариантов ответа.\n"
            "4. Вы выбираете вариант или генерируете новые.\n\n"
            "🎭 Режимы:\n"
            "• Логик — разбор аргументов\n"
            "• Зеркало — переворот смысла\n"
            "• Сарказм — ирония\n"
            "• Провокатор — вызов эмоций\n"
            "• Психолог — анализ с юмором\n"
            "• Хаос — абсурдные ответы\n"
            "• Статистик — статистика по оппоненту\n\n"
            "📊 Тон — анализ агрессии, сарказма, пассивности.\n"
            "📤 Экспорт — выгрузка диалога в CSV.\n"
            "⏹ Завершить сессию — остановить анализ."
        )
        return

    if data == "action_back":
        await query.edit_message_text("🔙 Возврат в главное меню.", reply_markup=get_main_keyboard())
        return

async def stop_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in session_data:
        del session_data[user_id]
    if user_id in user_sessions:
        del user_sessions[user_id]
    await update.message.reply_text(
        "⏹ Сессия завершена.\nДанные сохранены в историю диалогов.",
        reply_markup=get_main_keyboard()
    )

# ===== КОМАНДЫ =====
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Я — бот-ассистент по троллингу.\n\n"
        "📌 Как я работаю:\n"
        "1. Отправьте сообщение оппонента (скопируйте из игры).\n"
        "2. Я определю цель или попрошу уточнить.\n"
        "3. Я анализирую тон и предлагаю 5 вариантов ответа.\n"
        "4. Вы выбираете вариант или генерируете новые.\n\n"
        "🎭 Режимы: Логик, Зеркало, Сарказм, Провокатор, Психолог, Хаос, Статистик.\n"
        "📊 Тон — анализ агрессии, сарказма, пассивности.\n"
        "📤 Экспорт — выгрузка диалога в CSV.\n"
        "⏹ Завершить сессию — остановить анализ.\n\n"
        "Нажмите кнопку, чтобы начать:",
        reply_markup=get_main_keyboard()
    )

async def target_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.args:
        target_name = " ".join(context.args)
        session_data[user_id] = {
            "target_id": target_name,
            "target_name": target_name,
            "history": [],
            "mode": "логик",
            "created_at": datetime.now().isoformat()
        }
        save_session_to_db(user_id, session_data[user_id])
        await update.message.reply_text(
            f"🎯 Цель установлена: {target_name}\n\n"
            "Теперь отправляйте сообщения, и я буду анализировать их.",
            reply_markup=get_main_keyboard()
        )
    else:
        await update.message.reply_text("❌ Укажите имя: /target <имя>")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_session(update, context)

# ===== ЗАПУСК =====
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("target", target_command))
    app.add_handler(CommandHandler("stop", stop_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^(confirm_|deny_|choose_|mode_|sender_|action_)"))

    logger.info("Бот запущен с полной функциональностью (HF + OpenRouter)...")
    app.run_polling()

if __name__ == "__main__":
    main()
