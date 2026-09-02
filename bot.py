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

    # 1. Пробуем HF модели
    for model in HF_MODELS:
        logger.info(f"Пробуем HF модель: {model}")
        reply = call_hf(prompt, model)
        if reply:
            logger.info(f"✅ Ответ от HF: {model}")
            return reply
        else:
            logger.warning(f"❌ HF модель {model} не ответила")

    # 2. Пробуем OpenRouter
    if OPENROUTER_KEY:
        logger.info("Пробуем OpenRouter...")
        reply = call_openrouter(prompt)
        if reply:
            logger.info("✅ Ответ от OpenRouter")
            return reply
        else:
            logger.warning("❌ OpenRouter не ответил")

    # 3. Если ничего не сработало — возвращаем None (без Fallback)
    logger.warning("Все модели не ответили. Ответ не будет отправлен.")
    return None

# ===== ИЗВЛЕЧЕНИЕ ИМЁН (УЛУЧШЕННОЕ) =====
STOP_WORDS = {"я", "ты", "он", "она", "оно", "мы", "вы", "они", "меня", "тебя", "себя",
              "слабость", "сила", "кда", "скилл", "глуп", "туп", "ум", "возраст", "старый", "молод",
              "fuck", "why", "because", "nigga", "man", "stuff", "body", "mother", "fucker"}

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
    offensive_words = ["мам", "дурак", "лох", "идиот", "тупой", "гандон", "дебил", "fuck", "nigga", "mother", "fucker"]
    return any(word in text.lower() for word in offensive_words)

# ===== ХРАНЕНИЕ СОСТОЯНИЙ =====
user_sessions = {}
user_chat_collection = {}
dialog_histories = {}
banned_players = {}
last_message_time = {}

# ===== ОБРАБОТЧИК СООБЩЕНИЙ =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message

    if not message:
        return

    # Проверяем, не забанен ли пользователь
    if str(user_id) in banned_players:
        return

    # ===== ОТЛОЖЕННЫЙ ОТВЕТ (ЗАДЕРЖКА) =====
    last_time = last_message_time.get(user_id, 0)
    if time.time() - last_time < 5:
        await asyncio.sleep(5 - (time.time() - last_time))
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

    # ===== АНАЛИЗ КОНТЕКСТА (ЭМОДЗИ, ДЛИНА, КАПС) =====
    context_analysis = analyze_context(text)

    if user_id in user_sessions:
        if datetime.now() - user_sessions[user_id].get("created_at", datetime.now()) > timedelta(minutes=10):
            del user_sessions[user_id]
            await update.message.reply_text("⏰ Сессия устарела. Начните заново.")
            return

    if user_id in user_chat_collection:
        if text.lower() == "/done":
            full_text = "\n".join(user_chat_collection[user_id])
            del user_chat_collection[user_id]
            await process_text(update, context, full_text, context_analysis)
            return
        else:
            user_chat_collection[user_id].append(text)
            await update.message.reply_text(f"📝 Сообщение добавлено. Всего: {len(user_chat_collection[user_id])}. Напишите /done, когда закончите.")
            return

    if text.lower() == "/done":
        await update.message.reply_text("❌ Вы не в режиме сбора переписки. Напишите /chat чтобы начать.")
        return

    if text.lower().startswith("/auto"):
        context.user_data["auto_mode"] = True
        await update.message.reply_text("✅ Режим /auto включён. Теперь бот будет отвечать сразу без уточнения.")
        return

    if text.lower().startswith("/test"):
        # ===== ТЕСТОВЫЙ РЕЖИМ =====
        await update.message.reply_text("🧪 Тестовый режим: я бы ответил так:")
        await process_text(update, context, text, context_analysis)
        return

    if text.lower().startswith("/ban") and update.effective_user.id in [YOUR_ADMIN_ID]:
        parts = text.split()
        if len(parts) > 1:
            banned_players[parts[1]] = {"reason": " ".join(parts[2:]), "timestamp": datetime.now()}
            await update.message.reply_text(f"🚫 Игрок {parts[1]} забанен.")
        return

    await process_text(update, context, text, context_analysis)

def analyze_context(text):
    """Анализирует контекст сообщения (эмодзи, длина, капс)"""
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

