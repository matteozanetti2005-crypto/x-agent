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
    cursor.execute('''CREATE TABLE IF NOT EXISTS scraped_posts (id TEXT PRIMARY KEY, author TEXT, content TEXT, published_at TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS style_memory (id INTEGER PRIMARY KEY AUTOINCREMENT, sample_text TEXT, added_at TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS conversation_memory (user_id TEXT PRIMARY KEY, history TEXT, updated_at TEXT)''')
    conn.commit()
    conn.close()

init_db()

RSS_FEEDS = [
    {"source": "Feed Personalizzato BJ", "url": "https://rss.app/feeds/t5ooMu9TaY8RO77f.xml"},
    {"source": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"source": "MIT Tech Review", "url": "https://technologyreview.com/topic/artificial-intelligence/feed"}
]

async def scan_and_notify_feeds(bot_application):
    total_added = 0
    new_items = []
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    for feed_info in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            if feed.entries:
                for entry in feed.entries[:2]:
                    p_id = getattr(entry, 'id', getattr(entry, 'link', ''))
                    content = f"{getattr(entry,'title','')} - {getattr(entry,'summary','')}"[:500]
                    if p_id:
                        cursor.execute("INSERT OR IGNORE INTO scraped_posts VALUES (?,?,?,?)", (p_id, feed_info["source"], content, getattr(entry,'published',time.ctime())))
                        if cursor.rowcount > 0:
                            total_added += 1
                            new_items.append(content)
        except: continue
    conn.commit()
    conn.close()
    if total_added > 0 and ALLOWED_USER_ID_RAW and GEMINI_API_KEY:
        await evaluate_and_poke_user(bot_application, new_items)
    return total_added

async def evaluate_and_poke_user(bot_application, new_items):
    joined_news = "\n---\n".join(new_items[:6])
    prompt = f"""Sei un editor molto esigente per BJ. Analizza queste notizie e scegli solo quelle forti sul Human Edge. Se nessuna è forte rispondi SKIP.\n{joined_news}"""
    try:
        text = genai.GenerativeModel("gemini-1.5-flash").generate_content(prompt).text.strip()
        if "skip" not in text.lower() and len(text) > 15:
            await bot_application.bot.send_message(chat_id=ALLOWED_USER_ID_RAW, text="🚨 Phoenix Alert\n\n" + text, parse_mode="Markdown")
    except: pass

def save_style_sample(text):
    if len(text.strip()) < 10: return 0
    conn = sqlite3.connect(DB_NAME)
    conn.execute("INSERT INTO style_memory VALUES (NULL,?,?)", (text.strip(), time.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM style_memory").fetchone()[0]
    conn.close()
    return count

def get_style_samples(topic=None):
    conn = sqlite3.connect(DB_NAME)
    if topic and len(topic) > 3:
        rows = conn.execute("SELECT sample_text FROM style_memory WHERE LOWER(sample_text) LIKE ? ORDER BY RANDOM() LIMIT 8", (f"%{topic.lower()}%",)).fetchall()
    else:
rows = conn.execute("SELECT sample_text FROM style_memory ORDER BY RANDOM() LIMIT 7").fetchall()
    conn.close()
    return "\n---\n".join([f"Post Reale di BJ:\n{r[0]}" for r in rows]) if rows else "Nessun esempio"

def save_conversation(user_id, history):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("INSERT OR REPLACE INTO conversation_memory VALUES (?,?,?)", (str(user_id), history, time.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def load_conversation(user_id):
    conn = sqlite3.connect(DB_NAME)
    row = conn.execute("SELECT history FROM conversation_memory WHERE user_id=?", (str(user_id),)).fetchone()
    conn.close()
    return row[0] if row else ""

def generate_ai_drafts(topic, lang="both"):
    style = get_style_samples(topic)
    prompt = f"""Sei il Ghostwriter di BJ. Tono naturale e tagliente. Human Edge.\nMEMORIA:\n{style}\n\nTEMA: {topic}\n\nGenera 3 opzioni."""
    try:
        return genai.GenerativeModel("gemini-1.5-flash").generate_content(prompt).text
    except Exception as e:
        return f"Errore: {e}"

def is_authorized(uid):
    return str(uid) == str(ALLOWED_USER_ID_RAW) if ALLOWED_USER_ID_RAW else True

async def start_cmd(update, context):
    if not is_authorized(update.effective_user.id): return
    await update.message.reply_text("👋 BJ X Agent online.")

async def scan_cmd(update, context):
    if not is_authorized(update.effective_user.id): return
    await update.message.reply_text("🔄 Scansione in corso...")
    added = scan_feeds_manual()
    await update.message.reply_text(f"✅ Completata! Nuovi elementi: {added}")

def scan_feeds_manual():
    total = 0
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    for f in RSS_FEEDS:
        try:
            feed = feedparser.parse(f["url"])
            for entry in feed.entries[:3]:
                pid = getattr(entry, 'id', getattr(entry, 'link', ''))
                if pid:
                    c.execute("INSERT OR IGNORE INTO scraped_posts VALUES (?,?,?,?)", (pid, f["source"], f"{getattr(entry,'title','')}", time.ctime()))
                    if c.rowcount > 0: total += 1
        except: continue
    conn.commit()
    conn.close()
    return total

async def learn_cmd(update, context):
    if not is_authorized(update.effective_user.id): return
    text = " ".join(context.args)
    if not text: return
    total = save_style_sample(text)
    await update.message.reply_text(f"🧠 Stile appreso! ({total} esempi)")

async def memory_cmd(update, context):
    if not is_authorized(update.effective_user.id): return
    await update.message.reply_text(get_style_samples())

async def clear_memory_cmd(update, context):
    if not is_authorized(update.effective_user.id): return
    clear_all_memory()
    await update.message.reply_text("🧹 Memoria azzerata.")

async def handle_document(update, context):
    if not is_authorized(update.effective_user.id): return

async def it_cmd(update, context):
    if not is_authorized(update.effective_user.id): return
    topic = " ".join(context.args)
    if not topic: return
    await update.message.reply_text(generate_ai_drafts(topic, "it"))

async def en_cmd(update, context):
    if not is_authorized(update.effective_user.id): return
    topic = " ".join(context.args)
    if not topic: return
    await update.message.reply_text(generate_ai_drafts(topic, "en"))

async def handle_message(update, context):
    if not is_authorized(update.effective_user.id): return
    text = update.message.text
    history = load_conversation(update.effective_user.id)

    if "genera i post" in text.lower():
        result = generate_ai_drafts(history or "Tema libero", "both")
        await update.message.reply_text(result)
        save_conversation(update.effective_user.id, "")
        return

    prompt = f"""Sei il ghostwriter di BJ. Rispondi in modo naturale e tagliente (max 2-3 frasi).\nStorico: {history}\nBJ: {text}"""
    try:
        reply = genai.GenerativeModel("gemini-1.5-flash").generate_content(prompt).text.strip()
save_conversation(update.effective_user.id, (history + f"\nBJ: {text}\nAI: {reply}")[-3000:])
        await update.message.reply_text(reply)
    except Exception as e:
        await update.message.reply_text(str(e))

app = Flask(__name__)
@app.route('/')
def health(): return "OK", 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

async def run_bot():
    bot = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: asyncio.run_coroutine_threadsafe(scan_and_notify_feeds(bot), asyncio.get_event_loop()), 'interval', minutes=45)
    scheduler.start()
    await bot.initialize()
    await bot.start()
    await bot.updater.start_polling()
    while True: await asyncio.sleep(3600)

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()
