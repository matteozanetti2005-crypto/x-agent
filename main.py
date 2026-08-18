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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

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

    c.execute('''
        CREATE TABLE IF NOT EXISTS style_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_text TEXT,
            score REAL DEFAULT 1.0,
            times_used INTEGER DEFAULT 0,
            last_used TEXT,
            added_at TEXT,
            source TEXT DEFAULT 'manual'
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS preferences (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS conversation_memory (
            user_id TEXT PRIMARY KEY,
            history TEXT,
            updated_at TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS scraped_posts (
            id TEXT PRIMARY KEY,
            author TEXT,
            content TEXT,
            published_at TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS system_prompt (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            prompt TEXT,
            updated_at TEXT
        )
    ''')

    # Tabella temporanea per i draft generati (per feedback)
    c.execute('''
        CREATE TABLE IF NOT EXISTS generated_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT,
            created_at TEXT
        )
    ''')

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
def save_style_sample(text: str, initial_score: float = 1.0, source: str = "manual") -> int:
    if len(text.strip()) < 15:
        return 0
    conn = sqlite3.connect(DB_NAME)
    conn.execute(
        "INSERT INTO style_memory (sample_text, score, added_at, source) VALUES (?, ?, ?, ?)",
        (text.strip(), initial_score, time.strftime("%Y-%m-%d %H:%M:%S"), source)
    )
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM style_memory").fetchone()[0]
    conn.close()
    return count

def update_style_score(sample_id: int, delta: float):
    conn = sqlite3.connect(DB_NAME)
    conn.execute(
        "UPDATE style_memory SET score = MAX(0.1, score + ?), times_used = times_used + 1, last_used = ? WHERE id = ?",
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

    conn = sqlite3.connect(DB_NAME)
    for r in rows:
        conn.execute("UPDATE style_memory SET times_used = times_used + 1, last_used = ? WHERE id = ?",
                     (time.strftime("%Y-%m-%d %H:%M:%S"), r[0]))
    conn.commit()
    conn.close()

    return "\n---\n".join([f"[Score {r[2]:.1f}] {r[1]}" for r in rows])

# ====================== GENERATED DRAFTS (per feedback) ======================
def save_generated_draft(text: str) -> int:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.execute(
        "INSERT INTO generated_drafts (text, created_at) VALUES (?, ?)",
        (text.strip(), time.strftime("%Y-%m-%d %H:%M:%S"))
    )
    draft_id = cur.lastrowid
    conn.commit()
    conn.close()
    return draft_id

def get_generated_draft(draft_id: int) -> str | None:
    conn = sqlite3.connect(DB_NAME)
    row = conn.execute("SELECT text FROM generated_drafts WHERE id = ?", (draft_id,)).fetchone()
    conn.close()
    return row[0] if row else None

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
    prefs = get_all_preferences()
    best_samples = get_best_style_samples(limit=5)

    prompt = f"""Sei un meta-agente che migliora il system prompt di un ghostwriter.
Analizza le preferenze e gli esempi di stile migliori.
Riscrivi un system prompt più preciso e fedele allo stile di BJ.
Rispondi SOLO con il nuovo system prompt.

Preferenze:
{json.dumps(prefs, indent=2, ensure_ascii=False)}

Esempi migliori:
{best_samples}
"""
    try:
        model = get_model()
        new_prompt = model.generate_content(prompt).text.strip()
        if len(new_prompt) > 80:
            update_system_prompt(new_prompt)
            return True
    except Exception as e:
        logger.error(f"Errore evolve: {e}")
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

# ====================== GENERAZIONE CON FEEDBACK ======================
def generate_ai_drafts(topic: str) -> tuple[str, list]:
    """Ritorna (testo formattato, lista di draft_id)"""
    style = get_best_style_samples(topic)
    prefs = get_all_preferences()
    system = get_system_prompt()

    prompt = f"""{system}

PREFERENZE:
{json.dumps(prefs, indent=2, ensure_ascii=False)}

ESEMPI DI STILE DI ALTA QUALITÀ:
{style}

TEMA: {topic}

Genera esattamente 3 opzioni di post.
Ogni opzione deve essere potente e fedele allo stile.
Formato obbligatorio:
1. <testo>
2. <testo>
3. <testo>
"""
    try:
        model = get_model()
        raw = model.generate_content(prompt).text.strip()
    except Exception as e:
        return f"Errore generazione: {e}", []

    # Parsing grezzo delle 3 opzioni
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    options = []
    current = []
    for line in lines:
        if line.startswith(("1.", "2.", "3.")):
            if current:
                options.append(" ".join(current).strip())
            current = [line[2:].strip()]
        else:
            current.append(line)
    if current:
        options.append(" ".join(current).strip())

    options = options[:3]
    if not options:
        options = [raw]

    draft_ids = []
    formatted = []
    for i, opt in enumerate(options, 1):
        draft_id = save_generated_draft(opt)
        draft_ids.append(draft_id)
        formatted.append(f"**{i}.** {opt}")

    return "\n\n".join(formatted), draft_ids

def build_feedback_keyboard(draft_ids: list) -> InlineKeyboardMarkup:
    buttons = []
    for i, did in enumerate(draft_ids, 1):
        row = [
            InlineKeyboardButton(f"👍 {i}", callback_data=f"up:{did}"),
            InlineKeyboardButton(f"👎 {i}", callback_data=f"down:{did}"),
            InlineKeyboardButton(f"💾 {i}", callback_data=f"save:{did}"),
        ]
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)

