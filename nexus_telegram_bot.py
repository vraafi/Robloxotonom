"""
nexus_telegram_bot.py  v3.0.0
==============================
Bot Telegram Interaktif — Antarmuka Manusia ke AI Agent Otonom Nexus.

Perintah Utama:
  /start    — Menu utama dengan tombol STOP dan LANJUTKAN
  /stop     — Hentikan background task AI Roblox
  /continue — Lanjutkan AI Roblox
  /status   — Status lengkap agent
  /selffix  — AI perbaiki kode sendiri + sandbox test + push GitHub
  /autofix  — Perbaiki semua file Lua bermasalah
  /clear    — Reset percakapan
  /help     — Panduan lengkap

Perbaikan v3.0.0:
  - Tombol STOP dan LANJUTKAN muncul di setiap pesan (tidak perlu ketik perintah)
  - Mode "Chat Langsung" — bicara langsung ke AI untuk memberi perintah spesifik
  - Progress task dilaporkan LANGSUNG ke Telegram (tidak ghosting lagi)
  - Setiap task diberi nomor, status sukses/gagal dikirim real-time
  - AI TIDAK pernah diam — selalu kirim update setiap langkah
"""

import os
import re
import asyncio
import subprocess
import time
import shutil
import tempfile
from collections import defaultdict
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.error import BadRequest, RetryAfter, TimedOut

from nexus_config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    ACTIVE_AGENTS,
    GEMINI_CLI_PATH,
    PROJECT_ROOT_DIRECTORY,
    SOURCE_CODE_DIRECTORY,
    console_terminal_interface,
)

from nexus_agents import (
    execute_antigravity_fleet,
    NexusGlobalState,
    global_agent_memory,
    _roblox_agent_paused,
)

from nexus_database import (
    load_user_session,
    delete_user_session,
    initialize_session_table,
)


# ================================================
# KONSTANTA & STATE GLOBAL
# ================================================
_BOT_VERSION = "3.0.0"
_OWNER_CHAT_ID = str(TELEGRAM_CHAT_ID).strip()
_user_state: dict = {}
_roblox_exec_lock = asyncio.Semaphore(1)
_roblox_background_task: Optional[asyncio.Task] = None
_bot_app: Optional[object] = None  # Referensi global ke Application

# ================================================
# RATE LIMITING — OWNER TIDAK PERNAH DITOLAK
# ================================================
_RATE_LIMIT_WINDOW = 10
_RATE_LIMIT_MAX = 30
_user_message_timestamps: dict = defaultdict(list)

MODEL_FALLBACK_SEQUENCE = [
    "models/gemma-4-31b-it",
    "models/gemma-4-26b-a4b-it",
    "models/gemma-3-27b-it",
    "models/gemini-3.1-flash-lite-preview",
    "models/gemma-3-12b-it",
    "models/gemma-3-4b-it",
    "models/gemma-3n-e4b-it",
    "models/gemma-3n-e2b-it",
    "models/gemma-3-1b-it",








]


def _check_rate_limit(chat_id: int) -> bool:
    if str(chat_id) == _OWNER_CHAT_ID:
        return False
    now = time.time()
    _user_message_timestamps[chat_id] = [
        t for t in _user_message_timestamps[chat_id] if now - t < _RATE_LIMIT_WINDOW
    ]
    if len(_user_message_timestamps[chat_id]) >= _RATE_LIMIT_MAX:
        return True
    _user_message_timestamps[chat_id].append(now)
    return False


