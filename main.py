import os
import sys
import time
import sqlite3
import threading
import logging
import asyncio
import feedparser
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import google.generativeai as genai
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Variabili d'ambiente
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID_RAW = os.getenv("ALLOWED_TELEGRAM_USER_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PORT = int(os.getenv("PORT", 8080))

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# Database
DB_NAME = "agent_vault.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scraped_posts (
            id TEXT PRIMARY KEY,
            author TEXT,
            content TEXT,
            published_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# Scraper
ACCOUNTS = ["sama", "karpathy", "ylecun", "paulg", "drjimfan"]
NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.woodland.cafe"
]

def scan_feeds():
    logger.info("Scansione X...")
    total_added = 0
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    for acc in ACCOUNTS:
        scraped = False
        for inst in NITTER_INSTANCES:
            url = f"{inst}/{acc}/rss"
            try:
                feed = feedparser.parse(url)
                if feed.entries:
                    for entry in feed.entries[:5]:
                        p_id = getattr(entry, 'id', entry.link)
                        content = getattr(entry, 'summary', entry.title)
                        pub = getattr(entry, 'published', time.ctime())
                        
                        cursor.execute(
                            "INSERT OR IGNORE INTO scraped_posts (id, author, content, published_at) VALUES (?, ?, ?, ?)",
                            (p_id, acc, content, pub)
                        )
                        if cursor.rowcount > 0:
                            total_added += 1
                    scraped = True
                    break
            except Exception:
                continue
    conn.commit()
    conn.close()
    return total_added

def generate_ai_drafts(prompt_topic: str) -> str:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT author, content FROM scraped_posts ORDER BY ROWID DESC LIMIT 8")
    rows = cursor.fetchall()
    conn.close()

    context_text = "\n---\n".join([f"Autore @{r[0]}: {r[1]}" for r in rows]) if rows else "Nessun dato recente."

    full_prompt = f"""
Sei un Ghostwriter esperto per X (Twitter).
Unione di AI, creatività e Human Edge.

Dati recenti:
{context_text}

Richiesta:
"{prompt_topic}"

Genera 3 bozze pronte per X (Hook magnetico, analisi approfondita, prospettiva Human Edge).
"""
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(full_prompt)
        return response.text
    except Exception as e:
        return f"⚠️ Errore Gemini: {e}"

def is_authorized(user_id) -> bool:
    if not ALLOWED_USER_ID_RAW:
        return True
    return str(user_id) == str(ALLOWED_USER_ID_RAW)

# Telegram Handlers
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text(f"⛔ Non autorizzato. Il tuo ID è: {user_id}")
        return
    await update.message.reply_text(
        "👋 BJ X Agent è Online!\n\n"
        "Comandi:\n"
        "• /scan : Scansione immediata dei post su X\n"
        "• Scrivi qualsiasi tema per generare 3 bozze"
    )

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    await update.message.reply_text("🔄 Scansione in corso...")
    added = scan_feeds()
    await update.message.reply_text(f"✅ Scansione terminata! Post archiviati: {added}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    user_topic = update.message.text
    await update.message.reply_text("🧠 Elaboro le bozze virali...")
    result = generate_ai_drafts(user_topic)
    await update.message.reply_text(result)

# Web Server Flask per Render
app = Flask(__name__)

@app.route('/')
def health():
    return "OK", 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

async def run_bot():
    bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start_cmd))
    bot_app.add_handler(CommandHandler("scan", scan_cmd))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    logger.info("Bot Telegram avviato con successo!")
    
    # Mantiene il loop asincrono in vita
    while True:
        await asyncio.sleep(3600)

def main():
    # 1. Avvia Flask in background
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # 2. Avvia lo Scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(scan_feeds, 'interval', minutes=45)
    scheduler.start()

    # 3. Avvia il loop asincrono nativo per Telegram
    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Chiusura servizio.")

if __name__ == "__main__":
    main()
