import os
import sys
import time
import subprocess
import asyncio
import json
import re
import difflib
import shutil
import glob as glob_module
from typing import Tuple, List
from nexus_config import console_terminal_interface, GEMINI_CLI_PATH

def _cleanup_aider_artifacts(working_dir: str):
    """Hapus semua file/folder sementara yang dibuat oleh Aider setelah setiap sesi bedah."""
    artifacts = [
        ".aider.chat.history.md",
        ".aider.input.history",
        ".aider.llm.history",
        ".aider.tags.cache.v3",
        ".aider.tags.cache.v4",
    ]
    for name in artifacts:
        full_path = os.path.join(working_dir, name)
        try:
            if os.path.isdir(full_path):
                shutil.rmtree(full_path)
            elif os.path.isfile(full_path):
                os.remove(full_path)
        except Exception:
            pass

    for pattern in ["*.aider.*.md", "*.orig", ".aider*"]:
        for match in glob_module.glob(os.path.join(working_dir, pattern)):
            try:
                if os.path.isdir(match):
                    shutil.rmtree(match)
                else:
                    os.remove(match)
            except Exception:
                pass

class ApexKeyRotator:
    def __init__(self, keys: list):
        self.keys = [k for k in keys if k and k.strip() != ""]
        self.index = 0
        self.rate_limited_keys = {}

    def get_key(self) -> str:
        if not self.keys: return ""
        for _ in range(len(self.keys)):
            key = self.keys[self.index % len(self.keys)]
            self.index += 1
            if not self.rate_limited_keys.get(key, False):
                return key
        
        self.rate_limited_keys.clear()
        key = self.keys[self.index % len(self.keys)]
        self.index += 1
        return key

    def mark_rate_limited(self, key: str):
        self.rate_limited_keys[key] = True

def _find_gemini_binary() -> str:
    return GEMINI_CLI_PATH

def _panggil_gemini_cli_dengan_rotasi(prompt_text: str, api_key_aktif: str, model_name: str) -> str:
    """Eksekutor sinkron untuk background healer Python."""
    env_vars = os.environ.copy()
    env_vars["GEMINI_API_KEY"] = api_key_aktif
    env_vars["CI"] = "true"
    env_vars["TERM"] = "dumb"
    env_vars["NO_COLOR"] = "1"

    current_path = env_vars.get("PATH", "")
    env_vars["PATH"] = "/home/runner/.local/bin:" + current_path

    _gemini_binary = _find_gemini_binary()

    command = [
        _gemini_binary,
        "-m", model_name,
        "-y",
        "-p", prompt_text,
    ]

    try:
        proses = subprocess.run(
            command,
            env=env_vars,
            capture_output=True,
            text=True,
            timeout=1800,
        )

        if proses.returncode != 0:
            return ""

        return proses.stdout
    except Exception:
        return ""

async def call_gemini_rest(
    prompt: str,
    system_instruction: str = "",
    model: str = "models/gemma-4-31b-it",
    max_retries: int = 3,
) -> Tuple[bool, str]:
    """Eksekutor asinkron untuk panggilan REST langsung (fallback)."""
    full_prompt = prompt
    if system_instruction:
        full_prompt = f"[SYSTEM INSTRUCTION]:\n{system_instruction}\n\n[PROMPT]:\n{prompt}"

    model_cascade = [
        "models/gemma-4-31b-it",
        "models/gemma-4-26b-a4b-it",
        "models/gemini-3.1-flash-lite-preview",
        "models/gemini-2.0-flash",
    ]

    return False, "REST Fallback Not Implemented. Use CLI."

