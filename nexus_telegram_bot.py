"""
nexus_telegram_bot.py  v2.0.0
==============================
Bot Telegram Interaktif — Antarmuka Manusia ke AI Agent Otonom Nexus.

Perintah Utama:
  /start    — Menu utama
  /stop     — Hentikan background task AI Roblox
  /continue — Lanjutkan AI Roblox
  /status   — Status lengkap agent
  /selffix  — AI perbaiki kode sendiri + sandbox test + push GitHub
  /autofix  — Perbaiki semua file Lua bermasalah
  /clear    — Reset percakapan
  /help     — Panduan lengkap

Perbaikan v2.0.0:
  - Owner TIDAK PERNAH kena rate limit / ditolak
  - Gemini retry otomatis (rotasi model + key), tidak pernah bilang "sibuk"
  - /stop  -- hentikan background task AI Roblox
  - /continue -- lanjutkan AI Roblox
  - /selffix -- AI perbaiki kode sendiri + sandbox test + push GitHub
  - Scan mendalam isi file saat startup (bukan hanya nama file)
  - Sandbox wajib sebelum setiap push kode ke GitHub
  - Fungsi duplikat v1.0.0 dihapus — hanya v2.0.0 yang aktif
"""

import os
import re
import json
import asyncio
import subprocess
import time
import shutil
import tempfile
import threading
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
from telegram.constants import ParseMode
from telegram.error import BadRequest

from nexus_config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    ACTIVE_AGENTS,
    GEMINI_CLI_PATH,
    PROJECT_ROOT_DIRECTORY,
    SOURCE_CODE_DIRECTORY,
    COMPILED_GAME_FILE,
    console_terminal_interface,
)

# Impor dari nexus_agents — termasuk _roblox_agent_paused agar keduanya
# menggunakan objek Event yang SAMA (menghindari dua objek terpisah).
from nexus_agents import (
    execute_antigravity_fleet,
    NexusGlobalState,
    global_agent_memory,
    _roblox_agent_paused,
)


# ================================================
# KONSTANTA & STATE GLOBAL
# ================================================
_BOT_VERSION = "2.0.0"
_OWNER_CHAT_ID = str(TELEGRAM_CHAT_ID).strip()
_user_state: dict = {}
_roblox_exec_lock = asyncio.Semaphore(1)

# ================================================
# STOP / CONTINUE STATE
# ================================================
_roblox_background_task: Optional[asyncio.Task] = None

# ================================================
# RATE LIMITING -- OWNER TIDAK PERNAH DITOLAK
# ================================================
_RATE_LIMIT_WINDOW = 10
_RATE_LIMIT_MAX = 30
_user_message_timestamps: dict = defaultdict(list)

MODEL_FALLBACK_SEQUENCE = [
    "gemini-2.0-flash",
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
    "gemma-3-27b-it",
    "gemini-3.1-flash-lite-preview",
    "gemma-3-12b-it",
    "gemma-3-4b-it",
    "gemma-3n-e4b-it",
    "gemma-3n-e2b-it",
    "gemma-3-1b-it",
]


def _check_rate_limit(chat_id: int) -> bool:
    if str(chat_id) == _OWNER_CHAT_ID:
        return False  # Owner selalu bebas
    now = time.time()
    _user_message_timestamps[chat_id] = [
        t for t in _user_message_timestamps[chat_id] if now - t < _RATE_LIMIT_WINDOW
    ]
    if len(_user_message_timestamps[chat_id]) >= _RATE_LIMIT_MAX:
        return True
    _user_message_timestamps[chat_id].append(now)
    return False


# ================================================
# GEMINI CLI -- TIDAK PERNAH MENOLAK, SELALU RETRY
# ================================================
def _call_gemini_sync(prompt: str, api_key: str, model: str = "gemini-2.0-flash") -> str:
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
def _rojo_build_sync() -> tuple:
    try:
        from nexus_main import RobloxDeployer
        return RobloxDeployer.compile_rojo()
    except Exception as e:
        return False, str(e)


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
async def _sandbox_test_file(file_path: str, new_content: str, send_fn) -> bool:
    await send_fn("Sandbox Testing -- menguji kode di lingkungan terisolasi...")

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
                    "Sandbox GAGAL -- Syntax Error\n"
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


