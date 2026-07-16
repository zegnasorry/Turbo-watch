# -*- coding: utf-8 -*-
"""
Скрипт мониторинга новых объявлений на turbo.az.
Каждый запуск: скачивает список последних объявлений, сравнивает
с уже отправленными (seen_ids.json), новые — шлёт в Telegram-канал.

ВАЖНО: turbo.az закрыт Cloudflare (JS-challenge), поэтому обычный
requests.get() возвращает 403. Вместо него используется headless-браузер
Playwright, который реально проходит проверку, как настоящий браузер.
"""

import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

LISTING_URL = "https://turbo.az/autos"
SEEN_FILE = "seen_ids.json"
MAX_PER_RUN = 15

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL = os.environ.get("TELEGRAM_CHANNEL")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen_ids):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_ids), f, ensure_ascii=False)


CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "cf-browser-verification",
    "checking your browser",
    "cf-challenge",
)


def _looks_like_challenge(page):
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""
    if any(m in title for m in CHALLENGE_MARKERS):
        return True
    try:
        html_snippet = page.content()[:2000].lower()
    except Exception:
        html_snippet = ""
    return any(m in html_snippet for m in CHALLENGE_MARKERS)


def _save_debug_artifacts(page, tag):
    """Сохраняет скриншот и HTML для диагностики, если что-то пошло не так."""
    os.makedirs("debug", exist_ok=True)
    try:
        page.screenshot(path=f"debug/{tag}.png", full_page=True)
    except Exception as e:
        print("Не удалось сохранить скриншот:", e)
    try:
        with open(f"debug/{tag}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception as e:
        print("Не удалось сохранить HTML:", e)


def fetch_page_html(retries=2):
    """
    Открывает LISTING_URL в headless-браузере, ждёт прохождения
    Cloudflare-проверки и возвращает готовый HTML страницы.
    """
    last_error = None

    for attempt in range(1, retries + 2):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    locale="az-AZ",
                    viewport={"width": 1366, "height": 900},
                )
                page = context.new_page()

                # маскируем самые очевидные признаки headless/автоматизации
                page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )

                page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=60000)

                # Ждём прохождения Cloudflare challenge, если он есть,
                # проверяя каждые 2 секунды до 30 секунд суммарно.
                waited = 0
                while _looks_like_challenge(page) and waited < 30:
                    print(f"Попытка {attempt}: похоже на Cloudflare challenge, жду... ({waited}s)")
                    page.wait_for_timeout(2000)
                    waited += 2

                if _looks_like_challenge(page):
                    print(f"Попытка {attempt}: challenge не прошёл за 30с.")
                    _save_debug_artifacts(page, f"challenge_attempt{attempt}")
                    browser.close()
                    raise RuntimeError("Cloudflare challenge не пройден за отведённое время")

                try:
                    page.wait_for_selector("a[href^='/autos/']", timeout=20000)
                except Exception:
                    print(f"Попытка {attempt}: карточки не появились. Заголовок страницы: {page.title()!r}")
                    _save_debug_artifacts(page, f"noselector_attempt{attempt}")
                    browser.close()
                    raise

                html = page.content()
                browser.close()
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
