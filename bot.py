import asyncio
import json
import logging
import sqlite3
import uuid
import re
from datetime import datetime, timedelta
from pathlib import Path
from io import BytesIO

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

# ==================== НАСТРОЙКИ ====================
logging.basicConfig(level=logging.INFO)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# GigaChat credentials
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")

# Кеш для токена
_gigachat_token_cache = {
    "token": None,
    "expires_at": None
}

PRICES = {1: 30, 7: 150, 30: 500}

CONFIG_FILE = "config.json"
if Path(CONFIG_FILE).exists():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        PRICES = json.load(f)

DAILY_LIMIT = 50
FREE_GENERATIONS = 5

DB_FILE = "bot.db"

# ==================== БАЗА ДАННЫХ ====================
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
    
    cur.execute("""CREATE TABLE IF NOT EXISTS free_generations (
        user_id INTEGER PRIMARY KEY,
        remaining INTEGER DEFAULT 5
    )""")
    
    cur.execute("""CREATE TABLE IF NOT EXISTS user_activity (
        user_id INTEGER PRIMARY KEY,
        last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_reminder_sent TIMESTAMP,
        reminder_type TEXT
    )""")
    
    conn.commit()
    conn.close()

def update_activity(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO user_activity (user_id, last_active, last_reminder_sent, reminder_type)
        VALUES (?, CURRENT_TIMESTAMP, ?, ?)
    """, (user_id, None, None))
    conn.commit()
    conn.close()

def get_inactive_users(days: int = 7) -> list:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    threshold = (datetime.now() - timedelta(days=days)).isoformat()
    cur.execute("""
        SELECT ua.user_id 
        FROM user_activity ua
        WHERE ua.last_active < ?
        AND (ua.last_reminder_sent IS NULL OR ua.last_reminder_sent < ?)
    """, (threshold, (datetime.now() - timedelta(days=1)).isoformat()))
    users = [row[0] for row in cur.fetchall()]
    conn.close()
    return users

def get_users_with_expiring_subscription(days_left: int = 1) -> list:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    now = datetime.now()
    end_threshold = now + timedelta(days=days_left)
    cur.execute("""
        SELECT user_id 
        FROM users 
        WHERE subscription_end > ? AND subscription_end <= ?
    """, (now.isoformat(), end_threshold.isoformat()))
    users = [row[0] for row in cur.fetchall()]
    conn.close()
    return users

def get_users_without_subscription_and_free() -> list:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    now = datetime.now()
    cur.execute("""
        SELECT u.user_id 
        FROM users u
        LEFT JOIN free_generations f ON u.user_id = f.user_id
        WHERE (u.subscription_end IS NULL OR u.subscription_end < ?)
        AND (f.remaining IS NULL OR f.remaining = 0)
        AND (SELECT last_reminder_sent FROM user_activity WHERE user_id = u.user_id) IS NULL
    """, (now.isoformat(),))
    users = [row[0] for row in cur.fetchall()]
    conn.close()
    return users

def mark_reminder_sent(user_id: int, reminder_type: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        UPDATE user_activity 
        SET last_reminder_sent = CURRENT_TIMESTAMP, reminder_type = ?
        WHERE user_id = ?
    """, (reminder_type, user_id))
    conn.commit()
    conn.close()

def get_free_generations(user_id: int) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT remaining FROM free_generations WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else FREE_GENERATIONS

