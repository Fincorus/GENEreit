import asyncio
import json
import logging
import sqlite3
import uuid
import re
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, BufferedInputFile
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

# Лимиты (можно убрать или оставить для теста)
DAILY_LIMIT = 100  # Безлимит, но ограничим 100 в день чтобы не перегружать API
FREE_GENERATIONS = 10  # Бесплатных генераций (можно убрать, но оставим как тест)

DB_FILE = "bot.db"

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT
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
    builder.button(text="🎁 Бесплатные", callback_data="show_free")
    builder.button(text="📊 Статус", callback_data="show_status")
    builder.adjust(2)
    return builder.as_markup()

def after_generation_keyboard(prompt: str, style: str):
    """Клавиатура, которая появляется после генерации"""
    builder = InlineKeyboardBuilder()
    
    short_prompt = prompt[:40].replace("|", " ").replace("\n", " ").strip()
    
    builder.button(text="🔄 Сгенерировать ещё", callback_data=f"reg|{short_prompt}|{style}")
    builder.button(text="🎨 Выбрать стиль", callback_data="show_styles")
    builder.button(text="📊 Статус", callback_data="show_status")
    
    builder.adjust(1)
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
    """Генерирует изображение через GigaChat API и возвращает bytes"""
    token = await get_gigachat_token()
    if not token:
        return None
    
    # Ограничиваем длину промта
    if len(prompt) > 500:
        prompt = prompt[:500]
        logging.warning(f"Prompt truncated to 500 chars")
    
    url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    full_prompt = f"Нарисуй фотореалистичное изображение: {prompt}"
    
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
        async with session.post(url, json=payload, headers=headers, ssl=False) as resp:
            if resp.status != 200:
                text = await resp.text()
                logging.error(f"GigaChat generation error {resp.status}: {text}")
                return None
            data = await resp.json()
            
            message_content = data["choices"][0]["message"]["content"]
            logging.info(f"GigaChat response: {message_content[:300]}")
            
            # Проверяем отказ
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
        
        for attempt in range(10):
            async with session.get(download_url, headers=headers_download, ssl=False) as resp:
                if resp.status == 200:
                    image_bytes = await resp.read()
                    if image_bytes and len(image_bytes) > 1000:
                        return image_bytes
                    logging.warning(f"Download attempt {attempt+1}: empty image ({len(image_bytes)} bytes)")
                elif resp.status == 404:
                    logging.info(f"Image not ready yet (attempt {attempt+1}/10)")
                else:
                    text = await resp.text()
                    logging.error(f"Download error {resp.status}: {text}")
            
            await asyncio.sleep(3)
        
        logging.error("Failed to download image after 10 attempts")
        return None

# ========================= БОТ =========================
session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()
router = Router()
dp.include_router(router)

user_style = {}

# ==================== ОБРАБОТЧИКИ ====================
@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    update_activity(user_id)
    
    free_left = get_free_generations(user_id)
    
    welcome_text = (
        "🌟 Привет! Я генерирую крутые картинки через нейросеть GigaChat.\n\n"
        f"🎁 **У тебя есть {free_left} бесплатных генераций!**\n\n"
        "1️⃣ Выбери стиль\n"
        "2️⃣ Отправь текстовое описание\n"
        "3️⃣ Получи картинку\n\n"
        "✨ После бесплатных можно запросить бонус у администратора."
    )
    
    await message.answer(welcome_text, reply_markup=main_menu_keyboard())

@router.callback_query(F.data == "show_styles")
async def show_styles(callback: CallbackQuery):
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

@router.callback_query(F.data.startswith("reg|"))
async def regenerate_image(callback: CallbackQuery):
    """Обработчик кнопки 'Сгенерировать ещё'"""
    user_id = callback.from_user.id
    update_activity(user_id)
    
    data = callback.data.split("|")
    if len(data) >= 3:
        prompt = data[1]
        style = data[2]
        user_style[user_id] = style
    else:
        await callback.answer("❌ Ошибка: не удалось восстановить промт")
        return
    
    await callback.answer("🔄 Генерирую снова...")
    
    free_left = get_free_generations(user_id)
    if free_left > 0:
        await generate_and_send(callback.message, user_id, prompt, is_free=True)
    else:
        await callback.message.answer(
            "❌ У тебя закончились бесплатные генерации.\n\n"
            "Напиши администратору, чтобы получить бонус.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📊 Статус", callback_data="show_status")
            ]])
        )

