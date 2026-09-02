# -*- coding: utf-8 -*-
import os
import json
import sqlite3
import hashlib
import pickle
import re
import time
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler

# ===== КОНФИГУРАЦИЯ =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
HF_TOKEN = os.getenv("HF_TOKEN")
HF_MODEL = os.getenv("HF_MODEL", "mistralai/Mistral-7B-Instruct-v0.3")

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
    conn.commit()
    conn.close()

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

# ===== ВЫЗОВ HF =====
def call_hf(prompt):
    if not HF_TOKEN:
        return None
    url = f"https://api-inference.huggingface.co/models/{HF_MODEL}"
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    payload = {
        "inputs": prompt,
        "parameters": {
            "max_new_tokens": 50,
            "temperature": 0.85,
            "do_sample": True
        }
    }
    try:
        time.sleep(1)
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and "generated_text" in data[0]:
                full = data[0]["generated_text"]
                return full[len(prompt):].strip()
        return None
    except:
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

    reply = call_hf(prompt)
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

# ===== ИЗВЛЕЧЕНИЕ ИМЁН ИЗ ТЕКСТА =====
def extract_aliases(text):
    """Ищет в тексте фразы 'я — X', 'меня зовут X', 'это X' и т.п."""
    patterns = [
        r'я\s+[\-–]\s*(\w+)',
        r'меня\s+зовут\s+(\w+)',
        r'это\s+(\w+)',
        r'я\s+(\w+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None

def find_mentioned_player(text):
    """Ищет в тексте упоминание имени (алиаса) из базы."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT player_id, alias FROM aliases")
    rows = c.fetchall()
    conn.close()
    for player_id, alias in rows:
        if alias.lower() in text.lower():
            return player_id, alias
    return None, None

# ===== ХРАНЕНИЕ СОСТОЯНИЙ (FINAL STATE MACHINE) =====
user_sessions = {}  # user_id -> { "step": "waiting_confirmation", "candidates": [...], "original_text": "..." }

# ===== ОБРАБОТЧИК СООБЩЕНИЙ =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главный обработчик: анализирует текст и уточняет цель"""
    user_id = update.effective_user.id
    text = update.message.text

    if not text:
        return

    # === 1. ПРОВЕРКА: БОТ ЖДЁТ ПОДТВЕРЖДЕНИЯ ===
    if user_id in user_sessions and user_sessions[user_id].get("step") == "waiting_confirmation":
        # Пользователь отвечает на уточнение
        if text.lower() in ["да", "yes", "+", "конечно", "ага"]:
            # Подтверждаем цель
            target = user_sessions[user_id].get("candidates", [None])[0]
            if target:
                # Генерируем ответ
                original_text = user_sessions[user_id].get("original_text", "")
                reply = generate_reply(original_text, target)
                await update.message.reply_text(f"🎯 Цель: {target}\n\n{reply}")
            else:
                await update.message.reply_text("❌ Не удалось определить цель. Отправьте переписку заново.")
            del user_sessions[user_id]
            return

        elif text.lower().startswith("нет"):
            # Пользователь говорит, что цель не та
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                new_target = parts[1].strip()
                if new_target:
                    # Сохраняем новый алиас
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute("INSERT OR IGNORE INTO aliases (player_id, alias) VALUES (?, ?)", (new_target, new_target))
                    conn.commit()
                    conn.close()
                    # Генерируем ответ для новой цели
                    original_text = user_sessions[user_id].get("original_text", "")
                    reply = generate_reply(original_text, new_target)
                    await update.message.reply_text(f"🎯 Цель изменена на: {new_target}\n\n{reply}")
                else:
                    await update.message.reply_text("❌ Укажите имя после 'нет'. Например: 'нет, это Баграт'")
            else:
                await update.message.reply_text("❌ Укажите имя после 'нет'. Например: 'нет, это Баграт'")
            del user_sessions[user_id]
            return

    # === 2. ОБЫЧНЫЙ АНАЛИЗ: ИЩЕМ ЦЕЛЬ ===
    # Проверяем, есть ли в тексте фраза "я — X" (самоидентификация)
    alias = extract_aliases(text)
    if alias:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO aliases (player_id, alias) VALUES (?, ?)", (str(update.effective_user.id), alias))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"👤 Запомнил: {alias} (это вы)")

    # Проверяем, есть ли упоминание кого-то из базы
    mentioned_id, mentioned_alias = find_mentioned_player(text)
    if mentioned_id:
        # Нашли цель
        reply = generate_reply(text, mentioned_id)
        await update.message.reply_text(f"🎯 Цель: {mentioned_alias}\n\n{reply}")
        return

    # === 3. НЕ НАШЛИ ЦЕЛЬ — УТОЧНЯЕМ ===
    # Ищем все возможные имена в тексте (по шаблонам)
    potential_names = re.findall(r'\b([А-ЯЁ][а-яё]+)\b', text)
    if potential_names:
        # Берём первое имя как кандидата
        candidate = potential_names[0]
        # Сохраняем состояние
        user_sessions[user_id] = {
            "step": "waiting_confirmation",
            "candidates": [candidate],
            "original_text": text
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
    else:
        await update.message.reply_text("❌ Не удалось определить цель в переписке. Укажите имя явно (например, 'я — Баграт' или 'Баграт, ты слабый').")

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
            del user_sessions[user_id]
    elif data.startswith("deny_"):
        candidate = data.replace("deny_", "")
        await query.edit_message_text(
            f"❌ Отмена. Укажите имя вручную: напишите 'нет, это <имя>'"
        )
        # Не удаляем сессию — ждём ручного ввода

# ===== КОМАНДЫ =====
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Я — бот для контр-атак в игровых чатах.\n\n"
        "Просто отправь мне переписку (текст), и я:\n"
        "1. Определю цель (игрока, которого нужно 'чморить').\n"
        "2. Уточню, правильно ли я определил.\n"
        "3. Сгенерирую ответ, который уничтожит его же логикой.\n\n"
        "Пример:\n"
        "«Баграт сказал мне, что я нуб, а я ему ответил, что он сам лох»"
    )

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

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Укажите ID: /stats <ID_игрока>")
        return
    agg_id = context.args[0]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT history, weak_points, style_used, success_count FROM aggressors WHERE id=?", (agg_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        await update.message.reply_text(f"ℹ️ Нет данных об игроке {agg_id}.")
        return
    history, weak, style, success = row
    history_list = json.loads(history) if history else []
    last_attacks = "\n".join([f"- {h['attack']} → {h['reply']}" for h in history_list[-3:]])
    text = (
        f"📊 Статистика для ID {agg_id}:\n"
        f"Слабые места: {weak}\n"
        f"Стиль: {style}\n"
        f"Успешных ударов: {success}\n"
        f"Последние 3 атаки/ответа:\n{last_attacks if last_attacks else 'нет'}"
    )
    await update.message.reply_text(text)

# ===== ЗАПУСК =====
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("stats", stats_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    print("Бот запущен с интерактивным уточнением цели...")
    app.run_polling()

if __name__ == "__main__":
    main()
