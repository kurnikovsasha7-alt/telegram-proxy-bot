import asyncio
import os
import socket
import time
from datetime import time as dtime
from urllib.parse import parse_qs, urlparse

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ================= НАСТРОЙКИ =================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

PROXY_LIST_URL = "https://raw.githubusercontent.com/SoliSpirit/mtproto/master/all_proxies.txt"

CONNECT_TIMEOUT = 2.5
MAX_PROXIES = 250
MAX_PING = 500

CHECK_TIME_LIMIT = 120  # максимум секунд на проверку

DAILY_HOUR = 9
DAILY_MINUTE = 0

# ============================================


def parse_proxy_line(line: str):
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    if line.startswith("tg://") or line.startswith("https://"):
        parsed = urlparse(line)
        qs = parse_qs(parsed.query)

        server = qs.get("server", [None])[0]
        port = qs.get("port", [None])[0]
        secret = qs.get("secret", [None])[0]

        if server and port:
            return {"host": server, "port": int(port), "secret": secret}

    parts = line.split(":")
    if len(parts) >= 2:
        try:
            return {
                "host": parts[0],
                "port": int(parts[1]),
                "secret": parts[2] if len(parts) > 2 else None,
            }
        except:
            return None

    return None


def fetch_proxies():
    resp = requests.get(PROXY_LIST_URL, timeout=10)
    resp.raise_for_status()

    proxies = []
    for line in resp.text.splitlines():
        p = parse_proxy_line(line)
        if p:
            proxies.append(p)

    return proxies[:MAX_PROXIES]


def tcp_ping(host, port):
    start = time.perf_counter()

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        sock.connect((host, port))
        sock.close()

        return True, (time.perf_counter() - start) * 1000

    except:
        return False, 9999


def check_proxies():
    proxies = fetch_proxies()
    results = []

    start_time = time.time()

    for p in proxies:
        # ⛔ ограничение по времени
        if time.time() - start_time > CHECK_TIME_LIMIT:
            break

        ok, ms = tcp_ping(p["host"], p["port"])

        if ok and ms < MAX_PING:
            results.append({
                **p,
                "ms": round(ms, 1)
            })

        # ⛔ если уже нашли 10 — хватит
        if len(results) >= 10:
            break

    results.sort(key=lambda x: x["ms"])
    return results[:10]


def format_result(proxies):
    if not proxies:
        return "Нет доступных прокси ❌"

    text = "🔥 Самые быстрые прокси:\n\n"

    for i, p in enumerate(proxies, 1):
        link = f"https://t.me/proxy?server={p['host']}&port={p['port']}&secret={p['secret']}"
        text += f"{i}. {link} — {p['ms']} ms\n"

    return text


# ================= TELEGRAM =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text("Бот запущен 🚀")


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Проверяю прокси...")

    loop = asyncio.get_running_loop()

    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, check_proxies),
            timeout=40
        )
    except asyncio.TimeoutError:
        await update.message.reply_text("Проверка заняла слишком много времени ❌")
        return

    await update.message.reply_text(format_result(result))


async def daily(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.bot_data.get("chat_id")
    if not chat_id:
        return

    loop = asyncio.get_running_loop()

    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, check_proxies),
            timeout=40
        )
    except:
        await context.bot.send_message(chat_id, "Ошибка проверки ❌")
        return

    await context.bot.send_message(chat_id, format_result(result))


# ================= MAIN =================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))

    app.job_queue.run_daily(
        daily,
        time=dtime(hour=DAILY_HOUR, minute=DAILY_MINUTE)
    )

    print("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
