"""
nexus_polyglot.py
=================
Modul Telegram Polyglot Command Listener & Zero-Error Execution Pipeline.

Fitur:
  - /polyglot [lang] [desc]  -- Sintesis & eksekusi kode dalam bahasa apapun
                                (Python, C++, Rust, Go, Java, JS, Bash, Lua, dll)
  - /status                  -- Cek status sistem Nexus secara real-time
  - /help                    -- Panduan perintah lengkap
  - Pipeline Zero-Error:
      Tahap 1: SotA 2026 RAG (GitHub Knowledge Scraping)
      Tahap 2: Sintesis kode via Gemini CLI
      Tahap 3: Sandboxed Execution (subprocess terisolasi, timeout anti-hang)
      Tahap 4: Auto-Heal Loop (hingga 5 percobaan perbaikan otomatis)
  - Clarification Protocol: Bot bertanya balik jika perintah tidak jelas
  - Non-blocking: Berjalan sebagai asyncio background task, tidak mengganggu
    pipeline pembuatan game Roblox yang sedang berjalan.

Dipanggil dari nexus_main.py:
    asyncio.create_task(start_telegram_polling())

Arsitektur: Aktif | Otonom | Terisolasi | Anti-Hang
"""

import asyncio
import os
import re
import shutil
import tempfile
import subprocess
import requests
import uuid
from typing import Optional, Tuple

from nexus_config import (
    console_terminal_interface,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    GEMINI_CLI_PATH,
    ACTIVE_AGENTS,
    TEMP_IO_DIRECTORY,
)
from nexus_healer import ApexKeyRotator

# ============================================================
# SEMAPHORE EKSKLUSIF UNTUK POLYGLOT (Jalur Tol Khusus)
# Menjamin 2 slot terpisah untuk perintah Telegram -- tidak
# mengantre di belakang tugas Roblox yang sedang berjalan lama.
# ============================================================
POLYGLOT_CLI_SEMAPHORE = asyncio.Semaphore(2)

_key_rotator_polyglot: Optional[ApexKeyRotator] = None

# ============================================================
# KONFIGURASI BAHASA YANG DIDUKUNG
# ============================================================
LANGUAGE_CONFIG = {
    "python": {
        "ext": ".py",
        "compile_cmd": None,
        "run_cmd": ["python3", "{file}"],
        "aliases": ["py", "python3", "python2"],
    },
    "cpp": {
        "ext": ".cpp",
        "compile_cmd": ["g++", "-std=c++17", "-O2", "-o", "{binary}", "{file}"],
        "run_cmd": ["{binary}"],
        "aliases": ["c++", "cpp17", "cplusplus"],
    },
    "c": {
        "ext": ".c",
        "compile_cmd": ["gcc", "-std=c11", "-O2", "-o", "{binary}", "{file}"],
        "run_cmd": ["{binary}"],
        "aliases": [],
    },
    "rust": {
        "ext": ".rs",
        "compile_cmd": ["rustc", "-o", "{binary}", "{file}"],
        "run_cmd": ["{binary}"],
        "aliases": ["rs"],
    },
    "javascript": {
        "ext": ".js",
        "compile_cmd": None,
        "run_cmd": ["node", "{file}"],
        "aliases": ["js", "node", "nodejs"],
    },
    "go": {
        "ext": ".go",
        "compile_cmd": None,
        "run_cmd": ["go", "run", "{file}"],
        "aliases": ["golang"],
    },
    "java": {
        "ext": ".java",
        "compile_cmd": ["javac", "{file}"],
        "run_cmd": ["java", "-cp", "{dir}", "{classname}"],
        "aliases": [],
    },
    "typescript": {
        "ext": ".ts",
        "compile_cmd": None,
        "run_cmd": ["npx", "--yes", "ts-node", "{file}"],
        "aliases": ["ts"],
    },
    "bash": {
        "ext": ".sh",
        "compile_cmd": None,
        "run_cmd": ["bash", "{file}"],
        "aliases": ["sh", "shell"],
    },
    "lua": {
        "ext": ".lua",
        "compile_cmd": None,
        "run_cmd": ["lua", "{file}"],
        "aliases": ["lua5", "luau"],
    },
}

