import asyncio
import json
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import time as dtime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
PROXY_LIST_URL = os.getenv(
    "PROXY_LIST_URL",
    "https://raw.githubusercontent.com/SoliSpirit/mtproto/master/all_proxies.txt",
)
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

CHECK_TIMEOUT = float(os.getenv("CHECK_TIMEOUT", "5.0"))
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "3.0"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "50"))

DAILY_HOUR = int(os.getenv("DAILY_HOUR", "9"))
DAILY_MINUTE = int(os.getenv("DAILY_MINUTE", "0"))


# ---------------- STATE ----------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------- PARSE ----------------

def parse_proxy_line(line: str) -> Optional[dict]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    if line.startswith("tg://proxy?") or line.startswith("https://t.me/proxy?"):
        parsed = urlparse(line)
        qs = parse_qs(parsed.query)

        server = qs.get("server", [None])[0]
        port = qs.get("port", [None])[0]
        secret = qs.get("secret", [None])[0]

        if server and port:
            return {
                "raw": line,
                "host": server,
                "port": int(port),
                "secret": secret,
            }
        return None

    parts = line.split(":")
    if len(parts) >= 2:
        host = parts[0].strip()

        try:
            port = int(parts[1].strip())
        except ValueError:
            return None

        if not host or port <= 0:
            return None

        secret = parts[2].strip() if len(parts) >= 3 else None

        return {
            "raw": line,
            "host": host,
            "port": port,
            "secret": secret,
        }

    return None


# ---------------- FETCH ----------------

def fetch_proxy_list() -> list[dict]:
    resp = requests.get(PROXY_LIST_URL, timeout=CHECK_TIMEOUT)
    resp.raise_for_status()

    proxies = []
    for line in resp.text.splitlines():
        item = parse_proxy_line(line)
        if item:
            proxies.append(item)

    return proxies


# ---------------- CHECK ----------------

def tcp_connect_latency(host: str, port: int, timeout: float):
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed = (time.perf_counter() - start) * 1000
            return True, elapsed, "ok"
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return False, elapsed, str(e)


def check_proxy_advanced(host: str, port: int, timeout: float):
    attempts = 3
    latencies = []

    for _ in range(attempts):
        ok, elapsed, err = tcp_connect_latency(host, port, timeout)

        if not ok:
            return False, elapsed, err

        latencies.append(elapsed)

    avg_latency = sum(latencies) / len(latencies)

    # фильтр по задержке
    if avg_latency > 2000:
        return False, avg_latency, "too slow"

    return True, avg_latency, "ok"


# ---------------- MAIN CHECK ----------------

def check_all_proxies():
    proxies = fetch_proxy_list()
    results = []

    def worker(item):
        ok, elapsed, error = check_proxy_advanced(
            item["host"],
            item["port"],
            CONNECT_TIMEOUT,
        )

        return {
            **item,
            "ok": ok,
            "elapsed_ms": round(elapsed, 1),
            "error": error,
        }

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for r in pool.map(worker, proxies):
            results.append(r)

    reachable = [r for r in results if r["ok"]]
    unreachable = [r for r in results if not r["ok"]]

    avg_ms = None
    if reachable:
        avg_ms = round(sum(r["elapsed_ms"] for r in reachable) / len(reachable), 1)

    fastest = sorted(
        [r for r in reachable if r["elapsed_ms"] < 1500],
        key=lambda x: x["elapsed_ms"]
    )[:10]

    return {
        "total": len(results),
        "reachable": len(reachable),
        "unreachable": len(unreachable),
        "avg_ms": avg_ms,
        "fastest": fastest,
    }


# ---------------- FORMAT ----------------

def format_report(report: dict) -> str:
    lines = [
        "Проверка прокси завершена",
        f"Всего: {report['total']}",
        f"Доступны: {report['reachable']}",
        f"Недоступны: {report['unreachable']}",
    ]

    if report["avg_ms"]:
        lines.append(f"Средняя задержка: {report['avg_ms']} ms")

    if report["fastest"]:
        lines.append("\nЛучшие прокси:")

        for i, p in enumerate(report["fastest"], 1):
            if p.get("secret"):
                link = f"https://t.me/proxy?server={p['host']}&port={p['port']}&secret={p['secret']}"
            else:
                link = f"{p['host']}:{p['port']}"

            lines.append(f"{i}. {link} — {p['elapsed_ms']} ms")

    return "\n".join(lines)


# ---------------- TELEGRAM ----------------

async def send_report(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    loop = asyncio.get_running_loop()
    report = await loop.run_in_executor(None, check_all_proxies)

    await context.bot.send_message(chat_id=chat_id, text=format_report(report))

    state = load_state()
    state["last_report"] = report
    save_state(state)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = load_state()
    state["chat_id"] = chat_id
    save_state(state)

    await update.message.reply_text(
        "Бот запущен\n/check — проверка\n/status — последний отчет"
    )


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Проверяю прокси...")
    await send_report(context, update.effective_chat.id)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    report = state.get("last_report")

    if not report:
        await update.message.reply_text("Нет данных. Запусти /check")
        return

    await update.message.reply_text(format_report(report))


async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    chat_id = state.get("chat_id")

    if chat_id:
        await send_report(context, int(chat_id))


# ---------------- MAIN ----------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Нет TELEGRAM_BOT_TOKEN")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("status", status))

    app.job_queue.run_daily(
        daily_job,
        time=dtime(hour=DAILY_HOUR, minute=DAILY_MINUTE),
    )

    print("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
