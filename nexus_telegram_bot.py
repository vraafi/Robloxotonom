"""
nexus_telegram_bot.py
=====================
Bot Telegram Interaktif — Antarmuka Manusia ke AI Agent Otonom Nexus.
"""

import os
import re
import json
import asyncio
import subprocess
import time
import textwrap
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

from nexus_config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ACTIVE_AGENTS, GEMINI_CLI_PATH,
    PROJECT_ROOT_DIRECTORY, SOURCE_CODE_DIRECTORY, COMPILED_GAME_FILE, console_terminal_interface
)

from nexus_agents import execute_antigravity_fleet

_BOT_VERSION = "1.0.0"

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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    welcome_text = (
        "Laporan Trading Otomatis:\n"
        "🤖 AI Agent Universal Code\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Ketik request kode kamu.\n\n"
        "Contoh:\n"
        "• python buat script rename file massal\n"
        "• rust implementasi linked list\n"
        "💬 Ketik sekarang:"
    )
    await update.message.reply_text(welcome_text)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text=f"Menu dipilih: {query.data}")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or update.edited_message
    if not message: return

    user_text = message.text or message.caption or ""

    if message.document:
        try:
            new_file = await context.bot.get_file(message.document.file_id)
            file_path = f"temp_nexus_{message.document.file_name}"

            await new_file.download_to_drive(file_path)

            with open(file_path, "r", encoding="utf-8") as f:
                file_content = f.read()

            os.remove(file_path)

            user_text = f"{user_text}\n\n--- 100% ISI FULL FILE ({message.document.file_name}) ---\n{file_content}\n--- AKHIR DARI FILE ---"
        except Exception as e:
            console_terminal_interface.print(f"[bold red]Gagal membaca file dari Telegram: {e}[/bold red]")
            pass

    if not user_text or not user_text.strip(): return

    chat_id = update.effective_chat.id
    if str(chat_id) != str(TELEGRAM_CHAT_ID): return

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action='typing')
    except Exception as e:
        console_terminal_interface.print(f"[bold yellow]⚠️ Peringatan: Gagal mengirim status typing: {e}[/bold yellow]")

    try:
        console_terminal_interface.print(f"[bold cyan][Telegram] Menerima percakapan: {user_text}[/bold cyan]")

        ai_response = ""
        is_success = False

        for current_model in MODEL_FALLBACK_SEQUENCE:
            console_terminal_interface.print(f"[bold magenta]🔄 Mencoba memproses dengan model: {current_model}...[/bold magenta]")
            try:
                ai_response = await execute_antigravity_fleet(user_text, model=current_model)

                if ai_response and "TUGAS" in ai_response and "GAGAL" in ai_response:
                    console_terminal_interface.print(f"[bold yellow]⚠️ Model {current_model} gagal. Beralih...[/bold yellow]")
                elif ai_response:
                    is_success = True
                    console_terminal_interface.print(f"[bold green]✅ Eksekusi berhasil: {current_model}[/bold green]")
                    break
            except Exception as model_err:
                console_terminal_interface.print(f"[bold red]❌ Error CLI model {current_model}: {model_err}. Beralih...[/bold red]")
                continue

        if not is_success:
            ai_response = "❌ Seluruh urutan model dari gemma-4-31b-it hingga gemma-3-1b-it telah diuji dan gagal mengeksekusi permintaan."

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

    except Exception as e:
        error_msg = f"❌ Terjadi kesalahan pada eksekusi AI Agent: {e}"
        await message.reply_text(error_msg)
        console_terminal_interface.print(f"[bold red]{error_msg}[/bold red]")


async def run_bot_async() -> None:
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
    app.add_handler(CallbackQueryHandler(callback_handler))

    app.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, message_handler))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, read_timeout=60, timeout=60)
    console_terminal_interface.print("[bold green][NexusBot] Bot aktif dan siap menerima pesan.[/bold green]")


def run_bot() -> None:
    """Jalankan bot Telegram (blocking, untuk testing langsung)."""
    if not TELEGRAM_BOT_TOKEN:
        console_terminal_interface.print("[bold red][NexusBot] TELEGRAM_BOT_TOKEN tidak ditemukan.[/bold red]")
        return
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, message_handler))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    run_bot()
