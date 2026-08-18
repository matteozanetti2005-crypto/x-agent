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
