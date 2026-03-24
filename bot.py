# bot.py
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
    LabeledPrice, Message, PreCheckoutQuery, SuccessfulPayment
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
import os

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Цены в Telegram Stars
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
    return cur.fetchall()

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
        ("📸 Фотореализм", "style_photo"),
        ("🎨 Аниме", "style_anime"),
        ("🌃 Киберпанк", "style_cyber"),
        ("🍭 3D Candy", "style_candy"),
        ("✨ Без стиля", "style_none")
    ]
    for text, data in styles:
        builder.button(text=text, callback_data=data)
    builder.adjust(2)
    return builder.as_markup()

# ========================= ГЕНЕРАЦИЯ Flux.2 Pro =========================
async def generate_flux(prompt: str, style: str = "none") -> str | None:
    style_add = {
        "photo": ", photorealistic, ultra detailed, 8k, cinematic lighting, sharp focus",
        "anime": ", anime style, vibrant colors, detailed eyes, studio ghibli influence",
        "cyber": ", cyberpunk, neon lights, futuristic city, blade runner aesthetic",
        "candy": ", glossy candy-colored 3D, vibrant, oversaturated, 70s kodachrome film",
        "none": ""
    }

    full_prompt = prompt.strip() + style_add.get(style, "")

    url = "https://api.replicate.com/v1/predictions"
    headers = {
        "Authorization": f"Token {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "version": "black-forest-labs/flux-2-pro",   # актуальная версия на март 2026
        "input": {
            "prompt": full_prompt,
            "aspect_ratio": "1:1",
            "output_format": "webp",
            "output_quality": 90,
            "safety_tolerance": 2
        }
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 201:
                logging.error(f"Replicate error: {await resp.text()}")
                return None
            data = await resp.json()
            prediction_id = data["id"]

        # Поллинг до готовности (Flux.2 Pro обычно 8–25 секунд)
        for _ in range(40):
            await asyncio.sleep(4)
            async with session.get(f"{url}/{prediction_id}", headers=headers) as r:
                status = await r.json()
                if status["status"] == "succeeded":
                    return status["output"][0]   # прямая ссылка на изображение
                if status["status"] == "failed":
                    logging.error("Flux generation failed")
                    break

    return None

# ========================= БОТ =========================
session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()
router = Router()
dp.include_router(router)

current_style = {}  # временное хранение выбранного стиля для пользователя

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🌟 Привет! Я бот на **Flux.2 Pro** — одной из лучших моделей 2026 года!\n\n"
        "Отправь мне любой текстовый промт — и я сделаю тебе крутую картинку.\n"
        "Сначала выбери стиль, потом пиши описание.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💎 Купить подписку", callback_data="buy_sub")
        ]])
    )

@router.callback_query(F.data == "buy_sub")
async def buy_sub(callback: CallbackQuery):
    await callback.message.edit_text("💎 Выбери длительность подписки:", reply_markup=subscribe_keyboard())
    await callback.answer()

@router.callback_query(F.data.startswith("sub_"))
async def process_sub(callback: CallbackQuery):
    days = int(callback.data.split("_")[1])
    stars = PRICES[days]
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Подписка Flux.2 Pro — {days} дней",
        description=f"Неограниченная генерация изображений Flux.2 Pro на {days} дней",
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
    await message.answer(f"✅ Подписка Flux.2 Pro активирована на {days} дней! 🎉\nТеперь просто отправляй промты.")

# Выбор стиля
@router.callback_query(F.data.startswith("style_"))
async def choose_style(callback: CallbackQuery):
    style = callback.data
    current_style[callback.from_user.id] = style
    await callback.message.edit_text(f"✅ Стиль выбран: {callback.message.reply_markup.inline_keyboard[0][0].text if 'none' not in style else 'Без стиля'}\n\nТеперь отправь мне текстовое описание картинки!")
    await callback.answer()

# Основная генерация
@router.message(F.text)
async def handle_prompt(message: Message):
    user_id = message.from_user.id
    prompt = message.text.strip()

    if not has_active_subscription(user_id):
        await message.answer("❌ У тебя нет активной подписки.\nНажми /subscribe чтобы получить доступ к Flux.2 Pro.")
        return

    if get_daily_generations(user_id) >= DAILY_LIMIT:
        await message.answer("⏳ Сегодня лимит 50 генераций исчерпан.\nПриходи завтра или купи подписку подольше.")
        return

    style_code = current_style.get(user_id, "style_none").replace("style_", "")

    await message.answer("🌀 Генерирую на **Flux.2 Pro**... Это займёт 10–25 секунд.")

    image_url = await generate_flux(prompt, style_code)

    if image_url:
        await message.answer_photo(photo=image_url, caption=f"✨ Flux.2 Pro\nПромт: {prompt[:100]}...")
        save_to_history(user_id, prompt, image_url)
    else:
        await message.answer("⚠️ Не удалось сгенерировать изображение. Попробуй через минуту.")

# Остальные команды (status, history, admin) — оставил те же, что были раньше
@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    await message.answer("💎 Выбери подписку:", reply_markup=subscribe_keyboard())

@router.message(Command("status"))
async def cmd_status(message: Message):
    end = get_user(message.from_user.id)
    if end and end > datetime.now():
        days_left = (end - datetime.now()).days
        gens = get_daily_generations(message.from_user.id)
        await message.answer(f"✅ Подписка активна ещё {days_left} дней.\nСегодня: {gens}/{DAILY_LIMIT} генераций")
    else:
        await message.answer("❌ Подписка неактивна. /subscribe")

@router.message(Command("history"))
async def cmd_history(message: Message):
    rows = get_history(message.from_user.id)
    if not rows:
        await message.answer("История пуста.")
        return
    for prompt, url in rows:
        await message.answer_photo(photo=url, caption=prompt[:80])

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🛠 Админ-панель:\n/stats\n/broadcast [текст]\n/setprice [дни] [stars]")

# ... (stats, broadcast, setprice — такие же как в предыдущей версии)

async def main():
    init_db()
    print("🚀 Flux.2 Pro бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
