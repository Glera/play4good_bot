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
BOT_VERSION = "0.12.0"  # ← plan revision flow, enhanced /status steps, done→progress remap
BOT_STARTED_AT = int(time.time())
BUILD_ID = os.environ.get("BUILD_ID", os.environ.get("RAILWAY_DEPLOYMENT_ID", os.environ.get("RENDER_GIT_COMMIT", "local")))

# Notifications state
# Maps branch → {chat_id, user_id, first_name} (запоминаем откуда и кто создавал тикеты)
DEV_CHAT: Dict[str, Dict[str, Any]] = {}  # e.g. {"dev/Gleb": {"chat_id": -100123, "user_id": 456, "first_name": "Глеб"}}

# Track recently created branches to distinguish "created from main" deploys
BRANCH_JUST_CREATED: Dict[str, float] = {}  # branch → timestamp

# Ticket queue: branch → list of pending tickets
# Each ticket: {"title": str, "body": str, "labels": list, "chat_id": int, "user_id": int, "first_name": str, "dev_info": dict}
TICKET_QUEUE: Dict[str, List[Dict[str, Any]]] = {}

# Currently executing ticket: branch → issue info (None if idle)
ACTIVE_TICKET: Dict[str, Optional[Dict[str, Any]]] = {}  # {"issue_number": int, "title": str}

# Last Netlify deploy URL per branch (saved when CI is active, included in final notification)
LAST_DEPLOY_URL: Dict[str, str] = {}  # branch → ssl_url

# CI progress tracking per branch (for /status command)
# {"started_at": int, "last_phase": str, "last_message": str, "last_update_at": int}
CI_PROGRESS: Dict[str, Dict[str, Any]] = {}

# CI plan approval requests: "{branch}:{issue_number}" → status dict
# Status dict: {"status": "pending"|"approved"|"rejected"|"revision", "feedback": str|None}
APPROVAL_REQUESTS: Dict[str, Any] = {}

# Track which chat is awaiting feedback text for plan revision
# chat_id → {"approval_key": str, "issue_number": str}
APPROVAL_AWAITING_FEEDBACK: Dict[int, Dict[str, str]] = {}

# Default ticket options
DEFAULT_OPTIONS = {"multi_agent": False, "testing": False, "approve_plan": False}

# Labels for ticket options
OPTION_LABELS = {
    "multi_agent": "ci:multi-agent",
    "testing": "ci:testing",
    "approve_plan": "ci:approve",
}

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
    parse_mode: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": {"inline_keyboard": keyboard},
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True
    r = requests.post(f"{TG_API}/sendMessage", json=payload, timeout=30)
    resp = r.json()
    if not resp.get("ok"):
        print(f"[TG ERROR] sendMessage failed: {resp.get('error_code')} {resp.get('description')}")
        print(f"[TG ERROR] payload keys: {list(payload.keys())}, keyboard_rows: {len(keyboard)}")
    return resp


def tg_edit_message_with_keyboard(
    chat_id: int,
    message_id: int,
    text: str,
    keyboard: List[List[Dict[str, str]]],
) -> Optional[Dict[str, Any]]:
    """Edit existing message text and inline keyboard."""
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "reply_markup": {"inline_keyboard": keyboard},
    }
    r = requests.post(f"{TG_API}/editMessageText", json=payload, timeout=30)
    resp = r.json()
    if not resp.get("ok"):
        print(f"[TG EDIT ERROR] {resp.get('error_code')} {resp.get('description')}")
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


def gh_get_file(branch: str, path: str) -> Optional[Dict[str, str]]:
    """Get file content and SHA from GitHub. Returns {"content": str, "sha": str} or None."""
    owner, repo = gh_repo_parts()
    r = requests.get(
        f"{GH_API}/repos/{owner}/{repo}/contents/{path}",
        headers=gh_headers(),
        params={"ref": branch},
        timeout=30,
    )
    if r.status_code == 404:
        return None
    if r.status_code >= 300:
        print(f"[GH] Get file failed {r.status_code}: {r.text[:200]}")
        return None
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return {"content": content, "sha": data["sha"]}


def gh_update_file(branch: str, path: str, content: str, sha: str, message: str) -> bool:
    """Update existing file on GitHub. Requires current SHA."""
    owner, repo = gh_repo_parts()
    b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    r = requests.put(
        f"{GH_API}/repos/{owner}/{repo}/contents/{path}",
        headers=gh_headers(),
        json={"message": message, "content": b64, "branch": branch, "sha": sha},
        timeout=30,
    )
    if r.status_code >= 300:
        print(f"[GH] Update file failed {r.status_code}: {r.text[:200]}")
        return False
    return True


