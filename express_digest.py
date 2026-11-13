import os
import sys
import json
import random
import logging
import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TOMTOM_API_KEY = os.getenv("TOMTOM_API_KEY", "").strip()
YANDEX_RASP_API_KEY = os.getenv("YANDEX_RASP_API_KEY", "").strip()

MSK_TZ = ZoneInfo("Europe/Moscow")
LOG_FILE = BASE_DIR / "express.log"
STATE_FILE = BASE_DIR / "last_sent.txt"
STATIONS_CACHE_FILE = BASE_DIR / "stations_cache.json"

WEEKDAYS_RU = [
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье"
]

MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
]

# Координаты станций для автоматического определения кодов Яндекс.Расписаний
STATION_COORDS = {
    "golitsyno": (55.6152, 36.9866),
    "belorussky": (55.7764, 37.5815),
    "selyatino": (55.5249, 36.9806),
    "aprelevka": (55.5453, 37.0736),
    "kievsky": (55.7436, 37.5673),
}

# Маршруты от Калининца до МКАД с промежуточными точками для фиксации шоссе
TRAFFIC_ROUTES = [
    ("Киевское ш.", "55.5665,36.9685:55.5245,36.9825:55.6378,37.4565"),
    ("Минское ш.", "55.5665,36.9685:55.6215,36.9935:55.7155,37.3825"),
]

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    encoding="utf-8"
)


def now_msk() -> datetime.datetime:
    return datetime.datetime.now(MSK_TZ)


def already_sent_today() -> bool:
    today_str = now_msk().date().isoformat()
    if STATE_FILE.exists():
        return STATE_FILE.read_text(encoding="utf-8").strip() == today_str
    return False


def mark_sent_today() -> None:
    today_str = now_msk().date().isoformat()
    STATE_FILE.write_text(today_str + "\n", encoding="utf-8")