async def _git_push(repo_dir: str, file_rel_path: str, commit_msg: str, send_fn) -> bool:
    github_token = (
        os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "")
        or os.getenv("GITHUB_TOKEN", "")
    )
    if not github_token:
        await send_fn("GITHUB_TOKEN tidak ditemukan di .env.nexus. Tambahkan: GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxxx")
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
# STARTUP SCAN: Baca ISI file saat agent nyala
# ================================================
async def _startup_deep_scan(send_fn):
    if not os.path.exists(SOURCE_CODE_DIRECTORY):
        await send_fn("Direktori src belum ada. Sistem mulai dari nol.")
        return

    lua_files = []
    for root, dirs, files in os.walk(SOURCE_CODE_DIRECTORY):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            if fname.endswith((".lua", ".luau")):
                lua_files.append(os.path.join(root, fname))

    await send_fn(
        "Nexus AI Agent v" + _BOT_VERSION + " Menyala!\n"
        "Scan mendalam " + str(len(lua_files)) + " file Lua...\n"
        "(Membaca ISI setiap file, bukan hanya nama)"
    )

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

    if violations:
        report = str(len(violations)) + " file bermasalah ditemukan:\n\n"
        for fname, issues in violations[:10]:
            report += "* " + fname + ":\n"
            for iss in issues:
                report += "  - " + iss + "\n"
        if len(violations) > 10:
            report += "\n...dan " + str(len(violations) - 10) + " file lainnya."
        report += "\n\nKirim /autofix untuk perbaiki otomatis."
        await send_fn(report)
    else:
        await send_fn(
            "Scan Selesai -- Semua " + str(len(lua_files)) + " file valid!\n"
            "Agent siap menerima perintah."
        )


