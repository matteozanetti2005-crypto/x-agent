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

# Feed RSS stabili + Istanze per profili X
RSS_FEEDS = [
    {"source": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"source": "The Verge AI", "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"},
    {"source": "MIT Tech Review", "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed"},
    {"source": "Hacker News", "url": "https://hnrss.org/frontpage"},
    {"source": "ArXiv AI", "url": "http://export.arxiv.org/rss/cs.AI"}
]

X_ACCOUNTS = ["sama", "karpathy", "ylecun", "paulg", "drjimfan"]
NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.woodland.cafe"
]

def scan_feeds():
    logger.info("Avvio scansione feed RSS & X...")
    total_added = 0
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 1. Scansione feed RSS
    for feed_info in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            if feed.entries:
                for entry in feed.entries[:4]:
                    p_id = getattr(entry, 'id', getattr(entry, 'link', ''))
                    title = getattr(entry, 'title', '')
                    summary = getattr(entry, 'summary', '')
                    content = f"{title} - {summary}"[:500]
                    pub = getattr(entry, 'published', time.ctime())

                    if p_id:
                        cursor.execute(
                            "INSERT OR IGNORE INTO scraped_posts (id, author, content, published_at) VALUES (?, ?, ?, ?)",
                            (p_id, feed_info["source"], content, pub)
                        )
                        if cursor.rowcount > 0:
                            total_added += 1
        except Exception:
            continue

    # 2. Scansione mirror X
    for acc in X_ACCOUNTS:
        for inst in NITTER_INSTANCES:
            url = f"{inst}/{acc}/rss"
            try:
                feed = feedparser.parse(url)
                if feed.entries:
                    for entry in feed.entries[:3]:
                        p_id = getattr(entry, 'id', getattr(entry, 'link', ''))
                        content = getattr(entry, 'summary', getattr(entry, 'title', ''))[:400]
                        pub = getattr(entry, 'published', time.ctime())
                        
                        if p_id:
                            cursor.execute(
                                "INSERT OR IGNORE INTO scraped_posts (id, author, content, published_at) VALUES (?, ?, ?, ?)",
                                (p_id, f"@{acc}", content, pub)
                            )
                            if cursor.rowcount > 0:
                                total_added += 1
                    break
            except Exception:
                continue

    conn.commit()
    conn.close()
    logger.info(f"Scansione completata. Nuovi elementi inseriti: {total_added}")
    return total_added

def generate_ai_drafts(prompt_topic: str, lang_mode: str = "both") -> str:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT author, content FROM scraped_posts ORDER BY ROWID DESC LIMIT 8")
    rows = cursor.fetchall()
    conn.close()

    context_text = "\n---\n".join([f"Fonte [{r[0]}]: {r[1]}" for r in rows]) if rows else "Nessun dato di contesto recente."

    if lang_mode == "it":
        output_instruction = """
Genera 3 opzioni per X rigorosamente in ITALIANO:
1. OPZIONE 1 (IT): Hook magnetico + concetto sintetico e diretto.
2. OPZIONE 2 (IT): Post di analisi approfondita a ritmo serrato e riflessivo.
3. OPZIONE 3 (IT): Prospettiva controintuitiva / provocazione costruttiva (Human Edge).
"""
    elif lang_mode == "en":
        output_instruction = """
Generate 3 options for X strictly in ENGLISH (natural, impactful, no fluff):
1. OPTION 1 (EN): Magnetic hook + concise direct insight.
2. OPTION 2 (EN): In-depth analytical post with fast-paced cadence.
3. OPTION 3 (EN): Counter-intuitive take / constructive provocation (Human Edge).
"""
    else:
        output_instruction = """
Genera 3 opzioni per X fornendo per ciascuna SIA la versione ITALIANA che la traduzione fluida in INGLESE:

**OPZIONE 1 / OPTION 1**
🇮🇹 IT:
[Testo italiano - Hook magnetico]

🇬🇧 EN:
[English translation]

***

**OPZIONE 2 / OPTION 2**
🇮🇹 IT:
[Testo italiano - Analisi approfondita]

🇬🇧 EN:
[English translation]

***

**OPZIONE 3 / OPTION 3 (Human Edge)**
🇮🇹 IT:
[Testo italiano - Angolo controintuitivo]

🇬🇧 EN:
[English translation]
"""

    full_prompt = f"""
Sei il Ghostwriter e Stratega personale di BJ (@BJ_Beyond), studioso di intelligenza artificiale, autore e appassionato d'arte contemporanea.
Scrivi post per X (Twitter) rispecchiando esattamente il suo tono di voce: autorevole, tagliente, sintetico e focalizzato sul "Human Edge" (il valore insostituibile dell'anima, dell'intenzione e della creatività umana nell'era dell'automazione).

I pilastri della comunicazione di BJ:
1. Human Edge: l'AI amplifica ma non sostituisce l'anima, l'intuizione e il tocco umano.
2. Arte e Creatività: rispetto per il processo autentico e la visione estetica.
3. Approccio 'Building in public': trasparenza, sperimentazione pratica, no fuffa.

Ultime notizie e trend raccolti:
{context_text}

Tema inviato da BJ:
"{prompt_topic}"

{output_instruction}

Fornisci direttamente l'output pronto per il copia-incolla, senza frasi introduttive o di chiusura.
"""
    try:
        supported_models = [
            m.name for m in genai.list_models()
            if 'generateContent' in m.supported_generation_methods
        ]
        
        flash_models = [m for m in supported_models if 'flash' in m.lower()]
        target_models = flash_models if flash_models else supported_models

        last_error = None
        for mod_name in target_models:
            try:
                model = genai.GenerativeModel(mod_name)
                response = model.generate_content(full_prompt)
                if response and response.text:
                    return response.text
            except Exception as inner_err:
                last_error = inner_err
                continue

        return f"⚠️ Errore generazione: {last_error}"
    except Exception as e:
        logger.error(f"Errore Gemini API: {e}")
        return f"⚠️ Errore Gemini API: {e}"

