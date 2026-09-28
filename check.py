import hashlib
import json
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import fitz  # PyMuPDF
import requests

BOT = os.environ["BOT_TOKEN"]
WORKER = os.environ["WORKER_URL"].rstrip("/")
API_SECRET = os.environ["API_SECRET"]
ONLY_CHAT = os.environ.get("CHAT_ID", "").strip()  # заполнено при ручной команде /check
STATE = "state/last.json"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
API = f"https://api.telegram.org/bot{BOT}/"


def candidate_urls():
    """Текущий месяц, затем прошлый (в начале месяца файл может ещё лежать в старой папке)."""
    now = datetime.now(ZoneInfo("Asia/Novosibirsk"))
    y, m = now.year, now.month
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    return [
        f"https://www.bgtc.su/wp-content/uploads/{yy}/{mm:02d}/zamena2k.pdf"
        for yy, mm in ((y, m), (py, pm))
    ]


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    os.makedirs("state", exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def fetch(st):
    """Возвращает (url, data, etag). data=None, если файл не изменился (304). url=None, если не найден."""
    for u in candidate_urls():
        h = dict(UA)
        if not ONLY_CHAT and st.get("url") == u and st.get("etag"):
            h["If-None-Match"] = st["etag"]
        r = requests.get(u, headers=h, timeout=60)
        if r.status_code == 304:
            return u, None, st["etag"]
        if r.status_code == 200 and r.content[:4] == b"%PDF":
            return u, r.content, r.headers.get("ETag", "")
    return None, None, None


def render(data):
    doc = fitz.open(stream=data, filetype="pdf")
    pages = []
    for p in doc:
        w, h = p.rect.width, p.rect.height
        z = min(200 / 72, 9500 / (w + h))  # 200 dpi, но w+h <= 10000 px (лимит Telegram)
        pix = p.get_pixmap(matrix=fitz.Matrix(z, z))
        img = pix.tobytes("png")
        if len(img) > 9_500_000:  # лимит фото 10 МБ
            img = pix.tobytes("jpg", jpg_quality=90)
        pages.append(img)
    mt = re.search(r"\bна\s+\d{1,2}\s+[а-я]+\s*-\s*[а-я]+", doc[0].get_text(), re.I)
    return pages, (mt.group(0) if mt else "")


def send(chat, pages, caption):
    for i in range(0, len(pages), 10):
        chunk = pages[i : i + 10]
        cap = caption if i == 0 else ""
        if len(chunk) == 1:
            r = requests.post(
                API + "sendPhoto",
                data={"chat_id": chat, "caption": cap},
                files={"photo": ("schedule.png", chunk[0])},
                timeout=120,
            )
        else:
            media = [{"type": "photo", "media": f"attach://f{j}"} for j in range(len(chunk))]
            if cap:
                media[0]["caption"] = cap
            r = requests.post(
                API + "sendMediaGroup",
                data={"chat_id": chat, "media": json.dumps(media)},
                files={f"f{j}": (f"{j}.png", b) for j, b in enumerate(chunk)},
                timeout=180,
            )
        if not r.ok:
            print("send failed", chat, r.status_code, r.text[:200])


def notify(chat, text):
    requests.post(API + "sendMessage", data={"chat_id": chat, "text": text}, timeout=30)


def main():
    st = load_state()
    url, data, etag = fetch(st)

    if not url:
        print("PDF не найден ни в текущем, ни в прошлом месяце")
        if ONLY_CHAT:
            notify(ONLY_CHAT, "Не удалось получить расписание: файл не найден.")
        return

    if ONLY_CHAT:  # ручная команда /check: всегда шлём текущее
        pages, d = render(data)
        send(ONLY_CHAT, pages, f"📅 Текущее расписание {d}".strip())
        return

    if data is None:
        print("Не изменилось (304)")
        return

    h = hashlib.sha256(data).hexdigest()
    if h == st.get("hash"):
        print("Содержимое то же")
        save_state({**st, "etag": etag, "url": url})
        return

    subs = requests.get(
        WORKER + "/subs", headers={"Authorization": "Bearer " + API_SECRET}, timeout=30
    )
    subs.raise_for_status()
    subs = subs.json()
    if not subs:
        print("Нет подписчиков, состояние не сохраняю")
        return

    pages, d = render(data)
    for chat in subs:
        send(chat, pages, f"📅 Расписание обновлено {d}".strip())
        time.sleep(0.1)

    save_state({"hash": h, "etag": etag, "url": url, "updated": datetime.now().isoformat()})
    print("Отправлено:", len(subs))


if __name__ == "__main__":
    main()
