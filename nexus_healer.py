import os
import sys
import time
import subprocess
import asyncio
import json
import re
from typing import Tuple, List
from nexus_config import console_terminal_interface, GEMINI_CLI_PATH


class ApexKeyRotator:
    def __init__(self, keys: list):
        self.keys = [k for k in keys if k and k.strip() != ""]
        self.index = 0
        self.rate_limited_keys = {}

    def get_key(self) -> str:
        if not self.keys:
            return ""
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
        "--temp", "1.0",
        "--top-p", "0.95",
        "--top-k", "64",
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
    model: str = "models/gemini-2.5-flash",
    max_retries: int = 3,
) -> Tuple[bool, str]:
    """Placeholder untuk REST fallback - tidak diimplementasikan sepenuhnya."""
    return False, "REST Fallback Not Implemented. Use CLI."


class SurgicalCodePatcher:
    @staticmethod
    async def patch_with_ai(
        original_code: str, error_text: str, file_name: str, knowledge_context: str = "",
        target_filepath: str = ""
    ) -> str:
        prompt = f"""BERPIKIRLAH SECARA MENDALAM SEBELUM MENJAWAB!
Anda adalah Dokter Bedah Kode Senior.

[ERROR MESSAGE]:
{error_text}

[FILE]:
{file_name}

Perbaiki error pada kode ini.
"""
        env_vars = os.environ.copy()

        if not target_filepath or not os.path.exists(target_filepath):
            console_terminal_interface.print(f"[bold yellow]   [Patcher] File tidak ditemukan: {target_filepath}[/bold yellow]")
            return original_code

        command = [
            "aider",
            target_filepath,
            "--message", prompt,
            "--yes",
            "--no-auto-commits",
            "--model", "gemini/gemini-2.5-flash"
        ]

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
                return original_code

            with open(target_filepath, "r", encoding="utf-8") as f:
                return f.read()

        except asyncio.TimeoutError:
            try:
                process.kill()
            except Exception:
                pass
            console_terminal_interface.print("[bold red]   [Aider] Timeout 1800s selama proses bedah![/bold red]")
            return original_code
        except Exception as e:
            return original_code


if __name__ == "__main__":
    print("Nexus Healer Watchdog: Aktif.")
    while True:
        time.sleep(60)
