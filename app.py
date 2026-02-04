import os
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
BOT_VERSION = "0.6.0"  # ← added Netlify deploy notifications
BOT_STARTED_AT = int(time.time())
BUILD_ID = os.environ.get("BUILD_ID", os.environ.get("RAILWAY_DEPLOYMENT_ID", os.environ.get("RENDER_GIT_COMMIT", "local")))

# Netlify deploy notifications
# Maps branch → chat_id (запоминаем откуда разработчик создавал тикеты)
DEV_CHAT: Dict[str, int] = {}  # e.g. {"dev/Gleb": -100123456789}

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


def extract_ticket_command(text: str) -> Tuple[bool, str]:
    t = (text or "").strip()
    if not t:
        return False, ""
    if t.startswith("/ticket"):
        rest = t[len("/ticket") :].strip()
        return True, rest
    return False, ""


# ===================== TELEGRAM HELPERS =====================
def tg_send_message(chat_id: int, text: str, reply_to_message_id: Optional[int] = None) -> None:
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True
    requests.post(f"{TG_API}/sendMessage", json=payload, timeout=30)


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
            # language="ru",
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
def show_apps_menu(chat_id: int, reply_to_message_id: Optional[int] = None) -> None:
    """Send inline keyboard with WebApp buttons for dev/test environments."""
    print(f"[APPS] show_apps_menu called for chat={chat_id}")
    print(f"[APPS] WEBAPP_URL_DEV_1={WEBAPP_URL_DEV_1!r}")
    print(f"[APPS] WEBAPP_URL_DEV_2={WEBAPP_URL_DEV_2!r}")

    keyboard: List[List[Dict[str, Any]]] = []

    if WEBAPP_URL_DEV_1:
        keyboard.append([{
            "text": f"\U0001f535 Тест — {WEBAPP_DEV_1_NAME}",
            "web_app": {"url": WEBAPP_URL_DEV_1},
        }])
    if WEBAPP_URL_DEV_2:
        keyboard.append([{
            "text": f"\U0001f7e1 Тест — {WEBAPP_DEV_2_NAME}",
            "web_app": {"url": WEBAPP_URL_DEV_2},
        }])

    if not keyboard:
        print("[APPS] No dev URLs configured — sending error message")
        tg_send_message(chat_id, "Dev URLs не настроены. Задайте WEBAPP_URL_DEV_* в env.", reply_to_message_id=reply_to_message_id)
        return

    print(f"[APPS] Sending keyboard with {len(keyboard)} buttons")
    resp = tg_send_message_with_keyboard(
        chat_id,
        "Тестовые приложения:",
        keyboard,
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


def _branch_to_dev() -> Dict[str, Dict[str, Any]]:
    """Build reverse map: branch name → {tg_id, label, name}."""
    result: Dict[str, Dict[str, Any]] = {}
    for tg_id, info in DEVELOPER_MAP.items():
        result[info["branch"]] = {"tg_id": tg_id, "label": info["label"]}
    return result

BRANCH_TO_DEV = _branch_to_dev()


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

    # Определяем разработчика по ветке
    dev = BRANCH_TO_DEV.get(branch)
    dev_label = dev["label"] if dev else ""

    # Находим chat_id откуда разработчик работал
    chat_id = DEV_CHAT.get(branch)
    if not chat_id:
        print(f"[NETLIFY] No chat_id for branch={branch}, skipping (DEV_CHAT={DEV_CHAT})")
        return {"ok": True, "skipped": "no chat mapped for this branch"}

    if state == "ready":
        text = f"✅ Деплой готов"
        if dev_label:
            text += f" ({dev_label})"
        text += f"\n\nСайт: {site_name}"
        text += f"\nВетка: {branch}"
        if commit_msg:
            text += f"\nКоммит: {commit_msg}"
        text += f"\n\n🔗 {ssl_url}"
    elif state == "error":
        text = f"❌ Деплой упал"
        if dev_label:
            text += f" ({dev_label})"
        text += f"\n\nСайт: {site_name}"
        text += f"\nВетка: {branch}"
        if error_message:
            text += f"\nОшибка: {error_message}"
    elif state == "building":
        text = f"🔨 Деплой начался"
        if dev_label:
            text += f" ({dev_label})"
        text += f"\nСайт: {site_name} | Ветка: {branch}"
    else:
        print(f"[NETLIFY] Ignoring state={state}")
        return {"ok": True, "skipped": state}

    tg_send_message(chat_id, text)
    return {"ok": True, "notified": True, "chat_id": chat_id}


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
                    DEV_CHAT[dev_info["branch"]] = chat_id
                    print(f"[CREATE] Developer: user={clicker_id} → branch={dev_info['branch']} label={dev_info['label']} chat={chat_id}")
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

                tg_send_message(chat_id, f"Готово 🎉\nIssue: {issue_url}", reply_to_message_id=reply_to_id)

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

    # Запоминаем chat_id разработчика при любом взаимодействии
    dev_info_for_tracking = DEVELOPER_MAP.get(user_id)
    if dev_info_for_tracking:
        DEV_CHAT[dev_info_for_tracking["branch"]] = chat_id

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
        show_apps_menu(chat_id, reply_to_message_id=message_id)
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
            f"NOTIFY_CHAT: {DEV_CHAT or '(empty, send any command to register)'}\n"
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