def get_station_code(key: str) -> str | None:
    """Находит и кэширует код ж/д станции Яндекс.Расписаний по координатам."""
    cache = {}
    if STATIONS_CACHE_FILE.exists():
        try:
            cache = json.loads(STATIONS_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    if key in cache:
        return cache[key]

    lat, lng = STATION_COORDS[key]
    try:
        resp = requests.get(
            "https://api.rasp.yandex.net/v3.0/nearest_stations/",
            params={
                "apikey": YANDEX_RASP_API_KEY,
                "lat": lat,
                "lng": lng,
                "distance": 2,
                "transport_types": "suburban",
                "lang": "ru_RU",
            },
            timeout=10,
        )
        resp.raise_for_status()
        stations = resp.json().get("stations", [])
        if stations:
            code = stations[0]["code"]
            cache[key] = code
            STATIONS_CACHE_FILE.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return code
    except Exception as e:
        logging.warning(f"Не удалось определить код станции {key}: {e}")
    return None


def fetch_upcoming_trains(from_key: str, to_key: str, limit: int = 3) -> list[str]:
    """Возвращает список ближайших электричек в формате 'HH:MM (XX мин)'."""
    from_code = get_station_code(from_key)
    to_code = get_station_code(to_key)
    if not from_code or not to_code:
        return []

    current_dt = now_msk()
    today_str = current_dt.date().isoformat()

    try:
        resp = requests.get(
            "https://api.rasp.yandex.net/v3.0/search/",
            params={
                "apikey": YANDEX_RASP_API_KEY,
                "from": from_code,
                "to": to_code,
                "date": today_str,
                "transport_types": "suburban",
                "limit": 100,
            },
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()
        segments = data.get("segments", [])

        upcoming = []
        for seg in segments:
            dep_raw = seg.get("departure")
            if not dep_raw:
                continue
            dep_dt = datetime.datetime.fromisoformat(dep_raw)
            if dep_dt >= current_dt - datetime.timedelta(minutes=2):
                mins = round(seg.get("duration", 0) / 60)
                express = " ⚡" if seg.get("thread", {}).get("express_type") else ""
                upcoming.append(f"<b>{dep_dt.strftime('%H:%M')}</b>{express} <i>({mins} мин)</i>")
                if len(upcoming) >= limit:
                    break

        if upcoming:
            return upcoming

        # Если на сегодня рейсов больше нет (поздний вечер), проверяем интервальные сегменты МЦД
        intervals = data.get("interval_segments", [])
        if intervals:
            iv = intervals[0].get("interval", {})
            mins = round(intervals[0].get("duration", 0) / 60)
            return [f"по интервалу каждые {iv.get('density', '5–10')} мин <i>(~{mins} мин в пути)</i>"]

    except Exception as e:
        logging.warning(f"Ошибка получения расписания {from_key}->{to_key}: {e}")
    return []


def get_trains_block(limit_per_line: int = 3) -> list[str]:
    """Блок электричек: МЦД-1 (Голицыно) первым, затем МЦД-4 (Селятино)."""
    if not YANDEX_RASP_API_KEY:
        return []

    lines = []
    mcd1 = fetch_upcoming_trains("golitsyno", "belorussky", limit=limit_per_line)
    if mcd1:
        lines.append(f"   • <b>Голицыно → Белорусский вкз. (МЦД-1):</b> {', '.join(mcd1)}")

    mcd4 = fetch_upcoming_trains("selyatino", "kievsky", limit=limit_per_line)
    if mcd4:
        lines.append(f"   • <b>Селятино → Киевский вкз. (МЦД-4):</b> {', '.join(mcd4)}")

    if not lines:
        return []
    return ["🚆 <b>Ближайшие электрички в Москву:</b>", *lines]


def get_traffic_block() -> list[str]:
    """Считает время в пути от Калининца до МКАД по Киевскому и Минскому шоссе."""
    if not TOMTOM_API_KEY:
        return []

    results = []
    for road_name, coords in TRAFFIC_ROUTES:
        try:
            url = f"https://api.tomtom.com/routing/1/calculateRoute/{coords}/json"
            resp = requests.get(
                url,
                params={
                    "key": TOMTOM_API_KEY,
                    "traffic": "true",
                    "travelMode": "car",
                },
                timeout=10,
            )
            resp.raise_for_status()
            summary = resp.json()["routes"][0]["summary"]
            travel_min = round(summary["travelTimeInSeconds"] / 60)
            delay_min = round(summary.get("trafficDelayInSeconds", 0) / 60)

            if delay_min >= 15:
                status = f"🔴 пробка +{delay_min} мин"
            elif delay_min >= 5:
                status = f"🟡 задержка +{delay_min} мин"
            else:
                status = "🟢 свободно"

            results.append(f"   • <b>{road_name}:</b> ~{travel_min} мин <i>({status})</i>")
        except Exception as e:
            logging.warning(f"Ошибка TomTom для {road_name}: {e}")

    if not results:
        return []
    return ["🚗 <b>Дорога до МКАД:</b>", *results]


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


def build_digest() -> str:
    today = now_msk().date()
    weekday_str = WEEKDAYS_RU[today.weekday()]
    date_str = f"{today.day} {MONTHS_RU[today.month - 1]}"

    lines = [f"☕ <b>Утренний экспресс • {weekday_str}, {date_str}</b>"]

    for block in (
        get_trains_block(limit_per_line=3),
        get_traffic_block(),
        get_rates_block(),
        get_calendar_block(today),
        get_fact_of_the_day(today),
    ):
        if block:
            lines.append("")
            lines.extend(block)

    return "\n".join(lines)


def send_telegram_message(text: str, target_chat_id: str | int | None = None, reply_markup: dict | None = None) -> None:
    chat = target_chat_id or CHAT_ID
    if not BOT_TOKEN or not chat:
        raise ValueError("Не заданы BOT_TOKEN или CHAT_ID в файле .env")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    resp = requests.post(url, data=payload, timeout=15)
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