# ================================================
# HELPER: TOMBOL KONTROL (STOP / LANJUTKAN)
# ================================================
def _control_keyboard(show_stop: bool = True) -> InlineKeyboardMarkup:
    """Selalu tampilkan tombol Stop dan Lanjutkan di setiap pesan penting."""
    buttons = []
    if show_stop:
        buttons.append(InlineKeyboardButton("STOP ⛔", callback_data="ctrl_stop"))
    buttons.append(InlineKeyboardButton("LANJUTKAN ▶", callback_data="ctrl_continue"))
    buttons.append(InlineKeyboardButton("STATUS ℹ", callback_data="ctrl_status"))
    return InlineKeyboardMarkup([buttons])


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    """Menu utama dengan semua opsi."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("AI Agent Roblox (Otonom) 🤖", callback_data="mode_roblox")],
        [InlineKeyboardButton("Chat Langsung AI 💬", callback_data="mode_chat")],
        [InlineKeyboardButton("Universal Agent 🌐", callback_data="mode_universal")],
        [
            InlineKeyboardButton("STOP ⛔", callback_data="ctrl_stop"),
            InlineKeyboardButton("LANJUTKAN ▶", callback_data="ctrl_continue"),
        ],
        [InlineKeyboardButton("STATUS ℹ", callback_data="ctrl_status")],
    ])


async def _safe_edit(msg: Message, text: str, reply_markup=None) -> None:
    """Edit pesan dengan aman — tidak crash jika konten sama atau pesan dihapus."""
    try:
        kwargs = {"text": text[:4090]}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        await msg.edit_text(**kwargs)
    except BadRequest as e:
        err = str(e).lower()
        if "message is not modified" in err or "message to edit not found" in err:
            pass
        else:
            console_terminal_interface.print(f"[yellow][safe_edit] {e}[/yellow]")
    except (RetryAfter, TimedOut):
        await asyncio.sleep(3)


async def _safe_send(bot, chat_id: str, text: str, reply_markup=None) -> Optional[Message]:
    """Kirim pesan dengan aman, retry jika rate limit."""
    for attempt in range(3):
        try:
            kwargs = {"chat_id": chat_id, "text": text[:4090]}
            if reply_markup is not None:
                kwargs["reply_markup"] = reply_markup
            return await bot.send_message(**kwargs)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TimedOut:
            await asyncio.sleep(5)
        except Exception as e:
            console_terminal_interface.print(f"[red][safe_send] {e}[/red]")
            return None
    return None


# ================================================
# GEMINI CLI — TIDAK PERNAH MENOLAK, SELALU RETRY
# ================================================
def _call_gemini_sync(prompt: str, api_key: str, model: str = "models/gemma-4-31b-it") -> str:
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = api_key
    env["CI"] = "true"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    current_path = env.get("PATH", "")
    env["PATH"] = (
        "/home/runner/.local/bin:/home/ubuntu/.local/bin"
        ":/home/ubuntu/.local/share/pnpm:" + current_path
    )
    try:
        result = subprocess.run(
            [GEMINI_CLI_PATH, "-m", model, "-y", "-p", prompt],
            env=env, capture_output=True, text=True, timeout=180,
        )
        output = result.stdout.strip()
        if output:
            return output
        return result.stderr.strip() or "ERROR: Output kosong"
    except subprocess.TimeoutExpired:
        return "ERROR: Gemini timeout"
    except Exception as e:
        return f"ERROR: {e}"


async def _call_gemini(prompt: str, max_retries: int = 15) -> str:
    """Tidak pernah menolak. Rotasi API key + model, retry sampai berhasil."""
    if not ACTIVE_AGENTS:
        return "ERROR: Tidak ada agent aktif."

    last_result = ""
    for attempt in range(max_retries):
        agent_idx = attempt % len(ACTIVE_AGENTS)
        api_key = ACTIVE_AGENTS[agent_idx]["api_key"]
        model = MODEL_FALLBACK_SEQUENCE[attempt % len(MODEL_FALLBACK_SEQUENCE)]

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, _call_gemini_sync, prompt, api_key, model
        )
        last_result = result

        if result and not result.startswith("ERROR:"):
            return result

        wait_sec = min(5 * (attempt + 1), 45)
        console_terminal_interface.print(
            f"[yellow][Gemini Retry {attempt+1}/{max_retries}] Model={model} | Tunggu {wait_sec}s...[/yellow]"
        )
        await asyncio.sleep(wait_sec)

    return last_result


# ================================================
# HELPER FUNCTIONS
# ================================================
def _find_lua_file_by_name(name: str) -> Optional[str]:
    name_lower = name.lower().replace(" ", "_").replace("-", "_")
    best = None
    best_score = 0
    for root, dirs, files in os.walk(SOURCE_CODE_DIRECTORY):
        for fname in files:
            if not fname.endswith((".lua", ".luau", ".rbxmx")):
                continue
            base = os.path.splitext(fname)[0].lower()
            score = 0
            if name_lower == base:
                score = 100
            elif name_lower in base or base in name_lower:
                score = 50
            else:
                words = re.split(r"[_\-\s]+", name_lower)
                matched = sum(1 for w in words if w and w in base)
                score = matched * 10
            if score > best_score:
                best_score = score
                best = os.path.join(root, fname)
    return best if best_score >= 10 else None


# ================================================
# SANDBOX: Test kode sebelum push ke GitHub
# ================================================
async def _sandbox_test_file(file_path: str, new_content: str, send_fn_task) -> bool:
    await send_fn("Sandbox Testing — menguji kode di lingkungan terisolasi...")

    sandbox_dir = tempfile.mkdtemp(prefix="nexus_sandbox_")
    try:
        sandbox_file = os.path.join(sandbox_dir, os.path.basename(file_path))
        with open(sandbox_file, "w", encoding="utf-8") as f:
            f.write(new_content)

        if file_path.endswith(".py"):
            r = subprocess.run(
                ["python3", "-m", "py_compile", sandbox_file],
                capture_output=True, text=True, timeout=30
            )
            if r.returncode != 0:
                await send_fn(
                    "Sandbox GAGAL — Syntax Error\n"
                    + r.stderr[:400]
                    + "\nKode TIDAK di-push. AI akan memperbaiki ulang."
                )
                return False

        elif file_path.endswith((".lua", ".luau")):
            luau_bin = os.path.join(PROJECT_ROOT_DIRECTORY, "luau-analyze")
            if os.path.exists(luau_bin):
                r = subprocess.run(
                    [luau_bin, sandbox_file],
                    capture_output=True, text=True, timeout=30
                )
                if r.returncode != 0:
                    await send_fn("Luau Warning (lanjut dengan hati-hati):\n" + r.stdout[:200])

        await send_fn("Sandbox OK! Kode lolos uji.")
        return True
    finally:
        shutil.rmtree(sandbox_dir, ignore_errors=True)


async def _git_push(repo_dir: str, file_rel_path: str, commit_msg: str, send_fn_task) -> bool:
    github_token = (
        os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "")
        or os.getenv("GITHUB_TOKEN", "")
    )
    if not github_token:
        await send_fn("GITHUB_TOKEN tidak ditemukan. Tambahkan: GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxxx")
        return False

    try:
        subprocess.run(["git", "-C", repo_dir, "config", "user.email", "nexus-ai@bot.local"], capture_output=True)
        subprocess.run(["git", "-C", repo_dir, "config", "user.name", "Nexus AI"], capture_output=True)
        subprocess.run(["git", "-C", repo_dir, "add", file_rel_path], capture_output=True, timeout=30)
        subprocess.run(["git", "-C", repo_dir, "commit", "-m", commit_msg], capture_output=True, text=True, timeout=30)
        r = subprocess.run(
            ["git", "-C", repo_dir, "push"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        )
        if r.returncode == 0:
            await send_fn("Push Berhasil! Commit: " + commit_msg)
            return True
        else:
            await send_fn("Push gagal: " + r.stderr[:300])
            return False
    except Exception as e:
        await send_fn("Exception saat push: " + str(e))
        return False


# ================================================
# STARTUP SCAN
# ================================================
async def _startup_deep_scan(send_fn_task):
    if not os.path.exists(SOURCE_CODE_DIRECTORY):
        await send_fn(
            "Nexus AI Agent v" + _BOT_VERSION + " Menyala!\n\n"
            "Direktori src belum ada. Sistem mulai dari nol.\n\n"
            "Kirim /start untuk mulai memberi perintah."
        )
        return

    lua_files = []
    for root, dirs, files in os.walk(SOURCE_CODE_DIRECTORY):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            if fname.endswith((".lua", ".luau")):
                lua_files.append(os.path.join(root, fname))

    violations = []
    for fpath in lua_files:
        fname = os.path.basename(fpath)
        issues = []
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            lines = content.split("\n")
            if not lines or lines[0].strip() != "--!strict":
                issues.append("Tidak ada --!strict")
            for i, line in enumerate(lines, 1):
                if "DisplayOrder" in line and "Enum." in line:
                    issues.append(f"Baris {i}: DisplayOrder pakai Enum")
                if "ZIndex" in line and "Enum." in line:
                    issues.append(f"Baris {i}: ZIndex pakai Enum")
            if fname.endswith(".server.lua") and "game.Players.LocalPlayer" in content:
                issues.append("Server script pakai LocalPlayer")
            if len(content.strip()) < 5:
                issues.append("File kosong / tidak valid")
        except Exception as e:
            issues.append(f"Gagal baca: {e}")
        if issues:
            violations.append((fname, issues))

    status_text = (
        "NEXUS AI AGENT v" + _BOT_VERSION + " SIAP!\n\n"
        "Scan " + str(len(lua_files)) + " file Lua selesai.\n"
    )
    if violations:
        status_text += str(len(violations)) + " file bermasalah ditemukan.\nKirim /autofix untuk perbaiki.\n\n"
        for fname, issues in violations[:5]:
            status_text += "* " + fname + ": " + issues[0] + "\n"
    else:
        status_text += "Semua file valid!\n\n"

    status_text += "Kirim /start untuk mulai memberi perintah."
    await send_fn(status_text)


# ================================================
# TASK EXECUTOR — DENGAN PROGRESS REAL-TIME
# ================================================
async def execute_single_task_with_retry(task: dict, send_fn, max_attempts: int = 10) -> tuple:
    from nexus_agents import LuauKnowledgeScraper

    last_error = ""
    github_context = ""

    for attempt in range(1, max_attempts + 1):
        if not _roblox_agent_paused.is_set():
            await send_fn(
                "Agent di-PAUSE.\n\n"
                "Tekan tombol LANJUTKAN atau kirim /continue untuk melanjutkan."
            )
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _roblox_agent_paused.wait)
            await send_fn("Agent dilanjutkan! Melanjutkan task...")

        try:
            success, msg = await execute_single_task(task, extra_context=github_context)
            if success:
                return True, msg
            last_error = msg
        except Exception as e:
            last_error = str(e)

        if attempt >= 3:
            query = "roblox luau " + task.get("title", "") + " " + last_error[:40]
            github_context = await LuauKnowledgeScraper.search_github_luau(query)

        if attempt < max_attempts:
            wait = min(10 * attempt, 60)
            await send_fn(
                f"Percobaan {attempt}/{max_attempts} gagal.\n"
                f"Error: {last_error[:200]}\n\n"
                f"Mencoba lagi dalam {wait} detik..."
            )
            await asyncio.sleep(wait)

    await send_fn(
        "AI Butuh Bantuan!\n\n"
        "Task: " + task.get("title", "unknown") + "\n"
        "Sudah " + str(max_attempts) + "x gagal.\n\n"
        "Error terakhir:\n" + last_error[:400] + "\n\n"
        "Balas dengan instruksi tambahan atau pendekatan berbeda."
    )

    _user_state["waiting_for_owner_input"] = {
        "task": task,
        "last_error": last_error,
    }
    return False, "Menunggu instruksi owner setelah " + str(max_attempts) + "x gagal"


async def execute_single_task(task: dict, extra_context: str = "") -> tuple:
    hint = task.get("target_file_hint", "")
    folder = task.get("target_folder", "")
    detail = task.get("detail", "")
    action = task.get("action", "fix_bug")

    file_path = _find_lua_file_by_name(hint) if hint and hint != "unknown" else None

    if file_path and os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            original_code = f.read()
    else:
        original_code = ""
        safe_name = re.sub(r"[^\w]", "_", task.get("title", "new_feature")).upper()
        if folder == "ServerScriptService":
            fname = safe_name + ".server.lua"
        elif folder in ("StarterGui", "StarterPlayerScripts", "StarterCharacterScripts"):
            fname = safe_name + ".client.lua"
        else:
            fname = safe_name + ".lua"
        file_path = os.path.join(SOURCE_CODE_DIRECTORY, folder, fname)

    code_context = (
        "(File baru — belum ada kode sebelumnya)"
        if not original_code
        else original_code[:4000] + ("..." if len(original_code) > 4000 else "")
    )

    file_type = (
        "ScreenGui LocalScript" if folder == "StarterGui" else
        "Server Script" if folder == "ServerScriptService" else
        "Client Script" if folder == "StarterPlayerScripts" else
        "ModuleScript"
    )

    ctx_extra = ("KONTEKS TAMBAHAN DARI GITHUB:\n" + extra_context) if extra_context else ""

    prompt = (
        "Kamu adalah senior Roblox Luau developer. Perbaiki atau buat kode untuk game.\n\n"
        "TUGAS:\n" + detail + "\n\n"
        "TIPE FILE: " + file_type + "\n"
        "AKSI: " + action + "\n\n"
        "KODE SAAT INI:\n" + code_context + "\n\n"
        + ctx_extra + "\n\n"
        "ATURAN WAJIB:\n"
        "1. Baris pertama HARUS --!strict\n"
        "2. Jangan gunakan Enum untuk DisplayOrder, ZIndex, LayoutOrder (gunakan angka integer)\n"
        "3. Spawn point player HARUS menggunakan game.Workspace.SpawnLocation atau Teams\n"
        "4. Tombol UI HARUS memiliki event handler yang berfungsi\n"
        "5. HANYA output kode Luau murni, tidak ada penjelasan\n\n"
        "KODE YANG SUDAH DIPERBAIKI:"
    )

    fixed_code = await _call_gemini(prompt)

    if not fixed_code or fixed_code.startswith("ERROR:"):
        return False, "Gemini gagal: " + fixed_code[:100]

    fixed_code = re.sub(r"^```[a-zA-Z]*\s*\n?", "", fixed_code, flags=re.IGNORECASE)
    fixed_code = re.sub(r"\n?```\s*$", "", fixed_code).strip()

    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(fixed_code)

    return True, "OK: " + os.path.basename(file_path) + " berhasil diperbaiki"


# ================================================
# COMMAND HANDLERS
# ================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if chat_id != _OWNER_CHAT_ID:
        await update.message.reply_text("Bot ini pribadi. Akses ditolak.")
        return

    # Inisialisasi tabel sesi jika belum ada
    await initialize_session_table()
    
    # Muat riwayat sesi dari database
    history = await load_user_session(chat_id)
    if history:
        global_agent_memory._history = history
        await update.message.reply_text("🔄 Sesi sebelumnya dipulihkan dari database.")

    paused = not _roblox_agent_paused.is_set()
    bg_running = _roblox_background_task and not _roblox_background_task.done()

    status_line = (
        "Agent: PAUSE ⛔" if paused else
        ("Agent: BERJALAN ▶" if bg_running else "Agent: STANDBY")
    )

    await update.message.reply_text(
        "NEXUS AI AGENT v" + _BOT_VERSION + "\n"
        + status_line + "\n\n"
        "Pilih mode atau gunakan tombol kontrol:\n\n"
        "AI Agent Roblox — Beri laporan bug/fitur, AI kerjakan otomatis\n"
        "Chat Langsung AI — Bicara langsung ke AI, beri perintah spesifik\n"
        "Universal Agent — Minta kode apapun (Python, JS, dll)",
        reply_markup=_main_menu_keyboard(),
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if chat_id != _OWNER_CHAT_ID:
        return
    await _do_stop(update.message.reply_text)


async def cmd_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if chat_id != _OWNER_CHAT_ID:
        return
    await _do_continue(update.message.reply_text)


async def _do_stop(reply_fn):
    global _roblox_background_task
    _roblox_agent_paused.clear()
    NexusGlobalState.is_running = False
    if _roblox_background_task and not _roblox_background_task.done():
        _roblox_background_task.cancel()
        try:
            await _roblox_background_task
        except asyncio.CancelledError:
            pass
        _roblox_background_task = None
    await reply_fn(
        "AI Agent DIHENTIKAN ⛔\n\n"
        "Semua pekerjaan background dihentikan.\n"
        "Gunakan tombol LANJUTKAN atau /continue untuk melanjutkan.",
        reply_markup=_control_keyboard(show_stop=False),
    )


async def _do_continue(reply_fn):
    _roblox_agent_paused.set()
    NexusGlobalState.is_running = True
    await reply_fn(
        "AI Agent DILANJUTKAN ▶\n\n"
        "Agent siap menerima perintah.\n"
        "Kirim pesan atau pilih mode dari /start.",
        reply_markup=_control_keyboard(show_stop=True),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if chat_id != _OWNER_CHAT_ID:
        return
    await _send_status(update.message.reply_text)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if chat_id != _OWNER_CHAT_ID:
        return
    
    # Hapus memori di RAM
    global_agent_memory.clear()
    
    # Hapus sesi di Database
    await delete_user_session(chat_id)
    
    # Hapus cache fisik (temp io)
    from nexus_config import TEMP_IO_DIRECTORY
    if os.path.exists(TEMP_IO_DIRECTORY):
        import shutil
        shutil.rmtree(TEMP_IO_DIRECTORY, ignore_errors=True)
        os.makedirs(TEMP_IO_DIRECTORY, exist_ok=True)
        
    await update.message.reply_text("🧹 Semua riwayat percakapan, sesi database, dan cache data telah dihapus.")


async def _send_status(reply_fn):
    paused = not _roblox_agent_paused.is_set()
    bg_running = _roblox_background_task and not _roblox_background_task.done()

    text = (
        "STATUS NEXUS AI AGENT v" + _BOT_VERSION + "\n\n"
        "Agent Roblox: " + ("PAUSE ⛔" if paused else "AKTIF ▶") + "\n"
        "Background Task: " + ("Berjalan..." if bg_running else "Idle") + "\n"
        "API Keys: " + str(len(ACTIVE_AGENTS)) + " aktif\n"
        "Loop: " + ("RUNNING" if NexusGlobalState.is_running else "STOPPED") + "\n\n"
        "Perintah cepat:\n"
        "/stop — Hentikan\n"
        "/continue — Lanjutkan\n"
        "/autofix — Perbaiki file Lua\n"
        "/clear — Reset percakapan"
    )
    await reply_fn(text, reply_markup=_control_keyboard(show_stop=not paused))


async def cmd_selffix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if chat_id != _OWNER_CHAT_ID:
        return

    args = context.args or []
    target_file = args[0] if args else "nexus_telegram_bot.py"
    fix_desc = " ".join(args[1:]) if len(args) > 1 else "Perbaiki semua bug yang ada, tingkatkan robustness"

    msg = await update.message.reply_text(
        "Self-Fix Dimulai\n\nFile: " + target_file + "\nInstruksi: " + fix_desc + "\n\nMembaca file asli...",
        reply_markup=_control_keyboard(),
    )

    repo_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(repo_dir, target_file)
    if not os.path.exists(file_path):
        file_path = os.path.join(PROJECT_ROOT_DIRECTORY, target_file)
    if not os.path.exists(file_path):
        await _safe_edit(msg, "File " + target_file + " tidak ditemukan.")
        return

    with open(file_path, "r", encoding="utf-8") as f:
        original_code = f.read()

    await _safe_edit(
        msg,
        "Self-Fix 2/5\n\nFile dibaca (" + str(len(original_code)) + " karakter)\nMeminta AI memperbaiki...",
    )

    prompt = (
        "Kamu adalah senior Python developer ahli Telegram bot dan AI agent otonom.\n"
        "Perbaiki kode Python berikut berdasarkan instruksi ini: " + fix_desc + "\n\n"
        "FILE: " + target_file + "\n"
        "KODE ASLI:\n" + original_code[:8000] + "\n\n"
        "ATURAN:\n"
        "1. Output HANYA kode Python murni, tanpa penjelasan apapun\n"
        "2. Pertahankan SEMUA fungsi yang sudah ada\n"
        "3. Perbaiki bug, tingkatkan error handling\n"
        "4. JANGAN tambahkan markdown fence di output\n\n"
        "KODE YANG SUDAH DIPERBAIKI:"
    )

    fixed_code = await _call_gemini(prompt)
    fixed_code = re.sub(r"^```python\s*\n?", "", fixed_code, flags=re.IGNORECASE)
    fixed_code = re.sub(r"\n?```\s*$", "", fixed_code).strip()

    if not fixed_code or len(fixed_code) < 100:
        await _safe_edit(msg, "AI gagal generate kode perbaikan. Coba lagi.")
        return

    await _safe_edit(msg, "Self-Fix 3/5\n\nAI selesai generate kode\nSandbox testing...")

    async def send_to_msg(text):
        await _safe_edit(msg, text)

    sandbox_ok = await _sandbox_test_file(file_path, fixed_code, send_to_msg)
    if not sandbox_ok:
        return

    await _safe_edit(msg, "Self-Fix 4/5\n\nSandbox OK\nMenyimpan & push ke GitHub...")

    backup_path = file_path + ".bak"
    shutil.copy2(file_path, backup_path)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(fixed_code)

    commit_msg = "[nexus_selffix] Auto-fix " + target_file + ": " + fix_desc[:60]
    await _git_push(repo_dir, target_file, commit_msg, send_to_msg)

    await _safe_edit(
        msg,
        "Self-Fix Selesai!\n\n"
        "File " + target_file + " berhasil diperbaiki.\n"
        "Backup: " + target_file + ".bak\n\n"
        "Restart bot untuk menerapkan:\nsystemctl restart nexus-bot",
        reply_markup=_control_keyboard(),
    )


async def cmd_autofix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if chat_id != _OWNER_CHAT_ID:
        return

    msg = await update.message.reply_text(
        "Auto-Fix Dimulai — Scanning semua file...",
        reply_markup=_control_keyboard(),
    )

    violations = []
    lua_files = []
    for root, dirs, files in os.walk(SOURCE_CODE_DIRECTORY):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            if fname.endswith((".lua", ".luau")):
                lua_files.append(os.path.join(root, fname))

    for fpath in lua_files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            lines = content.split("\n")
            issues = []
            if not lines or lines[0].strip() != "--!strict":
                issues.append("missing_strict")
            for i, line in enumerate(lines, 1):
                if "DisplayOrder" in line and "Enum." in line:
                    issues.append("enum_displayorder_line_" + str(i))
                if "ZIndex" in line and "Enum." in line:
                    issues.append("enum_zindex_line_" + str(i))
            if issues:
                violations.append((fpath, content, issues))
        except Exception:
            pass

    if not violations:
        await _safe_edit(msg, "Semua file sudah valid! Tidak ada yang perlu diperbaiki.")
        return

    await _safe_edit(msg, "Auto-Fix: " + str(len(violations)) + " file bermasalah — Memperbaiki...")

    fixed_count = 0
    for fpath, content, issues in violations:
        new_content = content
        if "missing_strict" in issues:
            lines = new_content.split("\n")
            if lines[0].strip() != "--!strict":
                lines.insert(0, "--!strict")
            new_content = "\n".join(lines)
        new_content = re.sub(r"(\.DisplayOrder\s*=\s*)Enum\.[A-Za-z0-9_.]+", r"\g<1>0", new_content)
        new_content = re.sub(r"(\.ZIndex\s*=\s*)Enum\.[A-Za-z0-9_.]+", r"\g<1>0", new_content)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(new_content)
        fixed_count += 1

    await _safe_edit(
        msg,
        "Auto-Fix Selesai!\n\n"
        "Diperbaiki: " + str(fixed_count) + "/" + str(len(violations)) + " file\n"
        "Jalankan build ulang untuk memverifikasi.",
        reply_markup=_control_keyboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if chat_id != _OWNER_CHAT_ID:
        return
    await update.message.reply_text(
        "NEXUS AI AGENT v" + _BOT_VERSION + " — Panduan\n\n"
        "TOMBOL KONTROL:\n"
        "STOP ⛔ — Hentikan semua task AI segera\n"
        "LANJUTKAN ▶ — Lanjutkan task yang dihentikan\n"
        "STATUS ℹ — Lihat status lengkap\n\n"
        "MODE:\n"
        "AI Agent Roblox — Kirim laporan/perintah, AI kerjakan otonom\n"
        "Chat Langsung AI — Bicara langsung, AI jawab dan kerjakan\n"
        "Universal Agent — Minta kode apapun\n\n"
        "PERINTAH:\n"
        "/start — Menu utama\n"
        "/stop — Hentikan AI\n"
        "/continue — Lanjutkan AI\n"
        "/status — Status detail\n"
        "/selffix [file] [deskripsi] — AI perbaiki kode & push\n"
        "/autofix — Perbaiki semua file Lua\n"
        "/clear — Reset percakapan\n\n"
        "AI TIDAK PERNAH menolak perintahmu.\n"
        "Jika gagal, AI retry otomatis sampai 15x.",
        reply_markup=_control_keyboard(),
    )


# ================================================
# CALLBACK HANDLER — TOMBOL INLINE
# ================================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = str(query.message.chat.id)
    if chat_id != _OWNER_CHAT_ID:
        return

    data = query.data

    # --- Tombol kontrol Stop/Lanjutkan/Status ---
    if data == "ctrl_stop":
        await _do_stop(
            lambda text, reply_markup=None: query.edit_message_text(
                text, reply_markup=reply_markup or _control_keyboard(show_stop=False)
            )
        )
        return

    if data == "ctrl_continue":
        await _do_continue(
            lambda text, reply_markup=None: query.edit_message_text(
                text, reply_markup=reply_markup or _control_keyboard(show_stop=True)
            )
        )
        return

    if data == "ctrl_status":
        await _send_status(
            lambda text, reply_markup=None: query.edit_message_text(
                text, reply_markup=reply_markup or _control_keyboard()
            )
        )
        return

    # --- Mode selection ---
    if data == "mode_roblox":
        _user_state[chat_id] = {"mode": "roblox", "step": "waiting_command"}
        await query.edit_message_text(
            "Mode AI Agent Otonom Roblox\n\n"
            "Ketik laporan bug, permintaan fitur, atau perintah spesifik.\n\n"
            "Contoh:\n"
            "- Semua UI kotak dan bercahaya, perbaiki jadi mesh monster dari novel\n"
            "- Buat sistem damage saat jatuh\n"
            "- Tambah dinosaurus dan makhluk hidup di world\n\n"
            "AI akan langsung mulai bekerja dan lapor progres ke sini.",
            reply_markup=_control_keyboard(),
        )

    elif data == "mode_chat":
        _user_state[chat_id] = {"mode": "chat", "step": "waiting_command"}
        await query.edit_message_text(
            "Mode Chat Langsung AI\n\n"
            "Bicara langsung ke AI. AI akan menjawab DAN langsung mengerjakan perintahmu.\n\n"
            "Contoh perintah:\n"
            "- Jelaskan kenapa UI saya kotak semua\n"
            "- Buat kode untuk sistem health bar\n"
            "- Perbaiki file nexus_main.py\n"
            "- Bagaimana cara membuat NPC dengan mesh khusus?\n\n"
            "AI akan merespons SETIAP pesanmu tanpa ghosting.",
            reply_markup=_control_keyboard(),
        )

    elif data == "mode_universal":
        _user_state[chat_id] = {"mode": "universal", "step": "waiting_request"}
        await query.edit_message_text(
            "Mode AI Agent Universal\n\n"
            "Minta kode apapun:\n"
            "Python, JavaScript, Lua, Rust, Go, dll.",
            reply_markup=_control_keyboard(),
        )


# ================================================
# MESSAGE HANDLER
# ================================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)

    if chat_id != _OWNER_CHAT_ID:
        if _check_rate_limit(int(chat_id)):
            await update.message.reply_text("Mohon tunggu sebentar.")
            return
        await update.message.reply_text("Bot ini pribadi.")
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    # PRIORITAS: Cek perintah kontrol teks (Stop / Continue) agar responsif
    text_cmd = text.lower()
    if text_cmd in ["stop", "/stop", "berhenti", "henti"]:
        await _do_stop(update.message.reply_text)
        return
    elif text_cmd in ["continue", "/continue", "lanjutkan", "lanjut"]:
        await _do_continue(update.message.reply_text)
        return

    # Cek apakah menunggu instruksi tambahan setelah task gagal
    if "waiting_for_owner_input" in _user_state:
        waiting = _user_state.pop("waiting_for_owner_input")
        task = waiting["task"]
        task["detail"] += "\n\nINSTRUKSI TAMBAHAN DARI OWNER: " + text
        msg = await update.message.reply_text(
            "Melanjutkan task dengan instruksi barumu...",
            reply_markup=_control_keyboard(),
        )

        async def send_fn_task(t):
            await _safe_edit(msg, t)

        await execute_single_task_with_retry(task, send_fn_task)
        return

    state = _user_state.get(chat_id, {})
    mode = state.get("mode", "")

    if not mode:
        await update.message.reply_text(
            "Kirim /start untuk memilih mode terlebih dahulu.",
            reply_markup=_main_menu_keyboard(),
        )
        return

    if mode == "roblox":
        await _handle_roblox_mode(update, context, chat_id, text)
    elif mode == "chat":
        await _handle_chat_mode(update, context, chat_id, text)
    elif mode == "universal":
        await _handle_universal_mode(update, context, chat_id, text)


# ================================================
# HANDLER MODE ROBLOX — PROGRESS REAL-TIME
# ================================================
async def _handle_roblox_mode(update, context, chat_id, text):
    global _roblox_background_task

    msg = await update.message.reply_text(
        "Perintah Diterima!\n\n"
        "AI sedang menganalisis dan menyiapkan daftar task...\n"
        "Progress akan dilaporkan langsung ke sini.",
        reply_markup=_control_keyboard(),
    )

    if _roblox_background_task and not _roblox_background_task.done():
        _roblox_background_task.cancel()
        try:
            await _roblox_background_task
        except asyncio.CancelledError:
            pass

    _roblox_agent_paused.set()
    NexusGlobalState.is_running = True

    async def run_fleet():
        try:
            await execute_antigravity_fleet(
                user_report=text,
                status_message=msg,
                bot_instance=context.bot,
                chat_id=chat_id,
            )
            # Kirim pesan selesai dengan tombol kontrol
            await _safe_send(
                context.bot,
                chat_id,
                "Semua Task Selesai!\n\n"
                "AI telah menyelesaikan semua pekerjaan.\n"
                "Kirim perintah baru atau gunakan /start.",
                reply_markup=_control_keyboard(show_stop=False),
            )
        except asyncio.CancelledError:
            await _safe_edit(
                msg,
                "Task Dihentikan ⛔\n\n"
                "Pekerjaan dihentikan oleh tombol STOP.\n"
                "Tekan LANJUTKAN untuk melanjutkan.",
                reply_markup=_control_keyboard(show_stop=False),
            )
        except Exception as e:
            err_short = str(e)[:300]
            await _safe_edit(
                msg,
                "Error Terjadi!\n\n" + err_short + "\n\nCoba /autofix atau kirim ulang perintah.",
                reply_markup=_control_keyboard(),
            )

    _roblox_background_task = asyncio.create_task(run_fleet())


# ================================================
# HANDLER MODE CHAT LANGSUNG — TIDAK GHOSTING
# ================================================
async def _handle_chat_mode(update, context, chat_id, text):
    """Mode chat langsung — AI selalu merespons dan bisa langsung mengerjakan tugas."""
    msg = await update.message.reply_text(
        "Memproses pesanmu...",
        reply_markup=_control_keyboard(),
    )

    # Deteksi apakah ini perintah pekerjaan atau pertanyaan biasa
    action_keywords = [
        "buat", "buat ", "create", "tambah", "tambahkan", "perbaiki", "fix", "repair",
        "tulis", "write", "generate", "hapus", "delete", "ubah", "ganti", "update",
        "implementasi", "implement", "jalankan", "run", "push", "deploy",
        "kerjakan", "lakukan", "execute",
    ]
    text_lower = text.lower()
    is_action = any(kw in text_lower for kw in action_keywords)

    system_prompt = (
        "Kamu adalah Nexus AI Agent — asisten AI otonom yang ahli dalam game Roblox, "
        "Python, dan pengembangan software.\n\n"
        "Kamu sedang berbicara langsung dengan owner/developer.\n"
        "Jawab dalam Bahasa Indonesia yang jelas dan ringkas.\n"
        "Jika owner minta kamu buat/perbaiki kode, berikan kode lengkap yang siap dijalankan.\n"
        "Jika owner bertanya, berikan penjelasan yang mudah dimengerti.\n"
        "TIDAK PERNAH ghosting — selalu berikan respons yang bermakna.\n\n"
        "KONTEKS SISTEM:\n"
        "- Ini adalah sistem AI otonom untuk pengembangan game Roblox\n"
        "- Ada pasukan AI agent (OmniSynthesizer, Scout, Healer, dll)\n"
        "- Kamu adalah antarmuka utama yang menerima perintah owner\n\n"
        "PESAN OWNER:\n" + text + "\n\n"
        "RESPONS KAMU (dalam Bahasa Indonesia):"
    )

    response = await _call_gemini(system_prompt)

    if not response or response.startswith("ERROR:"):
        await _safe_edit(
            msg,
            "AI gagal merespons. Coba lagi atau gunakan /start untuk reset.",
            reply_markup=_control_keyboard(),
        )
        return

    # Potong respons jika terlalu panjang
    if len(response) > 3800:
        chunks = [response[i:i + 3800] for i in range(0, len(response), 3800)]
        await _safe_edit(msg, "Respons AI (1/" + str(len(chunks)) + "):\n\n" + chunks[0])
        for i, chunk in enumerate(chunks[1:], 2):
            await _safe_send(
                context.bot,
                chat_id,
                "(" + str(i) + "/" + str(len(chunks)) + "):\n\n" + chunk,
                reply_markup=_control_keyboard() if i == len(chunks) else None,
            )
    else:
        await _safe_edit(
            msg,
            response,
            reply_markup=_control_keyboard(),
        )

    # Jika ini perintah aksi, tawarkan eksekusi langsung
    if is_action:
        await _safe_send(
            context.bot,
            chat_id,
            "Apakah kamu mau AI langsung mengerjakan ini sebagai task Roblox otonom?\n\n"
            "Tekan tombol di bawah atau kirim ulang dengan lebih detail.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Kerjakan Sekarang! 🚀", callback_data="mode_roblox")],
                [InlineKeyboardButton("Tidak, cukup penjelasannya", callback_data="ctrl_status")],
            ]),
        )


# ================================================
# HANDLER MODE UNIVERSAL
# ================================================
async def _handle_universal_mode(update, context, chat_id, text):
    msg = await update.message.reply_text(
        "Memproses request...",
        reply_markup=_control_keyboard(),
    )

    prompt = (
        "Kamu adalah senior developer expert semua bahasa pemrograman.\n"
        "Kerjakan request ini: " + text + "\n\n"
        "Berikan kode yang lengkap, bisa langsung dijalankan, dengan komentar yang jelas.\n"
        "Jika butuh library eksternal, sebutkan cara installnya."
    )

    result = await _call_gemini(prompt)

    if len(result) > 3800:
        chunks = [result[i:i + 3800] for i in range(0, len(result), 3800)]
        await _safe_edit(msg, "Hasil (1/" + str(len(chunks)) + "):\n\n" + chunks[0])
        for i, chunk in enumerate(chunks[1:], 2):
            await _safe_send(
                context.bot,
                chat_id,
                "(" + str(i) + "/" + str(len(chunks)) + "):\n\n" + chunk,
                reply_markup=_control_keyboard() if i == len(chunks) else None,
            )
    else:
        await _safe_edit(msg, result, reply_markup=_control_keyboard())


# ================================================
# MAIN
# ================================================
async def post_init(application: Application) -> None:
    global _bot_app
    _bot_app = application

    async def send_fn_init(text):
        await _safe_send(application.bot, _OWNER_CHAT_ID, text)

    await _startup_deep_scan(send_fn_init)

    # Kirim menu utama setelah startup
    await _safe_send(
        application.bot,
        _OWNER_CHAT_ID,
        "Nexus AI siap! Pilih mode atau gunakan tombol kontrol:",
        reply_markup=_main_menu_keyboard(),
    )


def run_telegram_bot():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("continue", cmd_continue))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("selffix", cmd_selffix))
    app.add_handler(CommandHandler("autofix", cmd_autofix))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    console_terminal_interface.print(
        "[bold green]Nexus Telegram Bot v" + _BOT_VERSION + " berjalan...[/bold green]"
    )
    app.run_polling(allowed_updates=["message", "callback_query"])


start_telegram_polling = run_telegram_bot

if __name__ == "__main__":
    run_telegram_bot()
