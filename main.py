import os
import time
import json
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
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ====================== CONFIG ======================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID_RAW = os.getenv("ALLOWED_TELEGRAM_USER_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PORT = int(os.getenv("PORT", 8080))

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

DB_NAME = "agent_vault_v2.db"

# ====================== DATABASE ======================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    # Esempi di stile con score
    c.execute('''
        CREATE TABLE IF NOT EXISTS style_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_text TEXT,
            score REAL DEFAULT 1.0,
            times_used INTEGER DEFAULT 0,
            last_used TEXT,
            added_at TEXT
        )
    ''')

    # Preferenze strutturali (regole di stile)
    c.execute('''
        CREATE TABLE IF NOT EXISTS preferences (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
    ''')

    # Memoria di conversazione
    c.execute('''
        CREATE TABLE IF NOT EXISTS conversation_memory (
            user_id TEXT PRIMARY KEY,
            history TEXT,
            updated_at TEXT
        )
    ''')

    # Post scrapati
    c.execute('''
        CREATE TABLE IF NOT EXISTS scraped_posts (
            id TEXT PRIMARY KEY,
            author TEXT,
            content TEXT,
            published_at TEXT
        )
    ''')

    # System prompt dinamico
    c.execute('''
        CREATE TABLE IF NOT EXISTS system_prompt (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            prompt TEXT,
            updated_at TEXT
        )
    ''')

    # Inizializza preferenze di default se non esistono
    defaults = {
        "tone": "naturale, tagliente, umano, Human Edge",
        "max_length": "brevi (max 280-320 caratteri se possibile)",
        "banned_phrases": "Most people, In today's world, It's important to note",
        "preferred_structure": "diretto, no giri di parole, preferibilmente 1-3 frasi forti",
        "language_default": "italiano quando parli con me, inglese quando genero post per X"
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO preferences (key, value, updated_at) VALUES (?, ?, ?)",
                  (k, v, time.strftime("%Y-%m-%d %H:%M:%S")))

    # System prompt di base
    base_prompt = """Sei il ghostwriter ufficiale di BJ.
Tono: naturale, tagliente, umano. Human Edge.
Non suonare mai come un AI generico.
Usa gli esempi di stile forniti come riferimento forte.
Rispondi sempre in modo diretto e con personalità."""
    c.execute("INSERT OR IGNORE INTO system_prompt (id, prompt, updated_at) VALUES (1, ?, ?)",
              (base_prompt, time.strftime("%Y-%m-%d %H:%M:%S")))

    conn.commit()
    conn.close()

init_db()

# ====================== MODEL ======================
def get_model():
    candidates = [
        "gemini-3.6-flash",
        "gemini-2.5-flash",
        "gemini-1.5-flash-latest",
        "gemini-1.5-pro-latest"
    ]
    for name in candidates:
        try:
            return genai.GenerativeModel(name)
        except Exception as e:
            logger.warning(f"Modello {name} non disponibile: {e}")
    raise Exception("Nessun modello Gemini disponibile")

# ====================== PREFERENCES ======================
def get_preference(key: str, default: str = "") -> str:
    conn = sqlite3.connect(DB_NAME)
    row = conn.execute("SELECT value FROM preferences WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default

def set_preference(key: str, value: str):
    conn = sqlite3.connect(DB_NAME)
    conn.execute(
        "INSERT OR REPLACE INTO preferences (key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, time.strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()

def get_all_preferences() -> dict:
    conn = sqlite3.connect(DB_NAME)
    rows = conn.execute("SELECT key, value FROM preferences").fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}

# ====================== STYLE MEMORY ======================
def save_style_sample(text: str, initial_score: float = 1.0) -> int:
    if len(text.strip()) < 15:
        return 0
    conn = sqlite3.connect(DB_NAME)
    conn.execute(
        "INSERT INTO style_memory (sample_text, score, added_at) VALUES (?, ?, ?)",
        (text.strip(), initial_score, time.strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM style_memory").fetchone()[0]
    conn.close()
    return count

def update_style_score(sample_id: int, delta: float):
    conn = sqlite3.connect(DB_NAME)
    conn.execute(
        "UPDATE style_memory SET score = score + ?, times_used = times_used + 1, last_used = ? WHERE id = ?",
        (delta, time.strftime("%Y-%m-%d %H:%M:%S"), sample_id)
    )
    conn.commit()
    conn.close()

def get_best_style_samples(topic: str = None, limit: int = 6) -> str:
    conn = sqlite3.connect(DB_NAME)
    if topic and len(topic) > 3:
        rows = conn.execute('''
            SELECT id, sample_text, score FROM style_memory 
            WHERE LOWER(sample_text) LIKE ? 
            ORDER BY score DESC, times_used DESC 
            LIMIT ?
        ''', (f"%{topic.lower()}%", limit)).fetchall()
    else:
        rows = conn.execute('''
            SELECT id, sample_text, score FROM style_memory 
            ORDER BY score DESC, times_used DESC 
            LIMIT ?
        ''', (limit,)).fetchall()
    conn.close()

    if not rows:
        return "Nessun esempio di stile di alta qualità disponibile."

    # Aggiorna times_used
    conn = sqlite3.connect(DB_NAME)
    for r in rows:
        conn.execute("UPDATE style_memory SET times_used = times_used + 1, last_used = ? WHERE id = ?",
                     (time.strftime("%Y-%m-%d %H:%M:%S"), r[0]))
    conn.commit()
    conn.close()

    return "\n---\n".join([f"[Score {r[2]:.1f}] {r[1]}" for r in rows])

# ====================== SYSTEM PROMPT ======================
def get_system_prompt() -> str:
    conn = sqlite3.connect(DB_NAME)
    row = conn.execute("SELECT prompt FROM system_prompt WHERE id = 1").fetchone()
    conn.close()
    return row[0] if row else "Sei il ghostwriter di BJ."

def update_system_prompt(new_prompt: str):
    conn = sqlite3.connect(DB_NAME)
    conn.execute(
        "UPDATE system_prompt SET prompt = ?, updated_at = ? WHERE id = 1",
        (new_prompt, time.strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()

def evolve_system_prompt():
    """Fa evolvere il system prompt basandosi sulle preferenze e sugli esempi migliori"""
    prefs = get_all_preferences()
    best_samples = get_best_style_samples(limit=4)

    prompt = f"""Sei un meta-agente che migliora il system prompt di un ghostwriter.
Analizza le preferenze attuali e gli esempi di stile migliori.
Riscrivi un system prompt più preciso, potente e fedele allo stile di BJ.
Rispondi SOLO con il nuovo system prompt, nient'altro.

Preferenze attuali:
{json.dumps(prefs, indent=2, ensure_ascii=False)}

Esempi di stile migliori:
{best_samples}
"""
    try:
        model = get_model()
        new_prompt = model.generate_content(prompt).text.strip()
        if len(new_prompt) > 80:
            update_system_prompt(new_prompt)
            return True
    except Exception as e:
        logger.error(f"Errore evolve_system_prompt: {e}")
    return False

# ====================== CONVERSATION ======================
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
    row = conn.execute("SELECT history FROM conversation_memory WHERE user_id = ?", (str(user_id),)).fetchone()
    conn.close()
    return row[0] if row else ""

# ====================== GENERAZIONE ======================
def generate_ai_drafts(topic: str) -> str:
    style = get_best_style_samples(topic)
    prefs = get_all_preferences()
    system = get_system_prompt()

    prompt = f"""{system}

PREFERENZE ATTUALI:
{json.dumps(prefs, indent=2, ensure_ascii=False)}

ESEMPI DI STILE DI ALTA QUALITÀ:
{style}

TEMA RICHIESTO: {topic}

Genera 3 opzioni di post. 
Devono essere potenti, fedeli allo stile e rispettare le preferenze.
Numerale 1. 2. 3.
"""
    try:
        model = get_model()
        return model.generate_content(prompt).text.strip()
    except Exception as e:
        logger.error(f"Errore generate: {e}")
        return f"Errore generazione: {e}"

# ====================== FEEDBACK ======================
def apply_feedback(text: str, is_good: bool):
    """Cerca il testo più simile e aggiorna lo score"""
    conn = sqlite3.connect(DB_NAME)
    rows = conn.execute("SELECT id, sample_text FROM style_memory").fetchall()
    conn.close()

    best_id = None
    best_score = 0
    text_lower = text.lower()

    for rid, sample in rows:
        # matching grezzo ma efficace
        common = len(set(text_lower.split()) & set(sample.lower().split()))
        if common > best_score:
            best_score = common
            best_id = rid

    if best_id:
        delta = 0.35 if is_good else -0.45
        update_style_score(best_id, delta)
        return True
    return False

# ====================== RSS (invariato) ======================
RSS_FEEDS = [
    {"source": "Feed Personalizzato BJ", "url": "https://rss.app/feeds/t5ooMu9TaY8RO77f.xml"},
    {"source": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"source": "MIT Tech Review", "url": "https://technologyreview.com/topic/artificial-intelligence/feed"},
]

def scan_feeds_manual() -> int:
    total = 0
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    for f in RSS_FEEDS:
        try:
            feed = feedparser.parse(f["url"])
            for entry in feed.entries[:3]:
                pid = getattr(entry, "id", getattr(entry, "link", None))
                if not pid:
                    continue
                content = f"{getattr(entry, 'title', '')} - {getattr(entry, 'summary', '')}"[:500]
                c.execute("INSERT OR IGNORE INTO scraped_posts VALUES (?,?,?,?)",
                          (pid, f["source"], content, getattr(entry, "published", time.ctime())))
                if c.rowcount > 0:
                    total += 1
        except Exception as e:
            logger.warning(f"Errore feed: {e}")
    conn.commit()
    conn.close()
    return total

async def scan_and_notify_feeds(bot_application):
    # (stesso codice di prima, semplificato)
    total_added = scan_feeds_manual()
    return total_added

# ====================== HANDLERS ======================
def is_authorized(uid) -> bool:
    if not ALLOWED_USER_ID_RAW:
        return True
    return str(uid) == str(ALLOWED_USER_ID_RAW)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        "👋 *BJ Agent v2 Evolutivo* online.\n\n"
        "Comandi principali:\n"
        "/learn <testo> → impara stile\n"
        "/buono <testo> → rinforza\n"
        "/scarta <testo> → penalizza\n"
        "/preferenza <chiave> <valore>\n"
        "/evolve → fa evolvere il system prompt\n"
        "/memory → mostra memoria stile\n"
        "/prefs → mostra preferenze\n"
        "/it o /en <tema> → genera post",
        parse_mode="Markdown"
    )

async def learn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usa: /learn <testo>")
        return
    total = save_style_sample(text)
    await update.message.reply_text(f"🧠 Stile appreso. Totale esempi: {total}")

async def buono_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usa: /buono <testo da rinforzare>")
        return
    ok = apply_feedback(text, is_good=True)
    await update.message.reply_text("✅ Rinforzato." if ok else "⚠️ Non ho trovato un match abbastanza vicino.")

async def scarta_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usa: /scarta <testo da penalizzare>")
        return
    ok = apply_feedback(text, is_good=False)
    await update.message.reply_text("🗑️ Penalizzato." if ok else "⚠️ Non ho trovato un match abbastanza vicino.")

async def preferenza_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usa: /preferenza <chiave> <valore>\nEsempio: /preferenza tone più cinico e diretto")
        return
    key = context.args[0]
    value = " ".join(context.args[1:])
    set_preference(key, value)
    await update.message.reply_text(f"⚙️ Preferenza aggiornata:\n`{key}` → {value}", parse_mode="Markdown")

async def prefs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    prefs = get_all_preferences()
    text = "\n".join([f"• *{k}*: {v}" for k, v in prefs.items()])
    await update.message.reply_text(f"*Preferenze attuali:*\n\n{text}", parse_mode="Markdown")

async def evolve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text("🧬 Sto facendo evolvere il system prompt...")
    success = evolve_system_prompt()
    if success:
        await update.message.reply_text("✅ System prompt evoluto.")
    else:
        await update.message.reply_text("❌ Evoluzione fallita.")

async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    samples = get_best_style_samples(limit=8)
    await update.message.reply_text(samples[:3900])

async def it_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    topic = " ".join(context.args)
    if not topic:
        await update.message.reply_text("Usa: /it <tema>")
        return
    result = generate_ai_drafts(topic)
    await update.message.reply_text(result)

async def en_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    topic = " ".join(context.args)
    if not topic:
        await update.message.reply_text("Usa: /en <tema>")
        return
    result = generate_ai_drafts(topic)
    await update.message.reply_text(result)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    text = update.message.text.strip()
    user_id = update.effective_user.id
    history = load_conversation(user_id)

    if "genera i post" in text.lower():
        result = generate_ai_drafts(history[-400:] if history else "Tema libero")
        await update.message.reply_text(result)
        return

    system = get_system_prompt()
    prefs = get_all_preferences()

    prompt = f"""{system}

Preferenze:
{json.dumps(prefs, ensure_ascii=False)}

Storico recente:
{history[-1500:]}

BJ: {text}

Rispondi in modo naturale e tagliente (max 2-3 frasi)."""

    try:
        model = get_model()
        reply = model.generate_content(prompt).text.strip()
        new_history = (history + f"\nBJ: {text}\nAI: {reply}")[-3000:]
        save_conversation(user_id, new_history)
        await update.message.reply_text(reply)
    except Exception as e:
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

    bot.add_handler(CommandHandler("start", start_cmd))
    bot.add_handler(CommandHandler("learn", learn_cmd))
    bot.add_handler(CommandHandler("buono", buono_cmd))
    bot.add_handler(CommandHandler("scarta", scarta_cmd))
    bot.add_handler(CommandHandler("preferenza", preferenza_cmd))
    bot.add_handler(CommandHandler("prefs", prefs_cmd))
    bot.add_handler(CommandHandler("evolve", evolve_cmd))
    bot.add_handler(CommandHandler("memory", memory_cmd))
    bot.add_handler(CommandHandler("it", it_cmd))
    bot.add_handler(CommandHandler("en", en_cmd))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = BackgroundScheduler()
    loop = asyncio.get_event_loop()
    scheduler.add_job(lambda: asyncio.run_coroutine_threadsafe(scan_and_notify_feeds(bot), loop), "interval", minutes=45)
    scheduler.start()

    await bot.initialize()
    await bot.start()
    await bot.updater.start_polling()
    logger.info("BJ Agent v2 Evolutivo avviato")

    while True:
        await asyncio.sleep(3600)

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()