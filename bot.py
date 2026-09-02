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
GROQ_API_KEY = os.getenv("GROQ_API_KEY", None)  # <-- НОВОЕ: для голосовых

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
            created_at TEXT,
            last_activity TEXT
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

# ===== ВЫЗОВ HF (с несколькими моделями) =====
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

# ===== ИЗВЛЕЧЕНИЕ ИМЁН (УЛУЧШЕННОЕ) =====
STOP_WORDS = {"я", "ты", "он", "она", "оно", "мы", "вы", "они", "меня", "тебя", "себя",
              "слабость", "сила", "кда", "скилл", "глуп", "туп", "ум", "возраст", "старый", "молод",
              "fuck", "why", "because", "nigga", "man", "stuff", "body", "mother", "fucker",
              "твоей", "твоя", "твоё", "его", "её", "вашей", "ваше", "своей"}

OFFENSIVE_WORDS = ["мам", "дурак", "лох", "идиот", "тупой", "гандон", "дебил", 
                   "fuck", "nigga", "mother", "fucker", "хуй", "хуя", "пизда", 
                   "бля", "еб", "ебал", "шлюха", "сука", "блять"]

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

def is_offensive(text):
    return any(word in text.lower() for word in OFFENSIVE_WORDS)

# ===== РАСПОЗНАВАНИЕ ГОЛОСОВЫХ СООБЩЕНИЙ (через Groq Whisper API) =====
async def transcribe_voice(file_path):
    """Отправляет аудиофайл в Groq Whisper API и возвращает текст"""
    if not GROQ_API_KEY:
        logger.warning("GROQ_API_KEY не задан")
        return None
    try:
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
        with open(file_path, "rb") as f:
            files = {"file": f}
            data = {"model": "whisper-large-v3", "language": "ru"}
            resp = requests.post(url, headers=headers, files=files, data=data, timeout=30)
            if resp.status_code == 200:
                result = resp.json()
                return result.get("text", "").strip()
            else:
                logger.warning(f"Groq returned {resp.status_code}: {resp.text}")
                return None
    except Exception as e:
        logger.warning(f"Groq Whisper failed: {e}")
        return None

# ===== ХРАНЕНИЕ СОСТОЯНИЙ =====
user_sessions = {}
user_chat_collection = {}
dialog_histories = {}
banned_players = {}
last_message_time = {}
session_data = {}  # user_id -> {"target_id": ..., "target_name": ..., "history": [], "mode": "логик"}

