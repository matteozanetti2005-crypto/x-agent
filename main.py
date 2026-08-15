import os
import time
import sqlite3
import threading
import logging
import feedparser
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import google.generativeai as genai
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# Configurazione logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Variabili d'ambiente
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = os.getenv("ALLOWED_TELEGRAM_USER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 8080))

if ALLOWED_USER_ID:
    try:
        ALLOWED_USER_ID = int(ALLOWED_USER_ID)
    except ValueError:
        logger.error("ALLOWED_TELEGRAM_USER_ID non valido. Inserire solo cifre.")

# Inizializzazione Gemini AI
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# Configurazione Database SQLite
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

# Feed da monitorare
ACCOUNTS = ["sama", "karpathy", "ylecun", "paulg", "drjimfan"]
NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.woodland.cafe"
]

def scan_feeds():
    logger.info("Avvio scansione feed su X...")
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
            except Exception as e:
                logger.warning(f"Errore scansione {acc} su {inst}: {e}")
                continue
        if not scraped:
            logger.warning(f"Impossibile scansionare l'account @{acc} su tutte le istanze.")

    conn.commit()
    conn.close()
    logger.info(f"Scansione completata. Nuovi post aggiunti: {total_added}")
    return total_added

def generate_ai_drafts(prompt_topic: str) -> str:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT author, content FROM scraped_posts ORDER BY ROWID DESC LIMIT 8")
    rows = cursor.fetchall()
    conn.close()

    context_text = "\n---\n".join([f"Autore @{r[0]}: {r[1]}" for r in rows]) if rows else "Nessun dato recente."

    full_prompt = f"""
Sei un Ghostwriter e Stratega di Contenuti di alto livello specializzato su X (Twitter).
La tua voce unisce intelligenza artificiale, creatività, e il concetto del "Human Edge" con uno stile autorevole, chiaro e coinvolgente.

Questi sono alcuni spunti e trend catturati di recente su X:
{context_text}

Richiesta dell'utente:
"{prompt_topic}"

Genera esattamente 3 opzioni di post (o thread) per X:
1. OPZIONE 1: Hook magnetico + intuizione concisa e diretta.
2. OPZIONE 2: Post di analisi approfondita con ritmo incalzante.
3. OPZIONE 3: Prospettiva controintuitiva o provocazione costruttiva (Human Edge).

Fornisci direttamente le 3 opzioni pronte da pubblicare, senza convenevoli.
"""
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(full_prompt)
        return response.text
    except Exception as e:
        logger.error(f"Errore Gemini: {e}")
        return f"⚠️ Errore durante la generazione dei contenuti: {e}"

# Handlers Telegram
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(
        "👋 **BJ X Agent è Online!**\n\n"
        "Comandi disponibili:\n"
        "• `/scan` : Forza una scansione immediata dei post su X.\n"
        "• Invia qualsiasi messaggio di testo per generare 3 bozze su quel tema.",
        parse_mode="Markdown"
    )

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text("🔄 Scansione in corso sui profili target di X...")
    added = scan_feeds()
    await update.message.reply_text(f"✅ Scansione completata!\nNuovi post archiviati nel database: {added}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    user_topic = update.message.text
    await update.message.reply_text("🧠 Analizzo i dati raccolti ed elaboro le bozze virali...")
    result = generate_ai_drafts(user_topic)
    await update.message.reply_text(result)

# Server Web Flask per mantenere attivo Render
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Agent running", 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

def main():
    # Avvia Flask in un thread secondario
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Avvia lo Scheduler in background (ogni 45 minuti)
    scheduler = BackgroundScheduler()
    scheduler.add_job(scan_feeds, 'interval', minutes=45)
    scheduler.start()

    # Avvia l'Agente Telegram
    bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start_cmd))
    bot_app.add_handler(CommandHandler("scan", scan_cmd))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot avviato e in ascolto...")
    bot_app.run_polling()

if __name__ == "__main__":
    main()