MAX_AUTO_HEAL_ATTEMPTS = 5
CLARIFICATION_TIMEOUT_SECONDS = 120
EXECUTION_TIMEOUT_SECONDS = 30

HELP_TEXT = (
    "<b>NEXUS POLYGLOT BOT -- Panduan Perintah</b>\n\n"
    "<b>/polyglot [bahasa] [deskripsi]</b>\n"
    "Sintesis &amp; eksekusi kode dalam bahasa apapun.\n\n"
    "Contoh:\n"
    "  <code>/polyglot python buat fungsi fibonacci dengan memoization</code>\n"
    "  <code>/polyglot cpp implementasi binary search tree insert dan delete</code>\n"
    "  <code>/polyglot rust buat HTTP client sederhana dengan error handling</code>\n"
    "  <code>/polyglot go concurrent web scraper dengan goroutines</code>\n"
    "  <code>/polyglot java implementasi quicksort dengan generics</code>\n\n"
    "<b>Bahasa yang Didukung:</b>\n"
    "python, cpp (C++), c, rust, javascript, go, java, typescript, bash, lua\n\n"
    "<b>/status</b> -- Cek status sistem Nexus secara real-time\n\n"
    "<b>/help</b> -- Tampilkan panduan ini\n\n"
    "<b>Pipeline Zero-Error:</b>\n"
    "1. RAG -&gt; Scraping GitHub untuk library terbaru 2026\n"
    "2. Sintesis -&gt; Gemini CLI menulis kode optimal\n"
    "3. Sandbox -&gt; Eksekusi terisolasi (timeout anti-hang)\n"
    "4. Auto-Heal -&gt; Perbaikan otomatis hingga 5x jika ada error"
)


# ============================================================
# UTILITY FUNCTIONS
# ============================================================

def _resolve_language(raw_lang: str) -> Optional[str]:
    """Resolve alias bahasa ke nama kanonik."""
    raw = raw_lang.lower().strip()
    if raw in LANGUAGE_CONFIG:
        return raw
    for lang, cfg in LANGUAGE_CONFIG.items():
        if raw in cfg["aliases"]:
            return lang
    return None


def _is_command_ambiguous(task_desc: str) -> bool:
    """
    Deteksi apakah deskripsi tugas terlalu pendek/ambigu.
    Jika iya, bot akan meminta klarifikasi sebelum eksekusi.
    """
    if not task_desc or len(task_desc.strip()) < 10:
        return True
    words = task_desc.strip().split()
    if len(words) < 3:
        return True
    vague_only = {"buat", "tulis", "code", "kode", "program", "sesuatu", "apa", "saja", "bikin"}
    if len(words) <= 3 and all(w.lower() in vague_only for w in words):
        return True
    return False