def gh_mark_devlog_cherry_pick(branch: str, issue_number: int) -> bool:
    """Mark an issue's entry in DEVLOG.md as cherry-pick candidate."""
    file_data = gh_get_file(branch, "DEVLOG.md")
    if not file_data:
        print(f"[GH] DEVLOG.md not found on {branch}")
        return False

    content = file_data["content"]
    sha = file_data["sha"]

    # Find the entry for this issue and update status
    old_marker = f"## #{issue_number} —"
    if old_marker not in content:
        print(f"[GH] Issue #{issue_number} not found in DEVLOG.md")
        return False

    # Replace status line within this issue's section
    new_content = content.replace(
        f"**Статус:** ✅ готово к переносу",
        f"**Статус:** ⭐ забрать в main",
        1,  # Only first occurrence after the issue header — but we need to be smarter
    )

    # More targeted: replace only within the correct issue section
    # Split by issue headers and find the right section
    sections = re.split(r'(## #\d+ —)', content)
    updated = False
    result_parts = []
    for i, part in enumerate(sections):
        if part.strip() == f"## #{issue_number} —" or part.startswith(f"## #{issue_number} — "):
            # This is the header, next part is the body
            result_parts.append(part)
            if i + 1 < len(sections):
                body = sections[i + 1]
                body = body.replace(
                    "**Статус:** ✅ готово к переносу",
                    "**Статус:** ⭐ забрать в main",
                    1,
                )
                result_parts.append(body)
                updated = True
                # Skip the next iteration since we already processed it
                sections[i + 1] = ""
        else:
            result_parts.append(part)

    if not updated:
        print(f"[GH] Could not find status line for #{issue_number} in DEVLOG.md")
        return False

    new_content = "".join(result_parts)
    return gh_update_file(branch, "DEVLOG.md", new_content, sha,
                          f"Mark #{issue_number} for cherry-pick to main")


def gh_add_label(number: int, label: str) -> bool:
    """Add a label to an issue. Creates the label if it doesn't exist. Returns True on success."""
    owner, repo = gh_repo_parts()
    r = requests.post(
        f"{GH_API}/repos/{owner}/{repo}/issues/{number}/labels",
        headers=gh_headers(),
        json={"labels": [label]},
        timeout=30,
    )
    if r.status_code >= 300:
        print(f"[GH] Add label failed {r.status_code}: {r.text[:200]}")
        return False
    return True


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
    opts = state.get("options", DEFAULT_OPTIONS)

    meta = []
    meta.append(f"Скриншот: {'✅ есть' if screenshot else '— нет'}")
    if dev_info:
        meta.append(f"Ветка: {dev_info['branch']}")
    else:
        meta.append("Ветка: default (разработчик не привязан)")

    # Option toggles display
    opt_parts = []
    opt_parts.append(f"{'✅' if opts.get('multi_agent') else '—'} мультиагент")
    opt_parts.append(f"{'✅' if opts.get('testing') else '—'} тесты")
    opt_parts.append(f"{'✅' if opts.get('approve_plan') else '—'} апрув плана")

    return (
        "Вот что я распознал:\n\n"
        + f"\u201c{state['text']}\u201d\n\n"
        + " | ".join(meta)
        + f"\nCI: {' | '.join(opt_parts)}"
        + "\n\nЧто делаем?"
    )


def confirmation_keyboard(author_id: int, state: Dict[str, Any]) -> List[List[Dict[str, str]]]:
    opts = state.get("options", DEFAULT_OPTIONS)
    return [
        [{"text": "✅ Создать issue", "callback_data": f"create:{author_id}"}],
        [
            {"text": f"{'🤖' if opts.get('multi_agent') else '⬜'} Мультиагент", "callback_data": f"opt_ma:{author_id}"},
            {"text": f"{'🧪' if opts.get('testing') else '⬜'} Тесты", "callback_data": f"opt_test:{author_id}"},
            {"text": f"{'📋' if opts.get('approve_plan') else '⬜'} Апрув", "callback_data": f"opt_appr:{author_id}"},
        ],
        [{"text": "✏️ Правка текста", "callback_data": f"edit:{author_id}"}, {"text": "📎 Скриншот", "callback_data": f"shot:{author_id}"}],
        [{"text": "❌ Отмена", "callback_data": f"cancel:{author_id}"}],
    ]


def show_confirmation(chat_id: int, author_id: int, state: Dict[str, Any], reply_to_message_id: Optional[int] = None) -> None:
    keyboard = confirmation_keyboard(author_id, state)
    resp = tg_send_message_with_keyboard(chat_id, confirmation_text(state), keyboard, reply_to_message_id=reply_to_message_id)
    # Store message_id for later editing (toggle buttons)
    if resp and resp.get("ok"):
        state["confirmation_message_id"] = resp["result"]["message_id"]


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


# ===================== TICKET QUEUE =====================
def queue_add_ticket(branch: str, ticket: Dict[str, Any]) -> int:
    """Add ticket to queue. Returns queue position (1 = next, 0 = executing now)."""
    if branch not in TICKET_QUEUE:
        TICKET_QUEUE[branch] = []
    TICKET_QUEUE[branch].append(ticket)
    return len(TICKET_QUEUE[branch])


def queue_get_next(branch: str) -> Optional[Dict[str, Any]]:
    """Pop next ticket from queue. Returns None if empty."""
    if branch not in TICKET_QUEUE or not TICKET_QUEUE[branch]:
        return None
    return TICKET_QUEUE[branch].pop(0)


def queue_size(branch: str) -> int:
    """Get queue size for branch."""
    return len(TICKET_QUEUE.get(branch, []))


def queue_is_busy(branch: str) -> bool:
    """Check if branch has active ticket."""
    return ACTIVE_TICKET.get(branch) is not None


