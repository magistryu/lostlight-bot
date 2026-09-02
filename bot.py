# -*- coding: utf-8 -*-
import os
import json
import sqlite3
import hashlib
import pickle
import re
import time
import logging
import requests
from datetime import datetime, timedelta
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
            success_count INTEGER DEFAULT 0
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
    """Добавляет запрос в очередь и обрабатывает её с задержкой"""
    global last_request_time
    request_queue.append(prompt)
    # Если очередь слишком большая — сообщаем об этом
    if len(request_queue) > 50:
        return "⚠️ Слишком много запросов. Попробуйте позже."

    # Если время с последнего запроса меньше 2 секунд — ждём
    current_time = time.time()
    if current_time - last_request_time < 2:
        time.sleep(2 - (current_time - last_request_time))

    last_request_time = time.time()
    return None

# ===== ВЫЗОВ HF (с несколькими моделями) =====
def call_hf_with_fallback(prompt):
    """Пробует все модели из списка HF_MODELS, если не получается — пробует OpenRouter, потом Fallback"""
    
    # Проверяем очередь
    queue_status = queue_request(prompt)
    if queue_status:
        return queue_status

    # 1. Пробуем HF модели
    for model in HF_MODELS:
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
                continue
        except Exception as e:
            logger.warning(f"HF model {model} failed: {e}")
            continue

    # 2. Если HF не ответил — пробуем OpenRouter
    if OPENROUTER_KEY:
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
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if "choices" in data and len(data["choices"]) > 0:
                    return data["choices"][0]["message"]["content"].strip()
            else:
                logger.warning(f"OpenRouter returned {resp.status_code}")
        except Exception as e:
            logger.warning(f"OpenRouter failed: {e}")

    # 3. Если ничего не сработало — возвращаем None
    return None

# ===== ЗАПАСНЫЕ ФРАЗЫ =====
FALLBACKS = [
    "Твоя логика хромает на обе ноги. Попробуй ещё раз, но с умом.",
    "Оскорбление уровня 'мама' — классика для тех, у кого фантазия кончилась в 5 лет.",
    "Я бы ответил, но ты не поймёшь — слишком сложно для твоего словарного запаса.",
    "Твой аргумент — как твой скилл: нулевой. Иди тренируйся.",
    "Зеркало: ты сам только что описал себя. Спасибо за признание.",
    "Ой, смотри, кто заговорил про интеллект... Тишина в эфире.",
    "Продолжай — я собираю статистику твоих поражений.",
    "Даже бот понимает, что ты не прав. А ты — нет.",
    "Твои слова — как твой KDA: низкие и бесполезные.",
    "Ты бы лучше в игре что-то сделал, чем тут слова тратил.",
    "Кто-то забыл выключить CapsLock и заодно мозг.",
]

def get_fallback(attack_text):
    idx = hashlib.md5(attack_text.encode()).hexdigest()
    return FALLBACKS[int(idx, 16) % len(FALLBACKS)]

# ===== ЛОГИКА ГЕНЕРАЦИИ =====
def generate_reply(attack_text: str, agg_id: str) -> str:
    key = get_cache_key(attack_text, agg_id)
    if key in CACHE:
        return CACHE[key]

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT history, weak_points, style_used FROM aggressors WHERE id=?", (agg_id,))
    row = c.fetchone()
    history = []
    weak = "неизвестно"
    style = "auto"
    if row:
        history = json.loads(row[0]) if row[0] else []
        weak = row[1] if row[1] else "неизвестно"
        style = row[2] if row[2] else "auto"

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

    mat_count = sum(1 for w in attack_text.split() if w.lower() in ["мам", "дурак", "лох", "идиот", "тупой", "гандон"])
    if style == "auto":
        if mat_count >= 2:
            style = "зеркало+язвительный"
        elif len(attack_text) > 60:
            style = "холодный"
        else:
            style = "гибрид"

    prompt = (
        f"Ты — бот для контр-атак в игровом чате. Противник написал: \"{attack_text}\". "
        f"Его слабые места: {weak}. Твой стиль: {style}. "
        f"Твоя задача: ответить до 30 слов, используя его же логику и лексику, но перевернуть смысл так, "
        f"чтобы противник оправдывался или выглядел глупо. Не используй прямые оскорбления без причины. "
        f"Ответ:"
    )

    reply = call_hf_with_fallback(prompt)
    if not reply:
        reply = get_fallback(attack_text)

    words = reply.split()
    if len(words) > 30:
        reply = " ".join(words[:30]) + "..."

    history.append({"attack": attack_text, "reply": reply})
    if len(history) > 5:
        history = history[-5:]
    c.execute(
        "REPLACE INTO aggressors (id, history, weak_points, style_used, success_count) VALUES (?,?,?,?,?)",
        (agg_id, json.dumps(history), weak, style, 0)
    )
    conn.commit()
    conn.close()

    CACHE[key] = reply
    save_cache()
    return reply

# ===== ИЗВЛЕЧЕНИЕ ИМЁН =====
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
            return match.group(1)
    return None

def find_mentioned_player(text):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT player_id, alias FROM aliases")
    rows = c.fetchall()
    conn.close()
    for player_id, alias in rows:
        if alias.lower() in text.lower():
            return player_id, alias
    return None, None

# ===== ХРАНЕНИЕ СОСТОЯНИЙ =====
user_sessions = {}
user_chat_collection = {}

