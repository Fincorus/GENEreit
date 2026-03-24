import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, Message, PreCheckoutQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
import os

# Настройка логирования
logging.basicConfig(level=logging.INFO)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Цены по умолчанию (можно менять через /setprice)
PRICES = {1: 30, 7: 150, 30: 500}

CONFIG_FILE = "config.json"
if Path(CONFIG_FILE).exists():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        PRICES = json.load(f)

DAILY_LIMIT = 50

# ========================= БАЗА ДАННЫХ =========================
DB_FILE = "bot.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        subscription_end TIMESTAMP
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        prompt TEXT,
        image_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    conn.close()

def get_user(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT subscription_end FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return datetime.fromisoformat(row[0]) if row and row[0] else None

def update_subscription(user_id: int, days: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    now = datetime.now()
    end = get_user(user_id) or now
    new_end = max(end, now) + timedelta(days=days)
    cur.execute("INSERT OR REPLACE INTO users (user_id, username, subscription_end) VALUES (?, ?, ?)",
                (user_id, None, new_end.isoformat()))
    conn.commit()
    conn.close()

def has_active_subscription(user_id: int) -> bool:
    end = get_user(user_id)
    return end is not None and end > datetime.now()

def get_daily_generations(user_id: int) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    today = datetime.now().date().isoformat()
    cur.execute("SELECT COUNT(*) FROM history WHERE user_id = ? AND date(created_at) = ?", (user_id, today))
    count = cur.fetchone()[0]
    conn.close()
    return count

def save_to_history(user_id: int, prompt: str, image_url: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO history (user_id, prompt, image_url) VALUES (?, ?, ?)", (user_id, prompt, image_url))
    conn.commit()
    conn.close()

def get_history(user_id: int, limit=5):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT prompt, image_url FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

# ========================= КНОПКИ =========================
def subscribe_keyboard():
    builder = InlineKeyboardBuilder()
    for days, stars in PRICES.items():
        builder.button(text=f"{days} дн. — {stars} Stars", callback_data=f"sub_{days}")
    builder.adjust(1)
    return builder.as_markup()

def style_keyboard():
    builder = InlineKeyboardBuilder()
    styles = [
        ("📸 Фотореализм", "photo"),
        ("🎨 Аниме", "anime"),
        ("🌃 Киберпанк", "cyber"),
        ("🍭 3D Candy", "candy"),
        ("✨ Без стиля", "none")
    ]
    for text, data in styles:
        builder.button(text=text, callback_data=f"style_{data}")
    builder.adjust(2)
    return builder.as_markup()

# ========================= ГЕНЕРАЦИЯ Flux.2 Pro =========================
async def generate_flux(prompt: str, style: str = "none") -> str | None:
    style_prompts = {
        "photo": ", photorealistic, ultra detailed, 8k, cinematic lighting, sharp focus",
        "anime": ", anime style, vibrant colors, detailed eyes, studio ghibli influence",
        "cyber": ", cyberpunk, neon lights, futuristic city, blade runner aesthetic",
        "candy": ", glossy candy-colored 3D, vibrant, oversaturated, 70s kodachrome film",
        "none": ""
    }
    
    full_prompt = prompt.strip() + style_prompts.get(style, "")
    
    url = "https://api.replicate.com/v1/predictions"
    headers = {
        "Authorization": f"Token {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json"
    }
    
    # Используем актуальную версию Flux Schnell (быстрее и стабильнее)
    payload = {
        "version": "black-forest-labs/flux-schnell",
        "input": {
            "prompt": full_prompt,
            "go_fast": True,
            "aspect_ratio": "1:1",
            "output_format": "webp",
            "safety_tolerance": 2
        }
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 201:
                text = await resp.text()
                logging.error(f"Replicate error {resp.status}: {text}")
                return None
            data = await resp.json()
            prediction_id = data["id"]
        
        # Поллинг
        for _ in range(30):
            await asyncio.sleep(2)
            async with session.get(f"{url}/{prediction_id}", headers=headers) as r:
                if r.status != 200:
                    continue
                status = await r.json()
                if status["status"] == "succeeded":
                    # Flux возвращает список ссылок
                    output = status.get("output")
                    if output and isinstance(output, list) and len(output) > 0:
                        return output[0]
                    return None
                if status["status"] == "failed":
                    logging.error("Flux generation failed")
                    return None
    return None

# ========================= БОТ =========================
session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Хранилище выбранного стиля
user_style = {}

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🌟 Привет! Я бот на **Flux Schnell** — быстрая генерация изображений.\n\n"
        "1️⃣ Сначала выбери стиль кнопкой ниже\n"
        "2️⃣ Потом отправь текстовое описание\n\n"
        "💎 Чтобы купить подписку — /subscribe",
        reply_markup=style_keyboard()
    )

@router.callback_query(F.data.startswith("style_"))
async def choose_style(callback: CallbackQuery):
    style = callback.data.split("_")[1]
    user_style[callback.from_user.id] = style
    
    style_names = {
        "photo": "📸 Фотореализм",
        "anime": "🎨 Аниме",
        "cyber": "🌃 Киберпанк",
        "candy": "🍭 3D Candy",
        "none": "✨ Без стиля"
    }
    
    await callback.message.answer(
        f"✅ Стиль выбран: {style_names.get(style, 'Без стиля')}\n\n"
        "Теперь отправь мне текстовое описание картинки!"
    )
    await callback.answer()

@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    await message.answer("💎 Выбери длительность подписки:", reply_markup=subscribe_keyboard())

@router.callback_query(F.data.startswith("sub_"))
async def process_sub(callback: CallbackQuery):
    days = int(callback.data.split("_")[1])
    stars = PRICES.get(days, 30)
    
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Подписка Flux — {days} дней",
        description=f"Неограниченная генерация на {days} дней",
        payload=f"sub_{days}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"{days} дней", amount=stars)]
    )
    await callback.answer()

@router.pre_checkout_query()
async def pre_checkout(pre: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre.id, ok=True)

@router.message(F.successful_payment)
async def successful_payment(message: Message):
    days = int(message.successful_payment.invoice_payload.split("_")[1])
    update_subscription(message.from_user.id, days)
    await message.answer(f"✅ Подписка активирована на {days} дней!\nТеперь отправляй промты.")

@router.message(F.text)
async def handle_prompt(message: Message):
    user_id = message.from_user.id
    prompt = message.text.strip()
    
    # Проверка подписки
    if not has_active_subscription(user_id):
        await message.answer(
            "❌ У тебя нет активной подписки.\n"
            "Купить: /subscribe\n"
            "Проверить статус: /status"
        )
        return
    
    # Проверка дневного лимита
    daily_count = get_daily_generations(user_id)
    if daily_count >= DAILY_LIMIT:
        await message.answer(f"⏳ Сегодняшний лимит ({DAILY_LIMIT}) исчерпан.\nПриходи завтра!")
        return
    
    # Получаем стиль пользователя (по умолчанию "none")
    style = user_style.get(user_id, "none")
    
    await message.answer("🎨 Генерирую изображение... (10–20 секунд)")
    
    image_url = await generate_flux(prompt, style)
    
    if image_url:
        await message.answer_photo(
            photo=image_url,
            caption=f"✨ Готово!\nСтиль: {style}\nПромт: {prompt[:80]}..."
        )
        save_to_history(user_id, prompt, image_url)
    else:
        await message.answer("⚠️ Ошибка генерации. Попробуй другой промт или повтори позже.")

@router.message(Command("status"))
async def cmd_status(message: Message):
    user_id = message.from_user.id
    end = get_user(user_id)
    if end and end > datetime.now():
        days_left = (end - datetime.now()).days
        gens_today = get_daily_generations(user_id)
        style = user_style.get(user_id, "не выбран")
        await message.answer(
            f"📊 **Статус подписки**\n"
            f"✅ Активна до: {end.strftime('%d.%m.%Y')}\n"
            f"📅 Осталось дней: {days_left}\n"
            f"🎨 Текущий стиль: {style}\n"
            f"🖼 Сегодня: {gens_today}/{DAILY_LIMIT} генераций"
        )
    else:
        await message.answer("❌ Подписка неактивна. Купить: /subscribe")

@router.message(Command("history"))
async def cmd_history(message: Message):
    rows = get_history(message.from_user.id, limit=3)
    if not rows:
        await message.answer("История пуста. Сгенерируй первую картинку!")
        return
    await message.answer("🖼 Последние 3 генерации:")
    for prompt, url in rows:
        await message.answer_photo(photo=url, caption=prompt[:100])

# ==================== АДМИН-КОМАНДЫ ====================
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "🛠 Админ-панель:\n"
        "/stats — статистика\n"
        "/broadcast [текст] — рассылка\n"
        "/setprice [дни] [stars] — изменить цену\n"
        "/setlimit [число] — дневной лимит"
    )

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE subscription_end > datetime('now')")
    active_subs = cur.fetchone()[0]
    conn.close()
    await message.answer(f"📊 Статистика:\n👥 Всего пользователей: {total_users}\n💎 Активных подписок: {active_subs}")

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.answer("Укажи текст для рассылки после команды.")
        return
    
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    users = cur.fetchall()
    conn.close()
    
    success = 0
    for (user_id,) in users:
        try:
            await bot.send_message(user_id, f"📢 Рассылка:\n\n{text}")
            success += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await message.answer(f"Рассылка отправлена {success} пользователям.")

@router.message(Command("setprice"))
async def cmd_setprice(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Формат: /setprice [дни] [stars]")
        return
    days = int(parts[1])
    stars = int(parts[2])
    PRICES[days] = stars
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(PRICES, f)
    await message.answer(f"✅ Цена на {days} дней установлена: {stars} Stars")

# ==================== ЗАПУСК ====================
async def main():
    init_db()
    logging.info("🚀 Бот Flux Schnell запущен!")
    
    # Для Render: веб-сервер не нужен, просто long polling
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