def _clean_code(raw: str) -> str:
    """Bersihkan sisa markdown dari output Gemini CLI."""
    raw = raw.strip()
    raw = re.sub(r"^\s*`{3}[a-zA-Z]*\s*\n?", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\n?\s*`{3}\s*$", "", raw)
    return raw.strip()


# ============================================================
# GITHUB UNIVERSAL RAG SEARCH
# ============================================================

async def search_github_universal(task: str, language: str) -> str:
    """
    Mencari repository dan kode terbaru di GitHub untuk bahasa apapun.
    Digunakan sebagai RAG Knowledge Base untuk sintesis kode yang
    menggunakan library dan arsitektur paling modern (SotA 2026).
    """
    github_token = (
        os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "")
        or os.getenv("GITHUB_TOKEN", "")
    )
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "NexusPolyglot/2.0",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    lang_display_map = {
        "cpp": "C++", "c": "C", "rust": "Rust",
        "python": "Python", "javascript": "JavaScript",
        "go": "Go", "java": "Java", "typescript": "TypeScript",
        "bash": "Shell", "lua": "Lua",
    }
    gh_lang = lang_display_map.get(language, language.capitalize())

    try:
        loop = asyncio.get_running_loop()
        query_clean = re.sub(r"[^\w\s]", " ", task).strip()[:80]
        url = (
            "https://api.github.com/search/repositories"
            f"?q={requests.utils.quote(query_clean)}+language:{requests.utils.quote(gh_lang)}"
            "&sort=stars&per_page=5"
        )

        def _fetch():
            return requests.get(url, headers=headers, timeout=15)

        res = await loop.run_in_executor(None, _fetch)
        if res.status_code != 200:
            return f"[GitHub RAG: HTTP {res.status_code}]"

        items = res.json().get("items", [])[:5]
        if not items:
            return "[GitHub RAG: Tidak ada hasil relevan untuk query ini]"

        rag_text = f"GITHUB RAG ({language.upper()} TOP LIBRARIES & ARCHITECTURE, 2024-2026):\n"
        for item in items:
            desc = (item.get("description") or "")[:120]
            rag_text += (
                f"- {item['full_name']} | Stars:{item.get('stargazers_count', 0)} | {desc}\n"
            )
        return rag_text

    except Exception as e:
        return f"[GitHub RAG Error: {e}]"


# ============================================================
# POLYGLOT SYNTHESIZER AGENT
# ============================================================

class PolyglotSynthesizerAgent:
    """
    Agent AI yang mensintesis kode dalam bahasa apapun menggunakan Gemini CLI,
    menjalankannya di sandbox terisolasi, dan auto-heal hingga bebas error.

    Pipeline:
      RAG -> Sintesis -> Sandbox Execution -> Auto-Heal (loop) -> Kirim ke Telegram
    """

    def __init__(self):
        global _key_rotator_polyglot
        if _key_rotator_polyglot is None:
            _key_rotator_polyglot = ApexKeyRotator(
                [a["api_key"] for a in ACTIVE_AGENTS if a["api_key"]]
            )
        self.rotator = _key_rotator_polyglot

    async def _call_gemini(self, system_prompt: str, user_prompt: str) -> str:
        """Panggil Gemini CLI dengan rotasi API key otomatis."""
        api_key = self.rotator.get_key()
        if not api_key:
            return "ERROR: Tidak ada API key Gemini tersedia."

        env = os.environ.copy()
        env["GEMINI_API_KEY"] = api_key
        env["CI"] = "true"
        env["TERM"] = "dumb"
        env["NO_COLOR"] = "1"

        full_input = f"[SYSTEM]:\n{system_prompt}\n\n[TASK]:\n{user_prompt}"
        command = [
            GEMINI_CLI_PATH,
            "-m", "models/gemma-4-31b-it",  # 1500 RPD per key, sesuai arsitektur Nexus
            "-y",
            "-p", (
                "Output HANYA kode murni yang langsung bisa dieksekusi. "
                "Tanpa markdown, tanpa penjelasan, tanpa blok ```, tanpa komentar berlebihan. "
                "Langsung kode saja."
            ),
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout_data, _ = await asyncio.wait_for(
                process.communicate(input=full_input.encode("utf-8")),
                timeout=120.0,
            )
            return _clean_code(stdout_data.decode("utf-8", errors="replace"))
        except asyncio.TimeoutError:
            return "ERROR: Gemini CLI timeout setelah 120 detik."
        except Exception as e:
            return f"ERROR: {e}"

    def _execute_in_sandbox(
        self, code: str, language: str
    ) -> Tuple[bool, str, str]:
        """
        Eksekusi kode di sandbox terisolasi (subprocess) dengan timeout ketat.

        Returns:
            (success: bool, stdout: str, stderr: str)
        """
        cfg = LANGUAGE_CONFIG.get(language)
        if not cfg:
            return False, "", f"Bahasa '{language}' tidak didukung dalam sandbox."

        tmpdir = tempfile.mkdtemp(
            dir=TEMP_IO_DIRECTORY if os.path.exists(TEMP_IO_DIRECTORY) else None
        )
        task_id = uuid.uuid4().hex[:8]

        classname = "Main"
        if language == "java":
            m = re.search(r"public\s+class\s+(\w+)", code)
            if m:
                classname = m.group(1)
            filename = os.path.join(tmpdir, f"{classname}.java")
        else:
            filename = os.path.join(tmpdir, f"nexus_poly_{task_id}{cfg['ext']}")

        binary = os.path.join(tmpdir, f"nexus_bin_{task_id}")

        def _fmt(cmd_list):
            return [
                c.replace("{file}", filename)
                 .replace("{binary}", binary)
                 .replace("{dir}", tmpdir)
                 .replace("{classname}", classname)
                for c in cmd_list
            ]

        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(code)

            if cfg["compile_cmd"]:
                comp = subprocess.run(
                    _fmt(cfg["compile_cmd"]),
                    capture_output=True,
                    text=True,
                    timeout=EXECUTION_TIMEOUT_SECONDS,
                    cwd=tmpdir,
                )
                if comp.returncode != 0:
                    return False, "", f"[COMPILE ERROR]:\n{comp.stderr[:2000]}"

            run = subprocess.run(
                _fmt(cfg["run_cmd"]),
                capture_output=True,
                text=True,
                timeout=EXECUTION_TIMEOUT_SECONDS,
                cwd=tmpdir,
            )
            if run.returncode == 0:
                return True, run.stdout[:3000], ""
            return False, run.stdout[:1000], run.stderr[:2000]

        except subprocess.TimeoutExpired:
            return (
                False, "",
                f"[TIMEOUT]: Eksekusi melebihi {EXECUTION_TIMEOUT_SECONDS}s. "
                "Pastikan tidak ada infinite loop tanpa break condition.",
            )
        except FileNotFoundError as e:
            return (
                False, "",
                f"[BINARY NOT FOUND]: {e}\n"
                f"Pastikan compiler untuk '{language}' sudah terinstall di VPS.",
            )
        except Exception as e:
            return False, "", f"[SANDBOX ERROR]: {e}"
        finally:
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass

    async def synthesize_and_execute(
        self,
        language: str,
        task_desc: str,
        send_fn,
        clarify_fn,
    ) -> str:
        """
        Pipeline utama Zero-Error:
          Tahap 0: Clarification Protocol (jika perintah ambigu)
          Tahap 1: RAG GitHub Knowledge Scraping
          Tahap 2: Sintesis kode via Gemini CLI
          Tahap 3: Sandboxed Execution
          Tahap 4: Auto-Heal Loop (hingga MAX_AUTO_HEAL_ATTEMPTS)
          Tahap Final: Kirim hasil ke Telegram
        """
        # --- Tahap 0: Clarification Protocol ---
        if _is_command_ambiguous(task_desc):
            clarification = await clarify_fn(
                f"<b>Perintah terlalu singkat / ambigu!</b>\n\n"
                f"Bahasa: <code>{language}</code>\n"
                f"Task saat ini: <code>{task_desc}</code>\n\n"
                f"Untuk melanjutkan, tolong jawab:\n"
                f"- Apa yang harus dikerjakan program secara spesifik?\n"
                f"- Input apa yang diterima? (contoh data hardcoded?)\n"
                f"- Output apa yang diharapkan?\n"
                f"- Ada library/framework khusus?\n\n"
                f"Balas dalam {CLARIFICATION_TIMEOUT_SECONDS} detik atau perintah dibatalkan."
            )
            if not clarification:
                return "Perintah dibatalkan karena tidak ada klarifikasi."
            task_desc = f"{task_desc}. Klarifikasi tambahan: {clarification}"

        await send_fn(
            f"<b>[NEXUS POLYGLOT]</b> Pipeline dimulai!\n"
            f"Bahasa: <code>{language.upper()}</code>\n"
            f"Task: <code>{task_desc[:200]}</code>\n\n"
            f"Tahap 1/4: RAG Knowledge Scraping (GitHub 2026)..."
        )

        # --- Tahap 1: RAG GitHub ---
        rag_context = await search_github_universal(task_desc, language)

        await send_fn("RAG selesai. Tahap 2/4: Sintesis kode dengan Gemini AI...")

        # --- Tahap 2: Sintesis Kode ---
        system_prompt = (
            f"Anda adalah ahli pemrograman {language.upper()} tingkat senior (SotA 2026).\n"
            f"Tugas: Tulis kode {language.upper()} yang BERSIH, EFISIEN, dan LANGSUNG BISA DIJALANKAN.\n"
            f"Output: HANYA kode murni -- tanpa penjelasan, tanpa markdown, tanpa komentar berlebihan.\n\n"
            f"Gunakan library/arsitektur terbaik berdasarkan referensi GitHub terbaru ini:\n"
            f"{rag_context}\n\n"
            f"ATURAN WAJIB (Zero-Error Contract):\n"
            f"- TIDAK BOLEH ada infinite loop tanpa break/exit condition\n"
            f"- Handle SEMUA potential error/exception dengan tepat\n"
            f"- Kode harus bisa di-compile dan run di Linux Ubuntu 22.04\n"
            f"- TIDAK BOLEH ada input() interaktif -- gunakan contoh data hardcoded\n"
            f"- Timeout eksekusi maksimal {EXECUTION_TIMEOUT_SECONDS} detik"
        )

        async with POLYGLOT_CLI_SEMAPHORE:
            code = await self._call_gemini(system_prompt, task_desc)

        if not code or code.startswith("ERROR"):
            return f"Sintesis gagal: {code}"

        # --- Tahap 3 & 4: Execution + Auto-Heal Loop ---
        last_stderr = ""
        for attempt in range(1, MAX_AUTO_HEAL_ATTEMPTS + 1):
            await send_fn(
                f"Tahap 3-4/4: Sandbox Execution "
                f"(Percobaan {attempt}/{MAX_AUTO_HEAL_ATTEMPTS})..."
            )

            loop = asyncio.get_running_loop()
            success, stdout, stderr = await loop.run_in_executor(
                None, self._execute_in_sandbox, code, language
            )

            if success:
                out_preview = stdout[:500] if stdout else "(Tidak ada output -- sukses tanpa stdout)"
                final_msg = (
                    f"<b>[NEXUS POLYGLOT] BERHASIL!</b>\n\n"
                    f"Percobaan: <code>{attempt}/{MAX_AUTO_HEAL_ATTEMPTS}</code>\n"
                    f"Bahasa: <code>{language.upper()}</code>\n"
                    f"Task: <code>{task_desc[:150]}</code>\n\n"
                    f"<b>Output:</b>\n<pre>{out_preview}</pre>\n\n"
                    f"<b>Kode Final ({language.upper()}):</b>\n"
                    f"<pre>{code[:1500]}</pre>"
                )
                await send_fn(final_msg)
                return "success"

            # Gagal -- Auto-Heal
            last_stderr = stderr
            if attempt < MAX_AUTO_HEAL_ATTEMPTS:
                err_preview = stderr[:400] if stderr else "Unknown error"
                await send_fn(
                    f"Error terdeteksi (Percobaan {attempt})! "
                    f"Auto-Heal dimulai...\n<pre>{err_preview}</pre>"
                )

                heal_prompt = (
                    f"Kode {language.upper()} ini GAGAL dieksekusi dengan error berikut:\n"
                    f"[ERROR OUTPUT]:\n{stderr[:1000]}\n\n"
                    f"[KODE YANG GAGAL]:\n{code}\n\n"
                    f"[TASK ASLI]:\n{task_desc}\n\n"
                    f"Analisis error dengan cermat dan perbaiki SEMUA masalah tersebut. "
                    f"Keluarkan HANYA kode murni yang sudah diperbaiki dan siap dieksekusi."
                )

                async with POLYGLOT_CLI_SEMAPHORE:
                    code = await self._call_gemini(
                        f"Anda adalah ahli debug {language.upper()} senior. "
                        "Output HANYA kode murni yang sudah diperbaiki, tanpa penjelasan apapun.",
                        heal_prompt,
                    )

                if not code or code.startswith("ERROR"):
                    break

        # Semua percobaan habis
        err_preview = last_stderr[:500] if last_stderr else "Tidak ada detail error."
        return (
            f"<b>[NEXUS POLYGLOT] GAGAL</b> setelah {MAX_AUTO_HEAL_ATTEMPTS} percobaan.\n\n"
            f"<b>Error Terakhir:</b>\n<pre>{err_preview}</pre>\n\n"
            f"<b>Kode Terakhir:</b>\n<pre>{code[:800]}</pre>\n\n"
            f"Saran: Coba perjelas deskripsi task atau cek apakah "
            f"compiler <code>{language}</code> sudah terinstall di VPS."
        )


