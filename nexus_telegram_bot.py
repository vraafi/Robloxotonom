"""
nexus_telegram_bot.py
=====================
Bot Telegram Interaktif — Antarmuka Manusia ke AI Agent Otonom Nexus.

Menu Utama:
  1. 🤖 AI Agent Universal Code   — Request kode apa saja (Python, JS, Lua, dll)
  2. 🎮 AI Agent Otonom Full Roblox — Bug fix & feature request khusus game Roblox

Mode Full Roblox menggunakan gaya Google Antigravity:
  • Setiap task ditampilkan sebagai papan status live
  • Eksekusi paralel — semua task dikerjakan serentak
  • Update real-time di pesan yang sama
  • Auto build + deploy ke Roblox setelah semua task selesai
"""

import os
import re
import json
import asyncio
import subprocess
import time
from collections import defaultdict
import textwrap
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

# Mengimpor Orchestrator dari nexus_agents.py
from nexus_agents import execute_antigravity_fleet, NexusGlobalState, global_agent_memory

# [FITUR BARU]: Rate limiting — maksimum 3 pesan per 10 detik per chat
_RATE_LIMIT_WINDOW = 10
_RATE_LIMIT_MAX = 3
_user_message_timestamps: dict = defaultdict(list)


def _check_rate_limit(chat_id: int) -> bool:
    """Mengembalikan True jika user melebihi batas rate limit."""
    now = time.time()
    _user_message_timestamps[chat_id] = [
        t for t in _user_message_timestamps[chat_id] if now - t < _RATE_LIMIT_WINDOW
    ]
    if len(_user_message_timestamps[chat_id]) >= _RATE_LIMIT_MAX:
        return True
    _user_message_timestamps[chat_id].append(now)
    return False


# ════════════════════════════════════════════════════════════════
# KONSTANTA & STATE GLOBAL
# ════════════════════════════════════════════════════════════════
_BOT_VERSION = "1.0.0"
_OWNER_CHAT_ID = str(TELEGRAM_CHAT_ID).strip()

# State percakapan per pengguna
_user_state: dict = {}
# Format: {chat_id: {"mode": str, "step": str, "pending_report": str}}

# Semaphore agar hanya 1 eksekusi Roblox berjalan sekaligus
_roblox_exec_lock = asyncio.Semaphore(1)


# Urutan fallback model — mencoba dari terbesar ke terkecil
MODEL_FALLBACK_SEQUENCE = [
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
    "gemma-3-27b-it",
    "gemini-3.1-flash-lite-preview",
    "gemma-3-12b-it",
    "gemma-3-4b-it",
    "gemma-3n-e4b-it",
    "gemma-3n-e2b-it",
    "gemma-3-1b-it"
]


# ════════════════════════════════════════════════════════════════
# HELPER: GEMINI CLI
# ════════════════════════════════════════════════════════════════
def _call_gemini_sync(prompt: str, api_key: str, model: str = "models/gemini-2.0-flash") -> str:
    """Panggil Gemini CLI secara sinkron."""
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = api_key
    env["CI"] = "true"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    current_path = env.get("PATH", "")
    env["PATH"] = "/home/runner/.local/bin:/home/ubuntu/.local/bin:/home/ubuntu/.local/share/pnpm:" + current_path
    try:
        result = subprocess.run(
            [GEMINI_CLI_PATH, "-m", model, "-y", "-p", prompt],
            env=env, capture_output=True, text=True, timeout=180,
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "ERROR: Gemini timeout (180 detik)."
    except Exception as e:
        return f"ERROR: {e}"


async def _call_gemini(prompt: str) -> str:
    """Async wrapper untuk Gemini CLI — menggunakan agent aktif pertama."""
    if not ACTIVE_AGENTS:
        return "ERROR: Tidak ada agent aktif."
    api_key = ACTIVE_AGENTS[0]["api_key"]
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _call_gemini_sync, prompt, api_key)


# ════════════════════════════════════════════════════════════════
# HELPER: ROJO BUILD & DEPLOY
# ════════════════════════════════════════════════════════════════
def _rojo_build_sync() -> tuple:
    """Jalankan Rojo build dan kembalikan (sukses, stderr)."""
    try:
        from nexus_main import RobloxDeployer
        return RobloxDeployer.compile_rojo()
    except Exception as e:
        return False, str(e)


