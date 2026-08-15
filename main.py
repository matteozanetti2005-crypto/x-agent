cat << 'EOF' > main.py
import os
import asyncio
import logging
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
from ntscraper import Nitter
from google import genai
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = os.getenv("ALLOWED_TELEGRAM_USER_ID")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

ai_client = genai.Client(api_key=GEMINI_KEY)
DB_NAME = "viral_memory.db"

SEARCH_TERMS = [
    "AI tools",
    "intelligenza artificiale",
    "build in public",
    "creator economy"
]
MIN_LIKES = 25

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS viral_posts (
            id TEXT PRIMARY KEY,
            text TEXT,
            likes INTEGER,
            retweets INTEGER,
            collected_at TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def save_posts(posts):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    saved = 0
    for p in posts:
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO viral_posts (id, text, likes, retweets, collected_at)
                VALUES (?, ?, ?, ?, ?)
            """, (p['id'], p['text'], p['likes'], p['retweets'], datetime.now()))
            if cursor.rowcount > 0:
                saved += 1
        except Exception:
            continue
    conn.commit()
    conn.close()
    return saved

def get_top_posts(limit=8):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT text, likes, retweets FROM viral_posts
        ORDER BY (likes + retweets * 2) DESC, collected_at DESC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [{"text": r[0], "likes": r[1], "retweets": r[2]} for r in rows]

def get_post_count():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM viral_posts")
    count = cursor.fetchone()[0]
    conn.close()
    return count

def scan_x():
    logging.info("Scansione in background di X in corso...")
    scraper = Nitter(log_level=0)
    found = []

    for term in SEARCH_TERMS:
        try:
            results = scraper.get_tweets(term, mode='term', number=15)
            tweets = results.get('tweets', [])
            for t in tweets:
                stats = t.get('stats', {})
                likes = stats.get('likes', 0)
                retweets = stats.get('retweets', 0)
                text = t.get('text', '')
                link = t.get('link', '')

                if likes >= MIN_LIKES and len(text) > 40:
                    t_id = link.split('/')[-1] if link else str(hash(text))
                    found.append({
                        "id": t_id,
                        "text": text,
                        "likes": likes,
                        "retweets": retweets
                    })
        except Exception as e:
            logging.error(f"Errore scansione per '{term}': {e}")
            continue

    saved = save_posts(found)
    logging.info(f"Scansione terminata: {saved} nuovi post salvati nel DB.")

def is_authorized(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == str(ALLOWED_USER_ID)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    msg = (
        "🤖 **Agente Viral X Attivo**\n\n"
        "Monitoro X in background e salvo i post con metriche elevate.\n\n"
        "Comandi:\n"
        "• `/status` - Post totali in memoria\n"
        "• `/scan` - Forza una scansione ora\n"
        "• `/pattern` - Analisi degli hook più efficaci\n\n"
        "Oppure **inviami direttamente un tema** per generare 3 bozze di post."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    count = get_post_count()
    await update.message.reply_text(f"📊 Nel database ci sono **{count} post virali** memorizzati.")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("🔍 Avvio scansione istantanea su X...")
    scan_x()
    count = get_post_count()
    await update.message.reply_text(f"✅ Scansione completata. Totale post nel DB: **{count}**.")

async def pattern_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    posts = get_top_posts(limit=8)
    if not posts:
        await update.message.reply_text("⚠️ Nessun dato sufficiente. Esegui prima `/scan`.")
        return

    await update.message.reply_text("🧠 Analisi pattern in corso...")
    posts_text = "\n---\n".join([f"[{p['likes']} likes]\n{p['text']}" for p in posts])
    
    prompt = f"""
Sei uno stratega di crescita per X.
Analizza questi post ad alto engagement:

{posts_text}

1. Estrai i 3 modelli di Hook (prima riga) più efficaci.
2. Descrivi la struttura e il ritmo visivo dei testi.
3. Fornisci 3 consigli pratici per massimizzare bookmark e repost.
"""
    res = ai_client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    await update.message.reply_text(res.text)

async def handle_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    topic = update.message.text
    posts = get_top_posts(limit=6)

    if not posts:
        await update.message.reply_text("⏳ Primo avvio: raccolgo dati da X prima di generare...")
        scan_x()
        posts = get_top_posts(limit=6)

    await update.message.reply_text(f"✍️ Elaboro 3 bozze sul tema: *\"{topic}\"*...", parse_mode="Markdown")

    context_str = "\n---\n".join([f"[{p['likes']} likes]\n{p['text']}" for p in posts]) if posts else "Nessun post di riferimento."
    prompt = f"""
Sei un copywriter d'élite per X.
Ecco alcuni post performanti estratti dalla piattaforma:

{context_str}

TASK:
Genera 3 post originali pronti da pubblicare su X sul seguente tema: "{topic}".

Linee guida:
- Usa le formule di hook e il ritmo emersi dai post di riferimento.
- Niente hashtag generici o cliché.
- Struttura ottimizzata per lettura da mobile (spaziature, punti chiave).

Formatta l'output con:
- **Bozza 1 (Contrarian / Gancio contro-intuitivo)**
- **Bozza 2 (Framework pratico / Elenco ad alto valore)**
- **Bozza 3 (Storytelling / Visione diretta)**
"""
    res = ai_client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    await update.message.reply_text(res.text)

def main():
    init_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(scan_x, "interval", minutes=45)
    scheduler.start()

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("pattern", pattern_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_topic))

    logging.info("Agente avviato con successo.")
    app.run_polling()

if __name__ == "__main__":
    main()
EOF
