# -*- coding: utf-8 -*-
"""
Скрипт мониторинга новых объявлений на turbo.az.
Каждый запуск: скачивает список последних объявлений, сравнивает
с уже отправленными (seen_ids.json), новые — шлёт в Telegram-канал.

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

        listings.append({
            "id": ad_id,
            "title": title,
            "price": price,
            "specs": specs,
            "link": real_link,
        })

    return listings


def send_to_telegram(item):
    text = (
        f"🚗 <b>{item['title']}</b>\n"
        f"{item['specs']}\n"
        f"💰 {item['price']}\n"
        f"{item['link']}"
    )
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    r = requests.post(url, data=payload, timeout=20)
    if not r.ok:
        print("Ошибка отправки в Telegram:", r.status_code, r.text)
    return r.ok


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
        ok = send_to_telegram(item)
        if ok:
            seen.add(item["id"])
            print("Отправлено:", item["title"], item["price"])
        time.sleep(1.5)

    if len(seen) > 5000:
        seen = set(sorted(seen)[-5000:])

    save_seen(seen)


if __name__ == "__main__":
    main()
