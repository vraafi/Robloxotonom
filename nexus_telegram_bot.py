"""
nexus_telegram_bot.py
=====================
Bot Telegram Interaktif — Antarmuka Manusia ke AI Agent Otonom Nexus.

Versi: 1.1.0
Perubahan:
- Perbaikan: Penanganan error yang lebih robust pada pembacaan file
- Perbaikan: Chunking pesan yang lebih cerdas (tidak memotong di tengah kalimat)
- Fitur Baru: Perintah /clear untuk menghapus riwayat percakapan
- Fitur Baru: Perintah /status untuk melihat riwayat percakapan saat ini
- Fitur Baru: Perintah /help untuk melihat daftar perintah
- Fitur Baru: Rate limiting sederhana untuk mencegah spam
- Perbaikan: Validasi TELEGRAM_CHAT_ID yang lebih baik
- Perbaikan: Menangani file encoding selain UTF-8
"""

import os
import re
import json
import asyncio
import time
from typing import Optional
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TimedOut

from nexus_config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ACTIVE_AGENTS, GEMINI_CLI_PATH,
    PROJECT_ROOT_DIRECTORY, SOURCE_CODE_DIRECTORY, COMPILED_GAME_FILE, console_terminal_interface
)

# Mengimpor Orchestrator dan SAKELAR OVERRIDE dari nexus_agents.py
from nexus_agents import execute_antigravity_fleet, NexusGlobalState, global_agent_memory, get_memory_summary

_BOT_VERSION = "1.1.0"

# [FITUR BARU]: Urutan fallback model dari yang paling kuat ke paling ringan
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

# [FITUR BARU]: Rate limiting — maksimum 3 pesan per 10 detik per chat
_RATE_LIMIT_WINDOW = 10  # detik
_RATE_LIMIT_MAX = 3
_user_message_timestamps: dict = defaultdict(list)


def _check_rate_limit(chat_id: int) -> bool:
    """Mengembalikan True jika user melewati batas rate limit."""
    now = time.time()
    timestamps = _user_message_timestamps[chat_id]
    # Bersihkan timestamp yang sudah lebih dari window
    _user_message_timestamps[chat_id] = [t for t in timestamps if now - t < _RATE_LIMIT_WINDOW]
    if len(_user_message_timestamps[chat_id]) >= _RATE_LIMIT_MAX:
        return True
    _user_message_timestamps[chat_id].append(now)
    return False


async def _safe_reply(message: Message, text: str) -> None:
    """
    Kirim pesan dengan penanganan error RetryAfter dan BadRequest.
    [PERBAIKAN]: Tidak menggunakan parse_mode untuk menghindari error Markdown.
    """
    try:
        await message.reply_text(text)
    except RetryAfter as e:
        console_terminal_interface.print(f"[bold yellow]Rate limit Telegram, menunggu {e.retry_after} detik...[/bold yellow]")
        await asyncio.sleep(e.retry_after + 1)
        await message.reply_text(text)
    except BadRequest as e:
        console_terminal_interface.print(f"[bold red]BadRequest saat kirim pesan: {e}. Mencoba tanpa formatting.[/bold red]")
        # Coba kirim teks mentah tanpa Markdown
        clean_text = re.sub(r"[*_`\[\]()]", "", text)
        await message.reply_text(clean_text)
    except TimedOut:
        console_terminal_interface.print("[bold yellow]Timeout saat mengirim pesan, mencoba ulang...[/bold yellow]")
        await asyncio.sleep(3)
        await message.reply_text(text)


async def _send_long_message(message: Message, text: str) -> None:
    """
    Membagi dan mengirim pesan panjang dengan mempertahankan integritas code block.
    [PERBAIKAN]: Pemisahan lebih cerdas — tidak memotong di tengah kalimat atau code block.
    """
    MAX_CHUNK = 4000
    code_marker = "```"

    if len(text) <= MAX_CHUNK:
        await _safe_reply(message, text)
        return

    chunks = []
    remaining = text
    open_code_block = False

    while len(remaining) > MAX_CHUNK:
        # Temukan titik potong yang aman
        split_index = remaining.rfind("\n", 0, MAX_CHUNK)
        if split_index == -1:
            split_index = MAX_CHUNK

        chunk = remaining[:split_index]

        # Hitung apakah code block terbuka di chunk ini
        block_count = chunk.count(code_marker)
        if block_count % 2 != 0:
            # Code block terbuka — tutup di akhir chunk dan buka di awal chunk berikutnya
            chunk += f"\n{code_marker}"
            chunks.append(chunk)
            remaining = f"{code_marker}\n" + remaining[split_index:].lstrip()
        else:
            chunks.append(chunk)
            remaining = remaining[split_index:].lstrip()

    if remaining:
        chunks.append(remaining)

    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(1.5)
        await _safe_reply(message, chunk)


