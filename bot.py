import asyncio
import json
import logging
import sqlite3
import uuid
import re
import random
import zipfile
from io import BytesIO
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, BufferedInputFile, FSInputFile
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

# Хранилище последнего промта пользователя
last_user_prompt = {}

# Для ограничения частоты запросов
last_request_time = {}

# Лимиты
DAILY_LIMIT = 100
FREE_GENERATIONS = 10
DAILY_BONUS = 3

DB_FILE = "bot.db"

# Список случайных промтов
RANDOM_PROMPTS = [
    "кот в космосе",
    "девушка с зонтом под дождём",
    "киберпанк город ночью",
    "единорог в радужном лесу",
    "портрет девушки с зелеными глазами",
    "космическая станция на закате",
    "дракон в готическом соборе",
    "робот-самурай с катаной",
    "фея в магическом саду",
    "замок на облаках",
    "медуза горгона в античном стиле",
    "летающий корабль над океаном",
    "тигр в джунглях с неоновыми огнями",
    "девушка-воин в доспехах",
    "пейзаж в стиле студии Гибли"
]

# Список популярных промтов
POPULAR_PROMPTS = {
    "🐱 Кот в космосе": "кот в космосе",
    "👧 Девушка-аниме": "девушка в стиле аниме, большие глаза, яркие волосы",
    "🌆 Киберпанк город": "киберпанк город, неоновые огни, дождь",
    "🏰 Фэнтези замок": "фэнтези замок на горе, облака, закат",
    "🐉 Дракон": "огнедышащий дракон в эпическом стиле",
    "🌸 Сакура": "цветущая сакура, японский сад, весна",
    "🚀 Космос": "космическая туманность, звезды, планеты",
    "🎨 Абстракция": "абстрактное искусство, яркие цвета, геометрия"
}

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        style TEXT DEFAULT 'none',
        last_bonus TIMESTAMP
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
        remaining INTEGER DEFAULT 10
    )""")
    
    cur.execute("""CREATE TABLE IF NOT EXISTS user_activity (
        user_id INTEGER PRIMARY KEY,
        last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    cur.execute("""CREATE TABLE IF NOT EXISTS favorites (
        user_id INTEGER,
        image_id INTEGER,
        FOREIGN KEY (image_id) REFERENCES history (id),
        PRIMARY KEY (user_id, image_id)
    )""")
    
    conn.commit()
    conn.close()

def update_activity(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO user_activity (user_id, last_active)
        VALUES (?, CURRENT_TIMESTAMP)
    """, (user_id,))
    conn.commit()
    conn.close()

def get_user_style(user_id: int) -> str:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT style FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else "none"

def save_user_style(user_id: int, style: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO users (user_id, style) VALUES (?, ?)", (user_id, style))
    conn.commit()
    conn.close()

def can_claim_daily_bonus(user_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT last_bonus FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    
    if not row or not row[0]:
        return True
    last_bonus = datetime.fromisoformat(row[0])
    return (datetime.now() - last_bonus).days >= 1

def claim_daily_bonus(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO users (user_id, last_bonus) VALUES (?, ?)", 
                (user_id, datetime.now().isoformat()))
    cur.execute("INSERT OR REPLACE INTO free_generations (user_id, remaining) VALUES (?, COALESCE((SELECT remaining FROM free_generations WHERE user_id = ?), 10) + ?)",
                (user_id, user_id, DAILY_BONUS))
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

def add_free_generations(user_id: int, amount: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO free_generations (user_id, remaining) VALUES (?, COALESCE((SELECT remaining FROM free_generations WHERE user_id = ?), 0) + ?)",
                (user_id, user_id, amount))
    conn.commit()
    conn.close()

def reset_free_generations(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO free_generations (user_id, remaining) VALUES (?, ?)", (user_id, FREE_GENERATIONS))
    conn.commit()
    conn.close()

def get_daily_generations(user_id: int) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    today = datetime.now().date().isoformat()
    cur.execute("SELECT COUNT(*) FROM history WHERE user_id = ? AND date(created_at) = ?", (user_id, today))
    count = cur.fetchone()[0]
    conn.close()
    return count

def save_to_history(user_id: int, prompt: str, image_url: str) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO history (user_id, prompt, image_url) VALUES (?, ?, ?)", (user_id, prompt, image_url))
    image_id = cur.lastrowid
    conn.commit()
    conn.close()
    return image_id

def get_history(user_id: int, limit=5, search=None):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    if search:
        cur.execute("SELECT id, prompt, image_url FROM history WHERE user_id = ? AND prompt LIKE ? ORDER BY created_at DESC LIMIT ?", 
                    (user_id, f"%{search}%", limit))
    else:
        cur.execute("SELECT id, prompt, image_url FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_all_history(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, prompt, image_url FROM history WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def add_to_favorites(user_id: int, image_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO favorites (user_id, image_id) VALUES (?, ?)", (user_id, image_id))
        conn.commit()
        conn.close()
        return True
    except:
        conn.close()
        return False

def remove_from_favorites(user_id: int, image_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM favorites WHERE user_id = ? AND image_id = ?", (user_id, image_id))
    conn.commit()
    conn.close()

def get_favorites(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        SELECT h.id, h.prompt, h.image_url 
        FROM history h 
        JOIN favorites f ON h.id = f.image_id 
        WHERE f.user_id = ? 
        ORDER BY h.created_at DESC
    """, (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def is_favorite(user_id: int, image_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM favorites WHERE user_id = ? AND image_id = ?", (user_id, image_id))
    row = cur.fetchone()
    conn.close()
    return row is not None

# ========================= КНОПКИ =========================
def style_keyboard():
    builder = InlineKeyboardBuilder()
    styles = [
        ("📸 Фотореализм", "photo"),
        ("🎨 Аниме", "anime"),
        ("🌃 Киберпанк", "cyber"),
        ("🍭 3D Candy", "candy"),
        ("🖼 Акварель", "watercolor"),
        ("🖌 Масло", "oil"),
        ("🎮 Видеоигра", "game"),
        ("📷 Винтаж", "vintage"),
        ("✨ Без стиля", "none")
    ]
    for text, data in styles:
        builder.button(text=text, callback_data=f"style_{data}")
    builder.adjust(2)
    return builder.as_markup()

def style_prompts():
    return {
        "photo": "фотореалистичный стиль, высокая детализация, профессиональная фотография",
        "anime": "стиль аниме, яркие цвета, детализированные глаза",
        "cyber": "киберпанк стиль, неоновые огни, футуристический город",
        "candy": "глянцевый 3D стиль, яркие насыщенные цвета, конфетные оттенки",
        "watercolor": "стиль акварели, мягкие переходы, художественный",
        "oil": "стиль масляной живописи, текстурные мазки, импрессионизм",
        "game": "стиль видеоигры, 3D рендер, концепт-арт",
        "vintage": "винтажный стиль, плёночная фотография, ретро эффекты",
        "none": ""
    }

def main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🎨 Выбрать стиль", callback_data="show_styles")
    builder.button(text="🎁 Бесплатные", callback_data="show_free")
    builder.button(text="📊 Статус", callback_data="show_status")
    builder.button(text="🎲 Случайный промт", callback_data="random_prompt")
    builder.button(text="🔥 Популярные", callback_data="show_popular")
    builder.button(text="❤️ Избранное", callback_data="show_favorites")
    builder.button(text="📦 Ежедневный бонус", callback_data="daily_bonus")
    builder.adjust(2)
    return builder.as_markup()

def popular_prompts_keyboard():
    builder = InlineKeyboardBuilder()
    for name, prompt in POPULAR_PROMPTS.items():
        builder.button(text=name, callback_data=f"use_prompt|{prompt}")
    builder.button(text="🔙 Назад", callback_data="back_to_menu")
    builder.adjust(1)
    return builder.as_markup()

def after_generation_keyboard(style: str, image_id: int):
    """Клавиатура, которая появляется после генерации"""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Ещё раз", callback_data=f"reg|{style}")
    builder.button(text="🎨 Стиль", callback_data="show_styles")
    builder.button(text="❤️ В избранное", callback_data=f"fav|{image_id}")
    builder.button(text="📊 Статус", callback_data="show_status")
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
                if expires_at > 1_000_000_000_000:
                    expires_dt = datetime.fromtimestamp(expires_at / 1000)
                elif expires_at > 1_000_000_000:
                    expires_dt = datetime.fromtimestamp(expires_at)
                else:
                    expires_dt = now + timedelta(seconds=expires_at - 300)
            else:
                expires_dt = now + timedelta(minutes=25)
            
            _gigachat_token_cache["token"] = token
            _gigachat_token_cache["expires_at"] = expires_dt
            
            logging.info(f"GigaChat token obtained, expires at {expires_dt}")
            return token

async def generate_gigachat_image(prompt: str) -> bytes | None:
    """Генерирует изображение через GigaChat API с повторными попытками при 429"""
    token = await get_gigachat_token()
    if not token:
        return None
    
    if len(prompt) > 500:
        prompt = prompt[:500]
        logging.warning(f"Prompt truncated to 500 chars")
    
    url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    full_prompt = f"Нарисуй изображение: {prompt}"
    
    payload = {
        "model": "GigaChat",
        "messages": [
            {
                "role": "user",
                "content": full_prompt
            }
        ],
        "function_call": "auto"
    }
    
    async with aiohttp.ClientSession() as session:
        for attempt in range(3):
            async with session.post(url, json=payload, headers=headers, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    message_content = data["choices"][0]["message"]["content"]
                    logging.info(f"GigaChat response: {message_content[:300]}")
                    
                    if "не получилось" in message_content.lower() or "не удалось" in message_content.lower():
                        logging.error("GigaChat refused to generate")
                        return None
                    
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
                    
                    await asyncio.sleep(3)
                    
                    for download_attempt in range(10):
                        async with session.get(download_url, headers=headers_download, ssl=False) as download_resp:
                            if download_resp.status == 200:
                                image_bytes = await download_resp.read()
                                if image_bytes and len(image_bytes) > 1000:
                                    return image_bytes
                                logging.warning(f"Download attempt {download_attempt+1}: empty image")
                            elif download_resp.status == 404:
                                logging.info(f"Image not ready yet (attempt {download_attempt+1}/10)")
                            else:
                                text = await download_resp.text()
                                logging.error(f"Download error {download_resp.status}: {text}")
                        
                        await asyncio.sleep(3)
                    
                    logging.error("Failed to download image after 10 attempts")
                    return None
                    
                elif resp.status == 429:
                    wait_time = (attempt + 1) * 5
                    logging.warning(f"Rate limited (429), waiting {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    text = await resp.text()
                    logging.error(f"GigaChat generation error {resp.status}: {text}")
                    return None
        
        logging.error("Failed after 3 attempts due to rate limiting")
        return None

# ========================= БОТ =========================
session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()
router = Router()
dp.include_router(router)

user_style = {}

# ==================== АНИМАЦИЯ ЗАГРУЗКИ ====================
async def show_loading_animation(message: Message, duration: int = 30):
    """Показывает анимацию загрузки с точками"""
    loading_message = await message.answer("🎨 Генерирую изображение")
    dots = 0
    for i in range(duration):
        dots = (i % 3) + 1
        try:
            await loading_message.edit_text(f"🎨 Генерирую изображение{'.' * dots}")
        except:
            pass
        await asyncio.sleep(1)
    return loading_message

# ==================== CALLBACK ОБРАБОТЧИКИ ====================
@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    try:
        await callback.message.edit_text("📋 Главное меню:", reply_markup=main_menu_keyboard())
    except Exception:
        await callback.message.answer("📋 Главное меню:", reply_markup=main_menu_keyboard())
    await callback.answer()

@router.callback_query(F.data == "show_styles")
async def show_styles(callback: CallbackQuery):
    try:
        await callback.message.edit_text("🎨 Выбери стиль:", reply_markup=style_keyboard())
    except Exception:
        await callback.message.answer("🎨 Выбери стиль:", reply_markup=style_keyboard())
    await callback.answer()

@router.callback_query(F.data == "show_status")
async def show_status_callback(callback: CallbackQuery):
    await cmd_status(callback.message)
    await callback.answer()

@router.callback_query(F.data == "show_free")
async def show_free_callback(callback: CallbackQuery):
    free_left = get_free_generations(callback.from_user.id)
    await callback.message.answer(f"🎁 У тебя осталось **{free_left}** бесплатных генераций из {FREE_GENERATIONS}.")
    await callback.answer()

@router.callback_query(F.data == "daily_bonus")
async def daily_bonus_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    if can_claim_daily_bonus(user_id):
        claim_daily_bonus(user_id)
        free_left = get_free_generations(user_id)
        await callback.message.answer(f"✅ Ты получил ежедневный бонус +{DAILY_BONUS} генераций!\n\n🎁 Теперь у тебя {free_left} бесплатных генераций.")
    else:
        await callback.message.answer("⏳ Ты уже получал бонус сегодня. Приходи завтра!")
    
    await callback.answer()

@router.callback_query(F.data == "random_prompt")
async def random_prompt_callback(callback: CallbackQuery):
    prompt = random.choice(RANDOM_PROMPTS)
    user_id = callback.from_user.id
    
    await callback.answer(f"🎲 Выбран промт: {prompt}")
    
    last_user_prompt[user_id] = prompt
    
    free_left = get_free_generations(user_id)
    if free_left > 0:
        await generate_and_send(callback.message, user_id, prompt, is_free=True)
    else:
        await callback.message.answer("❌ Бесплатные генерации закончились. Получи ежедневный бонус или напиши администратору.")

@router.callback_query(F.data == "show_popular")
async def show_popular(callback: CallbackQuery):
    try:
        await callback.message.edit_text("🔥 Популярные промты:", reply_markup=popular_prompts_keyboard())
    except Exception:
        await callback.message.answer("🔥 Популярные промты:", reply_markup=popular_prompts_keyboard())
    await callback.answer()

@router.callback_query(F.data == "show_favorites")
async def show_favorites(callback: CallbackQuery):
    user_id = callback.from_user.id
    favorites = get_favorites(user_id)
    
    if not favorites:
        await callback.message.answer("❤️ У тебя пока нет избранных картинок. Чтобы добавить, нажми ❤️ В избранное после генерации.")
        await callback.answer()
        return
    
    await callback.message.answer(f"❤️ Твои избранные картинки ({len(favorites)}):")
    for img_id, prompt, url in favorites[:5]:
        await callback.message.answer_photo(photo=url, caption=f"📝 {prompt[:60]}\n🆔 ID: {img_id}")
    
    if len(favorites) > 5:
        await callback.message.answer(f"📌 Показано 5 из {len(favorites)}. Используй /favorites для просмотра всех.")
    
    await callback.answer()

@router.callback_query(F.data.startswith("use_prompt|"))
async def use_prompt(callback: CallbackQuery):
    prompt = callback.data.split("|")[1]
    user_id = callback.from_user.id
    
    await callback.answer(f"✅ Выбран промт: {prompt[:50]}...")
    
    last_user_prompt[user_id] = prompt
    
    free_left = get_free_generations(user_id)
    if free_left > 0:
        await generate_and_send(callback.message, user_id, prompt, is_free=True)
    else:
        await callback.message.answer("❌ Бесплатные генерации закончились. Получи ежедневный бонус или напиши администратору.")

@router.callback_query(F.data.startswith("style_"))
async def choose_style(callback: CallbackQuery):
    style = callback.data.split("_")[1]
    user_id = callback.from_user.id
    user_style[user_id] = style
    save_user_style(user_id, style)
    
    style_names = {
        "photo": "📸 Фотореализм",
        "anime": "🎨 Аниме",
        "cyber": "🌃 Киберпанк",
        "candy": "🍭 3D Candy",
        "watercolor": "🖼 Акварель",
        "oil": "🖌 Масло",
        "game": "🎮 Видеоигра",
        "vintage": "📷 Винтаж",
        "none": "✨ Без стиля"
    }
    
    text = f"✅ Стиль выбран: {style_names.get(style, 'Без стиля')}\n\nТеперь отправь мне текстовое описание картинки!\n\n🎲 Или используй кнопки меню для случайного/популярного промта."
    
    try:
        await callback.message.edit_text(text, reply_markup=main_menu_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=main_menu_keyboard())
    await callback.answer()

@router.callback_query(F.data.startswith("reg|"))
async def regenerate_image(callback: CallbackQuery):
    user_id = callback.from_user.id
    update_activity(user_id)
    
    now = datetime.now()
    last_time = last_request_time.get(user_id)
    if last_time and (now - last_time).seconds < 5:
        await callback.answer("⏳ Подожди 5 секунд!", show_alert=True)
        return
    last_request_time[user_id] = now
    
    data = callback.data.split("|")
    if len(data) >= 2:
        style = data[1]
        user_style[user_id] = style
        save_user_style(user_id, style)
    
    prompt = last_user_prompt.get(user_id)
    if not prompt:
        await callback.answer("❌ Не найден предыдущий промт")
        return
    
    await callback.answer("🔄 Генерирую...")
    
    free_left = get_free_generations(user_id)
    if free_left > 0:
        await generate_and_send(callback.message, user_id, prompt, is_free=True)
    else:
        await callback.message.answer("❌ Бесплатные генерации закончились.")

@router.callback_query(F.data.startswith("fav|"))
async def add_to_favorites_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    image_id = int(callback.data.split("|")[1])
    
    if add_to_favorites(user_id, image_id):
        await callback.answer("❤️ Добавлено в избранное!")
    else:
        await callback.answer("⚠️ Уже в избранном!")

# ==================== ОСНОВНЫЕ ОБРАБОТЧИКИ ====================
@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    update_activity(user_id)
    
    free_left = get_free_generations(user_id)
    style = get_user_style(user_id)
    user_style[user_id] = style
    
    welcome_text = (
        "🌟 Привет! Я генерирую крутые картинки через нейросеть GigaChat.\n\n"
        f"🎁 **У тебя есть {free_left} бесплатных генераций!**\n\n"
        "📌 **Что умею:**\n"
        "• 🎨 9 стилей генерации\n"
        "• 🎲 Случайный промт\n"
        "• 🔥 Популярные запросы\n"
        "• ❤️ Избранное\n"
        "• 📦 Ежедневный бонус\n"
        "• 🔍 Поиск по истории /search\n"
        "• 📦 Экспорт в ZIP /export\n\n"
        "👇 Выбери действие в меню!"
    )
    
    await message.answer(welcome_text, reply_markup=main_menu_keyboard())

@router.message(F.text)
async def handle_prompt(message: Message):
    user_id = message.from_user.id
    
    now = datetime.now()
    last_time = last_request_time.get(user_id)
    if last_time and (now - last_time).seconds < 5:
        await message.answer("⏳ Подожди 5 секунд перед следующим запросом!")
        return
    last_request_time[user_id] = now
    
    update_activity(user_id)
    
    prompt = message.text.strip()
    
    if prompt.startswith('/'):
        return
    
    last_user_prompt[user_id] = prompt
    
    free_left = get_free_generations(user_id)
    
    if free_left > 0:
        await generate_and_send(message, user_id, prompt, is_free=True)
    else:
        await message.answer(
            "❌ Бесплатные генерации закончились.\n\n"
            "📦 Получи ежедневный бонус в меню!\n"
            "👑 Или напиши администратору.",
            reply_markup=main_menu_keyboard()
        )

async def generate_and_send(message: Message, user_id: int, prompt: str, is_free: bool = False):
    """Общая функция генерации и отправки с анимацией загрузки"""
    style = user_style.get(user_id, get_user_style(user_id))
    
    style_prompts_dict = style_prompts()
    style_prompt = style_prompts_dict.get(style, "")
    
    full_prompt = prompt.strip()
    if style != "none" and style_prompt:
        full_prompt = f"{full_prompt}, {style_prompt}"
    
    if len(full_prompt) > 400:
        full_prompt = full_prompt[:400]
        await message.answer("⚠️ Промт слишком длинный, я немного сократил его.")
    
    daily_count = get_daily_generations(user_id)
    if daily_count >= DAILY_LIMIT:
        await message.answer(f"⏳ Дневной лимит ({DAILY_LIMIT}) исчерпан.\nПриходи завтра!")
        return
    
    # Запускаем анимацию загрузки
    loading_task = asyncio.create_task(show_loading_animation(message))
    
    try:
        image_bytes = await generate_gigachat_image(full_prompt)
    except Exception as e:
        logging.error(f"Generation exception: {e}")
        await message.answer("⚠️ Ошибка при генерации. Попробуй более простой промт.")
        return
    finally:
        # Останавливаем анимацию
        loading_task.cancel()
    
    if image_bytes:
        if is_free:
            use_free_generation(user_id)
            free_text = f"\n🎁 Бесплатных осталось: {get_free_generations(user_id)}"
        else:
            free_text = ""
        
        photo_file = BufferedInputFile(image_bytes, filename="image.jpg")
        
        # Сохраняем в историю и получаем ID
        image_id = save_to_history(user_id, prompt, "gigachat_generated")
        
        reply_markup = after_generation_keyboard(style, image_id)
        
        await message.answer_photo(
            photo=photo_file,
            caption=f"✨ Готово!\nСтиль: {style}\nПромт: {prompt[:60]}...{free_text}\n🆔 ID: {image_id}",
            reply_markup=reply_markup
        )
    else:
        await message.answer(
            "⚠️ Не удалось сгенерировать изображение.\n\n"
            "Попробуй:\n"
            "• Упростить промт\n"
            "• Сделать запрос короче\n"
            "• Использовать английский язык\n\n"
            "🎲 Или попробуй случайный промт в меню!"
        )

# ==================== КОМАНДЫ ====================
@router.message(Command("status"))
async def cmd_status(message: Message):
    user_id = message.from_user.id
    free_left = get_free_generations(user_id)
    style = user_style.get(user_id, get_user_style(user_id))
    gens_today = get_daily_generations(user_id)
    
    await message.answer(
        f"📊 **Статус**\n\n"
        f"🎨 **Стиль:** {style}\n"
        f"🖼 **Сегодня:** {gens_today}/{DAILY_LIMIT} генераций\n"
        f"🎁 **Бесплатных осталось:** {free_left}\n\n"
        f"📦 Ежедневный бонус: +{DAILY_BONUS} генераций"
    )

@router.message(Command("history"))
async def cmd_history(message: Message):
    user_id = message.from_user.id
    rows = get_history(user_id, limit=5)
    
    if not rows:
        await message.answer("📭 История пуста. Сгенерируй первую картинку!")
        return
    
    await message.answer(f"🖼 Последние 5 генераций:")
    for img_id, prompt, url in rows:
        fav_mark = "❤️ " if is_favorite(user_id, img_id) else ""
        await message.answer_photo(photo=url, caption=f"{fav_mark}📝 {prompt[:80]}\n🆔 ID: {img_id}")

@router.message(Command("search"))
async def cmd_search(message: Message):
    user_id = message.from_user.id
    search_text = message.text.replace("/search", "").strip()
    
    if not search_text:
        await message.answer("🔍 Укажи текст для поиска. Пример: /search кот")
        return
    
    rows = get_history(user_id, limit=5, search=search_text)
    
    if not rows:
        await message.answer(f"🔍 Ничего не найдено по запросу '{search_text}'")
        return
    
    await message.answer(f"🔍 Результаты поиска '{search_text}':")
    for img_id, prompt, url in rows:
        await message.answer_photo(photo=url, caption=f"📝 {prompt[:80]}\n🆔 ID: {img_id}")

@router.message(Command("favorites"))
async def cmd_favorites(message: Message):
    user_id = message.from_user.id
    favorites = get_favorites(user_id)
    
    if not favorites:
        await message.answer("❤️ У тебя пока нет избранных картинок. Чтобы добавить, нажми ❤️ В избранное после генерации.")
        return
    
    await message.answer(f"❤️ Твои избранные картинки ({len(favorites)}):")
    for img_id, prompt, url in favorites:
        await message.answer_photo(photo=url, caption=f"📝 {prompt[:80]}\n🆔 ID: {img_id}")

@router.message(Command("export"))
async def cmd_export(message: Message):
    user_id = message.from_user.id
    history = get_all_history(user_id)
    
    if not history:
        await message.answer("📭 Нет картинок для экспорта.")
        return
    
    await message.answer("📦 Создаю архив... Это может занять несколько секунд.")
    
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for idx, (img_id, prompt, url) in enumerate(history):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            image_bytes = await resp.read()
                            safe_prompt = "".join(c for c in prompt[:30] if c.isalnum() or c in " _-").strip()
                            filename = f"{idx+1:03d}_{safe_prompt}.jpg"
                            zip_file.writestr(filename, image_bytes)
                await asyncio.sleep(0.1)
            except Exception as e:
                logging.error(f"Export error for {img_id}: {e}")
    
    zip_buffer.seek(0)
    
    await message.answer_document(
        document=BufferedInputFile(zip_buffer.getvalue(), filename=f"generations_{user_id}.zip"),
        caption=f"📦 Экспорт {len(history)} картинок"
    )

@router.message(Command("free"))
async def cmd_free(message: Message):
    free_left = get_free_generations(message.from_user.id)
    await message.answer(f"🎁 У тебя осталось **{free_left}** бесплатных генераций из {FREE_GENERATIONS}.\n\n📦 Ежедневный бонус: +{DAILY_BONUS} генераций в день!")

@router.message(Command("bonus"))
async def cmd_bonus(message: Message):
    user_id = message.from_user.id
    if can_claim_daily_bonus(user_id):
        claim_daily_bonus(user_id)
        free_left = get_free_generations(user_id)
        await message.answer(f"✅ Ты получил ежедневный бонус +{DAILY_BONUS} генераций!\n\n🎁 Теперь у тебя {free_left} бесплатных генераций.")
    else:
        await message.answer("⏳ Ты уже получал бонус сегодня. Приходи завтра!")

# ==================== АДМИН-КОМАНДЫ ====================
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "🛠 Админ-панель:\n"
        "/stats — статистика\n"
        "/broadcast [текст] — рассылка\n"
        "/gift [user_id] — добавить 10 бесплатных пользователю\n"
        "/add_gen [user_id] [количество] — добавить генерации"
    )

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT SUM(remaining) FROM free_generations")
    free_remaining = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM history")
    total_images = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM favorites")
    total_favorites = cur.fetchone()[0]
    conn.close()
    await message.answer(
        f"📊 **Статистика**\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"🎁 Всего бесплатных осталось: {free_remaining}\n"
        f"🖼 Всего сгенерировано: {total_images}\n"
        f"❤️ Всего в избранном: {total_favorites}"
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

@router.message(Command("add_gen"))
async def cmd_add_gen(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Формат: /add_gen [user_id] [количество]")
        return
    try:
        user_id = int(parts[1])
        amount = int(parts[2])
        add_free_generations(user_id, amount)
        await message.answer(f"✅ Пользователю {user_id} добавлено {amount} бесплатных генераций!")
    except:
        await message.answer("Ошибка: укажи правильные данные")

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
    
    logging.info("🚀 Бот GigaChat запущен! Все функции активны.")
    
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
