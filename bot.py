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
CHECK_TIMEOUT = float(os.getenv("CHECK_TIMEOUT", "3.0"))
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "2.0"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "20"))
DAILY_HOUR = int(os.getenv("DAILY_HOUR", "9"))
DAILY_MINUTE = int(os.getenv("DAILY_MINUTE", "0"))
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Vienna")


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


def parse_proxy_line(line: str) -> Optional[dict]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # tg://proxy?server=...&port=...&secret=...
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

    # host:port or host:port:secret
    parts = line.split(":")
    if len(parts) >= 2:
        host = parts[0].strip()
        try:
            port = int(parts[1].strip())
        except ValueError:
            return None
        secret = parts[2].strip() if len(parts) >= 3 else None
        return {
            "raw": line,
            "host": host,
            "port": port,
            "secret": secret,
        }

    return None


def fetch_proxy_list() -> list[dict]:
    resp = requests.get(PROXY_LIST_URL, timeout=CHECK_TIMEOUT)
    resp.raise_for_status()

    proxies: list[dict] = []
    for line in resp.text.splitlines():
        item = parse_proxy_line(line)
        if item:
            proxies.append(item)
    return proxies


def tcp_connect_latency(host: str, port: int, timeout: float) -> tuple[bool, float, str]:
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed_ms = (time.perf_counter() - start) * 1000
            return True, elapsed_ms, "ok"
    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return False, elapsed_ms, str(e)


def check_all_proxies() -> dict:
    proxies = fetch_proxy_list()
    results = []

    def worker(item: dict) -> dict:
        ok, elapsed_ms, error = tcp_connect_latency(
            item["host"],
            item["port"],
            CONNECT_TIMEOUT,
        )
        return {
            **item,
            "ok": ok,
            "elapsed_ms": round(elapsed_ms, 1),
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

    fastest = sorted(reachable, key=lambda x: x["elapsed_ms"])[:10]

    return {
        "total": len(results),
        "reachable": len(reachable),
        "unreachable": len(unreachable),
        "avg_ms": avg_ms,
        "fastest": fastest,
        "results": results,
    }


def format_report(report: dict) -> str:
    lines = [
        "Проверка прокси завершена",
        f"Всего: {report['total']}",
        f"Доступны: {report['reachable']}",
        f"Недоступны: {report['unreachable']}",
    ]
    if report["avg_ms"] is not None:
        lines.append(f"Средняя задержка TCP-connect: {report['avg_ms']} ms")

    if report["fastest"]:
        lines.append("")
        lines.append("Самые быстрые:")
        for i, p in enumerate(report["fastest"][:10], 1):
            lines.append(f"{i}. {p['host']}:{p['port']} — {p['elapsed_ms']} ms")

    return "\n".join(lines)


async def send_report(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    loop = asyncio.get_running_loop()
    report = await loop.run_in_executor(None, check_all_proxies)
    text = format_report(report)

    await context.bot.send_message(chat_id=chat_id, text=text)

    state = load_state()
    state["last_report"] = report
    save_state(state)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = load_state()
    state["chat_id"] = chat_id
    save_state(state)

    await update.message.reply_text(
        "Бот запущен. Теперь я буду отправлять ежедневный отчет сюда.\n"
        "Команда /check — запустить проверку вручную."
    )


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Проверяю прокси...")
    chat_id = update.effective_chat.id
    await send_report(context, chat_id)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    report = state.get("last_report")
    if not report:
        await update.message.reply_text("Пока нет сохраненного отчета. Сначала запусти /check.")
        return

    await update.message.reply_text(format_report(report))


async def daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    chat_id = state.get("chat_id")
    if not chat_id:
        return
    await send_report(context, int(chat_id))


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("status", status))

    # Ежедневный запуск. По умолчанию — по локальному времени сервера.
    app.job_queue.run_daily(
        daily_job,
        time=dtime(hour=DAILY_HOUR, minute=DAILY_MINUTE),
        name="daily_proxy_check",
    )

    print("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