class SurgicalCodePatcher:
    @staticmethod
    def compute_diff_lines(original: str, patched: str) -> List[str]:
        return list(difflib.unified_diff(original.splitlines(), patched.splitlines()))

    @staticmethod
    async def patch_with_ai(
        original_code: str, error_text: str, file_name: str, knowledge_context: str = "",
        target_filepath: str = ""
    ) -> str:
        
        prompt = f"""<|think|>
BERPIKIRLAH SECARA MENDALAM DAN EKSTENSIF (REASON LONGER) SEBELUM MENJAWAB! Evaluasi setiap kemungkinan kesalahan kode sebelum Anda memperbaikinya.
Anda adalah Dokter Bedah Kode Senior.

[ERROR MESSAGE]:
{error_text}

[FILE]:
{file_name}

Tugas Anda adalah memperbaiki file tersebut menggunakan `aider` format.
"""
        env_vars = os.environ.copy()

        # Ambil API key dari rotator nexus agar tidak rate limit key yang sama
        try:
            from nexus_agents import _key_rotator as _nexus_rotator
            aider_api_key = _nexus_rotator.get_key()
            if aider_api_key:
                env_vars["GEMINI_API_KEY"] = aider_api_key
        except Exception:
            pass

        command = [
            "aider",
            target_filepath,
            "--message", prompt,
            "--yes",
            "--no-auto-commits",
            "--model", "gemini/gemma-4-31b-it"
        ]

        working_dir = os.path.dirname(os.path.abspath(target_filepath)) if target_filepath else os.getcwd()
        console_terminal_interface.print(f"[dim cyan]   [Aider CLI] Menganalisis file fisik {target_filepath}...[/dim cyan]")

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env_vars
            )

            stdout_data, stderr_data = await asyncio.wait_for(process.communicate(), timeout=1800.0)

            if process.returncode != 0:
                _cleanup_aider_artifacts(working_dir)
                return original_code

            with open(target_filepath, "r", encoding="utf-8") as f:
                patched = f.read()

            _cleanup_aider_artifacts(working_dir)
            return patched

        except asyncio.TimeoutError:
            try:
                process.kill()
            except Exception:
                pass
            _cleanup_aider_artifacts(working_dir)
            console_terminal_interface.print("[bold red]   [Aider] Timeout 1800s selama proses bedah![/bold red]")
            return original_code
        except Exception as e:
            _cleanup_aider_artifacts(working_dir)
            return original_code


# ============================================================
# PRE-DEPLOYMENT VALIDATOR
# Memeriksa SEMUA file yang diharapkan sebelum upload ke Roblox.
# Jika ada yang hilang atau kosong → regenerasi otomatis dulu.
# Deployment HANYA jalan jika 100% file sudah ada dan valid.
# ============================================================

import aiofiles