def queue_set_active(branch: str, issue_number: int, title: str) -> None:
    """Mark ticket as active."""
    ACTIVE_TICKET[branch] = {"issue_number": issue_number, "title": title}
    CI_PROGRESS[branch] = {
        "started_at": now_ts(),
        "last_phase": "Запуск",
        "last_message": "",
        "last_update_at": now_ts(),
        # Phase tracking for /status step-by-step display
        "options": {},  # {multi_agent, testing, approve} — set by claude_started
        "phases_done": [],  # list of phase_num strings already completed
        "current_phase_num": "",  # e.g. "3"
    }


def queue_clear_active(branch: str) -> None:
    """Clear active ticket."""
    ACTIVE_TICKET[branch] = None
    CI_PROGRESS.pop(branch, None)


def queue_process_next(branch: str) -> Optional[Dict[str, Any]]:
    """Process next ticket in queue. Returns created issue or None."""
    ticket = queue_get_next(branch)
    if not ticket:
        return None
    
    try:
        # Create issue on GitHub
        issue = gh_create_issue(ticket["title"], ticket["body"], extra_labels=ticket.get("labels"))
        issue_number = issue["number"]
        issue_body = ticket["body"]
        
        # Mark as active
        queue_set_active(branch, issue_number, ticket["title"])
        
        # Upload screenshot if present (bytes already downloaded when queued)
        screenshot_info = ""
        if ticket.get("screenshot_bytes"):
            try:
                ext = ticket.get("screenshot_ext", "jpg")
                path_in_repo = f"tickets/issue-{issue_number}/screenshot.{ext}"
                html_url = gh_put_file(
                    branch=branch,
                    path=path_in_repo,
                    content_bytes=ticket["screenshot_bytes"],
                    message=f"Add screenshot for issue #{issue_number}",
                )
                screenshot_info = f"\nScreenshot: {html_url}"
                # Update issue body with screenshot
                updated_body = issue_body + f"\n\n---\nBranch: `{branch}`{screenshot_info}"
                gh_update_issue(issue_number, updated_body)
            except Exception as e:
                print(f"[QUEUE] Failed to upload screenshot: {e}")
        
        # Notify user
        chat_id = ticket["chat_id"]
        remaining = queue_size(branch)
        queue_info = f"\n\n📋 В очереди ещё: {remaining}" if remaining > 0 else ""
        
        tg_send_html(chat_id,
            f"🎫 Тикет создан: <a href=\"{issue['html_url']}\">#{issue_number}</a>\n"
            f"{html_escape(ticket['title'])}{queue_info}")
        
        return issue
    except Exception as e:
        print(f"[QUEUE] Failed to create issue: {e}")
        # Notify about error
        tg_send_html(ticket["chat_id"], f"❌ Ошибка создания тикета: {html_escape(str(e))}")
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
        # Mark as active (helps recover queue state after bot restart)
        queue_set_active(branch, int(issue_number), issue_title)

        # Store CI options for /status step display
        options = payload.get("options", {})
        if branch in CI_PROGRESS and options:
            CI_PROGRESS[branch]["options"] = options

        tg_send_html(chat_id,
            f"🤖 Claude начал работу\n\n"
            f"#{issue_number} ({html_escape(dev_ctx['first_name'])}): {safe_title}\n"
            f"Ветка: {safe_branch}")
    elif event == "phase":
        phase_name = payload.get("phase", "")
        phase_num = payload.get("phase_num", "")
        silent = payload.get("silent", False)

        # Always update CI progress (for /status)
        if branch in CI_PROGRESS:
            # Mark previous phase as done when new phase starts
            prev = CI_PROGRESS[branch].get("current_phase_num", "")
            if prev and prev != phase_num:
                done_list = CI_PROGRESS[branch].get("phases_done", [])
                if prev not in done_list:
                    done_list.append(prev)
                CI_PROGRESS[branch]["phases_done"] = done_list
            CI_PROGRESS[branch]["current_phase_num"] = phase_num
            CI_PROGRESS[branch]["last_phase"] = phase_name
            CI_PROGRESS[branch]["last_update_at"] = now_ts()

        # Send TG notification unless silent (tracking-only)
        if not silent:
            safe_phase = html_escape(phase_name)
            tg_send_html(chat_id,
                f"🔄 <b>Фаза {phase_num}</b>: {safe_phase}\n"
                f"#{issue_number}: {safe_title}")
    elif event == "opus_unavailable":
        tg_send_html(chat_id,
            f"⚠️ Opus недоступен, переключаюсь на Sonnet\n\n"
            f"#{issue_number} ({html_escape(dev_ctx['first_name'])}): {safe_title}")
    elif event == "claude_failed":
        LAST_DEPLOY_URL.pop(branch, None)  # Clear stale deploy URL
        tg_send_html(chat_id,
            f"❌ Claude упал при работе над <b>#{issue_number}</b> ({html_escape(dev_ctx['first_name'])}): {safe_title}\n"
            f"Попробуй создать тикет ещё раз.")
        # Clear active and process queue
        queue_clear_active(branch)
        queue_process_next(branch)
    elif event == "merged":
        # Include saved deploy URL if available
        deploy_url = LAST_DEPLOY_URL.pop(branch, "")
        deploy_line = f"\n\n🔗 <a href=\"{deploy_url}\">Открыть билд</a>" if deploy_url else ""

        text = (
            f"📦 Задача завершена — {safe_branch}\n\n"
            f"#{issue_number} ({html_escape(dev_ctx['first_name'])}): {safe_title}"
            f"{deploy_line}")

        # Send with "cherry-pick to main" button
        keyboard = [[{"text": "⭐ Забрать в main", "callback_data": f"pick:{issue_number}"}]]
        tg_send_message_with_keyboard(chat_id, text, keyboard, parse_mode="HTML")

        # Clear active and process queue
        queue_clear_active(branch)
        queue_process_next(branch)
    else:
        print(f"[GH_NOTIFY] Unknown event={event}")
        return {"ok": True, "skipped": "unknown event"}

    return {"ok": True, "notified": True}


