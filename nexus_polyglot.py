"""
nexus_polyglot.py  (v3 — Autonomous Installer + Persistent Sandbox + No-Timeout Retry)
========================================================================================
Modul Telegram Polyglot Command Listener & Zero-Error Execution Pipeline.

FITUR BARU v3:
  - AUTO-INSTALL: Jika compiler/binary tidak ditemukan, sistem otomatis menginstall
    tanpa perlu bertanya ke pengguna (apt, pip3, cargo, npm, rustup).
  - PERSISTENT SANDBOX: Setiap task punya direktori sandbox sendiri yang TIDAK dihapus
    otomatis. Sandbox hanya dihapus ketika:
      - Pengguna mengirim /clearcache
      - Sistem benar-benar selesai total
  - NO-TIMEOUT RETRY: Setelah 5x Auto-Heal gagal, bot meminta instruksi tambahan
    dari pengguna TANPA timeout — menunggu selamanya sampai pengguna balas.
  - SANDBOX BARU: Dibuat fresh setiap kali pengguna membuat request /polyglot baru.

Perintah Telegram:
  /polyglot [lang] [desc]  — Sintesis & eksekusi kode (multi-bahasa)
  /status                  — Status sistem real-time
  /clearcache              — Hapus semua sandbox & cache (sandbox baru akan dibuat)
  /help                    — Panduan lengkap

Arsitektur: Aktif | Otonom | Terisolasi | Anti-Hang | Self-Installing
"""

import asyncio
import os
import re
import shutil
import tempfile
import subprocess
import requests
import uuid
from typing import Optional, Tuple, Dict

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
# 2 slot terpisah — tidak mengantre di belakang tugas Roblox.
# ============================================================
POLYGLOT_CLI_SEMAPHORE = asyncio.Semaphore(2)

_key_rotator_polyglot: Optional[ApexKeyRotator] = None

# ============================================================
# PERSISTENT SANDBOX STATE
# Sandbox TIDAK dihapus otomatis. Hanya dihapus saat /clearcache.
# ============================================================
_active_sandboxes: Dict[str, str] = {}   # task_id -> tmpdir path
_installed_compilers: set = set()        # bahasa yang sudah auto-installed
_sandbox_lock = asyncio.Lock()

POLYGLOT_SANDBOX_ROOT = os.path.join(TEMP_IO_DIRECTORY, "polyglot_sandboxes")

# ============================================================
# KONFIGURASI BAHASA & AUTO-INSTALL
# ============================================================
LANGUAGE_CONFIG = {
    "python": {
        "ext": ".py",
        "compile_cmd": None,
        "run_cmd": ["python3", "{file}"],
        "aliases": ["py", "python3", "python2"],
        "check_bin": "python3",
        "auto_install": None,  # sudah pasti ada di Ubuntu
    },
    "cpp": {
        "ext": ".cpp",
        "compile_cmd": ["g++", "-std=c++17", "-O2", "-o", "{binary}", "{file}"],
        "run_cmd": ["{binary}"],
        "aliases": ["c++", "cpp17", "cplusplus"],
        "check_bin": "g++",
        "auto_install": "sudo apt-get install -y build-essential g++",
    },
    "c": {
        "ext": ".c",
        "compile_cmd": ["gcc", "-std=c11", "-O2", "-o", "{binary}", "{file}"],
        "run_cmd": ["{binary}"],
        "aliases": [],
        "check_bin": "gcc",
        "auto_install": "sudo apt-get install -y build-essential gcc",
    },
    "rust": {
        "ext": ".rs",
        "compile_cmd": ["rustc", "-o", "{binary}", "{file}"],
        "run_cmd": ["{binary}"],
        "aliases": ["rs"],
        "check_bin": "rustc",
        "auto_install": (
            "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && "
            "source $HOME/.cargo/env"
        ),
    },
    "javascript": {
        "ext": ".js",
        "compile_cmd": None,
        "run_cmd": ["node", "{file}"],
        "aliases": ["js", "node", "nodejs"],
        "check_bin": "node",
        "auto_install": "curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs",
    },
    "go": {
        "ext": ".go",
        "compile_cmd": None,
        "run_cmd": ["go", "run", "{file}"],
        "aliases": ["golang"],
        "check_bin": "go",
        "auto_install": "sudo apt-get install -y golang-go",
    },
    "java": {
        "ext": ".java",
        "compile_cmd": ["javac", "{file}"],
        "run_cmd": ["java", "-cp", "{dir}", "{classname}"],
        "aliases": [],
        "check_bin": "javac",
        "auto_install": "sudo apt-get install -y openjdk-21-jdk",
    },
    "typescript": {
        "ext": ".ts",
        "compile_cmd": None,
        "run_cmd": ["npx", "--yes", "ts-node", "{file}"],
        "aliases": ["ts"],
        "check_bin": "node",
        "auto_install": "npm install -g typescript ts-node",
    },
    "bash": {
        "ext": ".sh",
        "compile_cmd": None,
        "run_cmd": ["bash", "{file}"],
        "aliases": ["sh", "shell"],
        "check_bin": "bash",
        "auto_install": None,  # bash pasti ada
    },
    "lua": {
        "ext": ".lua",
        "compile_cmd": None,
        "run_cmd": ["lua5.4", "{file}"],
        "aliases": ["lua5", "luau"],
        "check_bin": "lua5.4",
        "auto_install": "sudo apt-get install -y lua5.4",
    },
}

MAX_AUTO_HEAL_ATTEMPTS = 5
EXECUTION_TIMEOUT_SECONDS = 30

