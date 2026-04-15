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
    """Tangani pesan teks dari pengguna."""
    chat_id = str(update.effective_chat.id)
    text = update.message.text.strip()
    state = _user_state.get(chat_id, {})
    mode = state.get("mode")
    step = state.get("step")

    # ── Universal Code Mode
    if mode == "universal" and step == "waiting_input":
        thinking_msg = await update.message.reply_text(
            "🤖 *AI sedang memproses request kamu\\.\\.\\.*",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        prompt = f"""Kamu adalah senior software developer dengan keahlian multi-bahasa.
Buat kode yang LANGSUNG BISA DIPAKAI untuk request berikut.

REQUEST: {text}

ATURAN:
- Kode harus lengkap dan bisa langsung dijalankan
- Tambahkan komentar singkat yang penting
- Jika ada pilihan bahasa, pilih yang paling sesuai konteks
- Sertakan contoh penggunaan jika diperlukan

OUTPUT:"""
        response = await _call_gemini(prompt)
        await thinking_msg.delete()

        # Potong jika terlalu panjang
        if len(response) > 4000:
            parts = [response[i:i+4000] for i in range(0, len(response), 4000)]
            for i, part in enumerate(parts[:3]):  # Maks 3 bagian
                await update.message.reply_text(
                    f"📝 *Bagian {i+1}/{min(len(parts),3)}:*\n```\n{part}\n```",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
        else:
            await update.message.reply_text(
                f"✅ *Kode siap\\!*\n```\n{response}\n```",
                parse_mode=ParseMode.MARKDOWN_V2,
            )

        # Tawarkan request lagi
        await update.message.reply_text(
            "💬 Mau request kode lain? Ketik saja, atau /start untuk menu utama\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    # ── Roblox AI Mode — Bug atau Feature
    elif mode == "roblox" and step in ("waiting_bug", "waiting_feature"):
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

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    console_terminal_interface.print("[bold green][NexusBot] Bot aktif — menunggu pesan...[/bold green]")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


async def run_bot_async() -> None:
    """Jalankan bot Telegram sebagai asyncio task (non-blocking)."""
    if not TELEGRAM_BOT_TOKEN:
        console_terminal_interface.print("[bold yellow][NexusBot] TELEGRAM_BOT_TOKEN kosong. Bot dilewati.[/bold yellow]")
        return

    console_terminal_interface.print(f"[bold green][NexusBot] Bot Telegram v{_BOT_VERSION} dimulai (async mode)...[/bold green]")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
    console_terminal_interface.print("[bold green][NexusBot] Bot aktif dan siap menerima pesan.[/bold green]")
    # Bot berjalan di background — jangan await selamanya di sini
    # Main loop akan menangani shutdown


if __name__ == "__main__":
    run_bot()
