import os
import tempfile
import subprocess
import requests
from fastapi import FastAPI, Request

from openai import OpenAI

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def tg_send_message(chat_id: int, text: str) -> None:
    requests.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=30)

def tg_get_file_path(file_id: str) -> str | None:
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30)
    data = r.json()
    return data.get("result", {}).get("file_path")

def tg_download_file(file_path: str, dst_path: str) -> None:
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dst_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)

def convert_to_mp3(src_path: str, dst_path: str) -> None:
    # универсально: ogg/opus → mp3 (и многие другие тоже)
    subprocess.run(
        ["ffmpeg", "-y", "-i", src_path, "-vn", "-acodec", "libmp3lame", "-q:a", "4", dst_path],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    update = await req.json()
    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}

    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return {"ok": True}

    voice = message.get("voice")
    audio = message.get("audio")
    file_id = (voice or audio or {}).get("file_id")

    if not file_id:
        return {"ok": True}

    # можно сразу ответить “принял”
    tg_send_message(chat_id, "Принял. Распознаю…")

    file_path = tg_get_file_path(file_id)
    if not file_path:
        tg_send_message(chat_id, "Не смог получить файл из Telegram.")
        return {"ok": True}

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "input")
        mp3 = os.path.join(tmp, "audio.mp3")

        tg_download_file(file_path, src)

        # конвертируем (для voice почти всегда нужно)
        try:
            convert_to_mp3(src, mp3)
            audio_path = mp3
        except Exception:
            # если ffmpeg не сработал (или файл уже нормальный) — пробуем как есть
            audio_path = src

        with open(audio_path, "rb") as f:
            tr = client.audio.transcriptions.create(
                model="gpt-4o-transcribe",
                file=f,
                # language="ru",  # можно включить, если почти всегда русский
            )

    text = (tr.text or "").strip()
    if not text:
        tg_send_message(chat_id, "Не смог распознать (пусто). Попробуй записать ближе к микрофону.")
    else:
        # лимит Telegram по длине — просто подстрахуемся
        if len(text) > 3500:
            text = text[:3500] + "…"
        tg_send_message(chat_id, text)

    return {"ok": True}

@app.get("/")
def health():
    return {"status": "ok"}
