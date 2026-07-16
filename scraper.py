# -*- coding: utf-8 -*-
"""
Скрипт мониторинга новых объявлений на turbo.az.
Каждый запуск: скачивает список последних объявлений, сравнивает
с уже отправленными (seen_ids.json), новые — шлёт в Telegram-канал,
сохраняет в Supabase и рассылает лично юзерам, чьи фильтры совпали.

ВАЖНО: turbo.az закрыт Cloudflare (JS-challenge), поэтому обычный
requests.get() возвращает 403. Вместо него HTML получаем через ScrapingBee
(mode=auto — сервис сам подбирает минимально необходимый уровень
обхода защиты и списывает кредиты только за успешный вариант).
"""

import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup

LISTING_URL = "https://turbo.az/autos"
SEEN_FILE = "seen_ids.json"
MAX_PER_RUN = 15

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL = os.environ.get("TELEGRAM_CHANNEL")
SCRAPINGBEE_API_KEY = os.environ.get("SCRAPINGBEE_API_KEY")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

SCRAPINGBEE_ENDPOINT = "https://app.scrapingbee.com/api/v1/"


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen_ids):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_ids), f, ensure_ascii=False)


def _save_debug_artifact(html, tag):
    """Сохраняет полученный HTML для диагностики, если что-то пошло не так."""
    os.makedirs("debug", exist_ok=True)
    try:
        with open(f"debug/{tag}.html", "w", encoding="utf-8") as f:
            f.write(html or "")
    except Exception as e:
        print("Не удалось сохранить HTML:", e)


def fetch_page_html(retries=2):
    """
    Получает HTML страницы через ScrapingBee (mode=auto — сервис сам
    подбирает самый дешёвый вариант прокси/рендеринга, который реально
    проходит защиту сайта, и списывает кредиты только за то, что сработало).
    """
    if not SCRAPINGBEE_API_KEY:
        raise SystemExit("Не задан SCRAPINGBEE_API_KEY (проверь GitHub Secrets).")

    last_error = None
    params = {
        "api_key": SCRAPINGBEE_API_KEY,
        "url": LISTING_URL,
        "mode": "auto",
    }

    for attempt in range(1, retries + 2):
        try:
            resp = requests.get(SCRAPINGBEE_ENDPOINT, params=params, timeout=90)
            if not resp.ok:
                print(f"Попытка {attempt}: статус ScrapingBee {resp.status_code}")
                print(resp.text[:500])
                _save_debug_artifact(resp.text, f"error_attempt{attempt}")
                resp.raise_for_status()

            html = resp.text
            if "/autos/" not in html:
                print(f"Попытка {attempt}: в ответе не найдено ссылок на объявления.")
                _save_debug_artifact(html, f"noautos_attempt{attempt}")
                raise RuntimeError("В ответе ScrapingBee не найдено объявлений")

            return html

        except Exception as e:
            last_error = e
            print(f"Попытка {attempt} не удалась: {e}")
            time.sleep(3)

    raise last_error


def _parse_number(raw):
    """Достаёт число из строки вида '220 000 km' или '50 000 ₼' -> 220000 / 50000."""
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits else None


def enrich_item(item, card_text):
    """
    Достаёт из title/specs/text отдельные поля (бренд, модель, год, пробег,
    цена как число) — нужны для фильтров и записи в Supabase.
    Если что-то не распознаётся — оставляем None, не роняем скрипт.
    """
    title = item["title"]
    brand, model = None, None
    parts = title.split(",")[0].strip().split(" ", 1)
    if len(parts) == 2:
        brand, model = parts[0], parts[1]
    elif len(parts) == 1:
        brand = parts[0]

    year_match = re.search(r"\b(19[89]\d|20[0-3]\d)\b", card_text)
    year = int(year_match.group(1)) if year_match else None

    mileage_match = re.search(r"([\d\s]{3,})\s*km", card_text)
    mileage = _parse_number(mileage_match.group(1)) if mileage_match else None

    price_numeric = _parse_number(item["price"])

    item.update({
        "brand": brand,
        "model": model,
        "year": year,
        "mileage": mileage,
        "price_numeric": price_numeric,
    })
    return item


