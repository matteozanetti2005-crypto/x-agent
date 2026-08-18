import os
import io
import csv
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID_RAW = os.getenv("ALLOWED_TELEGRAM_USER_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PORT = int(os.getenv("PORT", 8080))

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

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

RSS_FEEDS = [
    {"source": "Feed Personalizzato BJ", "url": "https://rss.app/feeds/t5ooMu9TaY8RO77f.xml"},
    {"source": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"source": "MIT Tech Review", "url": "https://technologyreview.com/topic/artificial-intelligence/feed"}
]

async def scan_and_notify_feeds(bot_application):
    logger.info("Avvio scansione feed RSS (Proattiva)...")
    total_added = 0
    new_items_for_analysis = []
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    for feed_info in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            if feed.entries:
                for entry in feed.entries[:2]:
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
                            new_items_for_analysis.append(content)
        except Exception:
            continue

    conn.commit()
    conn.close()
    
    logger.info(f"Scansione completata. Nuovi elementi inseriti: {total_added}")

    if total_added > 0 and ALLOWED_USER_ID_RAW and GEMINI_API_KEY:
        await evaluate_and_poke_user(bot_application, new_items_for_analysis)

    return total_added

async def evaluate_and_poke_user(bot_application, new_items):
    joined_news = "\n---\n".join(new_items[:6])
    
    prompt = f"""
Sei un editor molto esigente per BJ (@BJ_Beyond), specializzato in "Human Edge", arte e intelligenza artificiale.

Analizza queste notizie:
{joined_news}

Regole severe:
- Seleziona SOLO se la notizia ha un forte legame con il valore umano, la creatività, l'intenzione o l'arte nell'era dell'AI.
- Se la notizia è debole, generica o poco originale → rispondi ESATTAMENTE con: SKIP
- Se è forte, scrivi un alert breve (max 3 frasi) + un'idea di post tagliente in stile BJ.

Rispondi solo con SKIP o con il testo dell'alert.
"""

    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(prompt)
        text_out = response.text.strip()
        
        if "skip" not in text_out.lower() and len(text_out) > 15:
            await bot_application.bot.send_message(
                chat_id=ALLOWED_USER_ID_RAW,
                text="🚨 **Phoenix Alert**\\n\\n" + text_out,
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Errore durante l'analisi proattiva: {e}")

def save_style_sample(text: str) -> int:
    clean_text = text.strip()
    if not clean_text:
        return 0
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO style_memory (sample_text, added_at) VALUES (?, ?)",
        (clean_text, time.strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    cursor.execute("SELECT COUNT(*) FROM style_memory")
    count = cursor.fetchone()[0]
    conn.close()
    return count

def save_bulk_samples(posts_list: list) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    added = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for p in posts_list:
        clean = p.strip()
        if len(clean) > 20:
            cursor.execute("INSERT INTO style_memory (sample_text, added_at) VALUES (?, ?)", (clean, now))
            added += 1
    conn.commit()
    conn.close()
    return added

def get_style_samples(topic: str = None) -> str:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    if topic and len(topic) > 3:
        search_term = f"%{topic.lower()}%"
        cursor.execute("""
            SELECT sample_text FROM style_memory 
            WHERE LOWER(sample_text) LIKE ? 
            ORDER BY RANDOM() 
            LIMIT 8
        """, (search_term,))
        rows = cursor.fetchall()

        if len(rows) < 4:
            cursor.execute("""
                SELECT sample_text FROM style_memory 
                ORDER BY RANDOM() 
                LIMIT 6
            """)
            extra = cursor.fetchall()
            rows = rows + extra
    else:
        cursor.execute("SELECT sample_text FROM style_memory ORDER BY RANDOM() LIMIT 7")
        rows = cursor.fetchall()

    conn.close()

    if not rows:
        return "Nessun esempio memorizzato."

    seen = set()
    unique_rows = []
    for r in rows:
        if r[0] not in seen:
            seen.add(r[0])
            unique_rows.append(r)

    return "\n---\n".join([f"Post Reale di BJ:\n{r[0]}" for r in unique_rows[:7]])

def clear_all_memory() -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM style_memory")
    conn.commit()
    conn.close()

def save_conversation(user_id: str, history: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT OR REPLACE INTO conversation_memory (user_id, history, updated_at)
        VALUES (?, ?, ?)
    """, (str(user_id), history, now))
    conn.commit()
    conn.close()

def load_conversation(user_id: str) -> str:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT history FROM conversation_memory WHERE user_id = ?", (str(user_id),))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else ""

def generate_ai_drafts(prompt_topic: str, lang_mode: str = "both") -> str:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT author, content FROM scraped_posts ORDER BY ROWID DESC LIMIT 8")
    rows = cursor.fetchall()
    conn.close()

    context_text = "\n---\n".join([f"Fonte [{r[0]}]: {r[1]}" for r in rows]) if rows else "Nessun dato di contesto recente."
    style_context = get_style_samples(topic=prompt_topic)

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
🇮🇹 IT: [Testo italiano - Hook magnetico]
🇬🇧 EN: [English translation]
***
**OPZIONE 2 / OPTION 2**
🇮🇹 IT: [Testo italiano - Analisi approfondita]
🇬🇧 EN: [English translation]
***
**OPZIONE 3 / OPTION 3 (Human Edge)**
🇮🇹 IT: [Testo italiano - Angolo controintuitivo]
🇬🇧 EN: [English translation]
"""

    full_prompt = f"""
Sei il Ghostwriter e Stratega personale di BJ (@BJ_Beyond).

TONO DI VOCE OBBLIGATORIO:
- Naturale, tagliente, sintetico
- Autorevole ma mai pomposo
- Focalizzato sul "Human Edge": l'AI amplifica, ma non sostituisce mai l'intuizione, la sensibilità e l'intenzione umana
- Evita elenchi, em-dash, frasi fatte e tono generico
- Preferisci ritmo fluido e osservazioni dirette

PILASTRI DA RISPETTARE:
1. Human Edge → l'essere umano rimane insostituibile nell'arte, nella strategia e nella creatività
2. Arte & Processo → rispetto per il lavoro autentico e la visione estetica
3. Building in public → trasparenza, sperimentazione reale, zero fuffa

MEMORIA DI STILE AUTENTICA DI BJ:
{style_context}

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
        "👋 **BJ X Agent (Proattivo & Brainstorm) è Online!**\n\n"
        "• /scan : Scansiona i feed manualmente\n"
        "• /learn <testo> : Memorizza un singolo post\n"
        "• /memory : Mostra esempi salvati\n"
        "• /clear_memory : Cancella l'archivio stile\n"
        "• Invia un file `.txt` o `.csv` per caricare l'archivio dei tuoi post!\n\n"
        "💡 Uso: Chiacchieriamo e facciamo brainstorming. Scrivi 'Genera i post' per avere le bozze bilingue.",
        parse_mode="Markdown"
    )

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
await update.message.reply_text("🔄 Scansione in corso...")
    added = scan_feeds_manual()
    await update.message.reply_text(f"✅ Scansione completata!\nNuovi elementi: {added}")

def scan_feeds_manual():
    total_added = 0
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    for feed_info in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            if feed.entries:
                for entry in feed.entries[:3]:
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
    conn.commit()
    conn.close()
    return total_added

async def learn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    sample_text = " ".join(context.args)
    if not sample_text:
        await update.message.reply_text("⚠️ Invia un testo da memorizzare.", parse_mode="Markdown")
        return
    total = save_style_sample(sample_text)
    await update.message.reply_text(f"🧠 Stile Appreso! Post memorizzati: {total}", parse_mode="Markdown")

async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    memory = get_style_samples()
    await update.message.reply_text(f"📚 **Campioni Memoria Attuale:**\n\n{memory}")

async def clear_memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    clear_all_memory()
    await update.message.reply_text("🧹 Memoria di stile azzerata.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    doc = update.message.document
    file_name = doc.file_name.lower()
    
    if not (file_name.endswith('.txt') or file_name.endswith('.csv')):
        await update.message.reply_text("⚠️ Invia un file in formato .txt o .csv.", parse_mode="Markdown")
        return
    
    await update.message.reply_text("📥 Ricezione e analisi archivio in corso...")
    file_obj = await doc.get_file()
    file_bytes = await file_obj.download_as_bytearray()
    content_str = file_bytes.decode('utf-8', errors='ignore')
    
    posts = []
    if file_name.endswith('.csv'):
        reader = csv.reader(io.StringIO(content_str))
        for row in reader:
            if row:
                posts.append(row[0])
    else:
        if "---" in content_str:
            posts = content_str.split("---")
        else:
            posts = content_str.split("\n\n")
            
    added = save_bulk_samples(posts)
    await update.message.reply_text(f"🚀 Archivio Appreso con Successo! Memorizzati {added} post.", parse_mode="Markdown")

async def it_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    user_topic = " ".join(context.args)
    if not user_topic:
        await update.message.reply_text("⚠️ Specifica un tema.", parse_mode="Markdown")
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
        await update.message.reply_text("⚠️ Specifica un tema.", parse_mode="Markdown")
        return
    await update.message.reply_text("🧠 Elaboro bozze in Inglese...")
    result = generate_ai_drafts(user_topic, lang_mode="en")
    await update.message.reply_text(result)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
        
    user_text = update.message.text
    
    history = load_conversation(user_id)
    
    if "genera i post" in user_text.lower() or "genera post" in user_text.lower():
        await update.message.reply_text("🚀 Assemblo le bozze bilingue definitive...")
        chat_context = history if history else "Nessuna conversazione precedente. Tema libero."
        tema_esteso = f"Basati su questa conversazione di brainstorming:\n{chat_context}"
        
        result = generate_ai_drafts(tema_esteso, lang_mode="both")
        await update.message.reply_text(result)
        save_conversation(user_id, "")
        return

    brainstorm_prompt = f"""
    Sei il ghostwriter e stratega di BJ (@BJ_Beyond). 
    Siamo in fase di BRAINSTORMING. Rispondi alle mie idee, dammi spunti sul "Human Edge", AI o arte.
    Rispondi in modo discorsivo, intelligente, tagliente e BREVE (massimo 2-3 frasi).
    Se l'idea è matura, ricordami che posso scrivere "Genera i post".
    
    Storico:
    {history}
    
    BJ ti dice: "{user_text}"
"""
    
    try:
        valid_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        reply = None
        last_err = None
        for mod_name in valid_models:
            try:
                model = genai.GenerativeModel(mod_name)
                response = model.generate_content(brainstorm_prompt)
                if response and response.text:
                    reply = response.text
                    break
            except Exception as inner_e:
                last_err = inner_e
                continue
                
        if not reply:
            await update.message.reply_text(f"⚠️ Errore modello: {last_err}")
            return
            
        new_history = history + f"\nBJ: {user_text}\nAI: {reply}\n"
        save_conversation(user_id, new_history[-3000:])
        await update.message.reply_text(reply)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Errore critico: {e}")

# Server Flask per Render
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
    bot_app.add_handler(CommandHandler("learn", learn_cmd))
    bot_app.add_handler(CommandHandler("memory", memory_cmd))
    bot_app.add_handler(CommandHandler("clear_memory", clear_memory_cmd))
    bot_app.add_handler(CommandHandler("it", it_cmd))
    bot_app.add_handler(CommandHandler("en", en_cmd))
    bot_app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        lambda: asyncio.run_coroutine_threadsafe(scan_and_notify_feeds(bot_app), asyncio.get_event_loop()),
        'interval', 
        minutes=45
    )
    scheduler.start()

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    logger.info("Bot Telegram in ascolto con proattività attiva...")
    
    while True:
        await asyncio.sleep(3600)

def main():
flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Chiusura servizio.")

if __name__ == "__main__":
    main()