HELP_TEXT = (
    "<b>NEXUS POLYGLOT BOT v3 -- Panduan Perintah</b>\n\n"
    "<b>/polyglot [bahasa] [deskripsi]</b>\n"
    "Sintesis &amp; eksekusi kode dalam bahasa apapun.\n\n"
    "Contoh:\n"
    "  <code>/polyglot python buat fungsi fibonacci dengan memoization</code>\n"
    "  <code>/polyglot cpp implementasi binary search tree</code>\n"
    "  <code>/polyglot rust HTTP client dengan error handling</code>\n"
    "  <code>/polyglot go concurrent web scraper dengan goroutines</code>\n"
    "  <code>/polyglot java quicksort dengan generics</code>\n\n"
    "<b>Bahasa Didukung:</b>\n"
    "python, cpp, c, rust, javascript, go, java, typescript, bash, lua\n\n"
    "<b>/status</b> -- Status sistem &amp; sandbox aktif\n\n"
    "<b>/clearcache</b> -- Hapus semua sandbox &amp; cache. Sandbox baru akan\n"
    "  dibuat otomatis saat request /polyglot berikutnya.\n\n"
    "<b>/help</b> -- Tampilkan panduan ini\n\n"
    "<b>Fitur Otomatis:</b>\n"
    "- Auto-install compiler jika belum ada (tanpa perlu tanya)\n"
    "- Sandbox PERSISTEN per task (tidak dihapus otomatis)\n"
    "- Setelah 5x gagal: meminta instruksi tambahan (tanpa batas waktu)\n"
    "- Kirim /clearcache untuk reset semua sandbox\n\n"
    "<b>Tips:</b> Tidak perlu tulis /polyglot!\n"
    "Cukup kirim deskripsi tugasmu dan sistem akan mendeteksi bahasanya otomatis."
)


# ============================================================
# UTILITY FUNCTIONS
# ============================================================

def _detect_language_from_text(text: str) -> Optional[str]:
    """
    Deteksi bahasa pemrograman dari kalimat natural language.
    Dipanggil saat perintah tidak sesuai format /polyglot.
    """
    t = text.lower()
    signals = [
        ("python",     ["python", " py ", "django", "flask", "pandas", "numpy", "pip install"]),
        ("javascript", ["javascript", " js ", "nodejs", "node.js", "npm ", "react", "vue"]),
        ("typescript", ["typescript", " ts ", " tsx"]),
        ("cpp",        ["c++", " cpp", "g++", "cplusplus"]),
        ("c",          [" bahasa c ", " kode c ", " gcc ", " in c "]),
        ("rust",       ["rust", "cargo", "rustc"]),
        ("go",         ["golang", "goroutine", " go "]),
        ("java",       [" java ", "jvm", "maven", "spring boot"]),
        ("lua",        ["lua", "luau", "roblox script", "roblox lua"]),
        ("bash",       ["bash", "shell script", " sh ", "linux command", "terminal"]),
    ]
    for lang, kws in signals:
        for kw in kws:
            if kw in t:
                return lang
    return None


def _resolve_language(raw_lang: str) -> Optional[str]:
    raw = raw_lang.lower().strip()
    if raw in LANGUAGE_CONFIG:
        return raw
    for lang, cfg in LANGUAGE_CONFIG.items():
        if raw in cfg["aliases"]:
            return lang
    return None


