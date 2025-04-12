import logging
import random
import sqlite3
import os
import httpx  # заменяем requests на httpx для асинхронных запросов
from telegram import Update, Poll
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

# Загрузка переменных окружения из .env файла
load_dotenv()

folder_id = os.getenv('FOLDER_ID')
yandexgpt_key = os.getenv('YANDEXGPT_KEY')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
AI_PROVIDER = os.getenv("AI_PROVIDER", "YANDEX")
DB_PATH = os.getenv("DB_PATH", "groups.db")


if not folder_id:
    raise ValueError("FOLDER_ID не установлен. Проверьте ваш .env файл.")
if not yandexgpt_key:
    raise ValueError("YANDEXGPT_KEY не установлен. Проверьте ваш .env файл.")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не установлен. Проверьте ваш .env файл.")

# --- Логирование ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Инициализация базы данных ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Создание таблицы, если не существует
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            is_active INTEGER DEFAULT 0
        )
    ''')

    # Миграция: добавляем колонку is_active, если её ещё нет
    try:
        cursor.execute('ALTER TABLE groups ADD COLUMN is_active INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass  # колонка уже есть — игнорируем ошибку

    conn.commit()
    conn.close()

def add_group(chat_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO groups (chat_id) VALUES (?)', (chat_id,))
    conn.commit()
    conn.close()

def get_all_groups():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT chat_id FROM groups')
    groups = [row[0] for row in cursor.fetchall()]
    conn.close()
    return groups

def set_active(chat_id, active=True):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE groups SET is_active = ? WHERE chat_id = ?', (1 if active else 0, chat_id))
    conn.commit()
    conn.close()

def is_chat_active(chat_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT is_active FROM groups WHERE chat_id = ?', (chat_id,))
    result = cursor.fetchone()
    conn.close()
    return result and result[0] == 1


# --- Генерация повода ---
async def generate_reason():
    prompt = "Придумай весёлую, немного философскую или жизненную причину, почему сегодня важно пойти пить пиво."
    return await generate_text(prompt)

# --- Генерация ответа на возражение ---
async def handle_objection(text: str) -> str:
    prompt = f"Тебе сказали: '{text}'. Ответь весело и с подначкой, чтобы всё-таки уговорить человека пойти пить пиво."
    return await generate_text(prompt)

# --- Универсальный генератор текста ---
async def generate_text(prompt: str) -> str:
    if AI_PROVIDER == "OPENAI":
        import openai
        openai.api_key = os.getenv("OPENAI_API_KEY")
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content.strip()
    else:
        async with httpx.AsyncClient() as client:
            url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
            headers = {
                "Authorization": f"Api-Key {yandexgpt_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "modelUri": f"gpt://{folder_id}/yandexgpt-lite/latest",
                "completionOptions": {
                    "stream": False,
                    "temperature": 0.8,
                    "maxTokens": 200
                },
                "messages": [
                    {"role": "user", "text": prompt}
                ]
            }
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()
            return result["result"]["alternatives"][0]["message"]["text"]

# --- Ежедневная агитация во всех группах ---
async def daily_motivation(application):
    reason = await generate_reason()
    for chat_id in get_all_groups():
        try:
            set_active(chat_id, True)  # активируем обсуждение
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"Друзья, сегодня отличный день, чтобы выпить пива! 🍺\nПочему?\n👉 {reason}\n\n"
                     f"Пишите, обсуждаем до голосования! Напишите /vote, чтобы завершить."
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить сообщение в чат {chat_id}: {e}")

# --- Обработка любого сообщения во время активной беседы ---
async def chat_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_chat_active(chat_id):
        return
    user_message = update.message.text
    prompt = f"Пользователь сказал: '{user_message}'. Ответь в духе пивного адвоката — весело, неформально и с подначкой, чтобы продолжить разговор о пиве."
    reply = await generate_text(prompt)
    await update.message.reply_text(reply)

# --- Команда для голосования ---
async def vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    set_active(chat_id, False)
    await update.message.reply_poll(
        question="Ну что, уговорил вас на пиво?",
        options=["Да, идём! 🍻", "Нет, сегодня без меня 😔"],
        is_anonymous=False
    )

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Произошла ошибка:", exc_info=context.error)

# --- Команда /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    add_group(chat_id)
    set_active(chat_id, True)
    reason = await generate_reason()
    await update.message.reply_text(
        f"Я — пивной адвокат! Каждый день буду мотивировать вас идти пить пиво. 🍻\n\n"
        f"👉 {reason}\n\n"
        f"Напишите /vote, чтобы проголосовать и закончить обсуждение."
    )

# --- Основной запуск ---
if __name__ == '__main__':
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: app.create_task(daily_motivation(app)), 'cron', hour=9, minute=0)
    scheduler.start()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("vote", vote_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), chat_response))

    app.add_error_handler(error_handler)

    logger.info("Бот запущен")
    app.run_polling()