@app.post("/claude/message")
async def claude_message(req: Request):
    """Receive messages from Claude during work — plans, progress, questions, reviews, tests."""
    try:
        payload = await req.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}

    branch = payload.get("branch", "")
    issue_number = payload.get("issue_number", "")
    message_type = payload.get("type", "info")
    text = payload.get("text", "")

    print(f"[CLAUDE_MSG] type={message_type} branch={branch} issue=#{issue_number}")
    print(f"[CLAUDE_MSG] text={text[:200]}")

    # Track CI progress for /status command
    if branch and branch in CI_PROGRESS:
        # Update phase label only for definitive events (phases tracked via /github/notify)
        phase_update_types = {
            "test_pass": "Тесты OK", "test_fail": "Тесты упали",
            "perf_pass": "Перфоманс OK", "perf_fail": "Перфоманс регрессия",
            "done": "Завершено", "error": "Ошибка",
        }
        if message_type in phase_update_types:
            CI_PROGRESS[branch]["last_phase"] = phase_update_types[message_type]

        # Clean up message for /status display
        clean_msg = text.replace('\\n', ' ').replace('\n', ' ').strip()
        # Strip "Phase N — " prefix (phase tracked separately)
        clean_msg = re.sub(r'^Phase \d+[a-z]?\s*[—–\-]\s*', '', clean_msg)
        # Skip raw JSON (useless in status)
        stripped = clean_msg.lstrip()
        if stripped.startswith('[') or stripped.startswith('{'):
            clean_msg = ""
        CI_PROGRESS[branch]["last_message"] = clean_msg[:150]
        CI_PROGRESS[branch]["last_update_at"] = now_ts()

    dev_ctx = DEV_CHAT.get(branch)
    if not dev_ctx:
        print(f"[CLAUDE_MSG] No chat for branch={branch}")
        return {"ok": True, "skipped": "no chat"}

    chat_id = dev_ctx["chat_id"]
    safe_text = html_escape(text)

    # Truncate very long messages (Codex reviews, test output)
    MAX_MSG_LEN = 3500
    if len(safe_text) > MAX_MSG_LEN:
        safe_text = safe_text[:MAX_MSG_LEN] + "\n\n<i>… (обрезано)</i>"

    # Emoji and header by message type
    TYPE_CONFIG = {
        "plan":          {"emoji": "📋", "header": "План"},
        "progress":      {"emoji": "⏳", "header": "Прогресс"},
        "question":      {"emoji": "❓", "header": "Вопрос"},
        "done":          {"emoji": "✅", "header": "Готово"},
        "error":         {"emoji": "⚠️", "header": "Ошибка"},
        "info":          {"emoji": "💬", "header": ""},
        # Multi-agent workflow types
        "codex_review":  {"emoji": "🔍", "header": "Codex Review"},
        "phase":         {"emoji": "🔄", "header": "Фаза"},
        "test_pass":     {"emoji": "✅", "header": "Тесты пройдены"},
        "test_fail":     {"emoji": "❌", "header": "Тесты упали"},
        "perf_pass":     {"emoji": "⚡", "header": "Перфоманс ОК"},
        "perf_fail":     {"emoji": "🐌", "header": "Перфоманс регрессия"},
    }

    # Remap "done" from Claude → "progress" — real completion is /github/notify "merged" event.
    # Claude sends "done" after Phase 3 (implementation), but code review & tests still follow.
    display_type = message_type
    if display_type == "done":
        display_type = "progress"

    config = TYPE_CONFIG.get(display_type, {"emoji": "💬", "header": ""})
    emoji = config["emoji"]
    header = config["header"]

    if header:
        tg_send_html(chat_id,
            f"{emoji} <b>{header}</b> — #{issue_number}\n\n{safe_text}")
    else:
        tg_send_html(chat_id,
            f"{emoji} <b>Claude #{issue_number}</b>\n\n{safe_text}")

    return {"ok": True, "sent": True}