# =========================================================================
# Handler Perintah
# =========================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Perintah /start dan /menu."""
    welcome_text = (
        f"🤖 Nexus AI Agent v{_BOT_VERSION}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "AI Agent Universal Code untuk Roblox & lebih.\n\n"
        "💬 Ketikkan permintaan kode kamu sekarang.\n"
        "📎 Bisa juga kirim file kode untuk dianalisis.\n\n"
        "Ketik /help untuk daftar perintah."
    )
    await update.message.reply_text(welcome_text)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """[FITUR BARU]: Perintah /help — tampilkan semua perintah yang tersedia."""
    help_text = (
        f"📖 Nexus AI Agent v{_BOT_VERSION} — Daftar Perintah\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "/start — Mulai & tampilkan pesan sambutan\n"
        "/help — Tampilkan daftar perintah ini\n"
        "/status — Lihat riwayat percakapan saat ini\n"
        "/clear — Hapus riwayat percakapan (mulai sesi baru)\n"
        "/menu — Sama dengan /start\n\n"
        "💡 Cara pakai:\n"
        "- Ketik pertanyaan atau permintaan kode langsung\n"
        "- Kirim file .lua, .py, .txt untuk dianalisis AI\n"
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
    """[FITUR BARU]: Perintah /status — tampilkan status dan riwayat percakapan."""
    chat_id = update.effective_chat.id
    if str(chat_id) != str(TELEGRAM_CHAT_ID):
        return

    override_status = "🔴 Aktif" if NexusGlobalState.TELEGRAM_OVERRIDE_ACTIVE else "🟢 Tidak aktif"
    history_count = len(global_agent_memory.history)

    status_text = (
        f"📊 Status Nexus AI Agent v{_BOT_VERSION}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Override Telegram: {override_status}\n"
        f"Riwayat tersimpan: {history_count} pesan\n\n"
    )

    if history_count > 0:
        context_str = global_agent_memory.get_context_string()
        # Tampilkan hanya 500 karakter terakhir agar tidak terlalu panjang
        if len(context_str) > 500:
            context_str = "...\n" + context_str[-500:]
        status_text += f"Riwayat terbaru:\n{context_str}"

    await _send_long_message(update.message, status_text)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler untuk InlineKeyboard callback."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text=f"Menu dipilih: {query.data}")


# =========================================================================
# Handler Pesan Utama
# =========================================================================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler untuk semua pesan teks dan file dari Telegram.
    [PERBAIKAN]: Penanganan error yang lebih robust, rate limiting, dan validasi chat ID.
    """
    message = update.message or update.edited_message
    if not message:
        return

    chat_id = update.effective_chat.id

    # [PERBAIKAN]: Validasi chat ID lebih awal untuk hemat resource
    if str(chat_id) != str(TELEGRAM_CHAT_ID):
        return

    # [FITUR BARU]: Cek rate limit
    if _check_rate_limit(chat_id):
        try:
            await message.reply_text(
                "⏳ Terlalu banyak permintaan. Tunggu beberapa detik sebelum mengirim lagi."
            )
        except Exception:
            pass
        return

    user_text = message.text or message.caption or ""

    # [PERBAIKAN]: Pembacaan file dengan encoding fallback
    if message.document:
        file_name = message.document.file_name or "unknown_file"
        file_path = f"temp_nexus_{file_name}"
        try:
            new_file = await context.bot.get_file(message.document.file_id)
            await new_file.download_to_drive(file_path)

            # Coba baca dengan berbagai encoding
            file_content = None
            for encoding in ["utf-8", "utf-8-sig", "latin-1", "cp1252"]:
                try:
                    with open(file_path, "r", encoding=encoding) as f:
                        file_content = f.read()
                    break
                except UnicodeDecodeError:
                    continue

            if file_content is None:
                # Fallback: baca sebagai binary dan decode dengan ignore
                with open(file_path, "rb") as f:
                    file_content = f.read().decode("utf-8", errors="ignore")

            user_text = (
                f"{user_text}\n\n"
                f"--- 100% ISI FULL FILE ({file_name}) ---\n"
                f"{file_content}\n"
                f"--- AKHIR DARI FILE ---"
            )
            console_terminal_interface.print(f"[bold green]✅ File '{file_name}' berhasil dibaca ({len(file_content)} karakter).[/bold green]")

        except Exception as e:
            console_terminal_interface.print(f"[bold red]❌ Gagal membaca file '{file_name}': {e}[/bold red]")
            await message.reply_text(f"❌ Gagal membaca file '{file_name}': {e}")
            return
        finally:
            # [PERBAIKAN]: Pastikan file temp selalu dihapus meski ada error
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass

    if not user_text or not user_text.strip():
        return

    # Kirim status "mengetik"
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception as e:
        console_terminal_interface.print(f"[bold yellow]⚠️ Gagal mengirim status typing: {e}[/bold yellow]")

    # =========================================================================
    # [PRIORITAS MUTLAK TELEGRAM] - Menghentikan Agen Latar Belakang
    # =========================================================================
    NexusGlobalState.TELEGRAM_OVERRIDE_ACTIVE = True
    console_terminal_interface.print("[bold yellow]🛑 MENGHENTIKAN AGEN OTONOM SEMENTARA - PRIORITAS TELEGRAM BERJALAN![/bold yellow]")

    try:
        console_terminal_interface.print(f"[bold cyan][Telegram] Menerima permintaan: {user_text[:80]}...[/bold cyan]")

        ai_response = ""
        is_success = False
        tried_models = []

        for current_model in MODEL_FALLBACK_SEQUENCE:
            console_terminal_interface.print(f"[bold magenta]🔄 Mencoba model: {current_model}...[/bold magenta]")
            tried_models.append(current_model)
            try:
                ai_response = await execute_antigravity_fleet(user_text, model=current_model)

                # [PERBAIKAN]: Deteksi kegagalan lebih akurat
                if not ai_response:
                    console_terminal_interface.print(f"[bold yellow]⚠️ Model {current_model} menghasilkan respons kosong. Beralih...[/bold yellow]")
                    continue
                elif "TUGAS" in ai_response and "GAGAL" in ai_response and len(ai_response) < 200:
                    # Kemungkinan besar seluruh tugas gagal
                    console_terminal_interface.print(f"[bold yellow]⚠️ Model {current_model} gagal semua tugas. Beralih...[/bold yellow]")
                    continue
                else:
                    is_success = True
                    console_terminal_interface.print(f"[bold green]✅ Eksekusi berhasil dengan model: {current_model}[/bold green]")
                    break

            except Exception as model_err:
                console_terminal_interface.print(f"[bold red]❌ Exception pada model {current_model}: {model_err}. Beralih...[/bold red]")
                continue

        if not is_success:
            models_tried_str = ", ".join(tried_models[:3]) + f"... ({len(tried_models)} total)"
            ai_response = (
                f"❌ Semua model AI telah dicoba dan gagal.\n"
                f"Model yang dicoba: {models_tried_str}\n\n"
                f"Kemungkinan penyebab:\n"
                f"- Gemini CLI tidak terkonfigurasi dengan benar\n"
                f"- Koneksi internet bermasalah\n"
                f"- Permintaan terlalu kompleks untuk model yang tersedia"
            )

        await _send_long_message(message, ai_response)

    except Exception as e:
        error_msg = f"❌ Terjadi kesalahan tak terduga: {e}"
        console_terminal_interface.print(f"[bold red]{error_msg}[/bold red]")
        try:
            await message.reply_text(error_msg)
        except Exception:
            pass
    finally:
        # =========================================================================
        # [MENGEMBALIKAN KONTROL] - Mengizinkan Agen Latar Belakang bekerja lagi
        # =========================================================================
        NexusGlobalState.TELEGRAM_OVERRIDE_ACTIVE = False
        console_terminal_interface.print("[bold green]▶️ AGEN OTONOM DIIZINKAN MELANJUTKAN PEMBARUAN FILE.[/bold green]")


# =========================================================================
# Fungsi Utama Bot
# =========================================================================

async def run_bot_async() -> None:
    """Menjalankan bot Telegram secara asinkron."""
    if not TELEGRAM_BOT_TOKEN:
        console_terminal_interface.print("[bold red]FATAL: TELEGRAM_BOT_TOKEN tidak diset! Bot tidak dapat dijalankan.[/bold red]")
        return

    if not TELEGRAM_CHAT_ID:
        console_terminal_interface.print("[bold yellow]⚠️ PERINGATAN: TELEGRAM_CHAT_ID tidak diset. Bot akan menerima pesan dari semua chat![/bold yellow]")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .pool_timeout(60)
        .build()
    )

    # Daftarkan semua command handler
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Handler untuk pesan teks dan file dokumen
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.Document.ALL) & ~filters.COMMAND,
            message_handler
        )
    )

    console_terminal_interface.print(f"[bold green]🚀 Nexus Telegram Bot v{_BOT_VERSION} dimulai...[/bold green]")

    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        read_timeout=60,
        timeout=60
    )

    console_terminal_interface.print("[bold green]✅ Bot berjalan dan mendengarkan pesan.[/bold green]")