def fetch_listings():
    html = fetch_page_html()
    soup = BeautifulSoup(html, "html.parser")

    listings = []
    seen_ids_on_page = set()

    for a in soup.find_all("a", href=re.compile(r"^/autos/\d+-")):
        m = re.match(r"^/autos/(\d+)-", a["href"])
        if not m:
            continue
        ad_id = m.group(1)
        if ad_id in seen_ids_on_page:
            continue
        seen_ids_on_page.add(ad_id)

        card = a
        text = ""
        for _ in range(4):
            card = card.parent
            if card is None:
                break
            text = card.get_text(separator="\n", strip=True)
            if len(text) > 20:
                break

        img = card.find("img") if card else None
        title = (img.get("alt").strip() if img and img.get("alt") else None) or a.get_text(strip=True)

        price_match = re.search(r"[\d\s]{3,}\s*₼", text)
        price = price_match.group(0).strip() if price_match else "цена не указана"

        specs_match = re.search(r"\d{4},\s*[\d.]+\s*L,\s*[\d\s]+\s*km", text)
        specs = specs_match.group(0).strip() if specs_match else ""

        real_link = "https://turbo.az" + a["href"]

        item = {
            "id": ad_id,
            "title": title,
            "price": price,
            "specs": specs,
            "link": real_link,
        }
        item = enrich_item(item, text)
        listings.append(item)

    return listings


def send_to_telegram(chat_id, item):
    text = (
        f"🚗 <b>{item['title']}</b>\n"
        f"{item['specs']}\n"
        f"💰 {item['price']}\n"
        f"{item['link']}"
    )
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    r = requests.post(url, data=payload, timeout=20)
    if not r.ok:
        print("Ошибка отправки в Telegram:", chat_id, r.status_code, r.text)
    return r.ok


# ---------------------------------------------------------------------------
# Supabase: сохранение объявлений + получение юзеров с подходящим фильтром
# ---------------------------------------------------------------------------

def supabase_configured():
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def save_listing_to_supabase(item):
    """Пишет объявление в таблицу listings. Дубликаты (turbo_id) игнорируются."""
    if not supabase_configured():
        return
    url = f"{SUPABASE_URL}/rest/v1/listings"
    headers = _supabase_headers()
    headers["Prefer"] = "resolution=ignore-duplicates,return=minimal"
    payload = {
        "turbo_id": item["id"],
        "brand": item.get("brand"),
        "model": item.get("model"),
        "year": item.get("year"),
        "price": item.get("price_numeric"),
        "mileage": item.get("mileage"),
        "url": item["link"],
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if not r.ok:
            print("Supabase insert error:", r.status_code, r.text)
    except Exception as e:
        print("Supabase insert exception:", e)


def get_matching_users(item):
    """
    Возвращает список user_id, чьи фильтры (users_filters) подходят
    под данное объявление. Если Supabase не настроен — пустой список
    (бот просто продолжит работать только через канал).
    """
    if not supabase_configured():
        return []
    if item.get("price_numeric") is None:
        return []

    url = f"{SUPABASE_URL}/rest/v1/users_filters"
    params = {"active": "eq.true", "select": "*"}
    try:
        r = requests.get(url, headers=_supabase_headers(), params=params, timeout=20)
        if not r.ok:
            print("Supabase filters fetch error:", r.status_code, r.text)
            return []
        filters = r.json()
    except Exception as e:
        print("Supabase filters fetch exception:", e)
        return []

    matched = []
    for f in filters:
        if f.get("brand") and item.get("brand"):
            if f["brand"].strip().lower() != item["brand"].strip().lower():
                continue
        if f.get("price_min") is not None and item["price_numeric"] < f["price_min"]:
            continue
        if f.get("price_max") is not None and item["price_numeric"] > f["price_max"]:
            continue
        if f.get("year_min") is not None and (item.get("year") is None or item["year"] < f["year_min"]):
            continue
        if f.get("year_max") is not None and (item.get("year") is None or item["year"] > f["year_max"]):
            continue
        matched.append(f["user_id"])

    return matched


def main():
    if not BOT_TOKEN or not CHANNEL:
        raise SystemExit(
            "Не заданы TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHANNEL "
            "(проверь GitHub Secrets)."
        )

    seen = load_seen()
    listings = fetch_listings()
    print(f"Найдено на странице: {len(listings)} объявлений")

    new_items = [item for item in listings if item["id"] not in seen]
    print(f"Новых: {len(new_items)}")

    new_items = list(reversed(new_items))[:MAX_PER_RUN]

    for item in new_items:
        ok = send_to_telegram(CHANNEL, item)
        if ok:
            seen.add(item["id"])
            print("Отправлено в канал:", item["title"], item["price"])

        save_listing_to_supabase(item)

        matched_users = get_matching_users(item)
        for user_id in matched_users:
            send_to_telegram(user_id, item)
            time.sleep(0.5)
        if matched_users:
            print(f"  -> персонально отправлено {len(matched_users)} юзерам")

        time.sleep(1.5)

    if len(seen) > 5000:
        seen = set(sorted(seen)[-5000:])

    save_seen(seen)


if __name__ == "__main__":
    main()