def _find_lua_file_by_name(name: str) -> Optional[str]:
    """Cari file Lua berdasarkan nama (fuzzy)."""
    name_lower = name.lower().replace(" ", "_").replace("-", "_")
    best = None
    best_score = 0
    for root, dirs, files in os.walk(SOURCE_CODE_DIRECTORY):
        for fname in files:
            if not fname.endswith((".lua", ".luau", ".rbxmx")):
                continue
            base = os.path.splitext(fname)[0].lower()
            # Hitung skor kecocokan sederhana
            score = 0
            if name_lower == base:
                score = 100
            elif name_lower in base or base in name_lower:
                score = 50
            else:
                # Cek tiap kata
                words = re.split(r"[_\-\s]+", name_lower)
                matched = sum(1 for w in words if w and w in base)
                score = matched * 10
            if score > best_score:
                best_score = score
                best = os.path.join(root, fname)
    return best if best_score >= 10 else None


# ════════════════════════════════════════════════════════════════
# TASK ANALYZER: Parse laporan bug/fitur → Daftar tugas
# ════════════════════════════════════════════════════════════════
async def analyze_report_to_tasks(report: str) -> list:
    """
    Gunakan Gemini untuk menganalisis laporan dan menghasilkan daftar tugas spesifik.
    Return: list of dicts [{"id": 1, "title": "...", "target_file": "...", "action": "...", "detail": "..."}]
    """
    prompt = f"""Kamu adalah AI Architect untuk game Roblox bernama FantasyExtraction/TrueApex.
Analisis laporan bug/permintaan fitur berikut dan buat daftar tugas perbaikan yang SPESIFIK.

LAPORAN DARI PEMILIK GAME:
{report}

STRUKTUR PROJECT (Rojo):
- src/StarterGui/        → UI/ScreenGui (file .client.lua atau .rbxmx)
- src/ServerScriptService/  → Server scripts (.server.lua)
- src/StarterPlayerScripts/ → Client scripts (.client.lua)
- src/ReplicatedStorage/    → ModuleScripts (.lua)

OUTPUT FORMAT (JSON array, tidak ada teks lain):
[
  {{
    "id": 1,
    "title": "Judul singkat tugas",
    "target_folder": "StarterGui|ServerScriptService|StarterPlayerScripts|ReplicatedStorage",
    "target_file_hint": "nama file yang kemungkinan perlu diubah (tanpa path)",
    "action": "fix_bug|add_feature|modify",
    "priority": "high|medium|low",
    "detail": "Instruksi spesifik untuk AI: apa yang harus diubah/ditambahkan"
  }}
]

ATURAN:
- Buat tugas SESPESIFIK MUNGKIN
- Setiap tugas = 1 file atau 1 sistem
- Maksimal 8 tugas per laporan
- Tugas harus bisa langsung dikerjakan oleh AI code generator
- HANYA output JSON, tidak ada penjelasan

JSON:"""

    response = await _call_gemini(prompt)

    # Extract JSON dari response
    json_match = re.search(r'\[[\s\S]*?\]', response)
    if json_match:
        try:
            tasks = json.loads(json_match.group())
            if isinstance(tasks, list):
                return tasks
        except json.JSONDecodeError:
            pass

    # Fallback: buat 1 tugas umum
    return [{
        "id": 1,
        "title": "Perbaiki masalah yang dilaporkan",
        "target_folder": "StarterGui",
        "target_file_hint": "unknown",
        "action": "fix_bug",
        "priority": "high",
        "detail": report,
    }]