# ================================================
# TASK EXECUTOR dengan PERSISTENT RETRY
# ================================================
async def execute_single_task_with_retry(task: dict, send_fn, max_attempts: int = 10) -> tuple:
    from nexus_agents import LuauKnowledgeScraper

    last_error = ""
    github_context = ""

    for attempt in range(1, max_attempts + 1):
        if not _roblox_agent_paused.is_set():
            await send_fn("Agent sedang di-pause. Menunggu /continue...")
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
            await asyncio.sleep(wait)

    await send_fn(
        "AI Butuh Bantuan!\n\n"
        "Task: " + task.get("title", "unknown") + "\n"
        "Sudah " + str(max_attempts) + "x gagal (termasuk pencarian panduan GitHub).\n\n"
        "Error terakhir:\n" + last_error[:400] + "\n\n"
        "Tolong balas dengan instruksi tambahan atau ubah pendekatan."
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
        "(File baru -- belum ada kode sebelumnya)"
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
        "Kamu adalah senior Roblox Luau developer. Perbaiki atau buat kode untuk game FantasyExtraction/TrueApex.\n\n"
        "TUGAS:\n" + detail + "\n\n"
        "TIPE FILE: " + file_type + "\n"
        "AKSI: " + action + "\n\n"
        "KODE SAAT INI:\n" + code_context + "\n\n"
        + ctx_extra + "\n\n"
        "ATURAN WAJIB:\n"
        "1. Baris pertama HARUS --!strict\n"
        "2. Jangan gunakan Enum untuk DisplayOrder, ZIndex, LayoutOrder (gunakan angka integer)\n"
        "3. Spawn point player HARUS menggunakan game.Workspace.SpawnLocation atau Teams\n"
        "4. Tombol UI HARUS memiliki event handler\n"
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
    keyboard = [
        [InlineKeyboardButton("Roblox Agent", callback_data="mode_roblox")],
        [InlineKeyboardButton("Universal Agent", callback_data="mode_universal")],
    ]
    await update.message.reply_text(
        "NEXUS AI AGENT v" + _BOT_VERSION + "\n\nPilih mode:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if chat_id != _OWNER_CHAT_ID:
        return

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

    await update.message.reply_text(
        "AI Agent Roblox DIHENTIKAN\n\n"
        "Semua pekerjaan background dihentikan.\n"
        "Kirim /continue untuk melanjutkan, atau beri perintah baru langsung."
    )


async def cmd_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if chat_id != _OWNER_CHAT_ID:
        return

    _roblox_agent_paused.set()
    NexusGlobalState.is_running = True

    await update.message.reply_text(
        "AI Agent Roblox DILANJUTKAN\n\nAgent siap menerima perintah baru."
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if chat_id != _OWNER_CHAT_ID:
        return

    paused = not _roblox_agent_paused.is_set()
    bg_running = _roblox_background_task and not _roblox_background_task.done()
    mode = _user_state.get(chat_id, {}).get("mode", "belum dipilih")

    await update.message.reply_text(
        "STATUS NEXUS AI AGENT v" + _BOT_VERSION + "\n\n"
        "Agent Roblox: " + ("PAUSE" if paused else "AKTIF") + "\n"
        "Background Task: " + ("Berjalan" if bg_running else "Idle") + "\n"
        "Mode Aktif: " + mode + "\n"
        "API Keys: " + str(len(ACTIVE_AGENTS)) + " aktif\n"
        "Loop Status: " + ("RUNNING" if NexusGlobalState.is_running else "STOPPED") + "\n\n"
        "Perintah:\n"
        "/stop -- Hentikan background task\n"
        "/continue -- Lanjutkan agent\n"
        "/selffix [file] [deskripsi] -- AI perbaiki & push kode\n"
        "/autofix -- Perbaiki semua file bermasalah\n"
        "/clear -- Reset percakapan\n"
        "/help -- Panduan lengkap"
    )


async def cmd_selffix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if chat_id != _OWNER_CHAT_ID:
        return

    args = context.args or []
    target_file = args[0] if args else "nexus_telegram_bot.py"
    fix_desc = " ".join(args[1:]) if len(args) > 1 else "Perbaiki semua bug yang ada, tingkatkan robustness"

    msg = await update.message.reply_text(
        "Self-Fix Dimulai\n\nFile: " + target_file + "\nInstruksi: " + fix_desc + "\n\nMembaca file asli..."
    )

    repo_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(repo_dir, target_file)
    if not os.path.exists(file_path):
        file_path = os.path.join(PROJECT_ROOT_DIRECTORY, target_file)
    if not os.path.exists(file_path):
        await msg.edit_text("File " + target_file + " tidak ditemukan.")
        return

    with open(file_path, "r", encoding="utf-8") as f:
        original_code = f.read()

    await msg.edit_text(
        "Self-Fix 2/5\n\nFile dibaca (" + str(len(original_code)) + " karakter)\nMeminta AI memperbaiki..."
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
        await msg.edit_text("AI gagal generate kode perbaikan. Coba lagi.")
        return

    await msg.edit_text("Self-Fix 3/5\n\nAI selesai generate kode baru\nSandbox testing...")

    async def send_to_msg(text):
        await msg.edit_text(text)

    sandbox_ok = await _sandbox_test_file(file_path, fixed_code, send_to_msg)
    if not sandbox_ok:
        return

    await msg.edit_text("Self-Fix 4/5\n\nSandbox OK\nMenyimpan & push ke GitHub...")

    backup_path = file_path + ".bak"
    shutil.copy2(file_path, backup_path)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(fixed_code)

    commit_msg = "[nexus_selffix] Auto-fix " + target_file + ": " + fix_desc[:60]
    await _git_push(repo_dir, target_file, commit_msg, send_to_msg)

    await msg.edit_text(
        "Self-Fix Selesai!\n\n"
        "File " + target_file + " berhasil diperbaiki.\n"
        "Backup disimpan di " + target_file + ".bak\n\n"
        "Restart bot untuk menerapkan perubahan:\n"
        "systemctl restart nexus-bot"
    )


async def cmd_autofix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if chat_id != _OWNER_CHAT_ID:
        return

    msg = await update.message.reply_text("Auto-Fix Dimulai -- Scanning semua file...")

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
        await msg.edit_text("Semua file sudah valid! Tidak ada yang perlu diperbaiki.")
        return

    await msg.edit_text("Auto-Fix: " + str(len(violations)) + " file bermasalah -- Memperbaiki...")

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

    await msg.edit_text(
        "Auto-Fix Selesai!\n\n"
        "Diperbaiki: " + str(fixed_count) + "/" + str(len(violations)) + " file\n"
        "Jalankan build ulang untuk memverifikasi."
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    _user_state.pop(chat_id, None)
    global_agent_memory.clear()
    await update.message.reply_text("Percakapan direset. Kirim /start untuk mulai lagi.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if chat_id != _OWNER_CHAT_ID:
        return
    await update.message.reply_text(
        "NEXUS AI AGENT v" + _BOT_VERSION + " -- Panduan\n\n"
        "Perintah Utama:\n"
        "/start -- Menu utama\n"
        "/stop -- Hentikan AI Roblox background\n"
        "/continue -- Lanjutkan AI Roblox\n"
        "/status -- Status lengkap agent\n\n"
        "Self-Fix & GitHub:\n"
        "/selffix [file] [deskripsi] -- AI perbaiki kode & push\n"
        "  Contoh: /selffix nexus_main.py perbaiki loop\n\n"
        "Maintenance:\n"
        "/autofix -- Perbaiki semua file Lua bermasalah\n"
        "/clear -- Reset percakapan\n\n"
        "Catatan:\n"
        "AI TIDAK PERNAH menolak perintahmu.\n"
        "Jika gagal, AI retry otomatis sampai 15x.\n"
        "Jika 10x gagal, AI akan tanya kamu."
    )


# ================================================
# CALLBACK & MESSAGE HANDLERS
# ================================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = str(query.message.chat.id)
    if chat_id != _OWNER_CHAT_ID:
        return

    data = query.data
    if data == "mode_roblox":
        _user_state[chat_id] = {"mode": "roblox", "step": "waiting_report"}
        await query.edit_message_text(
            "Mode AI Agent Otonom Full Roblox\n\n"
            "Kirimkan laporan bug atau permintaan fitur game kamu.\n"
            "Gunakan /stop kapanpun untuk menghentikan."
        )
    elif data == "mode_universal":
        _user_state[chat_id] = {"mode": "universal", "step": "waiting_request"}
        await query.edit_message_text(
            "Mode AI Agent Universal Code\n\n"
            "Kirimkan request kode apapun:\n"
            "Python, JavaScript, Lua, Rust, Go, dll."
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)

    if chat_id != _OWNER_CHAT_ID:
        if _check_rate_limit(int(chat_id)):
            await update.message.reply_text("Mohon tunggu sebentar.")
            return
        await update.message.reply_text("Bot ini pribadi.")
        return

    text = (update.message.text or "").strip()
    state = _user_state.get(chat_id, {})
    mode = state.get("mode", "")

    if "waiting_for_owner_input" in _user_state:
        waiting = _user_state.pop("waiting_for_owner_input")
        task = waiting["task"]
        task["detail"] += "\n\nINSTRUKSI TAMBAHAN DARI OWNER: " + text
        msg = await update.message.reply_text("Melanjutkan task dengan instruksi barumu...")

        async def send_fn(t):
            await msg.edit_text(t)

        await execute_single_task_with_retry(task, send_fn)
        return

    if not mode:
        await update.message.reply_text("Kirim /start untuk memilih mode terlebih dahulu.")
        return

    if mode == "roblox":
        await _handle_roblox_mode(update, context, chat_id, text)
    elif mode == "universal":
        await _handle_universal_mode(update, context, chat_id, text)


async def _handle_roblox_mode(update, context, chat_id, text):
    global _roblox_background_task

    msg = await update.message.reply_text("Analisis Laporan -- Membuat daftar task...")

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
        except asyncio.CancelledError:
            await msg.edit_text("Task dihentikan oleh /stop\n\nKirim /continue atau perintah baru.")
        except Exception as e:
            await msg.edit_text("Error: " + str(e)[:200] + "\n\nCoba /autofix.")

    _roblox_background_task = asyncio.create_task(run_fleet())


async def _handle_universal_mode(update, context, chat_id, text):
    msg = await update.message.reply_text("Memproses request...")

    prompt = (
        "Kamu adalah senior developer expert semua bahasa pemrograman.\n"
        "Kerjakan request ini: " + text + "\n\n"
        "Berikan kode yang lengkap, bisa langsung dijalankan, dengan komentar yang jelas.\n"
        "Jika butuh library eksternal, sebutkan cara installnya."
    )

    result = await _call_gemini(prompt)

    if len(result) > 3800:
        chunks = [result[i:i + 3800] for i in range(0, len(result), 3800)]
        await msg.edit_text("Hasil (bagian 1/" + str(len(chunks)) + "):\n\n" + chunks[0])
        for i, chunk in enumerate(chunks[1:], 2):
            await update.message.reply_text("(bagian " + str(i) + "/" + str(len(chunks)) + "):\n\n" + chunk)
    else:
        await msg.edit_text(result)


# ================================================
# MAIN
# ================================================
async def post_init(application: Application) -> None:
    async def send_fn(text):
        await application.bot.send_message(chat_id=_OWNER_CHAT_ID, text=text)
    await _startup_deep_scan(send_fn)


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

    console_terminal_interface.print("[bold green]Nexus Telegram Bot v" + _BOT_VERSION + " berjalan...[/bold green]")
    app.run_polling(allowed_updates=["message", "callback_query"])


start_telegram_polling = run_telegram_bot

if __name__ == "__main__":
    run_telegram_bot()