class PreDeploymentValidator:
    """
    Penjaga gerbang terakhir sebelum upload ke Roblox Creator API.
    Tidak ada satu file pun yang boleh hilang atau kosong.
    """

    # File di bawah ukuran ini dianggap gagal/kosong
    MIN_FILE_SIZE_BYTES: int = 100

    def __init__(self):
        from nexus_config import SOURCE_CODE_DIRECTORY, ACTIVE_AGENTS
        self.src_dir = SOURCE_CODE_DIRECTORY
        self.active_agents = ACTIVE_AGENTS

    # ----------------------------------------------------------
    # SCAN DISK: File apa saja yang benar-benar ada di disk?
    # ----------------------------------------------------------
    def scan_disk_files(self) -> dict:
        """Kembalikan dict {full_path: size_bytes} untuk semua .lua file di src/"""
        found = {}
        if not os.path.exists(self.src_dir):
            return found
        for root, dirs, files in os.walk(self.src_dir):
            for fname in files:
                if fname.endswith(".lua") or fname.endswith(".luau"):
                    full_path = os.path.join(root, fname)
                    try:
                        found[full_path] = os.path.getsize(full_path)
                    except OSError:
                        found[full_path] = 0
        return found

    # ----------------------------------------------------------
    # ANALISIS: Task mana yang hilang / terlalu kecil?
    # ----------------------------------------------------------
    def find_incomplete_tasks(self, task_queue: list) -> dict:
        """
        Cross-reference task_queue dengan file di disk.
        Kembalikan dict dengan 3 kategori:
          - missing  : file tidak ada sama sekali
          - empty    : file ada tapi < MIN_FILE_SIZE_BYTES
          - ok       : file ada dan ukurannya cukup
        """
        disk_files = self.scan_disk_files()
        result = {"missing": [], "empty": [], "ok": []}

        for task in task_queue:
            path = task.get("path", "")
            if not path:
                continue
            if path not in disk_files and not os.path.exists(path):
                result["missing"].append(task)
            elif disk_files.get(path, 0) < self.MIN_FILE_SIZE_BYTES or \
                 os.path.getsize(path) < self.MIN_FILE_SIZE_BYTES:
                result["empty"].append(task)
            else:
                result["ok"].append(task)

        return result

    # ----------------------------------------------------------
    # CETAK LAPORAN: Tampilkan status ke terminal
    # ----------------------------------------------------------
    def print_report(self, analysis: dict):
        total = len(analysis["missing"]) + len(analysis["empty"]) + len(analysis["ok"])
        ok_count = len(analysis["ok"])
        miss_count = len(analysis["missing"])
        empty_count = len(analysis["empty"])

        console_terminal_interface.print(
            f"\n[bold cyan]╔══════════════════════════════════════════════════╗[/bold cyan]"
        )
        console_terminal_interface.print(
            f"[bold cyan]║     PRE-DEPLOYMENT FILE COMPLETENESS REPORT     ║[/bold cyan]"
        )
        console_terminal_interface.print(
            f"[bold cyan]╠══════════════════════════════════════════════════╣[/bold cyan]"
        )
        console_terminal_interface.print(
            f"[bold cyan]║  Total Task  : {str(total).ljust(33)}║[/bold cyan]"
        )
        console_terminal_interface.print(
            f"[bold green]║  ✅ Siap     : {str(ok_count).ljust(33)}║[/bold green]"
        )
        console_terminal_interface.print(
            f"[bold red]║  ❌ Hilang   : {str(miss_count).ljust(33)}║[/bold red]"
        )
        console_terminal_interface.print(
            f"[bold yellow]║  ⚠️  Kosong   : {str(empty_count).ljust(33)}║[/bold yellow]"
        )
        console_terminal_interface.print(
            f"[bold cyan]╚══════════════════════════════════════════════════╝[/bold cyan]\n"
        )

        if analysis["missing"]:
            console_terminal_interface.print("[bold red]File yang HILANG:[/bold red]")
            for t in analysis["missing"]:
                console_terminal_interface.print(f"  ❌ {t['name']} → {t['path']}")

        if analysis["empty"]:
            console_terminal_interface.print("[bold yellow]File yang KOSONG/TERLALU KECIL:[/bold yellow]")
            for t in analysis["empty"]:
                size = os.path.getsize(t["path"]) if os.path.exists(t["path"]) else 0
                console_terminal_interface.print(f"  ⚠️  {t['name']} → {size} bytes (min {self.MIN_FILE_SIZE_BYTES})")

    # ----------------------------------------------------------
    # REGENERASI: Buat ulang file yang hilang/kosong pakai AI
    # Infinity retry seperti sistem utama — tidak berhenti sampai berhasil
    # ----------------------------------------------------------
    async def regenerate_missing(
        self, incomplete_tasks: list, synthesizer, agent: dict
    ):
        total = len(incomplete_tasks)
        console_terminal_interface.print(
            f"\n[bold yellow]🔧 [PRE-DEPLOY] Memulai regenerasi {total} file yang hilang/kosong...[/bold yellow]"
        )

        for idx, task in enumerate(incomplete_tasks, 1):
            console_terminal_interface.print(
                f"\n[bold magenta]  [{idx}/{total}] Regenerasi: {task['name']}[/bold magenta]"
            )

            attempt = 0
            prev_err = ""
            prev_code = ""

            # ── INFINITY RETRY sampai berhasil ──────────────────
            while True:
                attempt += 1
                try:
                    completed, prev_err, prev_code = await synthesizer.synthesize_handoff(
                        agent,
                        task["path"],
                        task["name"],
                        task["desc"],
                        task["req"],
                        task["forb"],
                        prev_err,
                        prev_code,
                    )

                    # Pastikan file benar-benar tertulis dan ukurannya cukup
                    if (
                        completed
                        and os.path.exists(task["path"])
                        and os.path.getsize(task["path"]) >= self.MIN_FILE_SIZE_BYTES
                    ):
                        file_size = os.path.getsize(task["path"])
                        console_terminal_interface.print(
                            f"  [bold green]✅ {task['name']} berhasil! ({file_size} bytes, percobaan ke-{attempt})[/bold green]"
                        )
                        break
                    else:
                        console_terminal_interface.print(
                            f"  [bold yellow]  Percobaan {attempt} gagal, retry dalam 10 detik...[/bold yellow]"
                        )
                        await asyncio.sleep(10)

                except Exception as exc:
                    console_terminal_interface.print(
                        f"  [bold red]  Exception percobaan {attempt}: {exc}. Retry dalam 15 detik...[/bold red]"
                    )
                    await asyncio.sleep(15)

    # ----------------------------------------------------------
    # VALIDASI AKHIR: Pipeline lengkap sebelum deployment
    # Kembalikan True = aman deploy, False = batalkan deploy
    # ----------------------------------------------------------
    async def validate_and_complete(
        self, task_queue: list, synthesizer, agent: dict, notify_fn=None
    ) -> bool:
        """
        Gerbang akhir keamanan deployment.
        1. Scan semua task yang diharapkan vs file di disk
        2. Cetak laporan detail
        3. Regenerasi file yang hilang/kosong (infinity retry)
        4. Verifikasi ulang setelah regenerasi
        5. Return True hanya jika 100% file siap
        """
        console_terminal_interface.print(
            "\n[bold yellow]🛡️  [PRE-DEPLOY VALIDATOR] Mulai pemeriksaan kelengkapan sebelum upload ke Roblox...[/bold yellow]"
        )

        # === TAHAP 1: ANALISIS AWAL ===
        analysis = self.find_incomplete_tasks(task_queue)
        self.print_report(analysis)

        total_incomplete = len(analysis["missing"]) + len(analysis["empty"])

        if total_incomplete == 0:
            msg = (
                f"✅ [PRE-DEPLOY] Semua {len(analysis['ok'])} file lengkap dan valid.\n"
                f"🚀 Deployment ke Roblox aman untuk dilanjutkan!"
            )
            console_terminal_interface.print(f"[bold green]{msg}[/bold green]")
            if notify_fn:
                try:
                    await notify_fn(msg, important=True)
                except Exception:
                    pass
            return True

        # === TAHAP 2: NOTIF TELEGRAM SEBELUM REGENERASI ===
        warn_msg = (
            f"⚠️ [PRE-DEPLOY] Ditemukan {total_incomplete} file hilang/kosong sebelum deployment.\n"
            f"❌ Hilang: {len(analysis['missing'])} | ⚠️ Kosong: {len(analysis['empty'])}\n"
            f"🔧 Memulai regenerasi otomatis... Deployment ditunda sementara."
        )
        console_terminal_interface.print(f"[bold yellow]{warn_msg}[/bold yellow]")
        if notify_fn:
            try:
                await notify_fn(warn_msg, important=True)
            except Exception:
                pass

        # === TAHAP 3: REGENERASI FILE HILANG/KOSONG ===
        all_incomplete = analysis["missing"] + analysis["empty"]
        await self.regenerate_missing(all_incomplete, synthesizer, agent)

        # === TAHAP 4: VERIFIKASI ULANG SETELAH REGENERASI ===
        console_terminal_interface.print(
            "\n[bold cyan]🔍 [PRE-DEPLOY] Verifikasi ulang setelah regenerasi...[/bold cyan]"
        )
        final_analysis = self.find_incomplete_tasks(task_queue)
        self.print_report(final_analysis)

        final_incomplete = len(final_analysis["missing"]) + len(final_analysis["empty"])

        if final_incomplete == 0:
            success_msg = (
                f"✅ [PRE-DEPLOY] Semua file berhasil dilengkapi!\n"
                f"📦 Total {len(final_analysis['ok'])} file siap.\n"
                f"🚀 Deployment ke Roblox Creator API aman dilanjutkan!"
            )
            console_terminal_interface.print(f"[bold green]{success_msg}[/bold green]")
            if notify_fn:
                try:
                    await notify_fn(success_msg, important=True)
                except Exception:
                    pass
            return True
        else:
            fail_msg = (
                f"❌ [PRE-DEPLOY] DEPLOYMENT DIBATALKAN!\n"
                f"Masih ada {final_incomplete} file yang tidak dapat di-generate.\n"
                f"Periksa log untuk detail error."
            )
            console_terminal_interface.print(f"[bold red]{fail_msg}[/bold red]")
            if notify_fn:
                try:
                    await notify_fn(fail_msg, important=True)
                except Exception:
                    pass
            return False


if __name__ == "__main__":
    print("Nexus Healer Watchdog: Aktif.")
    while True:
        time.sleep(60)