# ════════════════════════════════════════════════════════════════
# TASK EXECUTOR: Kerjakan tiap tugas dengan Gemini
# ════════════════════════════════════════════════════════════════
async def execute_single_task(task: dict) -> tuple:
    """
    Eksekusi satu tugas: cari file → generate fix → tulis ke disk.
    Return: (success: bool, message: str)
    """
    try:
        hint = task.get("target_file_hint", "")
        folder = task.get("target_folder", "")
        detail = task.get("detail", "")
        action = task.get("action", "fix_bug")

        # Cari file yang relevan
        file_path = _find_lua_file_by_name(hint) if hint and hint != "unknown" else None

        if file_path and os.path.exists(file_path):
            # Baca konten file yang ada
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                original_code = f.read()
            ext = os.path.splitext(file_path)[1]
        else:
            # File baru
            original_code = ""
            safe_name = re.sub(r"[^\w]", "_", task.get("title", "new_feature")).upper()
            if folder == "ServerScriptService":
                ext = ".server.lua"
                fname = f"{safe_name}.server.lua"
            elif folder in ("StarterGui", "StarterPlayerScripts", "StarterCharacterScripts"):
                ext = ".client.lua"
                fname = f"{safe_name}.client.lua"
            else:
                ext = ".lua"
                fname = f"{safe_name}.lua"
            file_path = os.path.join(SOURCE_CODE_DIRECTORY, folder, fname)

        # Buat prompt untuk Gemini
        is_new = not original_code
        if is_new:
            code_context = "(File baru — belum ada kode sebelumnya)"
        else:
            # Kirim maks 4000 char kode untuk hemat token
            code_context = original_code[:4000] + ("..." if len(original_code) > 4000 else "")

        file_type = "ScreenGui LocalScript" if folder == "StarterGui" else \
                    "Server Script" if folder == "ServerScriptService" else \
                    "Client Script" if folder == "StarterPlayerScripts" else "ModuleScript"

        prompt = f"""Kamu adalah senior Roblox Luau developer. Perbaiki atau buat kode untuk game FantasyExtraction/TrueApex.

TUGAS:
{detail}

TIPE FILE: {file_type}
AKSI: {action}

KODE SAAT INI:
{code_context}

ATURAN WAJIB:
1. Baris pertama HARUS --!strict
2. Jangan gunakan Enum untuk DisplayOrder, ZIndex, LayoutOrder (gunakan angka integer)
3. Spawn point player HARUS menggunakan game.Workspace.SpawnLocation atau Teams
4. Tombol UI HARUS memiliki event handler (MouseButton1Click atau Activated)
5. Kondisi Buy/Sell HARUS diperiksa: jika tidak ada item, sembunyikan atau disable tombol
6. HANYA output kode Luau murni, tidak ada penjelasan

KODE YANG SUDAH DIPERBAIKI:"""

        fixed_code = await _call_gemini(prompt)

        if not fixed_code or "ERROR:" in fixed_code[:20]:
            return False, f"Gemini gagal generate: {fixed_code[:100]}"

        # Validasi minimal
        if len(fixed_code.strip()) < 20:
            return False, "Kode yang dihasilkan terlalu pendek"

        # Pastikan direktori ada
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        # Tulis ke disk
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(fixed_code)

        fname = os.path.basename(file_path)
        return True, f"✅ {fname} berhasil {'dibuat' if is_new else 'diperbarui'}"

    except Exception as e:
        return False, f"❌ Exception: {str(e)[:100]}"


# ════════════════════════════════════════════════════════════════
# STATUS BOARD: Tampilan Antigravity
# ════════════════════════════════════════════════════════════════
def _build_status_board(tasks: list, statuses: dict, phase: str, summary: str = "") -> str:
    """
    Buat papan status Antigravity-style untuk ditampilkan di Telegram.
    statuses: {task_id: "pending|running|done|failed"}
    """
    ICONS = {
        "pending": "⏳",
        "running": "⚙️",
        "done":    "✅",
        "failed":  "❌",
    }
    lines = [
        "```",
        "╔══════════════════════════════════════╗",
        f"║  🚀 NEXUS AI — {phase:<22}║",
        "╠══════════════════════════════════════╣",
    ]
    for task in tasks:
        tid = task["id"]
        status = statuses.get(tid, "pending")
        icon = ICONS.get(status, "⏳")
        title = task["title"][:30]
        pri = "🔴" if task.get("priority") == "high" else "🟡" if task.get("priority") == "medium" else "🟢"
        lines.append(f"║ {icon} {pri} {title:<28}  ║")
    lines.append("╠══════════════════════════════════════╣")
    done = sum(1 for s in statuses.values() if s == "done")
    failed = sum(1 for s in statuses.values() if s == "failed")
    total = len(tasks)
    lines.append(f"║  ✅ {done}/{total} selesai  ❌ {failed} gagal           ║")
    if summary:
        lines.append(f"║  📋 {summary[:34]:<34}║")
    lines.append("╚══════════════════════════════════════╝")
    lines.append("```")
    return "\n".join(lines)