def use_free_generation(user_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT remaining FROM free_generations WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    
    if row:
        remaining = row[0]
        if remaining > 0:
            cur.execute("UPDATE free_generations SET remaining = ? WHERE user_id = ?", (remaining - 1, user_id))
            conn.commit()
            conn.close()
            return remaining - 1 > 0
        else:
            conn.close()
            return False
    else:
        cur.execute("INSERT INTO free_generations (user_id, remaining) VALUES (?, ?)", (user_id, FREE_GENERATIONS - 1))
        conn.commit()
        conn.close()
        return FREE_GENERATIONS - 1 > 0

def reset_free_generations(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO free_generations (user_id, remaining) VALUES (?, ?)", (user_id, FREE_GENERATIONS))
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

def main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🎨 Выбрать стиль", callback_data="show_styles")
    builder.button(text="💎 Купить подписку", callback_data="buy_sub")
    builder.button(text="📊 Статус", callback_data="show_status")
    builder.button(text="🎁 Бесплатные", callback_data="show_free")
    builder.adjust(2)
    return builder.as_markup()

# ========================= GIGACHAT API =========================
async def get_gigachat_token() -> str | None:
    """Получает access token для GigaChat API с кешированием на 25 минут"""
    global _gigachat_token_cache
    
    now = datetime.now()
    
    if (_gigachat_token_cache["token"] and 
        _gigachat_token_cache["expires_at"] and 
        now < _gigachat_token_cache["expires_at"]):
        return _gigachat_token_cache["token"]
    
    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    
    auth_header = f"Basic {GIGACHAT_CREDENTIALS}"
    
    headers = {
        "Authorization": auth_header,
        "RqUID": str(uuid.uuid4()),
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    data = {
        "scope": GIGACHAT_SCOPE
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=data, ssl=False) as resp:
            if resp.status != 200:
                text = await resp.text()
                logging.error(f"GigaChat token error {resp.status}: {text}")
                return None
            data = await resp.json()
            token = data.get("access_token")
            
            expires_at = data.get("expires_at")
            
            if expires_at:
                if isinstance(expires_at, (int, float)):
                    if expires_at > 1_000_000_000:
                        # Это timestamp (секунды с 1970)
                        expires_dt = datetime.fromtimestamp(expires_at)
                    else:
                        # Это количество секунд до истечения
                        expires_dt = now + timedelta(seconds=expires_at - 300)
                else:
                    expires_dt = now + timedelta(minutes=25)
            else:
                expires_dt = now + timedelta(minutes=25)
            
            _gigachat_token_cache["token"] = token
            _gigachat_token_cache["expires_at"] = expires_dt
            
            logging.info(f"GigaChat token obtained, expires at {expires_dt}")
            return token

async def generate_gigachat_image(prompt: str) -> bytes | None:
    """Генерирует изображение через GigaChat API и возвращает bytes"""
    token = await get_gigachat_token()
    if not token:
        return None
    
    url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    payload = {
        "model": "GigaChat",
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "function_call": "auto"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers, ssl=False) as resp:
            if resp.status != 200:
                text = await resp.text()
                logging.error(f"GigaChat generation error {resp.status}: {text}")
                return None
            data = await resp.json()
            
            message_content = data["choices"][0]["message"]["content"]
            logging.info(f"GigaChat response: {message_content[:200]}")
            
            match = re.search(r'<img src="([a-f0-9-]+)"', message_content)
            if not match:
                match = re.search(r'uuid:([a-f0-9-]+)', message_content)
            if not match:
                match = re.search(r'([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})', message_content)
            if not match:
                logging.error("No image UUID in response")
                return None
            file_id = match.group(1)
            logging.info(f"Image UUID: {file_id}")
        
        download_url = f"https://gigachat.devices.sberbank.ru/api/v1/files/{file_id}/content"
        headers_download = {
            "Authorization": f"Bearer {token}",
            "Accept": "image/jpeg"
        }
        
        await asyncio.sleep(2)
        
        for attempt in range(5):
            async with session.get(download_url, headers=headers_download, ssl=False) as resp:
                if resp.status == 200:
                    image_bytes = await resp.read()
                    if image_bytes and len(image_bytes) > 1000:
                        return image_bytes
                    logging.warning(f"Download attempt {attempt+1}: empty image")
                elif resp.status == 404:
                    logging.info(f"Image not ready yet (attempt {attempt+1}/5)")
                else:
                    text = await resp.text()
                    logging.error(f"Download error {resp.status}: {text}")
            
            await asyncio.sleep(2)
        
        logging.error("Failed to download image after 5 attempts")
        return None

# ========================= БОТ =========================
session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()
router = Router()
dp.include_router(router)

user_style = {}

# ==================== ФОНОВЫЕ НАПОМИНАНИЯ ====================
async def send_reminders():
    """Фоновая задача: раз в день отправляет напоминания"""
    while True:
        try:
            now = datetime.now()
            next_run = now.replace(hour=12, minute=0, second=0, microsecond=0)
            if now >= next_run:
                next_run += timedelta(days=1)
            wait_seconds = (next_run - now).total_seconds()
            await asyncio.sleep(wait_seconds)
            
            expiring_users = get_users_with_expiring_subscription(days_left=1)
            for user_id in expiring_users:
                try:
                    await bot.send_message(
                        user_id,
                        "⏰ **Подписка заканчивается завтра!**\n\n"
                        "Твоя безлимитная генерация скоро закончится.\n"
                        "Продли подписку сейчас — всего 30 Stars за 1 день!\n\n"
                        "Нажми /subscribe чтобы выбрать тариф.",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(text="💎 Продлить подписку", callback_data="buy_sub")
                        ]])
                    )
                    mark_reminder_sent(user_id, "expiring")
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logging.error(f"Ошибка отправки напоминания {user_id}: {e}")
            
            free_exhausted_users = get_users_without_subscription_and_free()
            for user_id in free_exhausted_users:
                try:
                    await bot.send_message(
                        user_id,
                        "🎁 **Твои бесплатные генерации закончились!**\n\n"
                        "Но ты всё ещё можешь пользоваться ботом — подписка стоит всего 30 Stars.\n"
                        "Это меньше чашки кофе, а генераций — безлимит!\n\n"
                        "👉 /subscribe чтобы начать генерировать снова",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(text="💎 Купить подписку", callback_data="buy_sub")
                        ]])
                    )
                    mark_reminder_sent(user_id, "free_exhausted")
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logging.error(f"Ошибка отправки напоминания {user_id}: {e}")
            
            inactive_users = get_inactive_users(days=7)
            for user_id in inactive_users:
                try:
                    if has_active_subscription(user_id):
                        await bot.send_message(
                            user_id,
                            "👋 **Давно не виделись!**\n\n"
                            "Твоя подписка активна, а ты не пользуешься ботом.\n"
                            "Попробуй сгенерировать что-нибудь новое — нейросети постоянно улучшаются!\n\n"
                            "Просто отправь любой промт :)"
                        )
                    else:
                        free_left = get_free_generations(user_id)
                        await bot.send_message(
                            user_id,
                            f"👋 **Давно не виделись!**\n\n"
                            f"У тебя осталось **{free_left}** бесплатных генераций.\n"
                            "Попробуй снова — нейросеть GigaChat делает отличные картинки!\n\n"
                            "Просто выбери стиль и отправь промт."
                        )
                    mark_reminder_sent(user_id, "inactive")
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logging.error(f"Ошибка отправки напоминания {user_id}: {e}")
                    
        except Exception as e:
            logging.error(f"Ошибка в фоновой задаче: {e}")
            await asyncio.sleep(3600)