# ===== КНОПКИ ГЛАВНОГО МЕНЮ =====
def get_main_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("🎯 Сменить цель", callback_data="action_change_target"),
            InlineKeyboardButton("🎭 Режим", callback_data="action_mode")
        ],
        [
            InlineKeyboardButton("📊 Градус", callback_data="action_degree"),
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

# ===== ОБРАБОТЧИК СООБЩЕНИЙ =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message

    if not message:
        return

    if str(user_id) in banned_players:
        return

    # ===== ОТЛОЖЕННЫЙ ОТВЕТ (ЗАДЕРЖКА) =====
    last_time = last_message_time.get(user_id, 0)
    if time.time() - last_time < 3:
        await asyncio.sleep(3 - (time.time() - last_time))
    last_message_time[user_id] = time.time()

    # ===== ОБРАБОТКА ГОЛОСОВЫХ СООБЩЕНИЙ =====
    if message.voice:
        await update.message.reply_text("🎤 Распознаю голосовое сообщение...")
        try:
            file = await context.bot.get_file(message.voice.file_id)
            file_path = f"voice_{user_id}_{int(time.time())}.ogg"
            await file.download_to_drive(file_path)
            text = await transcribe_voice(file_path)
            os.remove(file_path)
            if text:
                await update.message.reply_text(f"📝 Распознано: \"{text}\"")
                # Подставляем распознанный текст в обработку
                message.text = text
            else:
                await update.message.reply_text("❌ Не удалось распознать голосовое сообщение.")
                return
        except Exception as e:
            logger.error(f"Voice processing error: {e}")
            await update.message.reply_text("❌ Ошибка при обработке голосового сообщения.")
            return

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

    # ===== АНАЛИЗ КОНТЕКСТА =====
    context_analysis = analyze_context(text)

    # ===== СЕССИЯ: ПРОВЕРКА НА АКТИВНОСТЬ =====
    if user_id in session_data:
        session = session_data[user_id]
        # Если цель не определена — пытаемся определить
        if not session.get("target_id"):
            await auto_detect_target(update, context, text)
            return
        # Если цель определена — анализируем
        if text.lower().startswith("/stop"):
            await stop_session(update, context)
            return
        # Проверка на оскорбления
        if is_offensive(text):
            degree = calculate_degree(text)
            if degree >= 5:
                await generate_response(update, context, text)
                return
            else:
                await update.message.reply_text(f"📊 Градус: {degree}/10. Пока не критично.")
                return
        else:
            await update.message.reply_text("ℹ️ В сообщении нет оскорблений. Продолжаю наблюдение.")
            return

    # ===== НОВАЯ СЕССИЯ =====
    # Проверяем, есть ли в тексте явное указание на цель (через команду или контекст)
    if text.lower().startswith("/target"):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            target_name = parts[1].strip()
            session_data[user_id] = {
                "target_id": target_name,
                "target_name": target_name,
                "history": [],
                "mode": "логик",
                "created_at": datetime.now()
            }
            await update.message.reply_text(
                f"🎯 Цель установлена: {target_name}\n\n"
                "Теперь пересылайте мне сообщения оппонента, и я буду анализировать их.",
                reply_markup=get_main_keyboard()
            )
            return

    # Пытаемся автоматически определить цель
    await auto_detect_target(update, context, text)

async def auto_detect_target(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Автоматически определяет цель по первому сообщению"""
    user_id = update.effective_user.id
    # Ищем имя в тексте
    potential_names = re.findall(r'\b([A-Za-zА-ЯЁа-яё0-9_\-]+)\b', text)
    candidates = [name for name in potential_names if name.lower() not in STOP_WORDS]
    if candidates:
        target_name = candidates[0]
        session_data[user_id] = {
            "target_id": target_name,
            "target_name": target_name,
            "history": [],
            "mode": "логик",
            "created_at": datetime.now()
        }
        await update.message.reply_text(
            f"🎯 Я определил цель: {target_name}\n\n"
            "Это правильный игрок? (Если нет — используйте кнопку «Сменить цель»)",
            reply_markup=get_main_keyboard()
        )
        return
    else:
        await update.message.reply_text(
            "❌ Не удалось определить цель.\n"
            "Укажите имя вручную: /target <имя>\n"
            "Или нажмите кнопку «Сменить цель».",
            reply_markup=get_main_keyboard()
        )

async def generate_response(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Генерирует 3 варианта ответа"""
    user_id = update.effective_user.id
    session = session_data.get(user_id)
    if not session:
        return

    target_id = session.get("target_id")
    target_name = session.get("target_name")
    mode = session.get("mode", "логик")

    # Сохраняем в историю
    session["history"].append({"role": "user", "text": text, "timestamp": datetime.now().isoformat()})
    if len(session["history"]) > 20:
        session["history"] = session["history"][-20:]

    # Генерируем ответы
    styles = {
        "логик": "логический анализ, разбор аргументов",
        "зеркало": "копирование стиля с переворотом смысла",
        "сарказм": "уничтожение через иронию",
        "провокатор": "вызов эмоций, провокация",
        "психолог": "анализ поведения с юмором",
        "хаос": "абсурдные, но логически связанные ответы",
        "статистик": "статистика по оппоненту"
    }

    prompt = (
        f"Ты — бот-ассистент по троллингу. Оппонент: {target_name}. "
        f"Сообщение: \"{text}\". Режим: {mode} ({styles.get(mode, mode)}). "
        f"История диалога (последние 3 сообщения): {json.dumps(session['history'][-3:])}. "
        f"Твоя задача: сгенерировать 3 варианта ответа (до 20 слов каждый), "
        f"которые уничтожат оппонента, используя его же логику. "
        f"Варианты:"
    )

    reply = call_hf_with_fallback(prompt)
    if not reply:
        await update.message.reply_text("⚠️ Не удалось сгенерировать ответ. Попробуйте позже.")
        return

    # Разбиваем на варианты (если есть)
    options = reply.split("\n")
    options = [o.strip() for o in options if o.strip() and len(o.strip()) > 5]
    if len(options) < 3:
        options = options + ["Вариант 1: " + reply, "Вариант 2: " + reply, "Вариант 3: " + reply]
    options = options[:3]

    # Сохраняем последние варианты для кнопки "Ещё"
    context.user_data["last_options"] = options
    context.user_data["last_prompt"] = prompt

    keyboard = [
        [
            InlineKeyboardButton("📝 Вариант 1", callback_data=f"choose_0"),
            InlineKeyboardButton("📝 Вариант 2", callback_data=f"choose_1"),
            InlineKeyboardButton("📝 Вариант 3", callback_data=f"choose_2")
        ],
        [
            InlineKeyboardButton("🔄 Ещё вариант", callback_data="action_more"),
            InlineKeyboardButton("🔙 Назад", callback_data="action_back")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"🎯 Цель: {target_name} | Режим: {mode}\n\n"
        f"📊 Градус: {calculate_degree(text)}/10\n\n"
        f"**Варианты ответа:**\n"
        f"1️⃣ {options[0]}\n"
        f"2️⃣ {options[1]}\n"
        f"3️⃣ {options[2]}\n\n"
        f"Выберите вариант или сгенерируйте новый:",
        reply_markup=reply_markup
    )

def calculate_degree(text):
    """Оценивает градус напряжённости от 0 до 10"""
    score = 0
    for word in OFFENSIVE_WORDS:
        if word in text.lower():
            score += 2
    if len(text) > 50:
        score += 1
    if text.isupper():
        score += 1
    return min(10, score)

def analyze_context(text):
    emojis = re.findall(r'[\U0001F600-\U0001F64F]', text)
    caps = sum(1 for c in text if c.isupper())
    length = len(text)
    return {
        "emojis": emojis,
        "caps_percent": caps / len(text) if len(text) > 0 else 0,
        "length": length,
        "is_short": length < 30,
        "is_long": length > 60
    }

# ===== ОБРАБОТЧИК КНОПОК (CallbackQuery) =====
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    # ===== ВЫБОР ВАРИАНТА ОТВЕТА =====
    if data.startswith("choose_"):
        idx = int(data.replace("choose_", ""))
        options = context.user_data.get("last_options", [])
        if idx < len(options):
            await query.edit_message_text(f"✅ Выбран вариант {idx+1}:\n\n{options[idx]}")
            # Можно добавить кнопку "Отправить в чат" или "Копировать"
        return

    # ===== ЕЩЁ ВАРИАНТ =====
    if data == "action_more":
        prompt = context.user_data.get("last_prompt", "")
        if prompt:
            reply = call_hf_with_fallback(prompt + " Дай ещё 3 варианта.")
            if reply:
                options = reply.split("\n")
                options = [o.strip() for o in options if o.strip() and len(o.strip()) > 5][:3]
                context.user_data["last_options"] = options
                keyboard = [
                    [
                        InlineKeyboardButton("📝 Вариант 1", callback_data=f"choose_0"),
                        InlineKeyboardButton("📝 Вариант 2", callback_data=f"choose_1"),
                        InlineKeyboardButton("📝 Вариант 3", callback_data=f"choose_2")
                    ],
                    [
                        InlineKeyboardButton("🔄 Ещё вариант", callback_data="action_more"),
                        InlineKeyboardButton("🔙 Назад", callback_data="action_back")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    f"🎯 Новые варианты:\n\n"
                    f"1️⃣ {options[0]}\n"
                    f"2️⃣ {options[1]}\n"
                    f"3️⃣ {options[2]}\n\n"
                    f"Выберите вариант:",
                    reply_markup=reply_markup
                )
        return

    # ===== СМЕНИТЬ ЦЕЛЬ =====
    if data == "action_change_target":
        await query.edit_message_text(
            "📝 Введите имя цели вручную:\n"
            "Напишите: /target <имя>"
        )
        return

    # ===== РЕЖИМ =====
    if data == "action_mode":
        await query.edit_message_text(
            "🎭 Выберите режим троллинга:",
            reply_markup=get_mode_keyboard()
        )
        return

    # ===== ВЫБОР РЕЖИМА =====
    if data.startswith("mode_"):
        mode = data.replace("mode_", "")
        if user_id in session_data:
            session_data[user_id]["mode"] = mode
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
            f"Продолжайте пересылать сообщения."
        )
        return

    # ===== ГРАДУС =====
    if data == "action_degree":
        # Анализируем последнее сообщение из сессии
        if user_id in session_data and session_data[user_id]["history"]:
            last_msg = session_data[user_id]["history"][-1]["text"]
            degree = calculate_degree(last_msg)
            await query.edit_message_text(
                f"📊 Текущий градус: {degree}/10\n"
                f"{'🟢 Спокойно' if degree < 4 else '🟡 Напряжённо' if degree < 7 else '🔴 Взрыв!'}"
            )
        else:
            await query.edit_message_text("📊 Нет данных для анализа градуса.")
        return

    # ===== ЭКСПОРТ =====
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

    # ===== ЗАВЕРШИТЬ СЕССИЮ =====
    if data == "action_stop":
        await stop_session(update, context)
        return

    # ===== ПОМОЩЬ =====
    if data == "action_help":
        await query.edit_message_text(
            "👋 Я — бот-ассистент по троллингу.\n\n"
            "📌 Как я работаю:\n"
            "1. Перешлите мне сообщение оппонента.\n"
            "2. Я определю цель или попрошу уточнить.\n"
            "3. Я анализирую каждое сообщение и предлагаю 3 варианта ответа.\n"
            "4. Вы выбираете вариант или генерируете новый.\n\n"
            "🎭 Режимы:\n"
            "• Логик — разбор аргументов\n"
            "• Зеркало — переворот смысла\n"
            "• Сарказм — ирония\n"
            "• Провокатор — вызов эмоций\n"
            "• Психолог — анализ с юмором\n"
            "• Хаос — абсурдные ответы\n"
            "• Статистик — статистика по оппоненту\n\n"
            "📊 Градус — оценка напряжённости.\n"
            "📤 Экспорт — выгрузка диалога в CSV.\n"
            "⏹ Завершить сессию — остановить анализ."
        )
        return

    # ===== НАЗАД =====
    if data == "action_back":
        await query.edit_message_text(
            "🔙 Возврат в главное меню.",
            reply_markup=get_main_keyboard()
        )
        return

async def stop_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in session_data:
        del session_data[user_id]
    await update.message.reply_text(
        "⏹ Сессия завершена.\n"
        "Данные сохранены в историю диалогов.",
        reply_markup=get_main_keyboard()
    )

# ===== ОБРАБОТЧИК КОМАНД =====
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Я — бот-ассистент по троллингу.\n\n"
        "📌 Как я работаю:\n"
        "1. Перешлите мне сообщение оппонента.\n"
        "2. Я определю цель или попрошу уточнить.\n"
        "3. Я анализирую каждое сообщение и предлагаю 3 варианта ответа.\n"
        "4. Вы выбираете вариант или генерируете новый.\n\n"
        "🎭 Режимы: Логик, Зеркало, Сарказм, Провокатор, Психолог, Хаос, Статистик.\n"
        "📊 Градус — оценка напряжённости.\n"
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
            "created_at": datetime.now()
        }
        await update.message.reply_text(
            f"🎯 Цель установлена: {target_name}\n\n"
            "Теперь пересылайте мне сообщения оппонента, и я буду анализировать их.",
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
    app.add_handler(MessageHandler(filters.VOICE, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^(confirm_|deny_|choose_|mode_|action_)"))

    logger.info("Бот запущен с полной функциональностью (HF + OpenRouter + Groq Whisper)...")
    app.run_polling()

if __name__ == "__main__":
    main()