# ===== ОБРАБОТЧИК СООБЩЕНИЙ (ИСПРАВЛЕННЫЙ) =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message

    if not message:
        return

    # === ОБРАБОТКА ПЕРЕСЛАННЫХ СООБЩЕНИЙ ===
    text = None
    if message.text:
        text = message.text
    elif message.forward_from or message.forward_from_chat:
        # Если переслано — берём текст из оригинального сообщения
        if message.forward_from_message_id:
            # Пытаемся получить текст из пересланного
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

    # === ОЧИСТКА СЕССИЙ (таймаут 10 минут) ===
    if user_id in user_sessions:
        if datetime.now() - user_sessions[user_id].get("created_at", datetime.now()) > timedelta(minutes=10):
            del user_sessions[user_id]
            await update.message.reply_text("⏰ Сессия устарела. Начните заново.")
            return

    # === РЕЖИМ СБОРА ПЕРЕПИСКИ ===
    if user_id in user_chat_collection:
        if text.lower() == "/done":
            full_text = "\n".join(user_chat_collection[user_id])
            del user_chat_collection[user_id]
            await process_text(update, full_text)
            return
        else:
            user_chat_collection[user_id].append(text)
            await update.message.reply_text(f"📝 Сообщение добавлено. Всего: {len(user_chat_collection[user_id])}. Напишите /done, когда закончите.")
            return

    # === ОБРАБОТКА КОМАНДЫ /done (если не в режиме сбора) ===
    if text.lower() == "/done":
        await update.message.reply_text("❌ Вы не в режиме сбора переписки. Напишите /chat чтобы начать.")
        return

    # === РЕЖИМ /auto (без уточнения) ===
    if text.lower().startswith("/auto"):
        context.user_data["auto_mode"] = True
        await update.message.reply_text("✅ Режим /auto включён. Теперь бот будет отвечать сразу без уточнения.")
        return

    # === ОБЫЧНЫЙ АНАЛИЗ ===
    await process_text(update, text)

async def process_text(update: Update, text: str):
    user_id = update.effective_user.id
    auto_mode = update.effective_user.id in context.user_data and context.user_data.get("auto_mode", False)

    alias = extract_aliases(text)
    if alias:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO aliases (player_id, alias) VALUES (?, ?)", (str(update.effective_user.id), alias))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"👤 Запомнил: {alias} (это вы)")
        log_action(user_id, "self_identify", alias)

    mentioned_id, mentioned_alias = find_mentioned_player(text)
    if mentioned_id:
        reply = generate_reply(text, mentioned_id)
        await update.message.reply_text(f"🎯 Цель: {mentioned_alias}\n\n{reply}")
        log_action(user_id, "auto_reply", mentioned_alias, reply)
        return

    potential_names = re.findall(r'\b([A-Za-zА-ЯЁа-яё0-9_\-]+)\b', text)
    if potential_names and not auto_mode:
        candidate = potential_names[0]
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
    elif auto_mode and potential_names:
        candidate = potential_names[0]
        reply = generate_reply(text, candidate)
        await update.message.reply_text(f"🎯 Цель: {candidate}\n\n{reply}")
        log_action(user_id, "auto_reply", candidate, reply)
    else:
        await update.message.reply_text("❌ Не удалось определить цель. Напишите /chat, чтобы отправить переписку частями, или укажите имя явно.")

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
    c.execute("SELECT history, weak_points, style_used, success_count FROM aggressors WHERE id=?", (agg_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        await update.message.reply_text(f"ℹ️ Нет данных об игроке {query}.")
        return
    history, weak, style, success = row
    history_list = json.loads(history) if history else []
    last_attacks = "\n".join([f"- {h['attack']} → {h['reply']}" for h in history_list[-3:]])
    text = (
        f"📊 Статистика для {query}:\n"
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
    conn.commit()
    conn.close()
    keys_to_del = [k for k in CACHE.keys() if k.endswith(f"_{agg_id}")]
    for k in keys_to_del:
        del CACHE[k]
    save_cache()
    await update.message.reply_text(f"✅ Память для игрока {agg_id} сброшена.")

async def reset_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Полная очистка всей памяти бота"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM aggressors")
    c.execute("DELETE FROM aliases")
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
        conn.commit()
        conn.close()
        CACHE.clear()
        save_cache()
        await query.edit_message_text("🗑 Вся память бота очищена.")
    elif action == "action_stats":
        await query.edit_message_text("📊 Чтобы посмотреть статистику, введите: /stats <ID или имя>")
    elif action == "action_help":
        await query.edit_message_text(
            "👋 Я — бот для контр-атак.\n\n"
            "Команды:\n"
            "/chat — начать сбор переписки\n"
            "/auto — включить авторежим\n"
            "/stats <ID или имя> — статистика\n"
            "/reset <ID> — сбросить игрока\n"
            "/reset_all — стереть всё"
        )

# ===== ЗАПУСК =====
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("chat", chat_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("reset_all", reset_all_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("stats_aliases", stats_alias_command))

    # Обработчики сообщений и кнопок
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^(confirm_|deny_)"))
    app.add_handler(CallbackQueryHandler(handle_action_callback, pattern=r"^action_"))

    logger.info("Бот запущен с полной функциональностью...")
    app.run_polling()

if __name__ == "__main__":
    main()