def _is_command_ambiguous(task_desc: str) -> bool:
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
    raw = raw.strip()
    raw = re.sub(r"^\s*`{3}[a-zA-Z]*\s*\n?", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\n?\s*`{3}\s*$", "", raw)
    return raw.strip()


def _get_sandbox_dir(task_id: str) -> str:
    """Ambil atau buat sandbox directory persisten untuk task ini."""
    sandbox = _active_sandboxes.get(task_id)
    if not sandbox or not os.path.exists(sandbox):
        os.makedirs(POLYGLOT_SANDBOX_ROOT, exist_ok=True)
        sandbox = os.path.join(POLYGLOT_SANDBOX_ROOT, f"task_{task_id}")
        os.makedirs(sandbox, exist_ok=True)
        _active_sandboxes[task_id] = sandbox
    return sandbox


# ============================================================
# AUTO-INSTALLER (Tanpa Tanya Pengguna)
# ============================================================

async def _auto_install_compiler(language: str, send_fn) -> bool:
    """
    Install compiler/runtime yang hilang secara otomatis menggunakan
    apt-get / pip3 / npm / rustup tanpa meminta persetujuan pengguna.

    Returns True jika install berhasil, False jika gagal.
    """
    if language in _installed_compilers:
        return True

    cfg = LANGUAGE_CONFIG.get(language, {})
    install_cmd = cfg.get("auto_install")

    if not install_cmd:
        return True  # tidak perlu install (bash, python sudah ada)

    await send_fn(
        f"<b>Auto-Install Dimulai</b>\n"
        f"Compiler <code>{language}</code> tidak ditemukan.\n"
        f"Menginstall otomatis...\n"
        f"<code>{install_cmd[:120]}</code>"
    )

    console_terminal_interface.print(
        f"[bold yellow][Polyglot Auto-Install] {language}: {install_cmd}[/bold yellow]"
    )

    try:
        loop = asyncio.get_running_loop()

        # Untuk Rust (rustup) perlu env PATH tambahan setelah install
        env = os.environ.copy()
        cargo_bin = os.path.expanduser("~/.cargo/bin")
        if cargo_bin not in env.get("PATH", ""):
            env["PATH"] = cargo_bin + ":" + env.get("PATH", "")
        env["DEBIAN_FRONTEND"] = "noninteractive"

        def _run_install():
            result = subprocess.run(
                install_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=300,  # 5 menit max untuk install
                env=env,
            )
            return result

        result = await loop.run_in_executor(None, _run_install)

        if result.returncode == 0:
            _installed_compilers.add(language)
            await send_fn(
                f"<b>Auto-Install Berhasil!</b>\n"
                f"Compiler <code>{language}</code> sudah terpasang.\n"
                f"Melanjutkan eksekusi kode..."
            )
            return True
        else:
            err = result.stderr[:400] if result.stderr else "Unknown error"
            await send_fn(
                f"<b>Auto-Install Gagal</b>\n"
                f"Compiler <code>{language}</code> tidak bisa diinstall otomatis.\n"
                f"Error:\n<pre>{err}</pre>\n\n"
                f"Install manual di VPS:\n<code>{install_cmd}</code>"
            )
            return False

    except subprocess.TimeoutExpired:
        await send_fn(
            f"<b>Auto-Install Timeout</b> (5 menit).\n"
            f"Install manual: <code>{install_cmd}</code>"
        )
        return False
    except Exception as e:
        await send_fn(f"<b>Auto-Install Error:</b> <code>{e}</code>")
        return False


# ============================================================
# GITHUB UNIVERSAL RAG SEARCH
# ============================================================

async def search_github_universal(task: str, language: str) -> str:
    """
    Mencari repository terbaru di GitHub untuk bahasa apapun.
    RAG Knowledge Base — library & arsitektur SotA 2026.
    """
    github_token = (
        os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "")
        or os.getenv("GITHUB_TOKEN", "")
    )
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "NexusPolyglot/3.0",
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
            return "[GitHub RAG: Tidak ada hasil relevan]"

        rag_text = f"GITHUB RAG ({language.upper()} TOP LIBRARIES & ARCHITECTURE, 2024-2026):\n"
        for item in items:
            desc = (item.get("description") or "")[:120]
            rag_text += f"- {item['full_name']} | Stars:{item.get('stargazers_count', 0)} | {desc}\n"
        return rag_text

    except Exception as e:
        return f"[GitHub RAG Error: {e}]"


# ============================================================
# POLYGLOT SYNTHESIZER AGENT
# ============================================================

class PolyglotSynthesizerAgent:
    """
    Agent AI yang mensintesis kode dalam bahasa apapun, mengeksekusi di
    sandbox persisten, auto-install compiler yang hilang, dan meminta
    instruksi tambahan dari pengguna jika semua percobaan gagal.

    Pipeline v3:
      RAG -> Sintesis -> Auto-Install (jika perlu) -> Sandbox Execution
      -> Auto-Heal Loop -> [Jika tetap gagal] Minta Instruksi Tambahan
      -> Coba Lagi (tanpa batas waktu tunggu user)
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

        # Tambahkan cargo bin ke PATH untuk Rust
        cargo_bin = os.path.expanduser("~/.cargo/bin")
        if cargo_bin not in env.get("PATH", ""):
            env["PATH"] = cargo_bin + ":" + env.get("PATH", "")

        full_input = f"[SYSTEM]:\n{system_prompt}\n\n[TASK]:\n{user_prompt}"
        command = [
            GEMINI_CLI_PATH,
            "-m", "models/gemini-2.0-flash",  # Model TERPISAH dari Roblox agent (gemma-4-31b-it)
            "-y",
            "-p", (
                "Output HANYA kode murni yang langsung bisa dieksekusi. "
                "Tanpa markdown, tanpa penjelasan, tanpa blok ```, tanpa komentar berlebihan."
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
        self, code: str, language: str, task_id: str
    ) -> Tuple[bool, str, str, bool]:
        """
        Eksekusi kode di sandbox PERSISTEN (tidak dihapus otomatis).
        Sandbox untuk task ini ada di POLYGLOT_SANDBOX_ROOT/task_{task_id}/

        Returns:
            (success: bool, stdout: str, stderr: str, binary_missing: bool)
            binary_missing=True berarti perlu auto-install
        """
        cfg = LANGUAGE_CONFIG.get(language)
        if not cfg:
            return False, "", f"Bahasa '{language}' tidak didukung.", False

        # Gunakan sandbox persisten (tidak dibuat ulang setiap percobaan)
        tmpdir = _get_sandbox_dir(task_id)
        filename_base = f"nexus_{language}_{task_id[:8]}"

        classname = "Main"
        if language == "java":
            m = re.search(r"public\s+class\s+(\w+)", code)
            if m:
                classname = m.group(1)
            filename = os.path.join(tmpdir, f"{classname}.java")
        else:
            filename = os.path.join(tmpdir, f"{filename_base}{cfg['ext']}")

        binary = os.path.join(tmpdir, f"{filename_base}_bin")

        # Update PATH untuk Rust
        env = os.environ.copy()
        cargo_bin = os.path.expanduser("~/.cargo/bin")
        if cargo_bin not in env.get("PATH", ""):
            env["PATH"] = cargo_bin + ":" + env.get("PATH", "")

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
                    env=env,
                )
                if comp.returncode != 0:
                    return False, "", f"[COMPILE ERROR]:\n{comp.stderr[:2000]}", False

            run = subprocess.run(
                _fmt(cfg["run_cmd"]),
                capture_output=True,
                text=True,
                timeout=EXECUTION_TIMEOUT_SECONDS,
                cwd=tmpdir,
                env=env,
            )
            if run.returncode == 0:
                return True, run.stdout[:3000], "", False
            return False, run.stdout[:1000], run.stderr[:2000], False

        except subprocess.TimeoutExpired:
            return (
                False, "",
                f"[TIMEOUT]: Eksekusi melebihi {EXECUTION_TIMEOUT_SECONDS}s. "
                "Tidak ada infinite loop tanpa break condition.",
                False,
            )
        except FileNotFoundError as e:
            # Binary tidak ditemukan — perlu auto-install
            return False, "", f"[BINARY NOT FOUND]: {e}", True
        except Exception as e:
            return False, "", f"[SANDBOX ERROR]: {e}", False

    async def synthesize_and_execute(
        self,
        language: str,
        task_desc: str,
        task_id: str,
        send_fn,
        ask_instructions_fn,
        clarify_fn,
    ) -> str:
        """
        Pipeline utama v3:
          Tahap 0: Clarification (jika perintah ambigu)
          Tahap 1: RAG GitHub Knowledge Scraping
          Tahap 2: Sintesis kode via Gemini CLI
          Tahap 3: Auto-Install jika compiler hilang
          Tahap 4: Sandboxed Execution (sandbox persisten)
          Tahap 5: Auto-Heal Loop (5x)
          Tahap 6: [Jika semua gagal] Minta instruksi tambahan TANPA timeout
          Tahap 7: Coba ulang dari sintesis dengan konteks baru
        """
        # --- Tahap 0: Clarification Protocol ---
        if _is_command_ambiguous(task_desc):
            clarification = await clarify_fn(
                f"<b>Perintah terlalu singkat / ambigu!</b>\n\n"
                f"Bahasa: <code>{language}</code>\n"
                f"Task: <code>{task_desc}</code>\n\n"
                f"Tolong jawab:\n"
                f"- Apa yang harus dikerjakan program secara spesifik?\n"
                f"- Input/output apa yang diharapkan?\n"
                f"- Ada library/framework khusus?\n\n"
                f"(Tidak ada batas waktu — balas kapanpun)"
            )
            if not clarification:
                return "Perintah dibatalkan."
            task_desc = f"{task_desc}. Klarifikasi: {clarification}"

        await send_fn(
            f"<b>[NEXUS POLYGLOT v3]</b> Pipeline dimulai!\n"
            f"Bahasa: <code>{language.upper()}</code>\n"
            f"Task: <code>{task_desc[:200]}</code>\n"
            f"Sandbox ID: <code>{task_id[:8]}</code>\n\n"
            f"Tahap 1/4: RAG GitHub Scraping..."
        )

        # --- Tahap 1: RAG ---
        rag_context = await search_github_universal(task_desc, language)
        await send_fn("RAG selesai. Tahap 2/4: Sintesis kode dengan Gemini AI...")

        # --- Tahap 2: Sintesis ---
        system_prompt = (
            f"Anda adalah ahli {language.upper()} senior (SotA 2026).\n"
            f"Tulis kode {language.upper()} BERSIH, EFISIEN, LANGSUNG BISA DIJALANKAN.\n"
            f"Output: HANYA kode murni.\n\n"
            f"Referensi GitHub terbaru:\n{rag_context}\n\n"
            f"ZERO-ERROR CONTRACT:\n"
            f"- TIDAK ada infinite loop tanpa break\n"
            f"- Handle semua error/exception\n"
            f"- Harus bisa run di Linux Ubuntu 22.04\n"
            f"- TIDAK ada input() interaktif — data hardcoded\n"
            f"- Timeout maks {EXECUTION_TIMEOUT_SECONDS} detik"
        )

        async with POLYGLOT_CLI_SEMAPHORE:
            code = await self._call_gemini(system_prompt, task_desc)

        if not code or code.startswith("ERROR"):
            return f"Sintesis gagal: {code}"

        # --- Loop utama: Execution + Auto-Heal + Ask Instructions ---
        extra_context = ""
        round_number = 0

        while True:
            round_number += 1
            if round_number > 1:
                await send_fn(
                    f"<b>Mencoba lagi</b> dengan instruksi tambahan (Ronde {round_number})...\n"
                    f"Tahap 2: Re-sintesis kode..."
                )
                # Re-sintesis dengan konteks tambahan dari pengguna
                async with POLYGLOT_CLI_SEMAPHORE:
                    code = await self._call_gemini(
                        system_prompt + f"\n\nINSTRUKSI TAMBAHAN DARI PENGGUNA:\n{extra_context}",
                        task_desc
                    )
                if not code or code.startswith("ERROR"):
                    return f"Re-sintesis gagal: {code}"

            # --- Tahap 3 & 4: Auto-Install + Execution + Auto-Heal (5x) ---
            last_stderr = ""
            success = False

            for attempt in range(1, MAX_AUTO_HEAL_ATTEMPTS + 1):
                await send_fn(
                    f"Sandbox Execution (Ronde {round_number}, "
                    f"Percobaan {attempt}/{MAX_AUTO_HEAL_ATTEMPTS})..."
                )

                loop = asyncio.get_running_loop()
                ok, stdout, stderr, binary_missing = await loop.run_in_executor(
                    None, self._execute_in_sandbox, code, language, task_id
                )

                # Auto-install jika binary hilang
                if binary_missing:
                    installed = await _auto_install_compiler(language, send_fn)
                    if installed:
                        # Coba ulang eksekusi setelah install
                        ok, stdout, stderr, binary_missing = await loop.run_in_executor(
                            None, self._execute_in_sandbox, code, language, task_id
                        )
                    else:
                        # Install gagal — hentikan loop ini
                        last_stderr = f"[AUTO-INSTALL FAILED]: Compiler {language} tidak bisa diinstall."
                        break

                if ok:
                    out_preview = stdout[:500] if stdout else "(Sukses tanpa output stdout)"
                    final_msg = (
                        f"<b>[NEXUS POLYGLOT] BERHASIL!</b>\n\n"
                        f"Ronde: <code>{round_number}</code> | "
                        f"Percobaan: <code>{attempt}/{MAX_AUTO_HEAL_ATTEMPTS}</code>\n"
                        f"Bahasa: <code>{language.upper()}</code>\n"
                        f"Sandbox: <code>{task_id[:8]}</code> (PERSISTEN - tidak dihapus)\n\n"
                        f"<b>Output:</b>\n<pre>{out_preview}</pre>\n\n"
                        f"<b>Kode Final:</b>\n<pre>{code[:1500]}</pre>\n\n"
                        f"Kirim /clearcache untuk reset semua sandbox."
                    )
                    await send_fn(final_msg)
                    success = True
                    break

                # Gagal — Auto-Heal
                last_stderr = stderr
                if attempt < MAX_AUTO_HEAL_ATTEMPTS:
                    err_preview = stderr[:300] if stderr else "Unknown error"
                    await send_fn(
                        f"Error (Percobaan {attempt}). Auto-Heal...\n"
                        f"<pre>{err_preview}</pre>"
                    )

                    heal_prompt = (
                        f"Kode {language.upper()} ini GAGAL:\n"
                        f"[ERROR]:\n{stderr[:800]}\n\n"
                        f"[KODE GAGAL]:\n{code}\n\n"
                        f"[TASK]:\n{task_desc}\n\n"
                        f"Perbaiki SEMUA error. Output HANYA kode murni."
                    )
                    async with POLYGLOT_CLI_SEMAPHORE:
                        code = await self._call_gemini(
                            f"Ahli debug {language.upper()} senior. Output kode murni saja.",
                            heal_prompt,
                        )
                    if not code or code.startswith("ERROR"):
                        break

            if success:
                return "success"

            # --- Tahap 6: Semua percobaan gagal — Minta instruksi TANPA timeout ---
            err_preview = last_stderr[:400] if last_stderr else "Error tidak diketahui."

            await send_fn(
                f"<b>[Auto-Heal Habis]</b> {MAX_AUTO_HEAL_ATTEMPTS}x percobaan gagal di Ronde {round_number}.\n\n"
                f"Error terakhir:\n<pre>{err_preview}</pre>\n\n"
                f"Kode terakhir:\n<pre>{code[:600]}</pre>"
            )

            # Minta instruksi tambahan dari pengguna (TANPA TIMEOUT)
            extra_context = await ask_instructions_fn(
                f"<b>Butuh Instruksi Tambahan</b>\n\n"
                f"Sistem sudah mencoba {MAX_AUTO_HEAL_ATTEMPTS}x dan masih gagal.\n\n"
                f"Tolong berikan salah satu dari:\n"
                f"- Penjelasan lebih spesifik tentang logika yang diinginkan\n"
                f"- Library/versi tertentu yang harus dipakai\n"
                f"- Contoh input/output yang diharapkan\n"
                f"- Format data yang berbeda\n\n"
                f"<b>Tidak ada batas waktu</b> — balas kapanpun kamu siap.\n"
                f"Atau kirim /cancel untuk membatalkan task ini."
            )

            if not extra_context or extra_context.strip().lower() in ["/cancel", "cancel", "batal"]:
                return (
                    f"Task dibatalkan oleh pengguna setelah {round_number} ronde.\n"
                    f"Sandbox <code>{task_id[:8]}</code> tetap tersimpan.\n"
                    f"Kirim /clearcache untuk reset."
                )

            # Lanjut ke ronde berikutnya dengan instruksi baru
            await send_fn(
                f"Instruksi diterima! Memulai Ronde {round_number + 1} dengan konteks baru..."
            )