# ==================== ОБРАБОТЧИКИ ====================
@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    update_activity(user_id)
    
    welcome_text = (
        "🌟 Привет! Я генерирую крутые картинки через нейросеть GigaChat.\n\n"
        f"🎁 **У тебя есть {FREE_GENERATIONS} бесплатных генераций!**\n\n"
        "1️⃣ Выбери стиль\n"
        "2️⃣ Отправь текстовое описание\n"
        "3️⃣ Получи картинку\n\n"
        "💎 После бесплатных можно купить подписку — всего 30 Stars за 1 день.\n"
        "🚀 Безлимитная генерация для подписчиков!"
    )
    
    await message.answer(welcome_text, reply_markup=main_menu_keyboard())

@router.callback_query(F.data == "show_styles")
async def show_styles(callback: CallbackQuery):
    await callback.message.answer("🎨 Выбери стиль:", reply_markup=style_keyboard())
    await callback.answer()

@router.callback_query(F.data == "buy_sub")
async def buy_sub(callback: CallbackQuery):
    await callback.message.answer("💎 Выбери длительность подписки:", reply_markup=subscribe_keyboard())
    await callback.answer()

@router.callback_query(F.data == "show_status")
async def show_status_callback(callback: CallbackQuery):
    await cmd_status(callback.message)
    await callback.answer()