# ===================== CI APPROVAL GATE =====================
@app.post("/ci/request-approval")
async def ci_request_approval(req: Request):
    """Called by workflow after Phase 1+2 to request developer approval of the plan."""
    try:
        payload = await req.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}

    branch = payload.get("branch", "")
    issue_number = payload.get("issue_number", "")
    plan_summary = payload.get("plan_summary", "")

    approval_key = f"{branch}:{issue_number}"
    APPROVAL_REQUESTS[approval_key] = {"status": "pending", "feedback": None}

    print(f"[APPROVAL] Requested: {approval_key}")

    # Track approval gate as phase "A" for /status
    if branch in CI_PROGRESS:
        prev = CI_PROGRESS[branch].get("current_phase_num", "")
        if prev and prev != "A":
            done_list = CI_PROGRESS[branch].get("phases_done", [])
            if prev not in done_list:
                done_list.append(prev)
            CI_PROGRESS[branch]["phases_done"] = done_list
        CI_PROGRESS[branch]["current_phase_num"] = "A"
        CI_PROGRESS[branch]["last_phase"] = "Ожидание апрува"
        CI_PROGRESS[branch]["last_update_at"] = now_ts()

    dev_ctx = DEV_CHAT.get(branch)
    if not dev_ctx:
        print(f"[APPROVAL] No chat for branch={branch} — auto-approving")
        APPROVAL_REQUESTS[approval_key] = {"status": "approved", "feedback": None}
        return {"ok": True, "auto_approved": True}

    chat_id = dev_ctx["chat_id"]
    safe_plan = html_escape(plan_summary)[:2000] if plan_summary else "<i>план не передан</i>"

    keyboard = [
        [
            {"text": "✅ Одобрить", "callback_data": f"ci_ok:{issue_number}"},
            {"text": "✏️ Поправки", "callback_data": f"ci_edit:{issue_number}"},
            {"text": "❌ Отклонить", "callback_data": f"ci_no:{issue_number}"},
        ],
    ]

    tg_send_message_with_keyboard(
        chat_id,
        f"📋 Апрув плана — #{issue_number}\n\n"
        f"{safe_plan}\n\n"
        f"Claude ждёт одобрения. Одобрить план?",
        keyboard,
    )

    return {"ok": True, "approval_key": approval_key}