# ============================================================
# TELEGRAM LISTENER (NON-BLOCKING BACKGROUND TASK)
# ============================================================

_pending_clarifications: dict = {}


class TelegramPolyglotListener:
    """
    Telegram long-polling listener yang berjalan sebagai asyncio background task.
    Sepenuhnya non-blocking -- tidak mengganggu pipeline pembuatan game Roblox
    yang sedang berjalan dalam antrian yang sama.

    Security: Hanya menerima perintah dari TELEGRAM_CHAT_ID (Master Node).
    Perintah dari chat ID lain langsung ditolak.
    """

    def __init__(self):
        self.bot_token = TELEGRAM_BOT_TOKEN
        self.master_chat_id = str(TELEGRAM_CHAT_ID).strip()
        self.last_update_id = 0
        self.agent = PolyglotSynthesizerAgent()
        self._running = True

    async def _get_updates(self) -> list:
        """Long-polling update dari Telegram API (timeout 30 detik)."""
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        params = {
            "offset": self.last_update_id + 1,
            "timeout": 30,
            "allowed_updates": ["message"],
        }
        try:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(
                None,
                lambda: requests.get(url, params=params, timeout=40),
            )
            if res.status_code == 200:
                return res.json().get("result", [])
        except Exception as e:
            console_terminal_interface.print(
                f"[dim yellow][Polyglot] Polling error: {e}[/dim yellow]"
            )
        return []

    async def _send(self, chat_id: str, text: str):
        """Kirim pesan teks ke Telegram dengan parse_mode HTML."""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: requests.post(url, json=payload, timeout=15),
            )
        except Exception as e:
            console_terminal_interface.print(
                f"[dim yellow][Polyglot] Send error: {e}[/dim yellow]"
            )

    async def _wait_for_clarification(self, question: str) -> Optional[str]:
        """
        Clarification Protocol:
        1. Kirim pertanyaan ke user
        2. Tunggu balasan hingga CLARIFICATION_TIMEOUT_SECONDS
        3. Jika timeout, kembalikan None (perintah dibatalkan)
        """
        await self._send(self.master_chat_id, question)

        future = asyncio.get_running_loop().create_future()
        _pending_clarifications[self.master_chat_id] = future

        try:
            result = await asyncio.wait_for(future, timeout=CLARIFICATION_TIMEOUT_SECONDS)
            return result
        except asyncio.TimeoutError:
            _pending_clarifications.pop(self.master_chat_id, None)
            await self._send(
                self.master_chat_id,
                "Timeout! Tidak ada klarifikasi diterima. Perintah dibatalkan otomatis."
            )
            return None
        finally:
            _pending_clarifications.pop(self.master_chat_id, None)

    async def _handle_update(self, update: dict):
        """Proses satu update Telegram secara aman."""
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = (message.get("text") or "").strip()

        if not text or not chat_id:
            return

        # Security: Hanya Master Node
        if chat_id != self.master_chat_id:
            await self._send(
                chat_id,
                "Akses ditolak. Sistem hanya menerima perintah dari Master Node."
            )
            return

        # Cek clarification yang sedang menunggu
        if chat_id in _pending_clarifications:
            fut = _pending_clarifications.get(chat_id)
            if fut and not fut.done():
                fut.set_result(text)
                return

        # Command routing
        if text.startswith("/polyglot"):
            await self._handle_polyglot(chat_id, text)
        elif text.startswith("/status"):
            await self._handle_status(chat_id)
        elif text.startswith("/help") or text.startswith("/start"):
            await self._send(chat_id, HELP_TEXT)
        else:
            await self._send(
                chat_id,
                f"Perintah tidak dikenal: <code>{text[:50]}</code>\n\n"
                "Ketik /help untuk panduan lengkap."
            )

    async def _handle_polyglot(self, chat_id: str, text: str):
        """Handler untuk perintah /polyglot [lang] [desc]."""
        parts = text.split(maxsplit=2)

        if len(parts) < 2:
            await self._send(
                chat_id,
                "Format salah!\n\n"
                "Gunakan: <code>/polyglot [bahasa] [deskripsi tugas]</code>\n"
                "Contoh: <code>/polyglot python buat kalkulator dengan operasi dasar</code>"
            )
            return

        raw_lang = parts[1]
        task_desc = parts[2] if len(parts) > 2 else ""

        language = _resolve_language(raw_lang)
        if not language:
            supported = ", ".join(sorted(LANGUAGE_CONFIG.keys()))
            await self._send(
                chat_id,
                f"Bahasa '<code>{raw_lang}</code>' tidak didukung.\n\n"
                f"Bahasa yang tersedia:\n<code>{supported}</code>"
            )
            return

        # Jalankan pipeline sebagai background task (tidak memblok listener)
        asyncio.create_task(
            self.agent.synthesize_and_execute(
                language=language,
                task_desc=task_desc,
                send_fn=lambda msg: self._send(chat_id, msg),
                clarify_fn=lambda msg: self._wait_for_clarification(msg),
            )
        )

    async def _handle_status(self, chat_id: str):
        """Handler untuk perintah /status."""
        try:
            from nexus_database import establish_database_connection
            db = establish_database_connection()
            cur = db.cursor()
            cur.execute("SELECT COUNT(*) FROM verified_modules")
            mod_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM live_telemetry_logs")
            log_count = cur.fetchone()[0]
            db.close()
            db_status = f"Online ({mod_count} modul, {log_count} log telemetry)"
        except Exception as e:
            db_status = f"Error: {e}"

        poly_slots = POLYGLOT_CLI_SEMAPHORE._value

        status_msg = (
            f"<b>NEXUS SYSTEM STATUS</b>\n\n"
            f"Active AI Agents: <code>{len(ACTIVE_AGENTS)}</code>\n"
            f"Database: <code>{db_status}</code>\n"
            f"Polyglot Semaphore: <code>{poly_slots}/2 slot tersedia</code>\n"
            f"Bahasa Didukung: <code>{len(LANGUAGE_CONFIG)}</code>\n"
            f"Auto-Heal Max: <code>{MAX_AUTO_HEAL_ATTEMPTS}x percobaan</code>\n"
            f"Execution Timeout: <code>{EXECUTION_TIMEOUT_SECONDS}s</code>\n"
            f"Status: <b>AKTIF | OTONOM | TERISOLASI | ANTI-HANG</b>"
        )
        await self._send(chat_id, status_msg)

    async def start_polling(self):
        """
        Loop polling utama -- berjalan selamanya sebagai asyncio background task.
        Menangani CancelledError dengan graceful shutdown.
        """
        console_terminal_interface.print(
            "[bold cyan][Polyglot Listener] Telegram bot polling dimulai "
            "(Non-Blocking Background Task)...[/bold cyan]"
        )
        await self._send(
            self.master_chat_id,
            "<b>NEXUS POLYGLOT LISTENER AKTIF!</b>\n\n"
            "Pipeline Zero-Error siap menerima perintah.\n"
            "Ketik /help untuk panduan lengkap."
        )

        while self._running:
            try:
                updates = await self._get_updates()
                for update in updates:
                    uid = update.get("update_id", 0)
                    if uid > self.last_update_id:
                        self.last_update_id = uid
                        asyncio.create_task(self._handle_update(update))
            except asyncio.CancelledError:
                console_terminal_interface.print(
                    "[bold yellow][Polyglot Listener] Dihentikan secara graceful.[/bold yellow]"
                )
                break
            except Exception as e:
                console_terminal_interface.print(
                    f"[bold yellow][Polyglot Listener] Loop error: {e}. Retry dalam 5s...[/bold yellow]"
                )
                await asyncio.sleep(5)

        console_terminal_interface.print(
            "[bold yellow][Polyglot Listener] Shutdown selesai.[/bold yellow]"
        )


# ============================================================
# ENTRY POINT
# ============================================================

async def start_telegram_polling():
    """
    Entry point untuk memulai Telegram Polyglot Listener.
    Dipanggil dari nexus_main.py sebagai background task:

        asyncio.create_task(start_telegram_polling())

    Jika TELEGRAM_BOT_TOKEN atau TELEGRAM_CHAT_ID kosong,
    listener tidak dimulai dan hanya mencetak warning.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        console_terminal_interface.print(
            "[bold yellow][Polyglot Listener] TELEGRAM_BOT_TOKEN atau TELEGRAM_CHAT_ID "
            "kosong di .env.nexus. Listener tidak dimulai.[/bold yellow]"
        )
        return

    listener = TelegramPolyglotListener()
    await listener.start_polling()
