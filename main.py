import os
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

# ====================== LOGGING ======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ====================== CONFIG ======================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID_RAW = os.getenv("ALLOWED_TELEGRAM_USER_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PORT = int(os.getenv("PORT", 8080))

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

DB_NAME = "agent_vault.db"

# ====================== DATABASE ======================
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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS style_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_text TEXT,
            added_at TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS conversation_memory (
            user_id TEXT PRIMARY KEY,
            history TEXT,
            updated_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ====================== RSS FEEDS ======================
RSS_FEEDS = [
    {"source": "Feed Personalizzato BJ", "url": "https://rss.app/feeds/t5ooMu9TaY8RO77f.xml"},
    {"source": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"source": "MIT Tech Review", "url": "https://technologyreview.com/topic/artificial-intelligence/feed"},
]

# ====================== MODEL SELECTOR ======================
def get_model():
    """Prova i modelli Gemini disponibili in ordine di preferenza"""
    candidates = [
        "gemini-2.0-flash",
        "gemini-1.5-flash-latest",
        "gemini-2.5-flash",
        "gemini-1.5-pro-latest"
    ]
    for model_name in candidates:
        try:
            return genai.GenerativeModel(model_name)
        except Exception as e:
            logger.warning(f"Modello {model_name} non disponibile: {e}")
            continue
    raise Exception("Nessun modello Gemini disponibile. Controlla la API key e i modelli supportati.")

# ====================== CORE FUNCTIONS ======================
def is_authorized(uid) -> bool:
    if not ALLOWED_USER_ID_RAW:
        return True
    return str(uid) == str(ALLOWED_USER_ID_RAW)

def clear_all_memory():
    conn = sqlite3.connect(DB_NAME)
    conn.execute("DELETE FROM style_memory")
    conn.execute("DELETE FROM conversation_memory")
    conn.commit()
    conn.close()

def save_style_sample(text: str) -> int:
    if len(text.strip()) < 10:
        return 0
    conn = sqlite3.connect(DB_NAME)
    conn.execute(
        "INSERT INTO style_memory (sample_text, added_at) VALUES (?, ?)",
        (text.strip(), time.strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM style_memory").fetchone()[0]
    conn.close()
    return count

def get_style_samples(topic: str = None) -> str:
    conn = sqlite3.connect(DB_NAME)
    try:
        if topic and len(topic) > 3:
            rows = conn.execute(
                "SELECT sample_text FROM style_memory WHERE LOWER(sample_text) LIKE ? ORDER BY RANDOM() LIMIT 8",
                (f"%{topic.lower()}%",)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT sample_text FROM style_memory ORDER BY RANDOM() LIMIT 7"
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        return "Nessun esempio di stile disponibile."
    return "\n---\n".join([f"Post Reale di BJ:\n{r[0]}" for r in rows])

def save_conversation(user_id, history: str):
    conn = sqlite3.connect(DB_NAME)
    conn.execute(
        "INSERT OR REPLACE INTO conversation_memory (user_id, history, updated_at) VALUES (?, ?, ?)",
        (str(user_id), history, time.strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()

def load_conversation(user_id) -> str:
    conn = sqlite3.connect(DB_NAME)
    row = conn.execute(
        "SELECT history FROM conversation_memory WHERE user_id = ?",
        (str(user_id),)
    ).fetchone()
    conn.close()
    return row[0] if row else ""

def generate_ai_drafts(topic: str, lang: str = "both") -> str:
    style = get_style_samples(topic)
    prompt = f"""Sei il Ghostwriter di BJ. Tono naturale, tagliente e umano. Human Edge.
MEMORIA STILE:
{style}

TEMA: {topic}

Genera 3 opzioni di post (brevi, potenti, pronti da pubblicare)."""
    try:
        model = get_model()
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"Errore generazione AI: {e}")
        return f"Errore generazione: {e}"

def scan_feeds_manual() -> int:
    total = 0
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    for feed_info in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            for entry in feed.entries[:3]:
                pid = getattr(entry, "id", getattr(entry, "link", None))
                if not pid:
                    continue
                content = f"{getattr(entry, 'title', '')} - {getattr(entry, 'summary', '')}"[:500]
                cursor.execute(
                    "INSERT OR IGNORE INTO scraped_posts (id, author, content, published_at) VALUES (?, ?, ?, ?)",
                    (pid, feed_info["source"], content, getattr(entry, "published", time.ctime()))
                )
                if cursor.rowcount > 0:
                    total += 1
        except Exception as e:
            logger.warning(f"Errore feed {feed_info['source']}: {e}")
            continue
    conn.commit()
    conn.close()
    return total

async def scan_and_notify_feeds(bot_application):
    total_added = 0
    new_items = []
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    for feed_info in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            for entry in feed.entries[:2]:
                pid = getattr(entry, "id", getattr(entry, "link", None))
                if not pid:
                    continue
                content = f"{getattr(entry, 'title', '')} - {getattr(entry, 'summary', '')}"[:500]
                cursor.execute(
                    "INSERT OR IGNORE INTO scraped_posts (id, author, content, published_at) VALUES (?, ?, ?, ?)",
                    (pid, feed_info["source"], content, getattr(entry, "published", time.ctime()))
                )
                if cursor.rowcount > 0:
                    total_added += 1
                    new_items.append(content)
        except Exception as e:
            logger.warning(f"Errore scanning {feed_info['source']}: {e}")
            continue

    conn.commit()
    conn.close()

    if total_added > 0 and ALLOWED_USER_ID_RAW and GEMINI_API_KEY:
        await evaluate_and_poke_user(bot_application, new_items)

    return total_added

async def evaluate_and_poke_user(bot_application, new_items):
    joined_news = "\n---\n".join(new_items[:6])
    prompt = f"""Sei un editor molto esigente per BJ. Analizza queste notizie e scegli solo quelle forti sul Human Edge / AI + human residual.
Se nessuna è abbastanza forte rispondi solo con la parola SKIP.

Notizie:
{joined_news}"""
    try:
        model = get_model()
        text = model.generate_content(prompt).text.strip()
        if "skip" not in text.lower() and len(text) > 20:
            await bot_application.bot.send_message(
                chat_id=ALLOWED_USER_ID_RAW,
                text=f"🚨 *Phoenix Alert*\n\n{text}",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Errore evaluate_and_poke: {e}")

# ====================== TELEGRAM HANDLERS ======================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text("👋 BJ X Agent online. Pronto.")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text("🔄 Scansione in corso...")
    added = scan_feeds_manual()
    await update.message.reply_text(f"✅ Completata. Nuovi elementi: {added}")

async def learn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usa: /learn <testo da apprendere>")
        return
    total = save_style_sample(text)
    await update.message.reply_text(f"🧠 Stile appreso. Totale esempi: {total}")

async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    samples = get_style_samples()
    await update.message.reply_text(samples[:4000])

async def clear_memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    clear_all_memory()
    await update.message.reply_text("🧹 Memoria azzerata.")

async def it_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    topic = " ".join(context.args)
    if not topic:
        await update.message.reply_text("Usa: /it <tema>")
        return
    result = generate_ai_drafts(topic, "it")
    await update.message.reply_text(result)

async def en_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    topic = " ".join(context.args)
    if not topic:
        await update.message.reply_text("Usa: /en <tema>")
        return
    result = generate_ai_drafts(topic, "en")
    await update.message.reply_text(result)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    text = update.message.text.strip()
    user_id = update.effective_user.id
    history = load_conversation(user_id)

    if "genera i post" in text.lower():
        result = generate_ai_drafts(history or "Tema libero", "both")
        await update.message.reply_text(result)
        save_conversation(user_id, "")
        return

    prompt = f"""Sei il ghostwriter di BJ. Rispondi in modo naturale e tagliente (max 2-3 frasi).
Storico conversazione:
{history}

BJ: {text}"""

    try:
        model = get_model()
        reply = model.generate_content(prompt).text.strip()
        new_history = (history + f"\nBJ: {text}\nAI: {reply}")[-3000:]
        save_conversation(user_id, new_history)
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error(f"Errore handle_message: {e}")
        await update.message.reply_text(f"Errore: {e}")

# ====================== FLASK + BOT ======================
app = Flask(__name__)

@app.route("/")
def health():
    return "OK", 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)

async def run_bot():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN mancante")
        return

    bot = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Handlers
    bot.add_handler(CommandHandler("start", start_cmd))
    bot.add_handler(CommandHandler("scan", scan_cmd))
    bot.add_handler(CommandHandler("learn", learn_cmd))
    bot.add_handler(CommandHandler("memory", memory_cmd))
    bot.add_handler(CommandHandler("clear_memory", clear_memory_cmd))
    bot.add_handler(CommandHandler("it", it_cmd))
    bot.add_handler(CommandHandler("en", en_cmd))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Scheduler
    scheduler = BackgroundScheduler()
    loop = asyncio.get_event_loop()

    def scheduled_scan():
        asyncio.run_coroutine_threadsafe(scan_and_notify_feeds(bot), loop)

    scheduler.add_job(scheduled_scan, "interval", minutes=45)
    scheduler.start()

    await bot.initialize()
    await bot.start()
    await bot.updater.start_polling()
    logger.info("Bot avviato correttamente")

    while True:
        await asyncio.sleep(3600)

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()