@router.callback_query(F.data == "show_free")
async def show_free_callback(callback: CallbackQuery):
    free_left = get_free_generations(callback.from_user.id)
    await callback.message.answer(f"🎁 У тебя осталось **{free_left}** бесплатных генераций из {FREE_GENERATIONS}.\n\nПосле этого для генерации нужна подписка.")
    await callback.answer()

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

@router.callback_query(F.data.startswith("sub_"))
async def process_sub(callback: CallbackQuery):
    days = int(callback.data.split("_")[1])
    stars = PRICES.get(days, 30)
    
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Подписка GigaChat — {days} дней",
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
    await message.answer(f"✅ Подписка активирована на {days} дней!\nТеперь ты можешь генерировать безлимитно (до {DAILY_LIMIT} в день).")

@router.message(F.text)
async def handle_prompt(message: Message):
    user_id = message.from_user.id
    update_activity(user_id)
    
    prompt = message.text.strip()
    
    if prompt.startswith('/'):
        return
    
    if has_active_subscription(user_id):
        await generate_and_send(message, user_id, prompt)
        return
    
    free_left = get_free_generations(user_id)
    
    if free_left > 0:
        await generate_and_send(message, user_id, prompt, is_free=True)
    else:
        await message.answer(
            "❌ У тебя закончились бесплатные генерации.\n\n"
            f"💰 **Купи подписку всего за 30 Stars на 1 день** и генерируй безлимитно!\n\n"
            "Нажми /subscribe чтобы выбрать тариф.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="💎 Купить подписку", callback_data="buy_sub")
            ]])
        )

async def generate_and_send(message: Message, user_id: int, prompt: str, is_free: bool = False):
    """Общая функция генерации и отправки (GigaChat)"""
    style = user_style.get(user_id, "none")
    
    style_prompts = {
        "photo": "в фотореалистичном стиле, высокая детализация, 8k",
        "anime": "в стиле аниме, яркие цвета, детализированные глаза",
        "cyber": "в стиле киберпанк, неоновые огни, футуристический город",
        "candy": "в глянцевом 3D-стиле, яркие насыщенные цвета, конфетные оттенки",
        "none": ""
    }
    
    full_prompt = prompt.strip()
    if style != "none":
        full_prompt += f", {style_prompts.get(style, '')}"
    
    if not is_free:
        daily_count = get_daily_generations(user_id)
        if daily_count >= DAILY_LIMIT:
            await message.answer(f"⏳ Сегодняшний лимит ({DAILY_LIMIT}) исчерпан.\nПриходи завтра!")
            return
    
    await message.answer("🎨 Генерирую изображение через GigaChat... (15–30 секунд)")
    
    image_bytes = await generate_gigachat_image(full_prompt)
    
    if image_bytes:
        if is_free:
            use_free_generation(user_id)
            free_text = f"\n🎁 Бесплатных осталось: {get_free_generations(user_id)}"
        else:
            free_text = ""
        
        photo_file = BytesIO(image_bytes)
        photo_file.name = "image.jpg"
        
        await message.answer_photo(
            photo=photo_file,
            caption=f"✨ Готово! (GigaChat)\nСтиль: {style}\nПромт: {prompt[:80]}...{free_text}"
        )
        save_to_history(user_id, prompt, "gigachat_generated")
    else:
        await message.answer("⚠️ Ошибка генерации. Попробуй другой промт или повтори позже.")

@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    await message.answer("💎 Выбери подписку:", reply_markup=subscribe_keyboard())