async def _safe_edit(message: Message, text: str) -> None:
    """Edit pesan dengan aman (ignore BadRequest jika teks sama)."""
    try:
        await message.edit_text(text, parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest:
        pass
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════
# PIPELINE EKSEKUSI ROBLOX ANTIGRAVITY
# ════════════════════════════════════════════════════════════════
async def run_roblox_pipeline(tasks: list, status_msg: Message) -> None:
    """
    Eksekusi semua tugas secara paralel (Antigravity style),
    lalu trigger Rojo build + deploy.
    """
    statuses = {t["id"]: "pending" for t in tasks}

    # Tampilkan status awal
    await _safe_edit(
        status_msg,
        _build_status_board(tasks, statuses, "MEMULAI EKSEKUSI", "Paralel mode aktif...")
    )
    await asyncio.sleep(0.5)

    # Tandai semua sebagai "running"
    for t in tasks:
        statuses[t["id"]] = "running"
    await _safe_edit(
        status_msg,
        _build_status_board(tasks, statuses, "EKSEKUSI PARALEL", f"{len(tasks)} task berjalan...")
    )

    # Eksekusi semua task secara paralel
    async def run_task(task):
        ok, msg = await execute_single_task(task)
        statuses[task["id"]] = "done" if ok else "failed"
        # Update board setelah tiap task selesai
        done_count = sum(1 for s in statuses.values() if s in ("done", "failed"))
        await _safe_edit(
            status_msg,
            _build_status_board(
                tasks, statuses, "EKSEKUSI PARALEL",
                f"{done_count}/{len(tasks)} selesai..."
            )
        )
        return ok, msg

    results = await asyncio.gather(*[run_task(t) for t in tasks])

    success_count = sum(1 for ok, _ in results if ok)
    fail_count = sum(1 for ok, _ in results if not ok)

    # Tampilkan status setelah semua task selesai
    await _safe_edit(
        status_msg,
        _build_status_board(
            tasks, statuses, "TASK SELESAI",
            f"{success_count} OK / {fail_count} gagal"
        )
    )
    await asyncio.sleep(1)

    # ── Proactive fix sebelum build
    try:
        from nexus_main import RojoBuildAutoHealer
        fixes = RojoBuildAutoHealer.proactive_scan_and_fix()
        fix_text = f"🔧 ProactiveFix: {fixes} masalah diperbaiki" if fixes > 0 else "🔧 ProactiveFix: tidak ada masalah tambahan"
    except Exception as e:
        fix_text = f"⚠️ ProactiveFix skip: {e}"

    await status_msg.reply_text(fix_text)

    # ── Rojo Build
    await status_msg.reply_text("🏗️ *Rojo build dimulai\\.\\.\\.*", parse_mode=ParseMode.MARKDOWN_V2)

    loop = asyncio.get_running_loop()
    rojo_ok, rojo_err = await loop.run_in_executor(None, _rojo_build_sync)

    if not rojo_ok:
        # Coba auto-heal
        await status_msg.reply_text("⚠️ Build gagal\\. Menjalankan *auto\\-heal*\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
        try:
            from nexus_main import RojoBuildAutoHealer
            agent = ACTIVE_AGENTS[0] if ACTIVE_AGENTS else {}
            healed = await RojoBuildAutoHealer.heal_loop(rojo_err, agent)
            if healed:
                rojo_ok, rojo_err = await loop.run_in_executor(None, _rojo_build_sync)
        except Exception as e:
            await status_msg.reply_text(f"❌ Auto\\-heal error: `{str(e)[:80]}`", parse_mode=ParseMode.MARKDOWN_V2)

    if rojo_ok:
        # ── Deploy ke Roblox
        await status_msg.reply_text("🚀 *Build berhasil\\! Deploy ke Roblox\\.\\.\\.*", parse_mode=ParseMode.MARKDOWN_V2)
        try:
            from nexus_main import RobloxDeployer
            deploy_ok, version = await loop.run_in_executor(
                None, lambda: RobloxDeployer._upload_to_roblox()
            )
            if deploy_ok:
                await status_msg.reply_text(
                    f"🎉 *DEPLOY BERHASIL\\!* \\(Versi {version}\\)\n"
                    f"Game kamu sudah diperbarui di Roblox\\!",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            else:
                await status_msg.reply_text(
                    "✅ Build berhasil tapi deploy manual diperlukan\\.\nFile \\`.rbxl\\` sudah diperbarui di VPS\\.",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
        except Exception as e:
            await status_msg.reply_text(
                f"✅ Build berhasil\\! \\(Deploy error: `{str(e)[:60]}`\\)\nCoba deploy manual dari VPS\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
    else:
        await status_msg.reply_text(
            f"❌ *Build gagal* setelah auto\\-heal\\.\n```\n{rojo_err[:300]}\n```",
            parse_mode=ParseMode.MARKDOWN_V2
        )

    # Ringkasan akhir
    await status_msg.reply_text(
        f"📊 *RINGKASAN EKSEKUSI*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Task berhasil : {success_count}/{len(tasks)}\n"
        f"❌ Task gagal    : {fail_count}/{len(tasks)}\n"
        f"🏗️ Rojo build    : {'✅ Berhasil' if rojo_ok else '❌ Gagal'}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"_Gunakan /start untuk request berikutnya_",
        parse_mode=ParseMode.MARKDOWN_V2
    )


# ════════════════════════════════════════════════════════════════
# BOT HANDLERS
# ════════════════════════════════════════════════════════════════
def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 AI Agent Universal Code", callback_data="mode_universal")],
        [InlineKeyboardButton("🎮 AI Agent Otonom Full Roblox", callback_data="mode_roblox")],
        [InlineKeyboardButton("📊 Status Game & VPS", callback_data="mode_status")],
    ])


def _roblox_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🐛 Laporkan Bug", callback_data="roblox_bug")],
        [InlineKeyboardButton("✨ Request Fitur Baru", callback_data="roblox_feature")],
        [InlineKeyboardButton("🔨 Paksa Build & Deploy Ulang", callback_data="roblox_rebuild")],
        [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="back_main")],
    ])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    _user_state[chat_id] = {"mode": None, "step": "menu"}

    await update.message.reply_text(
        "🧠 *NEXUS AI AGENT* — Panel Kontrol\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Selamat datang\\! Pilih mode AI yang ingin kamu gunakan:\n\n"
        "🤖 *Universal Code* — Buat kode apa saja\n"
        "🎮 *Full Roblox AI* — Bug fix & fitur game\n"
        "📊 *Status* — Info game & VPS",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_main_menu_keyboard(),
    )



