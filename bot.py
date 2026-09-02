# -*- coding: utf-8 -*-
import os
import json
import sqlite3
import hashlib
import pickle
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ===== КОНФИГУРАЦИЯ (из переменных окружения Railway) =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
HF_TOKEN = os.getenv("HF_TOKEN")
HF_MODEL = os.getenv("HF_MODEL", "mistralai/Mistral-7B-Instruct-v0.3")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # например, https://ваш-проект.railway.app

if not TELEGRAM_TOKEN or not HF_TOKEN or not WEBHOOK_URL:
    raise ValueError("Не заданы TELEGRAM_TOKEN, HF_TOKEN или WEBHOOK_URL")

# ===== БАЗА ДАННЫХ (SQLite) =====
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
    conn.commit()
    conn.close()

# ===== КЭШ (pickle) =====
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

# ===== ВЫЗОВ HUGGINGFACE (бесплатный API) =====
def call_hf(prompt):
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
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and "generated_text" in data[0]:
                full = data[0]["generated_text"]
                return full[len(prompt):].strip()
            else:
                return None
        else:
            return None
    except:
        return None

# ===== ЗАПАСНЫЕ ФРАЗЫ (если HF не отвечает) =====
FALLBACKS = [
    "Твоя логика хромает на обе ноги. Попробуй ещё раз, но с умом.",
    "Оскорбление уровня 'мама' — классика для тех, у кого фантазия кончилась в 5 лет.",
    "Я бы ответил, но ты не поймёшь — слишком сложно для твоего словарного запаса.",
    "Твой аргумент — как твой скилл: нулевой. Иди тренируйся.",
    "Зеркало: ты сам только что описал себя. Спасибо за признание.",
    "Ой, смотри, кто заговорил про интеллект... Тишина в эфире.",
    "Продолжай — я собираю статистику твоих поражений.",
    "Даже бот понимает, что ты не прав. А ты — нет.",
]

def get_fallback(attack_text):
    import random
    idx = hashlib.md5(attack_text.encode()).hexdigest()
    return FALLBACKS[int(idx, 16) % len(FALLBACKS)]

# ===== ГЛАВНАЯ ЛОГИКА ГЕНЕРАЦИИ =====
def generate_reply(attack_text: str, agg_id: str) -> str:
    # 1. Кэш
    key = get_cache_key(attack_text, agg_id)
    if key in CACHE:
        return CACHE[key]

    # 2. Загружаем историю агрессора
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

    # 3. Определяем тип атаки и слабость (упрощённо)
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

    # Обновляем слабости (если новое)
    if weak == "неизвестно":
        weak = target
    elif target not in weak:
        weak += f",{target}"

    # 4. Выбор стиля (автоматически или из истории)
    mat_count = sum(1 for w in attack_text.split() if w.lower() in ["мам", "дурак", "лох", "идиот", "бал", "гандон"])
    if style == "auto":
        if mat_count >= 2:
            style = "зеркало+язвительный"
        elif len(attack_text) > 60:
            style = "холодный"
        else:
            style = "гибрид"

    # 5. Формируем промпт
    prompt = (
        f"Ты — бот для контр-атак в игровом чате. Противник написал: \"{attack_text}\". "
        f"Его слабые места: {weak}. Твой стиль: {style}. "
        f"Твоя задача: ответить до 30 слов, используя его же логику и лексику, но перевернуть смысл так, "
        f"чтобы противник оправдывался или выглядел глупо. Не используй прямые оскорбления без причины. "
        f"Ответ:"
    )

    # 6. Зовём HF или берём fallback
    reply = call_hf(prompt)
    if not reply:
        reply = get_fallback(attack_text)

    # 7. Обрезаем до 30 слов (приблизительно)
    words = reply.split()
    if len(words) > 30:
        reply = " ".join(words[:30]) + "..."

    # 8. Сохраняем историю
    history.append({"attack": attack_text, "reply": reply})
    if len(history) > 5:
        history = history[-5:]
    c.execute(
        "REPLACE INTO aggressors (id, history, weak_points, style_used, success_count) VALUES (?,?,?,?,?)",
        (agg_id, json.dumps(history), weak, style, 0)
    )
    conn.commit()
    conn.close()

    # 9. Пишем в кэш
    CACHE[key] = reply
    save_cache()
    return reply

# ===== TELEGRAM ОБРАБОТЧИКИ =====
async def answer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /answer <ID> <текст атаки>"""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "❌ Формат: /answer <ID_игрока> <текст атаки>\n"
            "Пример: /answer 12345 Я твою маму бал"
        )
        return
    agg_id = context.args[0]
    attack_text = " ".join(context.args[1:])
    if len(attack_text) == 0:
        await update.message.reply_text("❌ Текст атаки не может быть пустым.")
        return

    reply = generate_reply(attack_text, agg_id)
    await update.message.reply_text(reply)

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сброс памяти для конкретного ID: /reset <ID>"""
    if not context.args:
        await update.message.reply_text("❌ Укажите ID: /reset <ID_игрока>")
        return
    agg_id = context.args[0]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM aggressors WHERE id=?", (agg_id,))
    conn.commit()
    conn.close()
    # удаляем кэш по этому ID
    keys_to_del = [k for k in CACHE.keys() if k.endswith(f"_{agg_id}")]
    for k in keys_to_del:
        del CACHE[k]
    save_cache()
    await update.message.reply_text(f"✅ Память для игрока {agg_id} сброшена.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать статистику по ID: /stats <ID>"""
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

# ===== ЗАПУСК (ИСПРАВЛЕННАЯ ВЕРСИЯ) =====
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("answer", answer_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", 8080)),
        webhook_url=WEBHOOK_URL + "/webhook"
    )