@router.message(Command("status"))
async def cmd_status(message: Message):
    user_id = message.from_user.id
    end = get_user(user_id)
    free_left = get_free_generations(user_id)
    style = user_style.get(user_id, "не выбран")
    
    if end and end > datetime.now():
        days_left = (end - datetime.now()).days
        gens_today = get_daily_generations(user_id)
        await message.answer(
            f"📊 **Статус**\n\n"
            f"💎 **Подписка:** активна до {end.strftime('%d.%m.%Y')} (осталось {days_left} дн.)\n"
            f"🎨 **Стиль:** {style}\n"
            f"🖼 **Сегодня:** {gens_today}/{DAILY_LIMIT} генераций\n"
            f"🎁 **Бесплатных осталось:** {free_left}"
        )
    else:
        await message.answer(
            f"📊 **Статус**\n\n"
            f"❌ Подписка неактивна\n"
            f"🎨 **Стиль:** {style}\n"
            f"🎁 **Бесплатных осталось:** {free_left}\n\n"
            f"Купить подписку: /subscribe"
        )

@router.message(Command("history"))
async def cmd_history(message: Message):
    rows = get_history(message.from_user.id, limit=3)
    if not rows:
        await message.answer("История пуста. Сгенерируй первую картинку!")
        return
    await message.answer("🖼 Последние 3 генерации:")
    for prompt, url in rows:
        await message.answer_photo(photo=url, caption=prompt[:100])

@router.message(Command("free"))
async def cmd_free(message: Message):
    free_left = get_free_generations(message.from_user.id)
    await message.answer(f"🎁 У тебя осталось **{free_left}** бесплатных генераций из {FREE_GENERATIONS}.\n\nПосле этого для генерации нужна подписка.")

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
        "/gift [user_id] — добавить 5 бесплатных пользователю\n"
        "/activity_stats — активность пользователей"
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
    cur.execute("SELECT SUM(remaining) FROM free_generations")
    free_remaining = cur.fetchone()[0] or 0
    conn.close()
    await message.answer(
        f"📊 **Статистика**\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"💎 Активных подписок: {active_subs}\n"
        f"🎁 Всего бесплатных осталось: {free_remaining}"
    )

@router.message(Command("gift"))
async def cmd_gift(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Формат: /gift [user_id]")
        return
    try:
        user_id = int(parts[1])
        reset_free_generations(user_id)
        await message.answer(f"✅ Пользователю {user_id} выдано {FREE_GENERATIONS} бесплатных генераций!")
    except:
        await message.answer("Ошибка: укажи правильный user_id")

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

@router.message(Command("activity_stats"))
async def cmd_activity_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    cur.execute("SELECT COUNT(*) FROM user_activity WHERE last_active > ?", (week_ago,))
    active_week = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    
    conn.close()
    
    await message.answer(
        f"📊 **Активность пользователей**\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"🟢 Активны за 7 дней: {active_week}\n"
        f"📈 Конверсия: {round(active_week / total_users * 100, 1) if total_users > 0 else 0}%"
    )

# ==================== HEALTH CHECK ДЛЯ RENDER ====================
async def health_check_server():
    """Простой HTTP-сервер, чтобы Render не завершал процесс"""
    port = int(os.environ.get("PORT", 10000))
    
    async def handle_request(reader, writer):
        await reader.read(1024)
        response = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nBot is running"
        writer.write(response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    
    try:
        server = await asyncio.start_server(handle_request, "0.0.0.0", port)
        logging.info(f"✅ Health check server started on port {port}")
        async with server:
            await server.serve_forever()
    except Exception as e:
        logging.warning(f"⚠️ Health check server failed: {e}")
        await asyncio.Event().wait()

# ==================== ЗАПУСК ====================
async def main():
    init_db()
    
    asyncio.create_task(health_check_server())
    asyncio.create_task(send_reminders())
    
    logging.info("🚀 Бот GigaChat запущен! Бесплатные генерации активны.")
    
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