@router.message(F.text)
async def handle_prompt(message: Message):
    user_id = message.from_user.id
    update_activity(user_id)
    
    prompt = message.text.strip()
    
    if prompt.startswith('/'):
        return
    
    free_left = get_free_generations(user_id)
    
    if free_left > 0:
        await generate_and_send(message, user_id, prompt, is_free=True)
    else:
        await message.answer(
            "❌ У тебя закончились бесплатные генерации.\n\n"
            "Напиши администратору, чтобы получить бонус.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📊 Статус", callback_data="show_status")
            ]])
        )

async def generate_and_send(message: Message, user_id: int, prompt: str, is_free: bool = False):
    """Общая функция генерации и отправки"""
    style = user_style.get(user_id, "none")
    
    style_prompts = {
        "photo": "фотореалистичный стиль, высокая детализация, профессиональная фотография",
        "anime": "стиль аниме, яркие цвета, детализированные глаза",
        "cyber": "киберпанк стиль, неоновые огни",
        "candy": "глянцевый 3D стиль, яркие насыщенные цвета",
        "none": ""
    }
    
    full_prompt = prompt.strip()
    if style != "none":
        full_prompt = f"{full_prompt}, {style_prompts.get(style, '')}"
    
    if len(full_prompt) > 400:
        full_prompt = full_prompt[:400]
        await message.answer("⚠️ Промт слишком длинный, я немного сократил его.")
    
    daily_count = get_daily_generations(user_id)
    if daily_count >= DAILY_LIMIT:
        await message.answer(f"⏳ Дневной лимит ({DAILY_LIMIT}) исчерпан.\nПриходи завтра!")
        return
    
    await message.answer("🎨 Генерирую изображение... (20–40 секунд)")
    
    try:
        image_bytes = await generate_gigachat_image(full_prompt)
    except Exception as e:
        logging.error(f"Generation exception: {e}")
        await message.answer("⚠️ Ошибка при генерации. Попробуй более простой промт.")
        return
    
    if image_bytes:
        if is_free:
            use_free_generation(user_id)
            free_text = f"\n🎁 Бесплатных осталось: {get_free_generations(user_id)}"
        else:
            free_text = ""
        
        photo_file = BufferedInputFile(image_bytes, filename="image.jpg")
        reply_markup = after_generation_keyboard(prompt[:40], style)
        
        await message.answer_photo(
            photo=photo_file,
            caption=f"✨ Готово!\nСтиль: {style}\nПромт: {prompt[:60]}...{free_text}",
            reply_markup=reply_markup
        )
        save_to_history(user_id, prompt, "gigachat_generated")
    else:
        await message.answer(
            "⚠️ Не удалось сгенерировать изображение.\n\n"
            "Попробуй:\n"
            "• Упростить промт (убрать технические детали)\n"
            "• Сделать запрос короче\n"
            "• Использовать английский язык\n\n"
            "Пример: *фотореалистичный портрет девушки, зеленые глаза*"
        )

# ==================== КОМАНДЫ ====================
@router.message(Command("status"))
async def cmd_status(message: Message):
    user_id = message.from_user.id
    free_left = get_free_generations(user_id)
    style = user_style.get(user_id, "не выбран")
    gens_today = get_daily_generations(user_id)
    
    await message.answer(
        f"📊 **Статус**\n\n"
        f"🎨 **Стиль:** {style}\n"
        f"🖼 **Сегодня:** {gens_today}/{DAILY_LIMIT} генераций\n"
        f"🎁 **Бесплатных осталось:** {free_left}\n\n"
        f"После бесплатных напиши администратору для бонуса."
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
    await message.answer(f"🎁 У тебя осталось **{free_left}** бесплатных генераций из {FREE_GENERATIONS}.")

# ==================== АДМИН-КОМАНДЫ ====================
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "🛠 Админ-панель:\n"
        "/stats — статистика\n"
        "/broadcast [текст] — рассылка\n"
        "/gift [user_id] — добавить 10 бесплатных пользователю"
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
    conn.close()
    await message.answer(
        f"📊 **Статистика**\n\n"
        f"👥 Всего пользователей: {total_users}\n"
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
    
    logging.info("🚀 Бот GigaChat запущен! Безлимитные генерации активны.")
    
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
