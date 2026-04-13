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

    loop = asyncio.get_event_loop()
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

if __name__ == "__main__":
    print("Nexus Healer Watchdog: Aktif.")
    while True:
        time.sleep(60)