@app.get("/ci/check-approval")
async def ci_check_approval(req: Request):
    """Polled by workflow to check if approval was granted."""
    branch = req.query_params.get("branch", "")
    issue_number = req.query_params.get("issue_number", "")

    approval_key = f"{branch}:{issue_number}"
    entry = APPROVAL_REQUESTS.get(approval_key)

    if not entry:
        print(f"[APPROVAL] Check: {approval_key} → not_found")
        return {"ok": True, "status": "not_found"}

    # Support both old string format and new dict format
    if isinstance(entry, str):
        status = entry
        feedback = None
    else:
        status = entry.get("status", "not_found")
        feedback = entry.get("feedback")

    print(f"[APPROVAL] Check: {approval_key} → {status}")
    result: Dict[str, Any] = {"ok": True, "status": status}
    if feedback:
        result["feedback"] = feedback
    return result


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

        # Skip deploy notifications for merge-from-main commits (CI infra sync, not real changes)
        if commit_msg and ("Merge remote-tracking branch 'origin/main'" in commit_msg
                           or "Merge branch 'main'" in commit_msg):
            print(f"[NETLIFY] Skipping deploy notification for merge-from-main: {commit_msg}")
            return {"ok": True, "skipped": "merge from main"}

        # Skip deploy notifications for DEVLOG updates (CI meta-commit, not real changes)
        if commit_msg and (commit_msg.startswith("docs: update DEVLOG")
                           or "for cherry-pick to main" in commit_msg):
            print(f"[NETLIFY] Skipping deploy notification for DEVLOG/cherry-pick update: {commit_msg}")
            return {"ok": True, "skipped": "devlog update"}

        # Check if this is a deploy from branch just created from main
        created_at = BRANCH_JUST_CREATED.pop(branch, 0)
        if created_at and (time.time() - created_at) < 120:
            print(f"[NETLIFY] Branch {branch} just created from main, sending info message")
            tg_send_html(chat_id,
                f"🔄 Ветка {safe_branch} создана из main, деплой синхронизирован")
            return {"ok": True, "notified": True}

        # If CI is actively working on this branch — save URL, don't notify yet
        # The deploy URL will be included in the final "done" notification
        if queue_is_busy(branch):
            LAST_DEPLOY_URL[branch] = ssl_url
            print(f"[NETLIFY] CI active on {branch} — saved deploy URL, suppressing notification")
            return {"ok": True, "skipped": "ci_active", "deploy_url_saved": True}

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

        # --- Cherry-pick marking (not tied to PENDING) ---
        if data.startswith("pick:"):
            pick_issue = data.split(":", 1)[1]
            try:
                issue_num = int(pick_issue)
                ok = gh_add_label(issue_num, "cherry-pick")
                if ok:
                    # Also update DEVLOG.md on the dev branch
                    dev_info = DEVELOPER_MAP.get(clicker_id)
                    if dev_info:
                        devlog_ok = gh_mark_devlog_cherry_pick(dev_info["branch"], issue_num)
                        if devlog_ok:
                            print(f"[PICK] DEVLOG.md updated for #{issue_num} on {dev_info['branch']}")
                        else:
                            print(f"[PICK] DEVLOG.md update failed for #{issue_num}")

                    # Edit the message: replace button with ⭐ marker
                    tg_edit_message_with_keyboard(
                        chat_id, reply_to_id,
                        msg_obj.get("text", "") + "\n\n⭐ Помечен для переноса в main",
                        [],  # remove keyboard
                    )
                    print(f"[PICK] Issue #{issue_num} marked for cherry-pick by user={clicker_id}")
                else:
                    tg_send_message(chat_id, f"Не удалось пометить #{pick_issue}", reply_to_message_id=reply_to_id)
            except Exception as e:
                print(f"[PICK] Error: {e}")
                tg_send_message(chat_id, f"Ошибка: {e}", reply_to_message_id=reply_to_id)
            return {"ok": True}

        # --- CI Approval callbacks (not tied to PENDING author) ---
        if data.startswith("ci_ok:") or data.startswith("ci_no:") or data.startswith("ci_edit:"):
            ci_issue = data.split(":", 1)[1]

            # Find approval key for this issue across all branches
            target_key = None
            for key_candidate, entry in APPROVAL_REQUESTS.items():
                entry_status = entry.get("status") if isinstance(entry, dict) else entry
                if key_candidate.endswith(f":{ci_issue}") and entry_status == "pending":
                    target_key = key_candidate
                    break

            if not target_key:
                tg_send_message(chat_id, f"Запрос на апрув #{ci_issue} не найден или уже обработан.", reply_to_message_id=reply_to_id)
                return {"ok": True}

            if data.startswith("ci_ok:"):
                APPROVAL_REQUESTS[target_key] = {"status": "approved", "feedback": None}
                tg_send_message(chat_id, f"✅ План одобрен — #{ci_issue}", reply_to_message_id=reply_to_id)
                print(f"[APPROVAL] {target_key} → approved by user={clicker_id}")
            elif data.startswith("ci_edit:"):
                # Ask user to type their corrections
                APPROVAL_AWAITING_FEEDBACK[chat_id] = {
                    "approval_key": target_key,
                    "issue_number": ci_issue,
                }
                tg_send_message(chat_id,
                    f"✏️ Напиши поправки к плану #{ci_issue}.\n"
                    f"Следующее текстовое сообщение будет отправлено Claude как фидбек.",
                    reply_to_message_id=reply_to_id)
                print(f"[APPROVAL] {target_key} → awaiting feedback from user={clicker_id}")
            else:  # ci_no
                APPROVAL_REQUESTS[target_key] = {"status": "rejected", "feedback": None}
                tg_send_message(chat_id, f"❌ План отклонён — #{ci_issue}", reply_to_message_id=reply_to_id)
                print(f"[APPROVAL] {target_key} → rejected by user={clicker_id}")
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

        # Toggle ticket options (re-render confirmation in-place)
        if action in ("opt_ma", "opt_test", "opt_appr"):
            opts = state.get("options", dict(DEFAULT_OPTIONS))
            toggle_map = {"opt_ma": "multi_agent", "opt_test": "testing", "opt_appr": "approve_plan"}
            opt_key = toggle_map[action]
            opts[opt_key] = not opts.get(opt_key, False)
            state["options"] = opts

            # Edit existing message with updated text and keyboard
            conf_msg_id = state.get("confirmation_message_id")
            if conf_msg_id:
                tg_edit_message_with_keyboard(
                    chat_id, conf_msg_id,
                    confirmation_text(state),
                    confirmation_keyboard(author_id, state),
                )
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
                branch = None

                # Add CI option labels
                opts = state.get("options", DEFAULT_OPTIONS)
                for opt_key, label_name in OPTION_LABELS.items():
                    if opts.get(opt_key):
                        extra_labels.append(label_name)

                if dev_info:
                    branch = dev_info["branch"]
                    extra_labels.append(dev_info["label"])
                    # Ensure dev branch exists (create from main if deleted after merge)
                    if not gh_branch_exists(branch):
                        try:
                            gh_create_branch(branch, "main")
                            BRANCH_JUST_CREATED[branch] = time.time()
                        except Exception as e:
                            print(f"[CREATE] Failed to create branch {branch}: {e}")
                    # Запоминаем chat_id ТОЛЬКО при создании тикета
                    DEV_CHAT[branch] = {
                        "chat_id": chat_id,
                        "user_id": clicker_id,
                        "first_name": from_user.get("first_name", ""),
                    }
                    print(f"[CREATE] Developer: user={clicker_id} → branch={branch} label={dev_info['label']} chat={chat_id}")
                    print(f"[CREATE] DEV_CHAT updated: {branch} → chat_id={chat_id}")
                else:
                    print(f"[CREATE] No developer mapping for user={clicker_id}, using default branch")

                issue_fmt = format_issue(state["text"], chat_id, from_user, dev_info=dev_info)
                
                # Check if we should use queue
                if branch and queue_is_busy(branch):
                    # Download screenshot bytes now (file_id may expire while in queue)
                    screenshot_bytes = None
                    screenshot_ext = None
                    if state.get("screenshot"):
                        try:
                            shot = state["screenshot"]
                            file_path = tg_get_file_path(shot["file_id"])
                            if file_path:
                                screenshot_bytes = tg_download_file_bytes(file_path)
                                screenshot_ext = shot.get("ext", "jpg")
                        except Exception as e:
                            print(f"[QUEUE] Failed to download screenshot: {e}")
                    
                    # Add to queue
                    ticket_data = {
                        "title": issue_fmt["title"],
                        "body": issue_fmt["body"],
                        "labels": extra_labels,
                        "chat_id": chat_id,
                        "user_id": clicker_id,
                        "first_name": from_user.get("first_name", ""),
                        "dev_info": dev_info,
                        "screenshot_bytes": screenshot_bytes,
                        "screenshot_ext": screenshot_ext,
                    }
                    position = queue_add_ticket(branch, ticket_data)
                    active = ACTIVE_TICKET.get(branch, {})
                    active_num = active.get("issue_number", "?") if active else "?"
                    
                    screenshot_note = " (со скриншотом)" if screenshot_bytes else ""
                    tg_send_message(chat_id,
                        f"📋 Тикет добавлен в очередь{screenshot_note}\n\n"
                        f"Позиция: {position}\n"
                        f"Сейчас выполняется: #{active_num}\n\n"
                        f"Тикет создастся автоматически когда подойдёт очередь.",
                        reply_to_message_id=reply_to_id)
                    PENDING.pop(key, None)
                    return {"ok": True}
                
                # Create issue immediately
                issue = gh_create_issue(issue_fmt["title"], issue_fmt["body"], extra_labels=extra_labels)
                issue_url = issue["html_url"]
                issue_number = issue["number"]
                issue_body = issue_fmt["body"]
                
                # Mark as active in queue
                if branch:
                    queue_set_active(branch, issue_number, issue_fmt["title"])

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

                queue_info = ""
                if branch:
                    remaining = queue_size(branch)
                    if remaining > 0:
                        queue_info = f"\n\n📋 В очереди: {remaining}"
                
                tg_send_message(chat_id,
                    f"📋 Тикет создан!\n\n"
                    f"#{issue_number} ({from_user.get('first_name', '')}): {issue_fmt['title']}\n"
                    f"{issue_url}\n\n"
                    f"Claude скоро возьмётся за работу...{queue_info}",
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
            tg_send_message(chat_id, "В группе: /ticket (и потом голосовое в течение 120 сек) или /ticket <текст>.\n/status — статус текущего тикета\n/queue — очередь тикетов\n/apps — открыть приложения\n/clear — очистить застрявшую очередь\n/reset — сбросить dev-ветку до main", reply_to_message_id=message_id)
        else:
            tg_send_message(chat_id, "Пришли голосовое (или /ticket <текст>) — я создам GitHub Issue.\n/status — статус текущего тикета\n/queue — очередь тикетов\n/apps — открыть приложения\n/clear — очистить застрявшую очередь\n/reset — сбросить dev-ветку до main", reply_to_message_id=message_id)
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
        
        # Queue stats
        total_queued = sum(len(q) for q in TICKET_QUEUE.values())
        active_branches = [b for b, t in ACTIVE_TICKET.items() if t is not None]
        
        debug_text = (
            f"🔧 Bot debug\n"
            f"Version: {BOT_VERSION}\n"
            f"Build: {BUILD_ID}\n"
            f"Uptime: {mins}m {uptime % 60}s\n"
            f"Pending tickets: {len(PENDING)}\n"
            f"Armed users: {len(ARMED)}\n"
            f"Queue total: {total_queued}\n"
            f"Active branches: {active_branches or '—'}\n"
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

    # Clear stuck queue
    if cmd_base == "/clear":
        dev_info = DEVELOPER_MAP.get(user_id)
        if not dev_info:
            tg_send_message(chat_id, "У тебя нет привязанной dev-ветки.", reply_to_message_id=message_id)
            return {"ok": True}
        branch = dev_info["branch"]
        active = ACTIVE_TICKET.get(branch)
        pending_count = queue_size(branch)

        if not active and pending_count == 0:
            tg_send_message(chat_id, f"Очередь {branch} уже пуста, нечего очищать.", reply_to_message_id=message_id)
            return {"ok": True}

        # Clear active ticket and stale deploy URL
        queue_clear_active(branch)
        LAST_DEPLOY_URL.pop(branch, None)

        # Process queued tickets if any
        if pending_count > 0:
            next_issue = queue_process_next(branch)
            if next_issue:
                tg_send_message(chat_id,
                    f"🧹 Очередь {branch} очищена (был активен #{active['issue_number'] if active else '?'})\n"
                    f"▶️ Запущен следующий тикет из очереди",
                    reply_to_message_id=message_id)
            else:
                tg_send_message(chat_id,
                    f"🧹 Очередь {branch} очищена, но следующий тикет не удалось создать.",
                    reply_to_message_id=message_id)
        else:
            tg_send_message(chat_id,
                f"🧹 Активный тикет #{active['issue_number'] if active else '?'} снят с {branch}. Очередь пуста.",
                reply_to_message_id=message_id)
        return {"ok": True}

    # Queue status
    if cmd_base == "/queue":
        dev_info = DEVELOPER_MAP.get(user_id)
        if not dev_info:
            tg_send_message(chat_id, "У тебя нет привязанной dev-ветки.", reply_to_message_id=message_id)
            return {"ok": True}
        branch = dev_info["branch"]
        active = ACTIVE_TICKET.get(branch)
        pending = TICKET_QUEUE.get(branch, [])
        
        lines = [f"📋 Очередь для {branch}\n"]
        
        if active:
            lines.append(f"▶️ Выполняется: #{active['issue_number']} — {active['title'][:50]}")
        else:
            lines.append("▶️ Выполняется: —")
        
        if pending:
            lines.append(f"\n⏳ В очереди: {len(pending)}")
            for i, t in enumerate(pending[:5], 1):
                lines.append(f"  {i}. {t['title'][:40]}")
            if len(pending) > 5:
                lines.append(f"  ... и ещё {len(pending) - 5}")
        else:
            lines.append("\n⏳ Очередь пуста")
        
        tg_send_message(chat_id, "\n".join(lines), reply_to_message_id=message_id)
        return {"ok": True}

    # Status command — full CI status overview
    if cmd_base == "/status":
        dev_info = DEVELOPER_MAP.get(user_id)
        if not dev_info:
            tg_send_message(chat_id, "У тебя нет привязанной dev-ветки.", reply_to_message_id=message_id)
            return {"ok": True}
        branch = dev_info["branch"]
        active = ACTIVE_TICKET.get(branch)
        progress = CI_PROGRESS.get(branch)
        pending = TICKET_QUEUE.get(branch, [])
        deploy_url = LAST_DEPLOY_URL.get(branch)

        lines: List[str] = [f"📊 Статус — {branch}\n"]

        if active:
            lines.append(f"▶️ Тикет: #{active['issue_number']} — {active['title'][:60]}")

            if progress:
                elapsed = now_ts() - progress["started_at"]
                mins = elapsed // 60
                secs = elapsed % 60
                lines.append(f"⏱ Время: {mins}м {secs}с")

                # Build workflow steps display
                opts = progress.get("options", {})
                multi = opts.get("multi_agent") == "true"
                testing = opts.get("testing") == "true"
                approve = opts.get("approve") == "true"

                # Define all possible workflow steps
                all_steps = [
                    ("1", "Планирование", True),
                    ("2", "Codex ревью", multi),
                    ("A", "Апрув плана", approve),
                    ("3", "Реализация", True),
                    ("4", "Код ревью", multi),
                    ("5", "Тестирование", testing),
                    ("6", "Финализация", True),
                ]
                # Filter to only applicable steps
                steps = [(num, name) for num, name, enabled in all_steps if enabled]
                phases_done = progress.get("phases_done", [])
                current_num = progress.get("current_phase_num", "")

                if steps and (phases_done or current_num):
                    lines.append("")
                    for num, name in steps:
                        if num in phases_done:
                            lines.append(f"  ✅ {name}")
                        elif num == current_num:
                            lines.append(f"  ▶️ {name}")
                        else:
                            lines.append(f"  ⬜ {name}")
                else:
                    lines.append(f"📍 Этап: {progress['last_phase']}")

                since_update = now_ts() - progress["last_update_at"]
                if since_update > 300:
                    lines.append(f"\n⚠️ Последнее обновление: {since_update // 60}м назад")

                if progress["last_message"]:
                    msg_preview = progress["last_message"][:120]
                    lines.append(f"\n💬 {msg_preview}")
        else:
            lines.append("💤 Нет активных тикетов")

        if pending:
            lines.append(f"\n📋 В очереди: {len(pending)}")
            for i, t in enumerate(pending[:5], 1):
                lines.append(f"  {i}. {t['title'][:50]}")
            if len(pending) > 5:
                lines.append(f"  ... и ещё {len(pending) - 5}")
        else:
            lines.append("\n📋 Очередь пуста")

        if deploy_url:
            lines.append(f"\n🔗 Последний билд: {deploy_url}")

        tg_send_message(chat_id, "\n".join(lines), reply_to_message_id=message_id)
        return {"ok": True}

    # Approval feedback: user is typing plan corrections
    if chat_id in APPROVAL_AWAITING_FEEDBACK and text and not text.startswith("/"):
        fb = APPROVAL_AWAITING_FEEDBACK.pop(chat_id)
        approval_key = fb["approval_key"]
        ci_issue = fb["issue_number"]
        APPROVAL_REQUESTS[approval_key] = {"status": "revision", "feedback": text}
        tg_send_message(chat_id,
            f"✏️ Поправки отправлены Claude — #{ci_issue}\n\n"
            f"Claude перепланирует с учётом твоих замечаний.",
            reply_to_message_id=message_id)
        print(f"[APPROVAL] {approval_key} → revision with feedback: {text[:100]}")
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
                "options": dict(DEFAULT_OPTIONS),
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
                "options": dict(DEFAULT_OPTIONS),
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
            "options": dict(DEFAULT_OPTIONS),
        }
        show_confirmation(chat_id, user_id, PENDING[key], reply_to_message_id=message_id)
        return {"ok": True}

    except Exception as e:
        tg_send_message(chat_id, f"Ошибка: {type(e).__name__}\n{e}", reply_to_message_id=message_id)
        return {"ok": True}