# ===== ОСНОВНАЯ ЛОГИКА АНАЛИЗА ТЕКСТА =====
async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, context_analysis=None):
    user_id = update.effective_user.id
    auto_mode = context.user_data.get("auto_mode", False)

    if not is_offensive(text):
        await update.message.reply_text("ℹ️ В переписке не обнаружено оскорблений.")
        return

    # Сохраняем в историю диалога
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO dialog_history (aggressor_id, message, timestamp) VALUES (?, ?, ?)",
              (str(user_id), text, datetime.now().isoformat()))
    conn.commit()
    conn.close()

    alias = extract_aliases(text)
    if alias:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO aliases (player_id, alias) VALUES (?, ?)", (str(update.effective_user.id), alias))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"👤 Запомнил: {alias} (это вы)")
        log_action(user_id, "self_identify", alias)

    potential_names = re.findall(r'\b([A-Za-zА-ЯЁа-яё0-9_\-]+)\b', text)
    candidates = [name for name in potential_names if name.lower() not in STOP_WORDS]
    mentioned_id, mentioned_alias = find_mentioned_player(text)

    # ===== ДИНАМИЧЕСКАЯ СТАТИСТИКА =====
    if mentioned_id:
        stats = get_player_stats(mentioned_id)
        if stats:
            await update.message.reply_text(f"📊 Статистика по {mentioned_alias}: {stats}")

    # ===== РЕЖИМ "ТЕНЬ" (ПАССИВНАЯ АГРЕССИЯ) =====
    if mentioned_id and auto_mode:
        reply = generate_reply(text, mentioned_id)
        await update.message.reply_text(f"🎯 Цель: {mentioned_alias}\n\n{reply}")
        log_action(user_id, "auto_reply", mentioned_alias, reply)
        return

    if mentioned_id and not auto_mode:
        user_sessions[user_id] = {
            "step": "waiting_confirmation",
            "candidates": [mentioned_alias],
            "original_text": text,
            "created_at": datetime.now()
        }
        keyboard = [
            [
                InlineKeyboardButton("✅ Да", callback_data=f"confirm_{mentioned_alias}"),
                InlineKeyboardButton("❌ Нет", callback_data=f"deny_{mentioned_alias}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"🤔 Я нашёл цель: **{mentioned_alias}**.\n"
            f"Это правильный игрок? (Нажмите кнопку или напишите 'да' / 'нет, это <имя>')",
            reply_markup=reply_markup
        )
        log_action(user_id, "ask_confirmation", mentioned_alias)
        return

    if candidates and not auto_mode:
        candidate = candidates[0]
        user_sessions[user_id] = {
            "step": "waiting_confirmation",
            "candidates": [candidate],
            "original_text": text,
            "created_at": datetime.now()
        }
        keyboard = [
            [
                InlineKeyboardButton("✅ Да", callback_data=f"confirm_{candidate}"),
                InlineKeyboardButton("❌ Нет", callback_data=f"deny_{candidate}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"🤔 Я нашёл возможную цель: **{candidate}**.\n"
            f"Это правильный игрок? (Нажмите кнопку или напишите 'да' / 'нет, это <имя>')",
            reply_markup=reply_markup
        )
        log_action(user_id, "ask_confirmation", candidate)
        return

    if auto_mode and candidates:
        candidate = candidates[0]
        reply = generate_reply(text, candidate)
        await update.message.reply_text(f"🎯 Цель: {candidate}\n\n{reply}")
        log_action(user_id, "auto_reply", candidate, reply)
        return

    await update.message.reply_text("❌ Не удалось определить цель. Напишите /chat, чтобы отправить переписку частями, или укажите имя явно.")

def get_player_stats(player_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT total_attacks, weak_points, success_count FROM aggressors WHERE id=?", (player_id,))
    row = c.fetchone()
    conn.close()
    if row:
        total, weak, success = row
        return f"Всего атак: {total}, Слабые места: {weak}, Успешных ударов: {success}"
    return None

# ===== ГЕНЕРАЦИЯ ОТВЕТА (С АДАПТИВНЫМ СТИЛЕМ) =====
def generate_reply(attack_text: str, agg_id: str) -> str:
    key = get_cache_key(attack_text, agg_id)
    if key in CACHE:
        return CACHE[key]

    # ===== АДАПТИВНЫЙ СТИЛЬ =====
    style = select_style(attack_text, agg_id)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT history, weak_points, style_used, total_attacks FROM aggressors WHERE id=?", (agg_id,))
    row = c.fetchone()
    history = []
    weak = "неизвестно"
    total_attacks = 0
    if row:
        history = json.loads(row[0]) if row[0] else []
        weak = row[1] if row[1] else "неизвестно"
        total_attacks = row[3] if row[3] else 0

    attack_lower = attack_text.lower()
    if "мам" in attack_lower or "мать" in attack_lower:
        target = "семья"
    elif "играть" in attack_lower or "скилл" in attack_lower or "кда" in attack_lower:
        target = "скилл"
    elif "глуп" in attack_lower or "туп" in attack_lower or "ум" in attack_lower:
        target = "интеллект"
    elif "возраст" in attack_lower or "старый" in attack_lower or "молод" in attack_lower:
        target = "возраст"
    else:
        target = "общее"

    if weak == "неизвестно":
        weak = target
    elif target not in weak:
        weak += f",{target}"

    prompt = (
        f"Ты — бот для контр-атак в игровом чате. Противник написал: \"{attack_text}\". "
        f"Его слабые места: {weak}. Твой стиль: {style}. "
        f"Это его {total_attacks + 1}-я атака. "
        f"Твоя задача: ответить до 30 слов, используя его же логику и лексику, но перевернуть смысл так, "
        f"чтобы противник оправдывался или выглядел глупо. Не используй прямые оскорбления без причины. "
        f"Ответ:"
    )

    reply = call_hf_with_fallback(prompt)
    if not reply:
        return "⚠️ Не удалось сгенерировать ответ. Попробуйте позже."

    words = reply.split()
    if len(words) > 30:
        reply = " ".join(words[:30]) + "..."

    # Сохраняем историю
    history.append({"attack": attack_text, "reply": reply})
    if len(history) > 20:
        history = history[-20:]

    c.execute(
        "REPLACE INTO aggressors (id, history, weak_points, style_used, success_count, total_attacks) VALUES (?,?,?,?,?,?)",
        (agg_id, json.dumps(history), weak, style, 0, total_attacks + 1)
    )
    conn.commit()
    conn.close()

    CACHE[key] = reply
    save_cache()
    return reply

def select_style(attack_text: str, agg_id: str) -> str:
    """Выбирает стиль ответа на основе истории и контекста"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT total_attacks FROM aggressors WHERE id=?", (agg_id,))
    row = c.fetchone()
    total = row[0] if row else 0
    conn.close()

    length = len(attack_text)
    if total > 5:
        return "холодный"
    elif length < 30:
        return "зеркало"
    elif length > 60:
        return "логический"
    else:
        return "гибрид"

# ===== ЭКСПОРТ СТАТИСТИКИ =====
async def export_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, total_attacks, weak_points, success_count FROM aggressors")
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("ℹ️ Нет данных для экспорта.")
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Total Attacks", "Weak Points", "Success Count"])
    writer.writerows(rows)

    await update.message.reply_document(
        document=io.BytesIO(output.getvalue().encode()),
        filename="stats.csv",
        caption="📊 Статистика агрессоров"
    )

# ===== ОБРАБОТЧИК КНОПОК =====
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    if data.startswith("confirm_"):
        candidate = data.replace("confirm_", "")
        if user_id in user_sessions:
            original_text = user_sessions[user_id].get("original_text", "")
            reply = generate_reply(original_text, candidate)
            await query.edit_message_text(f"🎯 Цель подтверждена: {candidate}\n\n{reply}")
            log_action(user_id, "confirm_reply", candidate, reply)
            del user_sessions[user_id]
    elif data.startswith("deny_"):
        candidate = data.replace("deny_", "")
        await query.edit_message_text(
            f"❌ Отмена. Укажите имя вручную: напишите 'нет, это <имя>'"
        )

# ===== КОМАНДЫ =====
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("📝 Новая переписка", callback_data="action_chat"),
            InlineKeyboardButton("⚡ Авторежим", callback_data="action_auto")
        ],
        [
            InlineKeyboardButton("🗑 Стереть всё", callback_data="action_reset_all"),
            InlineKeyboardButton("📊 Статистика", callback_data="action_stats")
        ],
        [
            InlineKeyboardButton("📤 Экспорт CSV", callback_data="action_export"),
            InlineKeyboardButton("❓ Помощь", callback_data="action_help")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "👋 Я — бот для контр-атак в игровых чатах.\n\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )

async def chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_chat_collection[user_id] = []
    await update.message.reply_text("📝 Режим сбора переписки включён. Отправляйте сообщения по одному. Когда закончите — напишите /done.")

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_chat_collection or not user_chat_collection[user_id]:
        await update.message.reply_text("❌ Нет активного сбора переписки. Напишите /chat чтобы начать.")
        return
    full_text = "\n".join(user_chat_collection[user_id])
    del user_chat_collection[user_id]
    await process_text(update, context, full_text, None)

async def stats_alias_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT player_id, alias FROM aliases")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("ℹ️ Нет сохранённых имён.")
        return
    text = "📋 Сохранённые имена:\n" + "\n".join([f"- {alias} (ID: {pid})" for pid, alias in rows])
    await update.message.reply_text(text)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Укажите ID или имя: /stats <ID_игрока> или /stats <имя>")
        return
    query = context.args[0]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT player_id FROM aliases WHERE alias=?", (query,))
    row = c.fetchone()
    if row:
        agg_id = row[0]
    else:
        agg_id = query
    c.execute("SELECT history, weak_points, style_used, success_count, total_attacks FROM aggressors WHERE id=?", (agg_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        await update.message.reply_text(f"ℹ️ Нет данных об игроке {query}.")
        return
    history, weak, style, success, total = row
    history_list = json.loads(history) if history else []
    last_attacks = "\n".join([f"- {h['attack']} → {h['reply']}" for h in history_list[-3:]])
    text = (
        f"📊 Статистика для {query}:\n"
        f"Всего атак: {total}\n"
        f"Слабые места: {weak}\n"
        f"Стиль: {style}\n"
        f"Успешных ударов: {success}\n"
        f"Последние 3 атаки/ответа:\n{last_attacks if last_attacks else 'нет'}"
    )
    await update.message.reply_text(text)

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Укажите ID: /reset <ID_игрока>")
        return
    agg_id = context.args[0]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM aggressors WHERE id=?", (agg_id,))
    c.execute("DELETE FROM aliases WHERE player_id=?", (agg_id,))
    c.execute("DELETE FROM dialog_history WHERE aggressor_id=?", (agg_id,))
    conn.commit()
    conn.close()
    keys_to_del = [k for k in CACHE.keys() if k.endswith(f"_{agg_id}")]
    for k in keys_to_del:
        del CACHE[k]
    save_cache()
    await update.message.reply_text(f"✅ Память для игрока {agg_id} сброшена.")

async def reset_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM aggressors")
    c.execute("DELETE FROM aliases")
    c.execute("DELETE FROM dialog_history")
    conn.commit()
    conn.close()
    CACHE.clear()
    save_cache()
    await update.message.reply_text("🗑 Вся память бота очищена.")

# ===== ОБРАБОТЧИК ДЕЙСТВИЙ КНОПОК =====
async def handle_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    action = query.data

    if action == "action_chat":
        user_chat_collection[user_id] = []
        await query.edit_message_text("📝 Режим сбора переписки включён. Отправляйте сообщения по одному. Когда закончите — напишите /done.")
    elif action == "action_auto":
        context.user_data["auto_mode"] = True
        await query.edit_message_text("✅ Режим /auto включён. Теперь бот будет отвечать сразу без уточнения.")
    elif action == "action_reset_all":
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM aggressors")
        c.execute("DELETE FROM aliases")
        c.execute("DELETE FROM dialog_history")
        conn.commit()
        conn.close()
        CACHE.clear()
        save_cache()
        await query.edit_message_text("🗑 Вся память бота очищена.")
    elif action == "action_stats":
        await query.edit_message_text("📊 Чтобы посмотреть статистику, введите: /stats <ID или имя>")
    elif action == "action_export":
        await export_stats(update, context)
    elif action == "action_help":
        await query.edit_message_text(
            "👋 Я — бот для контр-атак.\n\n"
            "Команды:\n"
            "/chat — начать сбор переписки\n"
            "/auto — включить авторежим\n"
            "/test — тестовый режим (показать, что бы я ответил)\n"
            "/stats <ID или имя> — статистика\n"
            "/reset <ID> — сбросить игрока\n"
            "/reset_all — стереть всё\n"
            "/export_stats — экспорт статистики в CSV"
        )

# ===== ЗАПУСК =====
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("chat", chat_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("reset_all", reset_all_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("stats_aliases", stats_alias_command))
    app.add_handler(CommandHandler("export_stats", export_stats))
    app.add_handler(CommandHandler("test", lambda u, c: u.message.reply_text("🧪 Используйте /test <сообщение> для теста")))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^(confirm_|deny_)"))
    app.add_handler(CallbackQueryHandler(handle_action_callback, pattern=r"^action_"))

    logger.info("Бот запущен с полной функциональностью (HF + OpenRouter)...")
    app.run_polling()

if __name__ == "__main__":
    main()
