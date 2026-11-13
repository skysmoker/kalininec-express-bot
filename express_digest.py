import os
import sys
import random
import logging
import datetime
from pathlib import Path
import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TOMTOM_API_KEY = os.getenv("TOMTOM_API_KEY", "").strip()
YANDEX_RASP_API_KEY = os.getenv("YANDEX_RASP_API_KEY", "").strip()

LOG_FILE = BASE_DIR / "express.log"
STATE_FILE = BASE_DIR / "last_sent.txt"

WEEKDAYS_RU = [
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье"
]

MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
]

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    encoding="utf-8"
)


def already_sent_today() -> bool:
    today_str = datetime.date.today().isoformat()
    if STATE_FILE.exists():
        return STATE_FILE.read_text(encoding="utf-8").strip() == today_str
    return False


def mark_sent_today() -> None:
    today_str = datetime.date.today().isoformat()
    STATE_FILE.write_text(today_str + "\n", encoding="utf-8")


def get_rates_block() -> list[str]:
    """Получает курсы USD и EUR от ЦБ РФ и BTC от CoinGecko."""
    parts = []
    try:
        cbr = requests.get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=10).json()
        valute = cbr["Valute"]
        for code in ("USD", "EUR"):
            cur = valute[code]["Value"]
            prev = valute[code]["Previous"]
            diff = cur - prev
            sign = "+" if diff >= 0 else ""
            parts.append(f"<b>{code}:</b> {cur:.2f} ₽ <i>({sign}{diff:.2f})</i>")
    except Exception as e:
        logging.warning(f"Не удалось получить курсы ЦБ РФ: {e}")

    try:
        cg = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd", "include_24hr_change": "true"},
            timeout=10
        ).json()
        btc_price = cg["bitcoin"]["usd"]
        btc_change = cg["bitcoin"]["usd_24h_change"]
        sign = "+" if btc_change >= 0 else ""
        parts.append(f"<b>BTC:</b> ${btc_price:,.0f}".replace(",", " ") + f" <i>({sign}{btc_change:.1f}%)</i>")
    except Exception as e:
        logging.warning(f"Не удалось получить курс BTC: {e}")

    if not parts:
        return []
    return ["💱 <b>Курсы:</b>", "   • " + " • ".join(parts)]


def get_calendar_block(today: datetime.date) -> list[str]:
    """Проверяет праздники/выходные и напоминание о счётчиках (20–25 число)."""
    notes = []

    if 20 <= today.day <= 25:
        notes.append("⚡ Не забудьте передать показания счётчиков.")

    try:
        resp = requests.get(f"https://isdayoff.ru/{today.strftime('%Y%m%d')}", timeout=5)
        code = resp.text.strip()
        if code == "1" and today.weekday() < 5:
            notes.append("🎉 Сегодня официальный праздничный выходной день!")
        elif today.weekday() == 4 and code == "0":
            notes.append("Впереди выходные!")
    except Exception as e:
        logging.warning(f"Ошибка проверки isdayoff.ru: {e}")
        if today.weekday() == 4:
            notes.append("Впереди выходные!")

    if not notes:
        return []
    return ["📅 <b>Календарь:</b> " + " ".join(notes)]


def get_fact_of_the_day(today: datetime.date) -> list[str]:
    """Берёт одно историческое событие на текущую дату из русской Википедии."""
    try:
        url = f"https://ru.wikipedia.org/api/rest_v1/feed/onthisday/events/{today.month:02d}/{today.day:02d}"
        headers = {"User-Agent": "KalininetsExpressBot/1.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        events = resp.json().get("events", [])
        compact = [
            ev for ev in events
            if 50 <= len(ev.get("text", "")) <= 190 and ev.get("year")
        ]
        if compact:
            chosen = random.choice(compact)
            year = chosen["year"]
            text = chosen["text"].strip()
            if not text.endswith("."):
                text += "."
            return [f"📜 <b>Факт дня:</b> В {year} году — {text}"]
    except Exception as e:
        logging.warning(f"Не удалось получить факт дня из Википедии: {e}")
    return []


def get_traffic_block() -> list[str]:
    """Блок пробок (активируется после добавления TOMTOM_API_KEY в .env)."""
    if not TOMTOM_API_KEY:
        return []
    return []


def get_trains_block() -> list[str]:
    """Блок электричек: МЦД-1 (Голицыно) первым, затем МЦД-4 (Селятино / Апрелевка)."""
    if not YANDEX_RASP_API_KEY:
        return []
    return []


def build_digest() -> str:
    today = datetime.date.today()
    weekday_str = WEEKDAYS_RU[today.weekday()]
    date_str = f"{today.day} {MONTHS_RU[today.month - 1]}"

    lines = [f"☕ <b>Утренний экспресс • {weekday_str}, {date_str}</b>"]

    for block in (
        get_trains_block(),
        get_traffic_block(),
        get_rates_block(),
        get_calendar_block(today),
        get_fact_of_the_day(today),
    ):
        if block:
            lines.append("")
            lines.extend(block)

    return "\n".join(lines)


def send_telegram_message(text: str, target_chat_id: str | int | None = None) -> None:
    chat = target_chat_id or CHAT_ID
    if not BOT_TOKEN or not chat:
        raise ValueError("Не заданы BOT_TOKEN или CHAT_ID в файле .env")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=15)
    resp.raise_for_status()


def main() -> None:
    test_mode = "--test" in sys.argv

    if not test_mode and already_sent_today():
        logging.info("Дайджест за сегодня уже был отправлен. Повторный запуск отменён.")
        return

    try:
        message = build_digest()
        if test_mode:
            print(message)
            return

        send_telegram_message(message)
        mark_sent_today()
        logging.info("Утренний экспресс успешно отправлен в Telegram.")
    except Exception as e:
        logging.error(f"ОШИБКА: {e}")


if __name__ == "__main__":
    main()