# ============================================================
# TELEGRAM LISTENER (NON-BLOCKING BACKGROUND TASK)
# ============================================================

_pending_clarifications: dict = {}   # chat_id -> asyncio.Future
_pending_instructions: dict = {}     # chat_id -> asyncio.Future (no timeout)
_active_tasks: dict = {}             # chat_id -> task_id (task yang sedang berjalan)


class TelegramPolyglotListener:
    """
    Telegram long-polling listener — asyncio background task.
    Non-blocking: tidak mengganggu pipeline Roblox.

    Security: Hanya menerima dari TELEGRAM_CHAT_ID (Master Node).
    """

    def __init__(self):
        self.bot_token = TELEGRAM_BOT_TOKEN
        self.master_chat_id = str(TELEGRAM_CHAT_ID).strip()
        self.last_update_id = 0
        self.agent = PolyglotSynthesizerAgent()
        self._running = True

    async def _get_updates(self) -> list:
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
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
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
        Clarification Protocol dengan NO TIMEOUT.
        Menunggu selamanya sampai pengguna balas.
        """
        await self._send(self.master_chat_id, question)

        future = asyncio.get_running_loop().create_future()
        _pending_clarifications[self.master_chat_id] = future

        try:
            # Tanpa timeout — tunggu selamanya
            result = await future
            return result
        except asyncio.CancelledError:
            return None
        finally:
            _pending_clarifications.pop(self.master_chat_id, None)

    async def _wait_for_instructions(self, message: str) -> Optional[str]:
        """
        Post-Failure Instruction Protocol dengan NO TIMEOUT.
        Menunggu selamanya sampai pengguna memberikan instruksi tambahan
        atau mengirim /cancel.
        """
        await self._send(self.master_chat_id, message)

        future = asyncio.get_running_loop().create_future()
        _pending_instructions[self.master_chat_id] = future

        try:
            # Tanpa timeout — tunggu selamanya
            result = await future
            return result
        except asyncio.CancelledError:
            return None
        finally:
            _pending_instructions.pop(self.master_chat_id, None)

    async def _handle_update(self, update: dict):
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = (message.get("text") or "").strip()

        if not text or not chat_id:
            return

        # Security
        if chat_id != self.master_chat_id:
            await self._send(chat_id, "Akses ditolak. Hanya Master Node.")
            return

        # Priority 1: Cek pending instructions (post-failure)
        if chat_id in _pending_instructions:
            fut = _pending_instructions.get(chat_id)
            if fut and not fut.done():
                fut.set_result(text)
                return

        # Priority 2: Cek pending clarification
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
        elif text.startswith("/clearcache"):
            await self._handle_clearcache(chat_id)
        elif text.startswith("/help") or text.startswith("/start"):
            await self._send(chat_id, HELP_TEXT)
        else:
            # Langkah 1: Cek keyword bahasa secara cepat (tanpa API call)
            detected = _detect_language_from_text(text)
            if detected:
                await self._send(chat_id, f"Mendeteksi bahasa: <b>{detected}</b> — memproses...")
                await self._handle_polyglot(chat_id, f"/polyglot {detected} {text}")
                return

            # Langkah 2: Klasifikasi intent (rule-based, tanpa Gemini)
            intent = self._classify_intent_local(text)

            if intent == 'coding_request':
                # Request coding eksplisit tapi tidak ada keyword bahasa
                await self._send(
                    chat_id,
                    "Bahasa pemrograman apa yang kamu inginkan?\n\n"
                    "Tersedia: <code>python, cpp, c, rust, go, java, javascript, typescript, lua, bash</code>\n\n"
                    "Contoh: <code>buat fungsi python fibonacci</code>"
                )
            else:
                # Math, sapaan, pertanyaan umum, sistem — semua ke _handle_chat
                await self._handle_chat(chat_id, text)

    async def _handle_polyglot(self, chat_id: str, text: str):
        parts = text.split(maxsplit=2)

        if len(parts) < 2:
            await self._send(
                chat_id,
                "Format salah!\nGunakan: <code>/polyglot [bahasa] [deskripsi]</code>"
            )
            return

        raw_lang = parts[1]
        task_desc = parts[2] if len(parts) > 2 else ""

        language = _resolve_language(raw_lang)
        if not language:
            # Fallback: coba deteksi dari seluruh teks pesan
            full_msg = " ".join(parts[1:])
            language = _detect_language_from_text(full_msg)
            if language:
                task_desc = full_msg
                await self._send(chat_id, f"Terdeteksi bahasa: <b>{language}</b> — memproses...")
            else:
                supported = ", ".join(sorted(LANGUAGE_CONFIG.keys()))
                await self._send(
                    chat_id,
                    f"Bahasa '<code>{raw_lang}</code>' tidak dikenali.\n"
                    f"Coba tulis nama bahasa dengan jelas, contoh:\n"
                    f"<code>/polyglot python buat fungsi sorting</code>\n\n"
                    f"Bahasa tersedia: <code>{supported}</code>"
                )
                return

        # Buat task_id baru untuk setiap request (sandbox baru)
        task_id = uuid.uuid4().hex

        asyncio.create_task(
            self.agent.synthesize_and_execute(
                language=language,
                task_desc=task_desc,
                task_id=task_id,
                send_fn=lambda msg: self._send(chat_id, msg),
                ask_instructions_fn=lambda msg: self._wait_for_instructions(msg),
                clarify_fn=lambda msg: self._wait_for_clarification(msg),
            )
        )

    async def _handle_status(self, chat_id: str):
        sandbox_count = len(_active_sandboxes)
        installed_langs = ", ".join(sorted(_installed_compilers)) if _installed_compilers else "belum ada"

        # Hitung total ukuran sandbox
        total_size_mb = 0
        for path in _active_sandboxes.values():
            if os.path.exists(path):
                for root, dirs, files in os.walk(path):
                    for f in files:
                        try:
                            total_size_mb += os.path.getsize(os.path.join(root, f))
                        except Exception:
                            pass
        total_size_mb = round(total_size_mb / (1024 * 1024), 2)

        try:
            from nexus_database import establish_database_connection
            db = establish_database_connection()
            cur = db.cursor()
            cur.execute("SELECT COUNT(*) FROM verified_modules")
            mod_count = cur.fetchone()[0]
            db.close()
            db_status = f"Online ({mod_count} modul)"
        except Exception as e:
            db_status = f"Error: {e}"

        status_msg = (
            f"<b>NEXUS SYSTEM STATUS v3</b>\n\n"
            f"Active AI Agents: <code>{len(ACTIVE_AGENTS)}</code>\n"
            f"Database: <code>{db_status}</code>\n"
            f"Polyglot Semaphore: <code>{POLYGLOT_CLI_SEMAPHORE._value}/2 slot</code>\n"
            f"Sandbox Aktif: <code>{sandbox_count}</code> ({total_size_mb} MB)\n"
            f"Auto-Installed: <code>{installed_langs}</code>\n"
            f"Bahasa Didukung: <code>{len(LANGUAGE_CONFIG)}</code>\n"
            f"Auto-Heal Max: <code>{MAX_AUTO_HEAL_ATTEMPTS}x per ronde</code>\n"
            f"Clarification Timeout: <b>TIDAK ADA (tunggu selamanya)</b>\n"
            f"Instruction Timeout: <b>TIDAK ADA (tunggu selamanya)</b>\n\n"
            f"Kirim /clearcache untuk reset semua sandbox."
        )
        await self._send(chat_id, status_msg)

    async def _handle_clearcache(self, chat_id: str):
        """
        Hapus semua sandbox persisten dan reset state.
        Sandbox baru akan dibuat otomatis saat request /polyglot berikutnya.
        """
        deleted_count = 0
        errors = []

        # Hapus semua sandbox directory
        for task_id, sandbox_path in list(_active_sandboxes.items()):
            try:
                if os.path.exists(sandbox_path):
                    shutil.rmtree(sandbox_path, ignore_errors=True)
                    deleted_count += 1
            except Exception as e:
                errors.append(str(e))

        # Hapus sandbox root jika ada
        try:
            if os.path.exists(POLYGLOT_SANDBOX_ROOT):
                shutil.rmtree(POLYGLOT_SANDBOX_ROOT, ignore_errors=True)
        except Exception:
            pass

        # Reset state
        _active_sandboxes.clear()
        _installed_compilers.clear()

        # Batalkan pending futures jika ada
        for fut in list(_pending_clarifications.values()):
            if not fut.done():
                fut.cancel()
        _pending_clarifications.clear()

        for fut in list(_pending_instructions.values()):
            if not fut.done():
                fut.cancel()
        _pending_instructions.clear()

        err_text = f"\nError: {'; '.join(errors)}" if errors else ""
        await self._send(
            chat_id,
            f"<b>Cache Dibersihkan!</b>\n\n"
            f"Sandbox dihapus: <code>{deleted_count}</code>\n"
            f"Compiler cache direset: semua\n"
            f"Pending tasks dibatalkan: semua\n"
            f"{err_text}\n\n"
            f"Sandbox baru akan dibuat otomatis saat /polyglot berikutnya."
        )

        console_terminal_interface.print(
            f"[bold cyan][Polyglot] /clearcache: {deleted_count} sandbox dihapus.[/bold cyan]"
        )


    async def _call_gemini_chat(self, system_prompt: str, user_text: str) -> str:
        """
        Panggil Gemini CLI khusus untuk percakapan chat.
        Model: gemini-3.1-flash-lite-preview (ringan & cepat, hemat rate limit).
        Berbeda dari _call_gemini milik PolyglotSynthesizerAgent yang pakai gemini-2.0-flash.
        Rate limit TERPISAH dari Roblox AI agent (gemma-4-31b-it) sehingga tidak tabrakan.
        Prioritas key: GEMINI_TELEGRAM_KEY (eksklusif) → fallback ke pool agent.
        """
        api_key = os.getenv("GEMINI_TELEGRAM_KEY", "").strip() or self.agent.rotator.get_key()
        if not api_key:
            return "ERROR: Tidak ada API key tersedia."

        env = os.environ.copy()
        env["GEMINI_API_KEY"] = api_key
        env["CI"] = "true"
        env["TERM"] = "dumb"
        env["NO_COLOR"] = "1"

        full_input = f"[SYSTEM]:\n{system_prompt}\n\n[USER]:\n{user_text}"
        command = [
            GEMINI_CLI_PATH,
            "-m", "models/gemini-3.1-flash-lite-preview",
            "-y",
            "-p", "Jawab secara natural dan ringkas. Bukan kode, kecuali diminta.",
        ]

        FALLBACK_MODEL = "models/gemini-2.0-flash"  # Fallback ke model terpisah dari Roblox
        for attempt, model in enumerate([command[command.index("-m") + 1], FALLBACK_MODEL]):
            if attempt == 1:
                # Ganti model ke fallback
                command = [
                    GEMINI_CLI_PATH,
                    "-m", FALLBACK_MODEL,
                    "-y",
                    "-p", "Jawab secara natural dan ringkas. Bukan kode, kecuali diminta.",
                ]
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout_data, stderr_data = await asyncio.wait_for(
                    process.communicate(input=full_input.encode("utf-8")),
                    timeout=30.0,
                )
                result = stdout_data.decode("utf-8", errors="replace").strip()
                if result and not result.lower().startswith("error") and len(result) > 3:
                    return result
                # stdout kosong/error — coba fallback di iterasi berikut
                continue
            except asyncio.TimeoutError:
                if attempt == 1:
                    return "ERROR: Timeout."
                continue
            except Exception as e:
                if attempt == 1:
                    return f"ERROR: {e}"
                continue
        return "ERROR: Semua model gagal."

    def _classify_intent_local(self, text: str) -> str:
        """
        Klasifikasi intent menggunakan rule-based (TANPA Gemini, TANPA API call).
        Cepat, tidak kena rate limit, tidak bisa gagal.
        Returns: 'math', 'greeting', 'coding_request', 'system', 'general_question', atau 'unknown'
        """
        t = text.lower().strip()

        # --- Matematika: hitung langsung ---
        import re as _re
        math_pattern = _re.compile(
            r'^\s*[\d\s\.\,\+\-\*\/\^\(\)\%]+[\?=]?\s*$|'
            r'(berapa|hitung|hasil|sama dengan|equal)[\s\S]{0,30}[\d]|'
            r'[\d][\s\S]{0,20}(berapa|hasilnya|sama dengan|\?)'
        )
        if math_pattern.search(t) and len(t) < 60:
            return 'math'

        # --- Sapaan ---
        greetings = ['halo', 'hai', 'hi ', 'hello', 'hey', 'selamat pagi',
                     'selamat siang', 'selamat malam', 'apa kabar', 'assalamu',
                     'permisi', 'hei ']
        if any(t.startswith(g) or t == g.strip() for g in greetings):
            return 'greeting'

        # --- Request coding eksplisit ---
        coding_kw = ['buat kode', 'tulis kode', 'buatkan kode', 'bikin kode',
                     'buat program', 'buat script', 'buatkan script', 'bikin script',
                     'buat fungsi', 'buatkan fungsi', 'implementasi', 'implement',
                     'write a', 'create a function', 'make a program', 'code for']
        if any(kw in t for kw in coding_kw):
            return 'coding_request'

        # --- Pertanyaan tentang sistem/bot ---
        system_kw = ['kamu bisa', 'apa yang bisa', 'kemampuan', 'fitur',
                     'cara pakai', 'cara menggunakan', 'perintah apa',
                     'nexus', 'bot ini', '/help', 'status']
        if any(kw in t for kw in system_kw):
            return 'system'

        # --- Pertanyaan umum (apa itu X, siapa X, dll) ---
        question_kw = ['apa itu', 'apa yang', 'siapa', 'kenapa', 'mengapa',
                       'bagaimana', 'kapan', 'dimana', 'what is', 'who is',
                       'why ', 'how ', 'when ', 'where ', 'kamu tau', 'tahukah',
                       'jelaskan', 'ceritakan', 'maksud']
        if any(kw in t for kw in question_kw):
            return 'general_question'

        return 'unknown'

    async def _classify_intent(self, text: str) -> str:
        """Wrapper publik — gunakan rule-based classifier (tanpa Gemini)."""
        return self._classify_intent_local(text)

    async def _handle_chat(self, chat_id: str, text: str):
        """
        Jawab pesan obrolan.
        - Matematika, sapaan, info sistem: dijawab LANGSUNG tanpa Gemini (tidak kena rate limit).
        - Pertanyaan umum (apa itu X, dll): coba Gemini, fallback ke jawaban informatif.
        """
        import re as _re, math as _math

        intent = self._classify_intent_local(text)

        # ── MATEMATIKA: hitung langsung dengan Python ──────────────────
        if intent == 'math':
            t_clean = text.strip().rstrip('?=').strip()
            t_clean = t_clean.replace('x', '*').replace('×', '*').replace('÷', '/')
            t_clean = t_clean.replace('^', '**')
            t_clean = _re.sub(r'[^\d\s\.\+\-\*\/\(\)\%\*]', '', t_clean).strip()
            # Hapus leading zero (02837 → 2837) — Python 3 tidak izinkan 0-prefixed integer
            # Kecuali 0.5, 0.25 (desimal) tetap aman
            t_clean = _re.sub(r'(?<![\d\.])0+(\d)', r'\1', t_clean)
            try:
                result = eval(t_clean, {"__builtins__": {}, "sqrt": _math.sqrt,
                                        "pi": _math.pi, "pow": pow, "abs": abs})
                await self._send(chat_id, f"{result}")
            except Exception:
                await self._send(chat_id, "Maaf, ekspresi matematikanya tidak bisa dihitung. Coba tulis ulang.")
            return

        # ── SAPAAN: jawab ramah langsung ──────────────────────────────
        if intent == 'greeting':
            import random as _random
            replies = [
                "Halo! Ada yang bisa saya bantu? Saya bisa membuat dan menjalankan kode dalam berbagai bahasa.",
                "Hai! Saya Nexus Bot. Butuh kode Python, Rust, Go, atau bahasa lain? Langsung ketik saja.",
                "Halo! Siap membantu. Ketik /help untuk lihat kemampuan saya, atau langsung minta kode.",
            ]
            await self._send(chat_id, _random.choice(replies))
            return

        # ── PERTANYAAN SISTEM: jawab langsung ─────────────────────────
        if intent == 'system':
            await self._send(
                chat_id,
                "<b>Nexus Bot — Kemampuan:</b>\n\n"
                "Saya bisa membuat dan menjalankan kode dalam:\n"
                "<code>python, cpp, c, rust, go, java, javascript, typescript, lua, bash</code>\n\n"
                "Cara pakai:\n"
                "• Tulis deskripsi tugasmu, contoh: <code>buat fungsi python fibonacci</code>\n"
                "• Atau: <code>/polyglot python buat sorting algorithm</code>\n\n"
                "Perintah: /status /clearcache /help"
            )
            return

        # ── PERTANYAAN UMUM: coba Gemini, fallback jika gagal ─────────
        system_prompt = (
            "Kamu adalah Nexus Bot — asisten AI yang ramah dan cerdas. "
            "Jawab pertanyaan pengguna secara langsung, jelas, dan singkat (maksimal 3 kalimat). "
            "Gunakan bahasa yang sama dengan pengguna (Indonesia atau Inggris). "
            "Jangan buat kode kecuali diminta. Jangan bertele-tele."
        )
        try:
            # Gunakan model ringan khusus chat (gemini-3.1-flash-lite-preview)
            # agar tidak bersaing rate limit dengan pipeline coding (gemma-4-31b-it)
            reply = await self._call_gemini_chat(system_prompt, text)
            if reply and not reply.startswith("ERROR") and len(reply.strip()) > 5:
                await self._send(chat_id, reply.strip())
            else:
                await self._send(
                    chat_id,
                    "AI chat sedang sibuk. Coba lagi sebentar!"
                )
        except Exception:
            await self._send(chat_id, "AI chat sedang sibuk. Coba lagi sebentar!")

    async def start_polling(self):
        console_terminal_interface.print(
            "[bold cyan][Polyglot Listener v3] Telegram bot polling dimulai "
            "(Persistent Sandbox + Auto-Install + No-Timeout)...[/bold cyan]"
        )
        await self._send(
            self.master_chat_id,
            "<b>NEXUS POLYGLOT LISTENER v3 AKTIF!</b>\n\n"
            "Fitur baru:\n"
            "- Auto-install compiler (tanpa tanya)\n"
            "- Sandbox persisten (tidak dihapus otomatis)\n"
            "- Instruksi tambahan tanpa batas waktu\n\n"
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


# ============================================================
# ENTRY POINT
# ============================================================

async def start_telegram_polling():
    """
    Entry point — dipanggil dari nexus_main.py:
        asyncio.create_task(start_telegram_polling())
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        console_terminal_interface.print(
            "[bold yellow][Polyglot Listener] TELEGRAM_BOT_TOKEN atau TELEGRAM_CHAT_ID "
            "kosong. Listener tidak dimulai.[/bold yellow]"
        )
        return

    listener = TelegramPolyglotListener()
    await listener.start_polling()
