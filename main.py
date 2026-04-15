import os
import logging
import sqlite3
import requests
import tempfile
import subprocess
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from gtts import gTTS
from duckduckgo_search import DDGS
from openai import OpenAI
import pdfplumber
from docx import Document as DocxDocument
from bs4 import BeautifulSoup
import speech_recognition as sr

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

DB_PATH = "memory.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS conversations
                 (user_id INTEGER, role TEXT, content TEXT,
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def get_history(user_id, limit=15):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        'SELECT role, content FROM conversations WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?',
        (user_id, limit)
    )
    history = c.fetchall()
    conn.close()
    return list(reversed(history))

def save_message(user_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO conversations (user_id, role, content) VALUES (?, ?, ?)',
              (user_id, role, content))
    conn.commit()
    conn.close()

def clear_history(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM conversations WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def search_web(query):
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=3))
            if results:
                text = "\n".join([f"- {r['title']}: {r['body']}" for r in results])
                return f"Результаты поиска по запросу '{query}':\n{text}"
    except Exception as e:
        logger.error(f"Search error: {e}")
    return None

def read_url(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
            tag.decompose()
        text = soup.get_text(separator='\n', strip=True)
        lines = [line for line in text.split('\n') if line.strip()]
        return '\n'.join(lines[:100])
    except Exception as e:
        logger.error(f"URL read error: {e}")
    return None

def get_ai_response(user_id, user_message, context_info=None):
    history = get_history(user_id)

    system_prompt = (
        "Ты умный AI ассистент по имени Beka&_money бот. "
        "Отвечай всегда на русском языке. "
        "Ты помнишь историю разговора. "
        "Давай подробные и полезные ответы."
    )

    if context_info:
        system_prompt += f"\n\nДополнительная информация:\n{context_info}"

    messages = [{"role": "system", "content": system_prompt}]

    for role, content in history:
        if role == "Пользователь":
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "assistant", "content": content})

    messages.append({"role": "user", "content": user_message})

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        max_tokens=1000
    )
    return response.choices[0].message.content

async def send_voice(update, text):
    try:
        tts = gTTS(text=text[:500], lang='ru', slow=False)
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
            tts.save(f.name)
            fname = f.name
        with open(fname, 'rb') as audio:
            await update.message.reply_voice(voice=audio)
        os.unlink(fname)
    except Exception as e:
        logger.error(f"TTS error: {e}")
        await update.message.reply_text(text)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я твой AI ассистент.\n\n"
        "Вот что я умею:\n"
        "🧠 Отвечать на любые вопросы\n"
        "🌐 Искать в интернете (напиши 'поищи ...')\n"
        "🔗 Читать ссылки (просто отправь ссылку)\n"
        "📎 Читать PDF и Word файлы (загрузи файл)\n"
        "🎤 Отвечать голосом — /voice (вкл/выкл)\n"
        "🧹 Очистить память — /clear\n\n"
        "Напиши что-нибудь!"
    )

async def voice_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = context.user_data.get('voice_mode', False)
    context.user_data['voice_mode'] = not current
    mode = "включён 🎤" if not current else "выключен"
    await update.message.reply_text(f"Голосовой режим {mode}")

async def clear_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_history(update.effective_user.id)
    await update.message.reply_text("🧹 Память очищена!")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    await update.message.chat.send_action("typing")

    context_info = None

    if "http://" in text or "https://" in text:
        for word in text.split():
            if word.startswith("http"):
                await update.message.reply_text("🔗 Читаю ссылку...")
                url_content = read_url(word)
                if url_content:
                    context_info = f"Содержимое страницы ({word}):\n{url_content}"
                break
    elif any(kw in text.lower() for kw in ["поищи", "найди в интернете", "поиск", "погугли", "найди информацию"]):
        await update.message.reply_text("🌐 Ищу в интернете...")
        search_results = search_web(text)
        if search_results:
            context_info = search_results

    save_message(user_id, "Пользователь", text)

    try:
        response = get_ai_response(user_id, text, context_info)
        save_message(user_id, "Ассистент", response)

        if context.user_data.get('voice_mode', False):
            await send_voice(update, response)
        else:
            if len(response) > 4096:
                for i in range(0, len(response), 4096):
                    await update.message.reply_text(response[i:i+4096])
            else:
                await update.message.reply_text(response)
    except Exception as e:
        logger.error(f"AI error: {e}")
        await update.message.reply_text("Произошла ошибка. Попробуйте ещё раз.")

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text("🎤 Распознаю голосовое...")

    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)

        with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as f:
            await file.download_to_drive(f.name)
            ogg_path = f.name

        wav_path = ogg_path.replace('.ogg', '.wav')
        subprocess.run(['ffmpeg', '-i', ogg_path, wav_path, '-y'], capture_output=True)

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)

        recognized_text = recognizer.recognize_google(audio, language='ru-RU')

        os.unlink(ogg_path)
        os.unlink(wav_path)

        await update.message.reply_text(f'Вы сказали: "{recognized_text}"')

        save_message(user_id, "Пользователь", recognized_text)
        response = get_ai_response(user_id, recognized_text)
        save_message(user_id, "Ассистент", response)

        await send_voice(update, response)

    except sr.UnknownValueError:
        await update.message.reply_text("Не удалось распознать речь. Говорите чётче.")
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text("Ошибка при обработке голосового сообщения.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    doc = update.message.document

    if not doc.file_name.endswith(('.pdf', '.docx')):
        await update.message.reply_text("Поддерживаю только PDF и Word (.docx) файлы.")
        return

    await update.message.reply_text("📎 Читаю файл...")

    try:
        file = await context.bot.get_file(doc.file_id)

        suffix = '.pdf' if doc.file_name.endswith('.pdf') else '.docx'
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            await file.download_to_drive(f.name)
            fname = f.name

        text = ""
        if suffix == '.pdf':
            with pdfplumber.open(fname) as pdf:
                for page in pdf.pages[:10]:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"
        else:
            doc_obj = DocxDocument(fname)
            text = "\n".join([p.text for p in doc_obj.paragraphs if p.text.strip()])

        os.unlink(fname)

        if not text.strip():
            await update.message.reply_text("Не удалось прочитать текст из файла.")
            return

        save_message(user_id, "Пользователь", f"[Загружен файл: {doc.file_name}]")
        response = get_ai_response(
            user_id,
            "Проанализируй этот документ, расскажи о чём он и сделай краткое резюме.",
            context_info=f"Содержимое файла '{doc.file_name}':\n{text[:4000]}"
        )
        save_message(user_id, "Ассистент", response)

        if len(response) > 4096:
            for i in range(0, len(response), 4096):
                await update.message.reply_text(response[i:i+4096])
        else:
            await update.message.reply_text(response)

    except Exception as e:
        logger.error(f"Document error: {e}")
        await update.message.reply_text("Ошибка при чтении файла.")

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("voice", voice_toggle))
    app.add_handler(CommandHandler("clear", clear_memory))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
