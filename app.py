import os
import re
import time
import base64
import tempfile
import subprocess
from typing import Optional, Dict, Any, List, Tuple

import requests
from fastapi import FastAPI, Request
from openai import OpenAI

# ===================== ENV =====================
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")  # "owner/repo"
GITHUB_LABELS = os.environ.get("GITHUB_LABELS", "")

# In groups: require /ticket to avoid noise (recommended)
REQUIRE_TICKET_COMMAND = os.environ.get("REQUIRE_TICKET_COMMAND", "true").lower() in ("1", "true", "yes", "y")
ARM_TTL_SECONDS = int(os.environ.get("ARM_TTL_SECONDS", "120"))  # /ticket -> wait next voice within TTL

# WebApp URLs (Netlify)
def _ensure_https(url: str) -> str:
    """Ensure URL has https:// prefix (required by Telegram WebApp)."""
    url = url.strip()
    if not url:
        return ""
    if not url.startswith(("https://", "http://")):
        url = "https://" + url
    return url

WEBAPP_URL_PRODUCTION = _ensure_https(os.environ.get("WEBAPP_URL_PRODUCTION", ""))
WEBAPP_URL_DEV_1 = _ensure_https(os.environ.get("WEBAPP_URL_DEV_1", ""))
WEBAPP_URL_DEV_2 = _ensure_https(os.environ.get("WEBAPP_URL_DEV_2", ""))
WEBAPP_DEV_1_NAME = os.environ.get("WEBAPP_DEV_1_NAME", "Dev 1")
WEBAPP_DEV_2_NAME = os.environ.get("WEBAPP_DEV_2_NAME", "Dev 2")

# Developer mapping: Telegram user_id → dev branch & label
# Format: "tg_user_id1:branch1:label1,tg_user_id2:branch2:label2"
# Example: "123456:dev/alice:developer:alice,789012:dev/bob:developer:bob"
_DEV_MAP_RAW = os.environ.get("DEVELOPER_MAP", "")

def _parse_developer_map(raw: str) -> Dict[int, Dict[str, str]]:
    result: Dict[int, Dict[str, str]] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":", 2)
        if len(parts) < 3:
            print(f"[WARN] Invalid DEVELOPER_MAP entry: {entry!r} (expected tg_id:branch:label)")
            continue
        try:
            tg_id = int(parts[0])
        except ValueError:
            print(f"[WARN] Invalid tg_id in DEVELOPER_MAP: {parts[0]!r}")
            continue
        result[tg_id] = {"branch": parts[1], "label": parts[2]}
    return result

DEVELOPER_MAP: Dict[int, Dict[str, str]] = _parse_developer_map(_DEV_MAP_RAW)

# Debug / versioning
BOT_VERSION = "0.7.4"  # ← mention author in all notifications
BOT_STARTED_AT = int(time.time())
BUILD_ID = os.environ.get("BUILD_ID", os.environ.get("RAILWAY_DEPLOYMENT_ID", os.environ.get("RENDER_GIT_COMMIT", "local")))

# Notifications state
# Maps branch → {chat_id, user_id, first_name} (запоминаем откуда и кто создавал тикеты)
DEV_CHAT: Dict[str, Dict[str, Any]] = {}  # e.g. {"dev/Gleb": {"chat_id": -100123, "user_id": 456, "first_name": "Глеб"}}

client = OpenAI(api_key=OPENAI_API_KEY)

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TG_FILE_API = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"
GH_API = "https://api.github.com"

app = FastAPI()

print(f"[BOT] version={BOT_VERSION} build={BUILD_ID} started_at={BOT_STARTED_AT}")
print(f"[BOT] GITHUB_REPO={GITHUB_REPO} REQUIRE_TICKET_CMD={REQUIRE_TICKET_COMMAND}")
print(f"[BOT] WEBAPP_PROD={WEBAPP_URL_PRODUCTION or '(empty)'}")
print(f"[BOT] WEBAPP_DEV1={WEBAPP_URL_DEV_1 or '(empty)'} ({WEBAPP_DEV_1_NAME})")
print(f"[BOT] WEBAPP_DEV2={WEBAPP_URL_DEV_2 or '(empty)'} ({WEBAPP_DEV_2_NAME})")
print(f"[BOT] DEVELOPER_MAP={DEVELOPER_MAP}")

# ===== In-memory state. For production/multi-instance use Redis. =====
# key = f"{chat_id}:{user_id}"
PENDING: Dict[str, Dict[str, Any]] = {}
ARMED: Dict[str, int] = {}  # key chat_id:user_id -> expires_at unix


