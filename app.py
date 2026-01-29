import os
import tempfile
import subprocess
from typing import Optional, Dict, Any, List

import requests
from fastapi import FastAPI, Request
from openai import OpenAI

# ================= ENV =================
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")      # "owner/repo"
GITHUB_LABELS = os.environ.get("GITHUB_LABELS", "")

client = OpenAI(api_key=OPENAI_API_KEY)

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TG_FILE_API = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"

app = FastAPI()

# pending[chat_id] = {"stage": "confirm"|"edit", "text": "..."}
PENDING: Dict[int, Dict[str, Any]] = {}


# ================= TELEGRAM HELPERS =================
def tg_send_message(chat_id: int, text: str) -> None:
    requests.post(
        f"{TG_API}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=30,
    )


def tg_send_message_with_keyboard(chat_id: int, text: str, keyboard: List[List[Dict[str, str]]]) -> None:
    requests.post(
        f"{TG_API}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "reply_markup": {"inline_keyboard": keyboard},
        },
        timeout=30,
    )


def tg_answer_callback(callback_id: str) -> None:
    requests.post(
        f"{TG_API}/answerCallbackQuery",
        json={"callback_query_id": callback_id},
        timeout=10,
    )


def tg_get_file_path(file_id: str) -> Optional[str]:
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30)
    return r.json().get("result", {}).get("file_path")


def tg_download_file(file_path: str, dst_path: str) -> None:
    url = f"{TG_FILE_API}/{file_path}"
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dst_path, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                f.write(chunk)


# ================= AUDIO =================
def ffmpeg_to_mp3(src: str, dst: str) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-vn", "-acodec", "libmp3lame", "-q:a", "4", dst],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def transcribe(audio_path: str) -> str:
    with open(audio_path, "rb") as f:
        res = client.audio.transcriptions.create(
            model="gpt-4o-transcribe",
            file=f,
        )
    return (res.text or "").strip()


# ================= GITHUB =================
def github_create_issue(title: str, body: str) -> str:
    if not GITHUB_TOKEN or not GITHUB_REPO:
        raise RuntimeError("GitHub ENV not configured")

    r = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/issues",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        json={
            "title": title,
            "body": body,
            "labels": [x.strip() for x in GITHUB_LABELS.split(",") if x.strip()],
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["html_url"]


def format_issue(text: str, chat_id: int, user: dict) -> Dict[str, str]:
    title = text.split(".")[0][:80] or "Voice ticket"
    body = (
        f"{text}\n\n---\n"
        f"From Telegram\n"
        f"User: {user.get('username') or user.get('first_name')}\n"
        f"Chat ID: {chat_id}"
    )
    return {"title": title, "body": body}


# ================= UI =================
def show_confirmation(chat_id: int, text: str) -> None:
    tg_send_message_with_keyboard(
        chat_id,
        f"Вот что я распознал:\n\n“{text}”\n\nСоздать GitHub Issue?",
        [
            [{"text": "✅ Создать", "callback_data": "create"}],
            [
                {"text": "✏️ Правка", "callback_data": "edit"},
                {"text": "❌ Отмена", "callback_data": "cancel"},
            ],
        ],
    )


# ================= ROUTES =================
@app.get("/")
def health():
    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    update = await req.json()

    # ---------- BUTTONS ----------
    if "callback_query" in update:
        cq = update["callback_query"]
        tg_answer_callback(cq["id"])

        chat_id = cq["message"]["chat"]["id"]
        action = cq["data"]

        if chat_id not in PENDING:
            tg_send_message(chat_id, "Черновик не найден. Пришли голосовое ещё раз.")
            return {"ok": True}

        state = PENDING[chat_id]

        if action == "create":
            try:
                issue = format_issue(state["text"], chat_id, cq["from"])
                url = github_create_issue(issue["title"], issue["body"])
                tg_send_message(chat_id, f"Готово 🎉\n{url}")
            except Exception as e:
                tg_send_message(chat_id, f"Ошибка создания issue: {e}")
            finally:
                PENDING.pop(chat_id, None)

        elif action == "edit":
            state["stage"] = "edit"
            tg_send_message(chat_id, "Ок, пришли исправленный текст одним сообщением.")

        elif action == "cancel":
            PENDING.pop(chat_id, None)
            tg_send_message(chat_id, "Отменил.")

        return {"ok": True}

    # ---------- MESSAGES ----------
    msg = update.get("message")
    if not msg:
        return {"ok": True}

    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})
    text = (msg.get("text") or "").strip()

    # ---------- EDIT FLOW ----------
    if text and chat_id in PENDING and PENDING[chat_id]["stage"] == "edit":
        PENDING[chat_id]["text"] = text
        PENDING[chat_id]["stage"] = "confirm"
        show_confirmation(chat_id, text)
        return {"ok": True}

    # ---------- TEXT ----------
    if text and not msg.get("voice") and not msg.get("audio"):
        tg_send_message(chat_id, "Пришли голосовое — я сделаю GitHub Issue.")
        return {"ok": True}

    # ---------- VOICE / AUDIO ----------
    file = msg.get("voice") or msg.get("audio")
    if not file:
        return {"ok": True}

    tg_send_message(chat_id, "Распознаю голос…")

    try:
        file_path = tg_get_file_path(file["file_id"])
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "input")
            mp3 = os.path.join(tmp, "audio.mp3")
            tg_download_file(file_path, src)
            try:
                ffmpeg_to_mp3(src, mp3)
                audio_path = mp3
            except Exception:
                audio_path = src

            text_out = transcribe(audio_path)

        if not text_out:
            tg_send_message(chat_id, "Не смог распознать 😕")
            return {"ok": True}

        PENDING[chat_id] = {"stage": "confirm", "text": text_out}
        show_confirmation(chat_id, text_out)

    except Exception as e:
        tg_send_message(chat_id, f"Ошибка: {e}")

    return {"ok": True}