def is_authorized(user_id) -> bool:
    if not ALLOWED_USER_ID_RAW:
        return True
    return str(user_id) == str(ALLOWED_USER_ID_RAW)

# Telegram Handlers
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text(f"⛔ Non autorizzato. ID: {user_id}")
        return
    await update.message.reply_text(
        "👋 **BJ X Agent (Multilingua) è Online!**\n\n"
        "Comandi disponibili:\n"
        "• `/scan` : Scansiona i feed e aggiorna le fonti\n"
        "• Scrivi qualsiasi tema per ricevere le bozze in **IT + EN**\n"
        "• `/it <tema>` : Genera solo in Italiano\n"
        "• `/en <tema>` : Genera solo in Inglese",
        parse_mode="Markdown"
    )

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    await update.message.reply_text("🔄 Scansione in corso sui feed AI e profili target...")
    added = scan_feeds()
    await update.message.reply_text(f"✅ Scansione completata!\nNuovi elementi archiviati: {added}")

async def it_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    user_topic = " ".join(context.args)
    if not user_topic:
        await update.message.reply_text("⚠️ Specifica un tema. Es: `/it L'anima dell'arte nell'AI`", parse_mode="Markdown")
        return
    await update.message.reply_text("🧠 Elaboro bozze in Italiano...")
    result = generate_ai_drafts(user_topic, lang_mode="it")
    await update.message.reply_text(result)

async def en_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    user_topic = " ".join(context.args)
    if not user_topic:
        await update.message.reply_text("⚠️ Specifica un tema. Es: `/en The Human Edge in AI era`", parse_mode="Markdown")
        return
    await update.message.reply_text("🧠 Elaboro bozze in Inglese...")
    result = generate_ai_drafts(user_topic, lang_mode="en")
    await update.message.reply_text(result)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    user_topic = update.message.text
    await update.message.reply_text("🧠 Elaboro le bozze virali bilingue (IT + EN)...")
    result = generate_ai_drafts(user_topic, lang_mode="both")
    await update.message.reply_text(result)

# Server Flask
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
    bot_app.add_handler(CommandHandler("it", it_cmd))
    bot_app.add_handler(CommandHandler("en", en_cmd))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    logger.info("Bot Telegram multilingua in ascolto...")
    
    while True:
        await asyncio.sleep(3600)

def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    scheduler = BackgroundScheduler()
    scheduler.add_job(scan_feeds, 'interval', minutes=45)
    scheduler.start()

    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Chiusura servizio.")

if __name__ == "__main__":
    main()