# ===================== UTIL =====================
def is_group(chat: dict) -> bool:
    return chat.get("type") in ("group", "supergroup")


def state_key(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"


def now_ts() -> int:
    return int(time.time())


def parse_labels() -> List[str]:
    return [x.strip() for x in GITHUB_LABELS.split(",") if x.strip()]


def html_escape(s: str) -> str:
    """Escape HTML special characters for Telegram HTML parse_mode."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def extract_ticket_command(text: str) -> Tuple[bool, str]:
    t = (text or "").strip()
    if not t:
        return False, ""
    if t.startswith("/ticket"):
        rest = t[len("/ticket"):].strip()
        # Strip @botname suffix if present
        if rest.startswith("@"):
            parts = rest.split(" ", 1)
            rest = parts[1].strip() if len(parts) > 1 else ""
        return True, rest
    return False, ""


# ===================== TELEGRAM HELPERS =====================
def tg_send_message(chat_id: int, text: str, reply_to_message_id: Optional[int] = None) -> None:
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True
    r = requests.post(f"{TG_API}/sendMessage", json=payload, timeout=30)
    resp = r.json()
    if not resp.get("ok"):
        print(f"[TG ERROR] sendMessage failed: {resp.get('error_code')} {resp.get('description')}")


def tg_send_html(chat_id: int, html: str) -> None:
    """Send message with HTML parse_mode (for user mentions etc)."""
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": html, "parse_mode": "HTML"}
    r = requests.post(f"{TG_API}/sendMessage", json=payload, timeout=30)
    resp = r.json()
    if not resp.get("ok"):
        print(f"[TG_HTML ERROR] {resp.get('error_code')} {resp.get('description')}")
        print(f"[TG_HTML ERROR] chat_id={chat_id} text={html[:200]}")
        # Fallback: try without HTML parse_mode
        fallback_payload: Dict[str, Any] = {"chat_id": chat_id, "text": html}
        requests.post(f"{TG_API}/sendMessage", json=fallback_payload, timeout=30)


def tg_mention(user_id: int, first_name: str) -> str:
    """Create Telegram HTML mention link."""
    safe_name = html_escape(first_name)
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def tg_send_message_with_keyboard(
    chat_id: int,
    text: str,
    keyboard: List[List[Dict[str, str]]],
    reply_to_message_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": {"inline_keyboard": keyboard},
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True
    r = requests.post(f"{TG_API}/sendMessage", json=payload, timeout=30)
    resp = r.json()
    if not resp.get("ok"):
        print(f"[TG ERROR] sendMessage failed: {resp.get('error_code')} {resp.get('description')}")
        print(f"[TG ERROR] payload keys: {list(payload.keys())}, keyboard_rows: {len(keyboard)}")
    return resp


def tg_answer_callback(callback_id: str, text: Optional[str] = None, show_alert: bool = False) -> None:
    payload: Dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = show_alert
    requests.post(f"{TG_API}/answerCallbackQuery", json=payload, timeout=10)


def tg_get_file_path(file_id: str) -> Optional[str]:
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30)
    return r.json().get("result", {}).get("file_path")


def tg_download_file(file_path: str, dst_path: str) -> None:
    url = f"{TG_FILE_API}/{file_path}"
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dst_path, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                if chunk:
                    f.write(chunk)


def tg_download_file_bytes(file_path: str) -> bytes:
    url = f"{TG_FILE_API}/{file_path}"
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.content


# ===================== AUDIO =====================
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


# ===================== GITHUB =====================
def gh_headers() -> Dict[str, str]:
    if not GITHUB_TOKEN:
        raise RuntimeError("Missing GITHUB_TOKEN")
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_repo_parts() -> Tuple[str, str]:
    if not GITHUB_REPO or "/" not in GITHUB_REPO:
        raise RuntimeError("Missing or invalid GITHUB_REPO (expected owner/repo)")
    owner, repo = GITHUB_REPO.split("/", 1)
    return owner, repo


def gh_get_default_branch() -> str:
    owner, repo = gh_repo_parts()
    r = requests.get(f"{GH_API}/repos/{owner}/{repo}", headers=gh_headers(), timeout=30)
    r.raise_for_status()
    return r.json()["default_branch"]


def gh_branch_exists(branch: str) -> bool:
    owner, repo = gh_repo_parts()
    r = requests.get(f"{GH_API}/repos/{owner}/{repo}/git/ref/heads/{branch}", headers=gh_headers(), timeout=15)
    return r.status_code == 200


def gh_create_branch(branch: str, from_branch: str = "main") -> str:
    """Create a new branch from an existing branch. Returns the SHA."""
    owner, repo = gh_repo_parts()
    source_sha = gh_get_branch_sha(from_branch)
    r = requests.post(
        f"{GH_API}/repos/{owner}/{repo}/git/refs",
        headers=gh_headers(),
        json={"ref": f"refs/heads/{branch}", "sha": source_sha},
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Create branch failed {r.status_code}: {r.text[:500]}")
    print(f"[BRANCH] Created {branch} from {from_branch} ({source_sha[:7]})")
    return source_sha


def gh_get_branch_sha(branch: str) -> str:
    """Get the latest commit SHA of a branch."""
    owner, repo = gh_repo_parts()
    r = requests.get(f"{GH_API}/repos/{owner}/{repo}/git/ref/heads/{branch}", headers=gh_headers(), timeout=15)
    r.raise_for_status()
    return r.json()["object"]["sha"]


def gh_force_reset_branch(branch: str, to_branch: str = "main") -> str:
    """Force-update branch to point at the same commit as to_branch.
    Returns the SHA it was reset to."""
    owner, repo = gh_repo_parts()
    target_sha = gh_get_branch_sha(to_branch)
    r = requests.patch(
        f"{GH_API}/repos/{owner}/{repo}/git/refs/heads/{branch}",
        headers=gh_headers(),
        json={"sha": target_sha, "force": True},
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Force reset failed {r.status_code}: {r.text[:500]}")
    return target_sha


def gh_put_file(branch: str, path: str, content_bytes: bytes, message: str) -> str:
    owner, repo = gh_repo_parts()
    b64 = base64.b64encode(content_bytes).decode("utf-8")
    payload: Dict[str, Any] = {"message": message, "content": b64, "branch": branch}

    r = requests.put(
        f"{GH_API}/repos/{owner}/{repo}/contents/{path}",
        headers=gh_headers(),
        json=payload,
        timeout=60,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Upload file failed {r.status_code}: {r.text[:500]}")

    data = r.json()
    return data["content"]["html_url"]


def gh_create_issue(title: str, body: str, extra_labels: Optional[List[str]] = None) -> Dict[str, Any]:
    owner, repo = gh_repo_parts()
    payload: Dict[str, Any] = {"title": title, "body": body}
    labels = parse_labels()
    if extra_labels:
        labels.extend(extra_labels)
    if labels:
        payload["labels"] = labels

    r = requests.post(f"{GH_API}/repos/{owner}/{repo}/issues", headers=gh_headers(), json=payload, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Create issue failed {r.status_code}: {r.text[:500]}")
    return r.json()


def gh_update_issue(number: int, body: str) -> None:
    owner, repo = gh_repo_parts()
    r = requests.patch(
        f"{GH_API}/repos/{owner}/{repo}/issues/{number}",
        headers=gh_headers(),
        json={"body": body},
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Update issue failed {r.status_code}: {r.text[:500]}")


def format_issue(text: str, chat_id: int, user: dict, dev_info: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    clean = " ".join(text.split()).strip()
    title = clean
    for sep in [". ", "! ", "? ", "\n"]:
        if sep in title:
            title = title.split(sep, 1)[0]
            break
    title = title[:80].strip() or "Voice ticket"

    username = user.get("username") or f'{user.get("first_name","")} {user.get("last_name","")}'.strip()
    body = f"@claude\n\n{clean}\n\n---\n"
    body += f"Source: Telegram\n"
    body += f"From: {username or 'unknown'}\n"
    body += f"Chat ID: {chat_id}\n"
    if dev_info:
        body += f"Developer: {dev_info['label']}\n"
        body += f"Target branch: `{dev_info['branch']}`\n"
    return {"title": title, "body": body}


# ===================== UI / FLOW =====================
def show_apps_menu(chat_id: int, reply_to_message_id: Optional[int] = None, in_group: bool = False) -> None:
    """Send keyboard with WebApp buttons. In groups use ReplyKeyboard, in DM use InlineKeyboard."""
    print(f"[APPS] show_apps_menu called for chat={chat_id} in_group={in_group}")

    if not WEBAPP_URL_DEV_1 and not WEBAPP_URL_DEV_2:
        print("[APPS] No dev URLs configured — sending error message")
        tg_send_message(chat_id, "Dev URLs не настроены. Задайте WEBAPP_URL_DEV_* в env.", reply_to_message_id=reply_to_message_id)
        return

    if in_group:
        # web_app buttons don't work in groups (Telegram limitation)
        tg_send_message(chat_id,
            "WebApp кнопки работают только в личке. Напиши мне /apps в личные сообщения 👉 @play4good_bot",
            reply_to_message_id=reply_to_message_id)
        return

    # In DM: use InlineKeyboardMarkup with web_app
    keyboard_inline: List[List[Dict[str, Any]]] = []
    if WEBAPP_URL_DEV_1:
        keyboard_inline.append([{"text": f"\U0001f535 Тест — {WEBAPP_DEV_1_NAME}", "web_app": {"url": WEBAPP_URL_DEV_1}}])
    if WEBAPP_URL_DEV_2:
        keyboard_inline.append([{"text": f"\U0001f7e1 Тест — {WEBAPP_DEV_2_NAME}", "web_app": {"url": WEBAPP_URL_DEV_2}}])

    print(f"[APPS] Sending InlineKeyboard with {len(keyboard_inline)} buttons")
    resp = tg_send_message_with_keyboard(
        chat_id,
        "Тестовые приложения:",
        keyboard_inline,
        reply_to_message_id=reply_to_message_id,
    )
    print(f"[APPS] TG response ok={resp.get('ok') if resp else 'None'}")


def confirmation_text(state: Dict[str, Any]) -> str:
    screenshot = state.get("screenshot")
    dev_info = state.get("dev_info")

    meta = []
    meta.append(f"Скриншот: {'✅ есть' if screenshot else '— нет'}")
    if dev_info:
        meta.append(f"Ветка: {dev_info['branch']}")
    else:
        meta.append("Ветка: default (разработчик не привязан)")

    return (
        "Вот что я распознал:\n\n"
        + f"\u201c{state['text']}\u201d\n\n"
        + " | ".join(meta)
        + "\n\n"
        + "Что делаем?"
    )


def show_confirmation(chat_id: int, author_id: int, state: Dict[str, Any], reply_to_message_id: Optional[int] = None) -> None:
    keyboard = [
        [{"text": "✅ Создать issue", "callback_data": f"create:{author_id}"}],
        [{"text": "✏️ Правка текста", "callback_data": f"edit:{author_id}"}],
        [{"text": "📎 Скриншот", "callback_data": f"shot:{author_id}"}],
        [{"text": "❌ Отмена", "callback_data": f"cancel:{author_id}"}],
    ]
    tg_send_message_with_keyboard(chat_id, confirmation_text(state), keyboard, reply_to_message_id=reply_to_message_id)


def extract_image_from_message(msg: dict) -> Optional[Dict[str, Any]]:
    if msg.get("photo"):
        biggest = msg["photo"][-1]
        return {"file_id": biggest["file_id"], "ext": "jpg"}

    doc = msg.get("document")
    if doc and isinstance(doc, dict):
        mime = (doc.get("mime_type") or "").lower()
        if mime.startswith("image/"):
            ext = mime.split("/", 1)[1] or "png"
            ext = ext.replace("jpeg", "jpg")
            return {"file_id": doc["file_id"], "ext": ext}
    return None


# ===================== ROUTES =====================
@app.get("/")
def health():
    uptime = int(time.time()) - BOT_STARTED_AT
    return {
        "ok": True,
        "version": BOT_VERSION,
        "build": BUILD_ID,
        "uptime_sec": uptime,
    }


@app.post("/github/notify")
async def github_notify(req: Request):
    """Receive notifications from GitHub Actions workflow."""
    try:
        payload = await req.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}

    event = payload.get("event", "")
    branch = payload.get("branch", "")
    issue_number = payload.get("issue_number", "")
    issue_title = payload.get("issue_title", "")

    print(f"[GH_NOTIFY] event={event} branch={branch} issue=#{issue_number}")

    dev_ctx = DEV_CHAT.get(branch)
    if not dev_ctx:
        print(f"[GH_NOTIFY] No chat for branch={branch}, DEV_CHAT keys={list(DEV_CHAT.keys())}")
        return {"ok": True, "skipped": "no chat"}

    chat_id = dev_ctx["chat_id"]
    mention = tg_mention(dev_ctx["user_id"], dev_ctx["first_name"])

    safe_title = html_escape(issue_title)
    safe_branch = html_escape(branch)

    if event == "claude_started":
        tg_send_html(chat_id,
            f"🤖 Claude начал работу\n\n"
            f"#{issue_number} ({mention}): {safe_title}\n"
            f"Ветка: {safe_branch}")
    elif event == "opus_unavailable":
        tg_send_html(chat_id,
            f"⚠️ Opus недоступен, переключаюсь на Sonnet\n\n"
            f"#{issue_number} ({mention}): {safe_title}")
    elif event == "claude_failed":
        tg_send_html(chat_id,
            f"❌ Claude упал при работе над <b>#{issue_number}</b> ({mention}): {safe_title}\n"
            f"Попробуй создать тикет ещё раз.")
    elif event == "merged":
        tg_send_html(chat_id,
            f"📦 Изменения в ветке {safe_branch}\n\n"
            f"#{issue_number} ({mention}): {safe_title}\n\n"
            f"Ожидаем деплой...")
    else:
        print(f"[GH_NOTIFY] Unknown event={event}")
        return {"ok": True, "skipped": "unknown event"}

    return {"ok": True, "notified": True}


@app.post("/netlify/webhook")
async def netlify_webhook(req: Request):
    """Receive Netlify deploy notification and notify developer in Telegram."""
    try:
        payload = await req.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}

    state = payload.get("state", "")
    branch = payload.get("branch", "")
    site_name = payload.get("name", "")
    ssl_url = payload.get("ssl_url", "")
    error_message = payload.get("error_message", "")
    commit_msg = payload.get("title", "")

    print(f"[NETLIFY] state={state} branch={branch} site={site_name}")
    print(f"[NETLIFY] DEV_CHAT keys={list(DEV_CHAT.keys())}")

    # Находим контекст разработчика
    dev_ctx = DEV_CHAT.get(branch)
    if not dev_ctx:
        print(f"[NETLIFY] No chat for branch={branch}, skipping")
        return {"ok": True, "skipped": "no chat mapped"}

    chat_id = dev_ctx["chat_id"]
    mention = tg_mention(dev_ctx["user_id"], dev_ctx["first_name"])

    safe_site = html_escape(site_name)
    safe_branch = html_escape(branch)
    safe_commit = html_escape(commit_msg) if commit_msg else ""

    if state == "ready":
        # Skip deploy notifications for screenshot uploads (not real code changes)
        if commit_msg and "Add screenshot for issue" in commit_msg:
            print(f"[NETLIFY] Skipping deploy notification for screenshot commit: {commit_msg}")
            return {"ok": True, "skipped": "screenshot commit"}

        # Skip merge commits — pattern: "text (#123)" from GitHub merge/squash
        if commit_msg and re.search(r"\(#\d+\)$", commit_msg.strip()):
            print(f"[NETLIFY] Skipping deploy notification for merge commit: {commit_msg}")
            return {"ok": True, "skipped": "merge commit"}

        text = f"✅ Деплой готов! {mention}, можно тестировать"
        text += f"\n\nСайт: {safe_site}"
        text += f"\nВетка: {safe_branch}"
        if safe_commit:
            text += f"\nКоммит: {safe_commit}"
        text += f"\n\n🔗 {ssl_url}"
        tg_send_html(chat_id, text)
    elif state == "error":
        safe_error = html_escape(error_message) if error_message else ""
        text = f"❌ Деплой упал ({safe_branch})"
        text += f"\n\nСайт: {safe_site}"
        if safe_error:
            text += f"\nОшибка: {safe_error}"
        tg_send_html(chat_id, text)
    elif state == "building":
        tg_send_html(chat_id,
            f"🔨 Деплой начался\n"
            f"Сайт: {safe_site} | Ветка: {safe_branch}")
    else:
        print(f"[NETLIFY] Ignoring state={state}")
        return {"ok": True, "skipped": state}

    return {"ok": True, "notified": True}


@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    update = await req.json()

    # ---------- DEBUG LOG ----------
    update_id = update.get("update_id", "?")
    msg = update.get("message") or update.get("callback_query", {}).get("message") or {}
    chat_id_log = msg.get("chat", {}).get("id", "?")
    text_log = (msg.get("text") or "")[:50]
    has_voice = bool(msg.get("voice") or msg.get("audio"))
    has_cb = "callback_query" in update
    print(f"[UPD {update_id}] chat={chat_id_log} text={text_log!r} voice={has_voice} cb={has_cb}")

    # ---------- CALLBACK QUERIES ----------
    if "callback_query" in update:
        cq = update["callback_query"]
        cb_id = cq["id"]
        data = (cq.get("data") or "").strip()
        from_user = cq.get("from", {}) or {}
        clicker_id = from_user.get("id")

        msg_obj = cq.get("message", {}) or {}
        chat = msg_obj.get("chat", {}) or {}
        chat_id = chat.get("id")
        reply_to_id = msg_obj.get("message_id")

        try:
            tg_answer_callback(cb_id)
        except Exception:
            pass

        if not chat_id or not clicker_id:
            return {"ok": True}

        parts = data.split(":")
        action = parts[0] if parts else ""
        if len(parts) < 2:
            return {"ok": True}

        try:
            author_id = int(parts[1])
        except ValueError:
            return {"ok": True}

        extra = ":".join(parts[2:]) if len(parts) > 2 else ""

        if clicker_id != author_id:
            tg_answer_callback(cb_id, text="Это не твои кнопки 🙂", show_alert=False)
            return {"ok": True}

        # Reset branch handlers (не зависят от PENDING)
        if action == "reset_confirm":
            dev_info = DEVELOPER_MAP.get(clicker_id)
            if not dev_info:
                tg_send_message(chat_id, "Dev-ветка не найдена.", reply_to_message_id=reply_to_id)
                return {"ok": True}
            branch = dev_info["branch"]
            try:
                sha = gh_force_reset_branch(branch, "main")
                short_sha = sha[:7]
                print(f"[RESET] user={clicker_id} branch={branch} → main ({short_sha})")
                tg_send_message(chat_id, f"✅ Ветка `{branch}` сброшена до `main` ({short_sha}).", reply_to_message_id=reply_to_id)
            except Exception as e:
                print(f"[RESET] ERROR user={clicker_id} branch={branch}: {e}")
                tg_send_message(chat_id, f"❌ Ошибка: {type(e).__name__}\n{e}", reply_to_message_id=reply_to_id)
            return {"ok": True}

        if action == "reset_cancel":
            tg_send_message(chat_id, "Отменил сброс.", reply_to_message_id=reply_to_id)
            return {"ok": True}

        key = state_key(chat_id, author_id)
        state = PENDING.get(key)
        if not state:
            tg_send_message(chat_id, "Черновик не найден. Пришли голосовое ещё раз.", reply_to_message_id=reply_to_id)
            return {"ok": True}

        if action == "noop":
            return {"ok": True}

        if action == "cancel":
            PENDING.pop(key, None)
            tg_send_message(chat_id, "Отменил.", reply_to_message_id=reply_to_id)
            return {"ok": True}

        if action == "edit":
            state["stage"] = "edit"
            tg_send_message(chat_id, "Пришли исправленный текст одним сообщением.", reply_to_message_id=reply_to_id)
            return {"ok": True}

        if action == "shot":
            state["stage"] = "await_screenshot"
            tg_send_message(chat_id, "Ок. Пришли скриншот (картинку) одним сообщением.", reply_to_message_id=reply_to_id)
            return {"ok": True}

        if action == "create":
            try:
                dev_info = DEVELOPER_MAP.get(clicker_id)
                extra_labels: List[str] = []
                if dev_info:
                    extra_labels.append(dev_info["label"])
                    # Ensure dev branch exists (create from main if deleted after merge)
                    if not gh_branch_exists(dev_info["branch"]):
                        try:
                            gh_create_branch(dev_info["branch"], "main")
                        except Exception as e:
                            print(f"[CREATE] Failed to create branch {dev_info['branch']}: {e}")
                    # Запоминаем chat_id ТОЛЬКО при создании тикета
                    DEV_CHAT[dev_info["branch"]] = {
                        "chat_id": chat_id,
                        "user_id": clicker_id,
                        "first_name": from_user.get("first_name", ""),
                    }
                    print(f"[CREATE] Developer: user={clicker_id} → branch={dev_info['branch']} label={dev_info['label']} chat={chat_id}")
                    print(f"[CREATE] DEV_CHAT updated: {dev_info['branch']} → chat_id={chat_id}")
                else:
                    print(f"[CREATE] No developer mapping for user={clicker_id}, using default branch")

                issue_fmt = format_issue(state["text"], chat_id, from_user, dev_info=dev_info)
                issue = gh_create_issue(issue_fmt["title"], issue_fmt["body"], extra_labels=extra_labels)
                issue_url = issue["html_url"]
                issue_number = issue["number"]
                issue_body = issue_fmt["body"]

                # Branch from developer mapping or default
                chosen_branch: Optional[str] = dev_info["branch"] if dev_info else None
                branch_info = f"\nBranch: `{chosen_branch}`" if chosen_branch else ""
                screenshot_info = ""

                # Upload screenshot if present
                if state.get("screenshot"):
                    if not chosen_branch:
                        chosen_branch = gh_get_default_branch()
                        branch_info = f"\nBranch: `{chosen_branch}` (default)"

                    shot = state["screenshot"]
                    file_path = tg_get_file_path(shot["file_id"])
                    if not file_path:
                        raise RuntimeError("Could not resolve screenshot file_path in Telegram")

                    img_bytes = tg_download_file_bytes(file_path)
                    ext = shot.get("ext", "jpg")
                    path_in_repo = f"tickets/issue-{issue_number}/screenshot.{ext}"
                    html_url = gh_put_file(
                        branch=chosen_branch,
                        path=path_in_repo,
                        content_bytes=img_bytes,
                        message=f"Add screenshot for issue #{issue_number}",
                    )
                    screenshot_info = f"\nScreenshot: {html_url}"

                if branch_info or screenshot_info:
                    updated_body = issue_body + "\n\n---\n" + (branch_info + screenshot_info).strip()
                    gh_update_issue(issue_number, updated_body)

                tg_send_message(chat_id,
                    f"📋 Тикет создан!\n\n"
                    f"#{issue_number}: {issue_fmt['title']}\n"
                    f"{issue_url}\n\n"
                    f"Claude скоро возьмётся за работу...",
                    reply_to_message_id=reply_to_id)

            except Exception as e:
                tg_send_message(chat_id, f"Ошибка: {type(e).__name__}\n{e}", reply_to_message_id=reply_to_id)
            finally:
                PENDING.pop(key, None)

            return {"ok": True}

        return {"ok": True}

    # ---------- MESSAGES ----------
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return {"ok": True}

    chat = msg.get("chat", {}) or {}
    chat_id = chat.get("id")
    user = msg.get("from", {}) or {}
    user_id = user.get("id")
    message_id = msg.get("message_id")

    if not chat_id or not user_id:
        return {"ok": True}

    in_group = is_group(chat)
    key = state_key(chat_id, user_id)

    text = (msg.get("text") or "").strip()

    # НЕ перезаписываем DEV_CHAT тут — только при создании тикета (action=="create")

    # Help
    cmd_base = text.lower().split("@")[0]
    if cmd_base in ("/start", "/help", "help"):
        if in_group and REQUIRE_TICKET_COMMAND:
            tg_send_message(chat_id, "В группе: /ticket (и потом голосовое в течение 120 сек) или /ticket <текст>.\n/apps — открыть приложения\n/reset — сбросить dev-ветку до main", reply_to_message_id=message_id)
        else:
            tg_send_message(chat_id, "Пришли голосовое (или /ticket <текст>) — я создам GitHub Issue.\n/apps — открыть приложения\n/reset — сбросить dev-ветку до main", reply_to_message_id=message_id)
        return {"ok": True}

    # Apps menu
    if cmd_base == "/apps":
        print(f"[CMD] /apps from user={user_id} chat={chat_id} group={in_group}")
        show_apps_menu(chat_id, reply_to_message_id=message_id, in_group=in_group)
        return {"ok": True}

    # Debug info
    if cmd_base == "/debug":
        uptime = int(time.time()) - BOT_STARTED_AT
        mins = uptime // 60
        debug_text = (
            f"🔧 Bot debug\n"
            f"Version: {BOT_VERSION}\n"
            f"Build: {BUILD_ID}\n"
            f"Uptime: {mins}m {uptime % 60}s\n"
            f"Pending tickets: {len(PENDING)}\n"
            f"Armed users: {len(ARMED)}\n"
            f"---\n"
            f"GITHUB_REPO: {GITHUB_REPO or '(empty)'}\n"
            f"WEBAPP_PROD: {'✅' if WEBAPP_URL_PRODUCTION else '❌'}\n"
            f"WEBAPP_DEV1: {'✅' if WEBAPP_URL_DEV_1 else '❌'} {WEBAPP_DEV_1_NAME}\n"
            f"WEBAPP_DEV2: {'✅' if WEBAPP_URL_DEV_2 else '❌'} {WEBAPP_DEV_2_NAME}\n"
            f"DEV_CHAT: {DEV_CHAT or '(empty)'}\n"
            f"REQUIRE_TICKET_CMD: {REQUIRE_TICKET_COMMAND}\n"
            f"Group chat: {in_group}\n"
            f"---\n"
            f"This chat_id: {chat_id}\n"
            f"Your user_id: {user_id}\n"
            f"Your dev mapping: {DEVELOPER_MAP.get(user_id, 'not mapped')}\n"
            f"Total devs mapped: {len(DEVELOPER_MAP)}"
        )
        tg_send_message(chat_id, debug_text, reply_to_message_id=message_id)
        return {"ok": True}

    # Reset dev branch to main
    if cmd_base == "/reset":
        dev_info = DEVELOPER_MAP.get(user_id)
        if not dev_info:
            tg_send_message(chat_id, "У тебя нет привязанной dev-ветки. Проверь DEVELOPER_MAP.", reply_to_message_id=message_id)
            return {"ok": True}
        branch = dev_info["branch"]
        keyboard = [
            [{"text": f"⚠️ Да, перезатереть {branch}", "callback_data": f"reset_confirm:{user_id}"}],
            [{"text": "Отмена", "callback_data": f"reset_cancel:{user_id}"}],
        ]
        tg_send_message_with_keyboard(
            chat_id,
            f"Ты уверен? Ветка `{branch}` будет полностью заменена на текущий `main`.\n\n"
            f"Все незамерженные изменения в `{branch}` будут потеряны!",
            keyboard,
            reply_to_message_id=message_id,
        )
        return {"ok": True}

    # If pending exists: allow edit and screenshot
    if key in PENDING:
        state = PENDING[key]

        # screenshot input (photo or image doc)
        img = extract_image_from_message(msg)
        if img:
            state["screenshot"] = {"file_id": img["file_id"], "ext": img["ext"]}
            state["stage"] = "confirm"
            show_confirmation(chat_id, user_id, state, reply_to_message_id=message_id)
            return {"ok": True}

        # text edit
        if state.get("stage") == "edit" and text:
            state["text"] = text
            state["stage"] = "confirm"
            show_confirmation(chat_id, user_id, state, reply_to_message_id=message_id)
            return {"ok": True}

    # Group gating: /ticket arms next voice
    if in_group and REQUIRE_TICKET_COMMAND:
        is_cmd, rest = extract_ticket_command(text)
        if is_cmd and rest:
            # Text ticket
            dev_info = DEVELOPER_MAP.get(user_id)
            PENDING[key] = {
                "stage": "confirm",
                "text": rest,
                "ts": now_ts(),
                "screenshot": None,
                "dev_info": dev_info,
            }
            show_confirmation(chat_id, user_id, PENDING[key], reply_to_message_id=message_id)
            return {"ok": True}

        if is_cmd and not rest:
            ARMED[key] = now_ts() + ARM_TTL_SECONDS
            tg_send_message(
                chat_id,
                f"Ок. Пришли голосовое в течение {ARM_TTL_SECONDS} сек — сделаю черновик тикета.",
                reply_to_message_id=message_id,
            )
            return {"ok": True}

        # ignore other texts in group
        if text and not msg.get("voice") and not msg.get("audio"):
            return {"ok": True}

    # /ticket <text> in DM (outside group gating)
    if text and not in_group:
        is_cmd, rest = extract_ticket_command(text)
        if is_cmd and rest:
            dev_info = DEVELOPER_MAP.get(user_id)
            PENDING[key] = {
                "stage": "confirm",
                "text": rest,
                "ts": now_ts(),
                "screenshot": None,
                "dev_info": dev_info,
            }
            show_confirmation(chat_id, user_id, PENDING[key], reply_to_message_id=message_id)
            return {"ok": True}

    # Plain text outside group gating
    if text and not msg.get("voice") and not msg.get("audio"):
        tg_send_message(chat_id, "Пришли голосовое (или /ticket <текст>). Скриншот можно отправить после распознавания.", reply_to_message_id=message_id)
        return {"ok": True}

    # Voice/audio handling
    file_obj = msg.get("voice") or msg.get("audio")
    if not file_obj:
        return {"ok": True}

    # If in group and require command: accept only if armed
    if in_group and REQUIRE_TICKET_COMMAND:
        expires = ARMED.get(key, 0)
        if expires < now_ts():
            return {"ok": True}

    tg_send_message(chat_id, "Распознаю голос…", reply_to_message_id=message_id)

    try:
        file_path = tg_get_file_path(file_obj["file_id"])
        if not file_path:
            tg_send_message(chat_id, "Не смог получить файл из Telegram (getFile).", reply_to_message_id=message_id)
            return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "input")
            mp3 = os.path.join(tmp, "audio.mp3")
            tg_download_file(file_path, src)

            try:
                ffmpeg_to_mp3(src, mp3)
                audio_path = mp3
            except Exception:
                audio_path = src

            recognized = transcribe(audio_path)

        if not recognized:
            tg_send_message(chat_id, "Не смог распознать 😕", reply_to_message_id=message_id)
            return {"ok": True}

        # consume arm
        ARMED.pop(key, None)

        dev_info = DEVELOPER_MAP.get(user_id)
        PENDING[key] = {
            "stage": "confirm",
            "text": recognized,
            "ts": now_ts(),
            "screenshot": None,
            "dev_info": dev_info,
        }
        show_confirmation(chat_id, user_id, PENDING[key], reply_to_message_id=message_id)
        return {"ok": True}

    except Exception as e:
        tg_send_message(chat_id, f"Ошибка: {type(e).__name__}\n{e}", reply_to_message_id=message_id)
        return {"ok": True}