# ====================== HANDLERS ======================
def is_authorized(uid) -> bool:
    if not ALLOWED_USER_ID_RAW:
        return True
    return str(uid) == str(ALLOWED_USER_ID_RAW)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        "👋 *BJ Agent v2.1 Evolutivo*\n\n"
        "Comandi:\n"
        "/learn <testo>\n"
        "/buono /scarta (ancora disponibili)\n"
        "/preferenza <chiave> <valore>\n"
        "/prefs\n"
        "/evolve\n"
        "/memory\n"
        "/it <tema> o /en <tema>\n\n"
        "Dopo ogni generazione puoi usare i bottoni 👍 👎 💾",
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
    await update.message.reply_text(f"🧠 Stile appreso. Totale: {total}")

async def preferenza_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usa: /preferenza <chiave> <valore>")
        return
    key = context.args[0]
    value = " ".join(context.args[1:])
    set_preference(key, value)
    await update.message.reply_text(f"⚙️ `{key}` → {value}", parse_mode="Markdown")

async def prefs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    prefs = get_all_preferences()
    text = "\n".join([f"• *{k}*: {v}" for k, v in prefs.items()])
    await update.message.reply_text(f"*Preferenze:*\n\n{text}", parse_mode="Markdown")

async def evolve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text("🧬 Evoluzione in corso...")
    ok = evolve_system_prompt()
    await update.message.reply_text("✅ System prompt evoluto." if ok else "❌ Fallito.")

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
    await update.message.reply_text("⏳ Genero...")
    text, draft_ids = generate_ai_drafts(topic)
    keyboard = build_feedback_keyboard(draft_ids)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def en_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    topic = " ".join(context.args)
    if not topic:
        await update.message.reply_text("Usa: /en <tema>")
        return
    await update.message.reply_text("⏳ Generating...")
    text, draft_ids = generate_ai_drafts(topic)
    keyboard = build_feedback_keyboard(draft_ids)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    text = update.message.text.strip()
    user_id = update.effective_user.id
    history = load_conversation(user_id)

    if "genera i post" in text.lower():
        await update.message.reply_text("⏳ Genero...")
        result, draft_ids = generate_ai_drafts(history[-400:] if history else "Tema libero")
        keyboard = build_feedback_keyboard(draft_ids)
        await update.message.reply_text(result, reply_markup=keyboard, parse_mode="Markdown")
        return

    system = get_system_prompt()
    prefs = get_all_preferences()

    prompt = f"""{system}

Preferenze:
{json.dumps(prefs, ensure_ascii=False)}

Storico:
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

# ====================== CALLBACK FEEDBACK ======================
async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_authorized(query.from_user.id):
        return

    data = query.data
    action, draft_id_str = data.split(":")
    draft_id = int(draft_id_str)

    text = get_generated_draft(draft_id)
    if not text:
        await query.edit_message_text("Draft non più disponibile.")
        return

    if action == "up":
        # Cerca se esiste già in style_memory, altrimenti lo crea con score alto
        conn = sqlite3.connect(DB_NAME)
        row = conn.execute("SELECT id FROM style_memory WHERE sample_text = ?", (text,)).fetchone()
        if row:
            update_style_score(row[0], +0.4)
        else:
            save_style_sample(text, initial_score=1.4, source="generated_up")
        conn.close()
        await query.answer("👍 Rinforzato")
        await query.edit_message_reply_markup(reply_markup=None)

    elif action == "down":
        conn = sqlite3.connect(DB_NAME)
        row = conn.execute("SELECT id FROM style_memory WHERE sample_text = ?", (text,)).fetchone()
        if row:
            update_style_score(row[0], -0.5)
        else:
            save_style_sample(text, initial_score=0.4, source="generated_down")
        conn.close()
        await query.answer("👎 Penalizzato")
        await query.edit_message_reply_markup(reply_markup=None)

    elif action == "save":
        save_style_sample(text, initial_score=1.6, source="saved")
        await query.answer("💾 Salvato come esempio di stile")
        await query.edit_message_reply_markup(reply_markup=None)

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
    bot.add_handler(CommandHandler("preferenza", preferenza_cmd))
    bot.add_handler(CommandHandler("prefs", prefs_cmd))
    bot.add_handler(CommandHandler("evolve", evolve_cmd))
    bot.add_handler(CommandHandler("memory", memory_cmd))
    bot.add_handler(CommandHandler("it", it_cmd))
    bot.add_handler(CommandHandler("en", en_cmd))
    bot.add_handler(CallbackQueryHandler(feedback_callback))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = BackgroundScheduler()
    loop = asyncio.get_event_loop()
    scheduler.add_job(lambda: None, "interval", minutes=45)  # placeholder
    scheduler.start()

    await bot.initialize()
    await bot.start()
    await bot.updater.start_polling()
    logger.info("BJ Agent v2.1 Evolutivo avviato")

    while True:
        await asyncio.sleep(3600)

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()