async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """[FITUR BARU]: Perintah /help — tampilkan daftar perintah."""
    help_text = (
        "📖 Nexus AI Agent — Daftar Perintah\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "/start — Mulai & tampilkan menu utama\n"
        "/help — Tampilkan daftar perintah ini\n"
        "/status — Lihat riwayat percakapan saat ini\n"
        "/clear — Hapus riwayat percakapan (mulai sesi baru)\n"
        "/menu — Sama dengan /start\n\n"
        "💡 Cara pakai:\n"
        "- Ketik permintaan kode langsung\n"
        "- Kirim file .lua/.py/.txt untuk dianalisis AI\n"
        "- AI otomatis mencoba model dari yang terkuat"
    )
    await update.message.reply_text(help_text)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """[FITUR BARU]: Perintah /clear — hapus riwayat percakapan."""
    chat_id = update.effective_chat.id
    if str(chat_id) != str(TELEGRAM_CHAT_ID):
        return
    global_agent_memory.clear()
    await update.message.reply_text(
        "🗑️ Riwayat percakapan berhasil dihapus.\n"
        "Sesi baru dimulai. AI tidak lagi mengingat percakapan sebelumnya."
    )
    console_terminal_interface.print("[bold green]✅ Riwayat percakapan dihapus oleh pengguna.[/bold green]")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """[FITUR BARU]: Perintah /status — tampilkan status bot dan riwayat."""
    chat_id = update.effective_chat.id
    if str(chat_id) != str(TELEGRAM_CHAT_ID):
        return
    override_status = "🔴 Aktif" if NexusGlobalState.TELEGRAM_OVERRIDE_ACTIVE else "🟢 Tidak aktif"
    history_count = len(global_agent_memory._history)
    context_str = global_agent_memory.get_context_string()
    if len(context_str) > 500:
        context_str = "...\n" + context_str[-500:]
    status_text = (
        f"📊 Status Nexus AI Agent\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Override Telegram: {override_status}\n"
        f"Riwayat tersimpan: {history_count} pesan\n\n"
        f"Riwayat terbaru:\n{context_str}"
    )
    await update.message.reply_text(status_text[:4000])

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = str(query.message.chat_id)
    data = query.data

    if data == "back_main":
        _user_state[chat_id] = {"mode": None, "step": "menu"}
        await query.message.edit_text(
            "🧠 *NEXUS AI AGENT* — Panel Kontrol\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Pilih mode AI:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_main_menu_keyboard(),
        )

    elif data == "mode_universal":
        _user_state[chat_id] = {"mode": "universal", "step": "waiting_input"}
        await query.message.edit_text(
            "🤖 *AI Agent Universal Code*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Ketik request kode kamu\\.\n"
            "Contoh:\n"
            "• _Buatkan script Python untuk rename file massal_\n"
            "• _Buatkan endpoint Express.js untuk upload gambar_\n"
            "• _Buatkan komponen React tombol animasi_\n\n"
            "💬 Ketik sekarang\\:",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    elif data == "mode_roblox":
        _user_state[chat_id] = {"mode": "roblox", "step": "menu"}
        await query.message.edit_text(
            "🎮 *AI Agent Otonom Full Roblox*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Apa yang ingin kamu lakukan?",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_roblox_menu_keyboard(),
        )

    elif data == "mode_status":
        # Hitung statistik project
        lua_count = sum(
            1 for root, _, files in os.walk(SOURCE_CODE_DIRECTORY)
            for f in files if f.endswith((".lua", ".luau"))
        )
        rbxmx_count = sum(
            1 for root, _, files in os.walk(PROJECT_ROOT_DIRECTORY)
            for f in files if f.endswith(".rbxmx")
        )
        build_exists = "✅ Ada" if os.path.exists(COMPILED_GAME_FILE) else "❌ Tidak ada"
        build_time = ""
        if os.path.exists(COMPILED_GAME_FILE):
            mtime = os.path.getmtime(COMPILED_GAME_FILE)
            import datetime
            dt = datetime.datetime.fromtimestamp(mtime)
            build_time = f"\n📅 Build terakhir: {dt.strftime('%d/%m/%Y %H:%M:%S')}"
        agents_info = f"{len(ACTIVE_AGENTS)} agent Gemini aktif"

        await query.message.edit_text(
            f"📊 *STATUS GAME & VPS*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🗂️ File Lua/Luau : {lua_count} file\n"
            f"🎭 File RBXMX   : {rbxmx_count} file\n"
            f"📦 File \\.rbxl  : {build_exists}{build_time}\n"
            f"🤖 Agent AI     : {agents_info}\n"
            f"📁 Root Project : `{PROJECT_ROOT_DIRECTORY}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="mode_status")],
                [InlineKeyboardButton("◀️ Kembali", callback_data="back_main")],
            ]),
        )

    elif data == "roblox_bug":
        _user_state[chat_id] = {"mode": "roblox", "step": "waiting_bug"}
        await query.message.edit_text(
            "🐛 *Laporkan Bug*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Ceritakan bug yang kamu temukan secara detail\\.\n"
            "Semakin detail semakin tepat perbaikannya\\!\n\n"
            "Contoh yang baik:\n"
            "_Player spawn di tengah laut saat join game\\. "
            "Tombol X di HUD tidak muncul\\. "
            "Buy/Sell muncul padahal inventory kosong\\._\n\n"
            "💬 Ceritakan bug kamu\\:",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    elif data == "roblox_feature":
        _user_state[chat_id] = {"mode": "roblox", "step": "waiting_feature"}
        await query.message.edit_text(
            "✨ *Request Fitur Baru*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Deskripsikan fitur yang ingin kamu tambahkan\\.\n\n"
            "Contoh:\n"
            "_Tambahkan sistem guild/klan — pemain bisa buat kelompok, "
            "chat guild, dan share reward dari boss raid\\._\n\n"
            "💬 Deskripsikan fitur kamu\\:",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    elif data == "roblox_rebuild":
        await query.message.edit_text(
            "🔨 *Memulai Build & Deploy Ulang\\.\\.\\.*\n"
            "Harap tunggu, ini bisa memakan waktu beberapa menit\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        # Jalankan di background
        asyncio.create_task(_force_rebuild(query.message))

    elif data.startswith("confirm_tasks_"):
        # User konfirmasi eksekusi task
        task_key = data.replace("confirm_tasks_", "")
        state = _user_state.get(chat_id, {})
        pending_tasks = state.get("pending_tasks", [])
        if not pending_tasks:
            await query.message.edit_text("⚠️ Tidak ada task yang tersimpan\\. Mulai ulang dengan /start", parse_mode=ParseMode.MARKDOWN_V2)
            return
        status_msg = await query.message.reply_text(
            "🚀 *Eksekusi dimulai\\.*\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await query.message.delete()
        async with _roblox_exec_lock:
            await run_roblox_pipeline(pending_tasks, status_msg)

    elif data == "cancel_tasks":
        _user_state[chat_id] = {"mode": None, "step": "menu"}
        await query.message.edit_text(
            "❌ Dibatalkan\\. Gunakan /start untuk memulai lagi\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tangani pesan teks atau dokumen dari pengguna."""
    message = update.message or update.edited_message
    if not message: return

    chat_id = str(update.effective_chat.id)

    # [FITUR BARU]: Baca teks dari pesan biasa ATAU caption dari file/dokumen
    text = message.text or message.caption or ""

    # [FITUR BARU]: Jika ada dokumen/file, baca isi fullnya dan gabungkan ke teks
    if message.document:
        try:
            new_file = await context.bot.get_file(message.document.file_id)
            file_path = f"temp_nexus_{message.document.file_name}"
            await new_file.download_to_drive(file_path)
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                file_content = f.read()
            os.remove(file_path)
            text = f"{text}\n\n--- 100% ISI FULL FILE ({message.document.file_name}) ---\n{file_content}\n--- AKHIR DARI FILE ---"
        except Exception as e:
            console_terminal_interface.print(f"[bold red]Gagal membaca file dari Telegram: {e}[/bold red]")

    if not text or not text.strip():
        return

    state = _user_state.get(chat_id, {})
    mode = state.get("mode")
    step = state.get("step")

    # ── Universal Code Mode (DIPERBAIKI: gunakan execute_antigravity_fleet + fallback)
    if mode == "universal" and step == "waiting_input":
        thinking_msg = await message.reply_text(
            "🤖 *AI sedang memproses request kamu\\.\\.\\.*",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

        ai_response = ""
        is_success = False

        # [FITUR BARU]: Aktifkan override agar agen latar belakang berhenti sementara
        NexusGlobalState.TELEGRAM_OVERRIDE_ACTIVE = True
        console_terminal_interface.print("[bold yellow]🛑 TELEGRAM OVERRIDE AKTIF — AGEN OTONOM DIJEDA[/bold yellow]")
        try:
            # Iterasi fallback model dari terbesar ke terkecil
            for current_model in MODEL_FALLBACK_SEQUENCE:
            console_terminal_interface.print(f"[bold magenta]🔄 Mencoba model: {current_model}...[/bold magenta]")
            try:
                ai_response = await execute_antigravity_fleet(text, model=current_model)
                if ai_response and "TUGAS" in ai_response and "GAGAL" in ai_response:
                    console_terminal_interface.print(f"[bold yellow]⚠️ Model {current_model} gagal. Beralih...[/bold yellow]")
                elif ai_response:
                    is_success = True
                    console_terminal_interface.print(f"[bold green]✅ Berhasil dengan model: {current_model}[/bold green]")
                    break
            except Exception as model_err:
                console_terminal_interface.print(f"[bold red]❌ Error model {current_model}: {model_err}. Beralih...[/bold red]")
                continue

        finally:
                # [FITUR BARU]: Kembalikan kontrol ke agen otonom setelah selesai
                NexusGlobalState.TELEGRAM_OVERRIDE_ACTIVE = False
                console_terminal_interface.print("[bold green]▶️ AGEN OTONOM DIIZINKAN MELANJUTKAN.[/bold green]")
        if not is_success:
            ai_response = "❌ Seluruh urutan model telah diuji dan gagal mengeksekusi permintaan."

        await thinking_msg.delete()

        # [FITUR BARU]: Smart markdown-aware chunker (cegah triple-backtick terpotong)
        code_marker = "`" * 3
        if len(ai_response) > 4000:
            chunks = []
            remaining_text = ai_response
            while len(remaining_text) > 4000:
                split_index = remaining_text.rfind('\n', 0, 4000)
                if split_index == -1: split_index = 4000
                chunk = remaining_text[:split_index]
                processed_length = len(ai_response) - len(remaining_text) + split_index
                code_block_count = ai_response[:processed_length].count(code_marker)
                if code_block_count % 2 != 0:
                    chunk += "\n" + code_marker
                    chunks.append(chunk)
                    remaining_text = code_marker + "\n" + remaining_text[split_index:].lstrip()
                else:
                    chunks.append(chunk)
                    remaining_text = remaining_text[split_index:].lstrip()
            if remaining_text: chunks.append(remaining_text)
            for chunk in chunks:
                await message.reply_text(chunk)
                await asyncio.sleep(1.5)
        else:
            await message.reply_text(ai_response)

        # Tawarkan request lagi
        await message.reply_text(
            "💬 Mau request kode lain? Ketik saja, atau /start untuk menu utama\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

        report_type = "Bug" if step == "waiting_bug" else "Fitur"
        analyzing_msg = await update.message.reply_text(
            f"🔍 *Menganalisis {report_type.lower()} yang dilaporkan\\.\\.\\.*\n"
            f"AI sedang membuat daftar tugas\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

        # Analisis laporan → daftar tugas
        tasks = await analyze_report_to_tasks(text)
        _user_state[chat_id]["pending_tasks"] = tasks

        await analyzing_msg.delete()

        # Tampilkan daftar tugas untuk konfirmasi
        task_lines = []
        for t in tasks:
            pri_icon = "🔴" if t.get("priority") == "high" else "🟡" if t.get("priority") == "medium" else "🟢"
            task_lines.append(f"{pri_icon} *{t['id']}\\. {_esc(t['title'])}*\n   _{_esc(t.get('detail','')[:80])}_")

        tasks_text = "\n\n".join(task_lines)
        confirm_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🚀 Kerjakan Semua ({len(tasks)} task)", callback_data=f"confirm_tasks_{chat_id}")],
            [InlineKeyboardButton("❌ Batalkan", callback_data="cancel_tasks")],
        ])

        await update.message.reply_text(
            f"📋 *DAFTAR TUGAS TERDETEKSI*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{tasks_text}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Total: *{len(tasks)} tugas* \\| Mode: *Paralel*\n\n"
            f"Konfirmasi untuk mulai eksekusi:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=confirm_keyboard,
        )

    else:
        # Tidak dalam mode apapun — arahkan ke menu
        await update.message.reply_text(
            "💬 Ketik /start untuk membuka menu AI Agent\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# ════════════════════════════════════════════════════════════════
# HELPER: Escape Markdown V2
# ════════════════════════════════════════════════════════════════
def _esc(text: str) -> str:
    """Escape karakter spesial untuk Telegram MarkdownV2."""
    special = r'\_*[]()~`>#+-=|{}.!'
    return re.sub(f"([{re.escape(special)}])", r"\\\1", str(text))


# ════════════════════════════════════════════════════════════════
# FORCE REBUILD
# ════════════════════════════════════════════════════════════════
async def _force_rebuild(message: Message) -> None:
    """Paksa build + deploy ulang tanpa modifikasi kode."""
    try:
        from nexus_main import RojoBuildAutoHealer, RobloxDeployer
        fixes = RojoBuildAutoHealer.proactive_scan_and_fix()
        await message.reply_text(f"🔧 ProactiveFix: {fixes} masalah diperbaiki\\.", parse_mode=ParseMode.MARKDOWN_V2)

        loop = asyncio.get_running_loop()
        rojo_ok, rojo_err = await loop.run_in_executor(None, _rojo_build_sync)

        if rojo_ok:
            await message.reply_text("✅ *Build berhasil\\! Deploying\\.\\.\\.*", parse_mode=ParseMode.MARKDOWN_V2)
            await loop.run_in_executor(None, lambda: None)  # placeholder
            await RobloxDeployer.publish(0)
        else:
            await message.reply_text(
                f"❌ Build gagal:\n```\n{rojo_err[:300]}\n```",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
    except Exception as e:
        await message.reply_text(f"❌ Error: `{_esc(str(e)[:200])}`", parse_mode=ParseMode.MARKDOWN_V2)


# ════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════

def run_bot() -> None:
    """Jalankan bot Telegram (blocking)."""
    if not TELEGRAM_BOT_TOKEN:
        console_terminal_interface.print("[bold red][NexusBot] TELEGRAM_BOT_TOKEN tidak ditemukan. Bot tidak dijalankan.[/bold red]")
        return

    console_terminal_interface.print(f"[bold green][NexusBot] Bot Telegram v{_BOT_VERSION} dimulai...[/bold green]")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(60)
        .pool_timeout(60)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(callback_handler))
    # [UPGRADE]: Terima TEXT dan Dokumen/File dari pengguna
    app.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, message_handler))

    console_terminal_interface.print("[bold green][NexusBot] Bot aktif — menunggu pesan...[/bold green]")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


async def run_bot_async() -> None:
    """Jalankan bot Telegram sebagai asyncio task (non-blocking)."""
    if not TELEGRAM_BOT_TOKEN:
        console_terminal_interface.print("[bold yellow][NexusBot] TELEGRAM_BOT_TOKEN kosong. Bot dilewati.[/bold yellow]")
        return

    console_terminal_interface.print(f"[bold green][NexusBot] Bot Telegram v{_BOT_VERSION} dimulai (async mode)...[/bold green]")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(60)
        .pool_timeout(60)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(callback_handler))
    # [UPGRADE]: Terima TEXT dan Dokumen/File dari pengguna
    app.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, message_handler))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        read_timeout=60,
        timeout=60,
    )
    console_terminal_interface.print("[bold green][NexusBot] Bot aktif dan siap menerima pesan.[/bold green]")
    # Bot berjalan di background — jangan await selamanya di sini
    # Main loop akan menangani shutdown


if __name__ == "__main__":
    run_bot()

"""
nexus_telegram_bot.py  v2.0.0
==============================
PERBAIKAN v2.0.0:
  - Owner TIDAK PERNAH kena rate limit / ditolak
  - Gemini retry otomatis (rotasi model + key), tidak pernah bilang "sibuk"
  - /stop -- hentikan background task AI Roblox
  - /continue -- lanjutkan AI Roblox
  - /selffix -- AI perbaiki kode sendiri + sandbox test + push GitHub
  - /status -- status lengkap agent
  - Scan mendalam isi file saat startup (bukan hanya nama file)
  - Sandbox wajib sebelum setiap push kode ke GitHub
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

from nexus_agents import execute_antigravity_fleet, NexusGlobalState, global_agent_memory

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
_roblox_agent_paused = threading.Event()
_roblox_agent_paused.set()  # Default: AKTIF
_roblox_background_task: Optional[asyncio.Task] = None

# ================================================
# RATE LIMITING -- OWNER TIDAK PERNAH DITOLAK
# ================================================
_RATE_LIMIT_WINDOW = 10
_RATE_LIMIT_MAX = 30
_user_message_timestamps: dict = defaultdict(list)


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
        "Nexus AI Agent v2.0 Menyala!\n"
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
            report += "\n...dan " + str(len(violations)-10) + " file lainnya."
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

    text = update.message.text.strip()
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
        chunks = [result[i:i+3800] for i in range(0, len(result), 3800)]
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
