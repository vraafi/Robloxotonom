import asyncio
import base64
import json
import re
import os
import uuid
import shutil
import signal
import tempfile
import subprocess
import requests
import difflib
from typing import Tuple, List

from nexus_healer import ApexKeyRotator

from rich.progress import Progress, SpinnerColumn, TextColumn

from nexus_config import (
    console_terminal_interface,
    TEMP_IO_DIRECTORY,
    ACTIVE_AGENTS,
    GEMINI_CLI_PATH,
    ROBLOX_MCP_URL,
)
from nexus_database import retrieve_ecosystem_context, save_verified_module
from nexus_compiler import AbsoluteOmniValidator, NativeLuauCompiler
from nexus_asset_engine import AssetOrchestrator, detect_asset_type
from nexus_project_scanner import (
    get_armor_hitbox_mandatory_template,
    scan_existing_project,
    search_github_for_hitbox_armor,
    scan_and_repair_invalid_files,
)

_key_rotator = ApexKeyRotator([a["api_key"] for a in ACTIVE_AGENTS if a["api_key"]])

CLI_EXECUTION_SEMAPHORE = asyncio.Semaphore(1)

MARKDOWN_BLOCK = chr(96) * 3

# ============================================================
# GITHUB TOKEN — prioritaskan GITHUB_PERSONAL_ACCESS_TOKEN
# ============================================================
def _get_github_token() -> str:
    """
    Cari token GitHub dari env dengan urutan prioritas:
    1. GITHUB_PERSONAL_ACCESS_TOKEN  (nama rahasia utama di project ini)
    2. GITHUB_TOKEN                  (fallback umum)
    Mengembalikan string kosong jika tidak ada.
    """
    return (
        os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "")
        or os.getenv("GITHUB_TOKEN", "")
    )


def extract_pure_luau_code(raw_payload: str) -> str:
    """Penghancur Markdown tangguh. Membersihkan sisa simbol dan spasi liar."""
    if not raw_payload:
        return ""
    code = raw_payload.strip()
    code = re.sub(r'^\s*`{3}[a-zA-Z]*\s*\n*', '', code, flags=re.IGNORECASE)
    code = re.sub(r'\n*\s*`{3}\s*$', '', code)
    return code.strip()


class RobloxMCPBridge:
    """
    Jembatan HTTP ke PC Lokal Anda (Roblox Studio MCP).
    Membypass bug enum API dengan menembak langsung JSON-RPC ke server.
    """
    @staticmethod
    async def execute_tool(tool_name: str, arguments: dict) -> str:
        if not ROBLOX_MCP_URL:
            return "ERROR: ROBLOX_MCP_URL tidak dikonfigurasi di VPS Anda."

        payload = {
            "jsonrpc": "2.0",
            "method": tool_name,
            "params": arguments,
            "id": 1,
        }

        def _post():
            try:
                res = requests.post(f"{ROBLOX_MCP_URL}/jsonrpc", json=payload, timeout=45)
                if res.status_code == 200:
                    return res.text
                return f"MCP_ERROR: Kode {res.status_code} | Pesan: {res.text}"
            except Exception as e:
                return f"MCP_CONNECTION_FAILED: Pastikan ngrok aktif di PC Lokal Anda. Detail: {str(e)}"

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _post)


class LuauKnowledgeScraper:
    """Sistem RAG (Retrieval-Augmented Generation) Tingkat Militer Ekstrim."""

    @staticmethod
    def _clean_error_query(raw_error: str) -> str:
        clean_text = re.sub(r'temp_[a-zA-Z0-9_]+\.luau:\d+:\s*', '', raw_error)
        clean_text = re.sub(r'[^\w\s]', ' ', clean_text).strip()
        words = clean_text.split()[:6]
        return " ".join(words)

    @staticmethod
    def _clean_task_query(module_name: str) -> str:
        clean_name = re.sub(r'_\d+$', '', module_name)
        clean_name = clean_name.replace('_', ' ')
        return clean_name

    # ------------------------------------------------------------------
    # GITHUB CODE SEARCH  (butuh token sejak 2024)
    # ------------------------------------------------------------------
    @staticmethod
    async def search_github_luau(query: str) -> str:
        """
        Cari kode Luau/Roblox di GitHub menggunakan Code Search API.
        Menggunakan GITHUB_PERSONAL_ACCESS_TOKEN agar tidak kena rate-limit.
        FIX: Perbaikan lambda closure bug di dalam for-loop dengan
             menggunakan default-argument capture.
        """
        github_token = _get_github_token()
        try:
            encoded_query = query.replace(" ", "+")
            url = (
                f"https://api.github.com/search/code"
                f"?q={encoded_query}+language:lua+roblox"
                f"&per_page=3&sort=indexed&order=desc"
            )

            headers_list = [
                "-H", "Accept: application/vnd.github.v3+json",
                "-H", "User-Agent: NexusAgent/2.0",
            ]
            if github_token:
                headers_list += ["-H", f"Authorization: Bearer {github_token}"]

            command = ["curl", "-s", "--max-time", "15"] + headers_list + [url]

            loop = asyncio.get_running_loop()
            proses = await loop.run_in_executor(
                None,
                lambda: subprocess.run(command, capture_output=True, text=True, timeout=20),
            )

            if proses.returncode != 0 or not proses.stdout:
                return ""

            data = json.loads(proses.stdout)

            # Cek apakah API menolak karena rate-limit / autentikasi
            if "message" in data:
                msg = data["message"].lower()
                if "rate limit" in msg or "requires authentication" in msg:
                    console_terminal_interface.print(
                        f"[bold yellow][GitHub] Code Search terbatas: {data['message']}[/bold yellow]"
                    )
                    # Fallback ke Repository Search yang lebih terbuka
                    return await LuauKnowledgeScraper.search_github_repositories(query)

            items = data.get("items", [])[:3]
            if not items:
                return ""

            res = "GITHUB ROBLOX KNOWLEDGE (RAW FILE EXTRACT):\n"
            for item in items:
                repo_name = item.get("repository", {}).get("full_name", "")
                file_name = item.get("name", "")
                file_api_url = item.get("url", "")

                if not file_api_url:
                    continue

                # FIX lambda closure bug: ikat variabel loop dengan default arg
                raw_headers = [
                    "-H", "Accept: application/vnd.github.v3.raw",
                    "-H", "User-Agent: NexusAgent/2.0",
                ]
                if github_token:
                    raw_headers += ["-H", f"Authorization: Bearer {github_token}"]

                raw_cmd = ["curl", "-s", "--max-time", "10"] + raw_headers + [file_api_url]

                # Capture variabel loop secara eksplisit dengan default argument
                raw_proses = await loop.run_in_executor(
                    None,
                    lambda cmd=raw_cmd: subprocess.run(
                        cmd, capture_output=True, text=True, timeout=15
                    ),
                )

                if raw_proses.returncode == 0 and raw_proses.stdout:
                    raw_code = raw_proses.stdout[:4000]
                    res += f"--- FULL/RAW FILE: {repo_name}/{file_name} ---\n{raw_code}\n\n"

            return res if "---" in res else ""

        except Exception as exc:
            console_terminal_interface.print(
                f"[dim yellow][GitHub Code Search] Exception: {exc}[/dim yellow]"
            )
        return ""

    # ------------------------------------------------------------------
    # GITHUB REPOSITORY SEARCH  (tidak butuh autentikasi)
    # ------------------------------------------------------------------
    @staticmethod
    async def search_github_repositories(query: str) -> str:
        """
        Cari repository Roblox/Luau di GitHub menggunakan Repositories Search API.
        Tidak membutuhkan autentikasi, cocok sebagai fallback.
        Hasilkan README + deskripsi sebagai konteks untuk AI.
        """
        github_token = _get_github_token()
        try:
            encoded_query = query.replace(" ", "+")
            url = (
                f"https://api.github.com/search/repositories"
                f"?q={encoded_query}+roblox+language:lua&per_page=3&sort=stars"
            )

            headers_list = [
                "-H", "Accept: application/vnd.github.v3+json",
                "-H", "User-Agent: NexusAgent/2.0",
            ]
            if github_token:
                headers_list += ["-H", f"Authorization: Bearer {github_token}"]

            command = ["curl", "-s", "--max-time", "15"] + headers_list + [url]

            loop = asyncio.get_running_loop()
            proses = await loop.run_in_executor(
                None,
                lambda: subprocess.run(command, capture_output=True, text=True, timeout=20),
            )

            if proses.returncode != 0 or not proses.stdout:
                return ""

            data = json.loads(proses.stdout)
            repos = data.get("items", [])[:3]
            if not repos:
                return ""

            res = "GITHUB ROBLOX REPOSITORIES (Stars & Description):\n"
            for repo in repos:
                full_name = repo.get("full_name", "")
                description = repo.get("description", "")[:200]
                stars = repo.get("stargazers_count", 0)
                html_url = repo.get("html_url", "")

                # Coba fetch README untuk konteks lebih kaya
                readme_url = f"https://api.github.com/repos/{full_name}/readme"
                readme_cmd = ["curl", "-s", "--max-time", "10"] + headers_list + [readme_url]

                readme_proses = await loop.run_in_executor(
                    None,
                    lambda cmd=readme_cmd: subprocess.run(
                        cmd, capture_output=True, text=True, timeout=12
                    ),
                )

                readme_text = ""
                if readme_proses.returncode == 0 and readme_proses.stdout:
                    try:
                        readme_data = json.loads(readme_proses.stdout)
                        content_b64 = readme_data.get("content", "")
                        if content_b64:
                            readme_text = base64.b64decode(
                                content_b64.replace("\n", "")
                            ).decode("utf-8", errors="ignore")[:1500]
                    except Exception:
                        pass

                res += (
                    f"--- REPO: {full_name} ({stars}⭐) ---\n"
                    f"URL: {html_url}\n"
                    f"DESC: {description}\n"
                )
                if readme_text:
                    res += f"README:\n{readme_text}\n"
                res += "\n"

            return res

        except Exception as exc:
            console_terminal_interface.print(
                f"[dim yellow][GitHub Repo Search] Exception: {exc}[/dim yellow]"
            )
        return ""

    # ------------------------------------------------------------------
    # GITHUB TOPICS SEARCH  (mencari repo berdasarkan topik)
    # ------------------------------------------------------------------
    @staticmethod
    async def search_github_topics(topic: str) -> str:
        """
        Cari repository berdasarkan GitHub Topic (misal: roblox, luau, rojo).
        Berguna untuk menemukan library/framework populer.
        """
        github_token = _get_github_token()
        try:
            encoded_topic = topic.replace(" ", "-").lower()
            url = (
                f"https://api.github.com/search/repositories"
                f"?q=topic:{encoded_topic}+language:lua&per_page=3&sort=stars"
            )

            headers_list = [
                "-H", "Accept: application/vnd.github.v3+json",
                "-H", "User-Agent: NexusAgent/2.0",
            ]
            if github_token:
                headers_list += ["-H", f"Authorization: Bearer {github_token}"]

            command = ["curl", "-s", "--max-time", "15"] + headers_list + [url]

            loop = asyncio.get_running_loop()
            proses = await loop.run_in_executor(
                None,
                lambda: subprocess.run(command, capture_output=True, text=True, timeout=20),
            )

            if proses.returncode != 0 or not proses.stdout:
                return ""

            data = json.loads(proses.stdout)
            repos = data.get("items", [])[:3]
            if not repos:
                return ""

            res = f"GITHUB TOPIC [{topic.upper()}] TOP REPOSITORIES:\n"
            for repo in repos:
                full_name = repo.get("full_name", "")
                description = repo.get("description", "")[:200]
                stars = repo.get("stargazers_count", 0)
                res += f"  ⭐ {stars:>5} | {full_name}: {description}\n"

            return res + "\n"

        except Exception as exc:
            console_terminal_interface.print(
                f"[dim yellow][GitHub Topics] Exception: {exc}[/dim yellow]"
            )
        return ""

    # ------------------------------------------------------------------
    # DEVFORUM SEARCH
    # ------------------------------------------------------------------
    @staticmethod
    async def search_devforum(query: str) -> str:
        try:
            encoded_query = query.replace(" ", "+")
            url = f"https://devforum.roblox.com/search/query.json?q={encoded_query}"

            command = [
                "curl", "-s", "--max-time", "15",
                "-H", "User-Agent: NexusAgent/2.0",
                url,
            ]
            loop = asyncio.get_running_loop()
            proses = await loop.run_in_executor(
                None,
                lambda: subprocess.run(command, capture_output=True, text=True, timeout=20),
            )
            if proses.returncode == 0 and proses.stdout:
                data = json.loads(proses.stdout)
                posts = data.get("posts", [])[:2]
                if posts:
                    res = "ROBLOX DEVFORUM SOLUTIONS (FULL CODE BLOCKS):\n"
                    for p in posts:
                        post_id = p.get("id")
                        if post_id:
                            raw_post_url = f"https://devforum.roblox.com/posts/{post_id}.json"
                            raw_cmd = [
                                "curl", "-s", "--max-time", "10",
                                "-H", "User-Agent: NexusAgent/2.0",
                                raw_post_url,
                            ]
                            raw_proses = await loop.run_in_executor(
                                None,
                                lambda cmd=raw_cmd: subprocess.run(
                                    cmd, capture_output=True, text=True, timeout=15
                                ),
                            )
                            if raw_proses.returncode == 0 and raw_proses.stdout:
                                try:
                                    post_data = json.loads(raw_proses.stdout)
                                    raw_text = post_data.get("raw", "")[:2000]
                                    res += f"--- DEVFORUM RAW POST ---\n{raw_text}\n\n"
                                except Exception:
                                    pass
                    return res
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------
    # REDDIT SEARCH
    # ------------------------------------------------------------------
    @staticmethod
    async def search_reddit_robloxdev(query: str) -> str:
        try:
            encoded_query = query.replace(" ", "+")
            url = f"https://www.reddit.com/r/robloxdev/search.json?q={encoded_query}&restrict_sr=1&limit=3"
            command = [
                "curl", "-s", "--max-time", "15",
                "-H", "User-Agent: NexusAgent/2.0",
                url,
            ]
            loop = asyncio.get_running_loop()
            proses = await loop.run_in_executor(
                None,
                lambda: subprocess.run(command, capture_output=True, text=True, timeout=20),
            )
            if proses.returncode == 0 and proses.stdout:
                data = json.loads(proses.stdout)
                posts = data.get("data", {}).get("children", [])[:3]
                if posts:
                    res = "REDDIT r/robloxdev DISCUSSIONS:\n"
                    for p in posts:
                        post_data = p.get("data", {})
                        title = post_data.get("title", "")
                        body_text = post_data.get("selftext", "")[:600]
                        res += f"--- DISCUSSION: {title} ---\n{body_text}...\n\n"
                    return res
        except Exception:
            pass
        return ""


async def execute_gemini_cli_pure(agent: dict, system_instruction: str, prompt_payload: str) -> Tuple[bool, str]:
    """
    EKSEKUTOR MUTLAK SEQUENTIAL (File-to-File IPC): 100% Native CLI Execution.
    Menggunakan Gemma 4 31B IT sebagai mesin utama dengan parameter bypass performa.
    Batas waktu eksekusi: 30 Menit (1800 detik).
    """
    async with CLI_EXECUTION_SEMAPHORE:
        api_key = _key_rotator.get_key()
        if not api_key:
            return False, "API_KEY_KOSONG"

        unique_session_id = uuid.uuid4().hex
        temp_home_dir = os.path.join(TEMP_IO_DIRECTORY, f"gemini_cli_home_{unique_session_id}")

        try:
            os.makedirs(temp_home_dir, exist_ok=True)
            os.makedirs(os.path.join(temp_home_dir, ".gemini"), exist_ok=True)

            prompt_filepath = os.path.join(temp_home_dir, "input_prompt.txt")
            output_filepath = os.path.join(temp_home_dir, "output_response.txt")

            env_vars = os.environ.copy()
            env_vars["GEMINI_API_KEY"] = api_key
            env_vars["CI"] = "true"
            env_vars["TERM"] = "dumb"
            env_vars["NO_COLOR"] = "1"
            env_vars["HOME"] = temp_home_dir

            schema_enforcement = (
                "WAJIB OUTPUT JSON MURNI DENGAN SALAH SATU DARI 2 FORMAT BERIKUT INI (PILIH SALAH SATU):\n\n"
                "PILIHAN 1: JIKA INGIN MEMANGGIL MCP TOOL UNTUK DEBUGGING STUDIO:\n"
                '{"mcp_tool_call": {"tool_name": "start_playtest", "args": {}}}\n'
                '{"mcp_tool_call": {"tool_name": "read_logs", "args": {}}}\n'
                '{"mcp_tool_call": {"tool_name": "edit_script", "args": {"script_path": "...", "new_code": "..."}}}\n\n'
                "PILIHAN 2: JIKA SUDAH SELESAI DAN INGIN MEMBERIKAN KODE LUAU FINAL:\n"
                '{"luau_code_payload": "string kode luau murni"}'
            )

            full_payload = (
                f"[SYSTEM INSTRUCTION]:\n{system_instruction}\n\n"
                f"[WAJIB OUTPUT JSON MURNI]:\n{schema_enforcement}\n\n"
                f"[PROMPT TASK]:\n{prompt_payload}"
            )

            with open(prompt_filepath, "w", encoding="utf-8") as f:
                f.write(full_payload)

            model_candidates = [
                "models/gemma-4-31b-it",
                "models/gemma-4-26b-a4b-it",
                "models/gemini-3.1-flash-lite-preview",
                "models/gemini-2.0-flash",
            ]

            last_error = ""
            for model_name in model_candidates:
                try:
                    with open(prompt_filepath, "r", encoding="utf-8") as f:
                        prompt_content = f.read()

                    command = [
                        GEMINI_CLI_PATH,
                        "-m", model_name,
                        "-y",
                        "-p", "Baca seluruh data instruksi dari stdin. Keluarkan JSON murni.",
                    ]

                    process = await asyncio.create_subprocess_exec(
                        *command,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env_vars,
                        start_new_session=True,
                    )

                    try:
                        stdout_data, stderr_data = await asyncio.wait_for(
                            process.communicate(input=prompt_content.encode("utf-8")),
                            timeout=1800.0,
                        )
                    except asyncio.TimeoutError:
                        try:
                            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                        except (OSError, ProcessLookupError):
                            pass
                        try:
                            await asyncio.wait_for(process.communicate(), timeout=5.0)
                        except asyncio.TimeoutError:
                            pass
                        last_error = f"API Timeout 1800s ({model_name})."
                        continue

                    if process.returncode != 0:
                        error_details = stderr_data.decode("utf-8", errors="ignore").strip().lower()
                        if "429" in error_details or "quota" in error_details or "exhausted" in error_details or "rate" in error_details:
                            _key_rotator.mark_rate_limited(api_key)
                            return False, "RATE_LIMIT_REACHED"
                        last_error = f"CLI_ERROR ({model_name}): {error_details[:300]}"
                        continue

                    raw_output = stdout_data.decode("utf-8", errors="ignore")

                    with open(output_filepath, "w", encoding="utf-8") as f:
                        f.write(raw_output)

                    json_str = ""
                    markdown_match = re.search(
                        f'{MARKDOWN_BLOCK}(?:json)?\n(.*?)\n{MARKDOWN_BLOCK}',
                        raw_output, re.DOTALL | re.IGNORECASE,
                    )
                    if markdown_match:
                        json_str = markdown_match.group(1).strip()
                    else:
                        start_idx = raw_output.find('{')
                        end_idx = raw_output.rfind('}')
                        if start_idx != -1 and end_idx != -1 and end_idx >= start_idx:
                            json_str = raw_output[start_idx:end_idx + 1]

                    if json_str:
                        try:
                            parsed = json.loads(json_str, strict=False)
                            if "luau_code_payload" in parsed:
                                code = parsed["luau_code_payload"]
                                if code:
                                    return True, extract_pure_luau_code(code)
                            elif "mcp_tool_call" in parsed:
                                return True, json.dumps(parsed)
                        except Exception:
                            pass

                    last_error = f"JSON_PARSE_ERROR ({model_name}): Output rusak atau tidak sesuai skema.\nRaw: {raw_output[:200]}..."
                    continue

                except FileNotFoundError:
                    return False, "GEMINI_CLI_NOT_FOUND: CLI tidak ditemukan."
                except Exception as e:
                    last_error = f"SYSTEM_EXCEPTION ({model_name}): {str(e)}"
                    continue

            return False, last_error

        finally:
            if os.path.exists(temp_home_dir):
                shutil.rmtree(temp_home_dir, ignore_errors=True)


class AutoHealerAgent:
    def __init__(self):
        self.sys_inst = (
            "<|think|>\n"
            "BERPIKIRLAH SECARA MENDALAM DAN EKSTENSIF (REASON LONGER) SEBELUM MENJAWAB! Evaluasi setiap kemungkinan kesalahan kode sebelum Anda memperbaikinya. "
            "Anda adalah Ahli Bedah Kode Level Master dengan standar militer. "
            "TUGAS MUTLAK: Perbaiki kode Luau yang rusak berdasarkan error dari compiler. "
            "WAJIB: Searching github untuk mengetahui kode lua nya eror atau tidak. "
            "Terapkan pengujian tingkat militer sehingga kode perbaikan Anda 99% tidak mungkin eror.\n"
            "KEISTIMEWAAN MCP: Jika Anda kebingungan atas logika error, Anda memiliki akses MCP. Anda BISA memanggil Tool JSON untuk 'start_playtest', membaca log, dan berinteraksi dengan Studio."
        )
        self.heal_history: dict = {}
        self._project_context_cache: str = ""
        self._github_hitbox_context: str = ""

    def _analyze_error_type(self, error_msg: str) -> str:
        error_lower = error_msg.lower()
        if "but got" in error_lower or "expected" in error_lower:
            return "TYPE_MISMATCH"
        elif "unknown" in error_lower and ("global" in error_lower or "type" in error_lower):
            return "UNDEFINED_REFERENCE"
        elif "syntax" in error_lower or "unexpected symbol" in error_lower:
            return "SYNTAX_ERROR"
        elif "cannot assign" in error_lower or "function only returns" in error_lower:
            return "ASSIGNMENT_ERROR"
        elif "unknown property" in error_lower or "not found" in error_lower:
            return "PROPERTY_ERROR"
        else:
            return "GENERIC_ERROR"

    def _generate_fix_guidance(self, error_msg: str, error_type: str) -> str:
        base_searching = (
            "WAJIB SEARCHING SEBELUM FIX:\n"
            "- Wajib Searching github untuk mengetahui kode lua nya eror atau tidak\n"
            "- Reddit: r/robloxdev, r/lua untuk solutions serupa\n\n"
        )
        guidance = {
            "TYPE_MISMATCH": base_searching + "- Tambahkan type casting: `x as Y` atau `tostring()`.\n- Pastikan semua operands punya type yang compatible.",
            "UNDEFINED_REFERENCE": base_searching + "- Cek: apakah sudah di-require? apakah ada typo?\n- Untuk Roblox API: gunakan yang ada di ecosystem context.",
            "SYNTAX_ERROR": base_searching + "- Luau strict mode: semua variable harus declared dengan local/const.",
            "ASSIGNMENT_ERROR": base_searching + "- Fix: gunakan temp variable, atau ubah type target.",
            "PROPERTY_ERROR": base_searching + "- Untuk Roblox Instance: gunakan GetChildren(), FindFirstChild() dengan benar.",
            "GENERIC_ERROR": base_searching + "- Bacalah error message dengan teliti, cari highlight line number.",
        }
        return guidance.get(error_type, guidance["GENERIC_ERROR"])

    async def heal_code(
        self,
        broken_code: str,
        compiler_error: str,
        module_name: str,
        agent: dict,
        task_description: str = "",
        ecosystem_context: str = "",
        previous_error: str = "",
        target_filepath: str = "",
    ) -> str:
        last_error_line = compiler_error.splitlines()[-1] if compiler_error else "Unknown"
        error_type = self._analyze_error_type(compiler_error)

        if module_name not in self.heal_history:
            self.heal_history[module_name] = []
        self.heal_history[module_name].append(error_type)

        console_terminal_interface.print(
            f"[bold magenta]   [Auto-Healer] Membedah {module_name} ({error_type}): {last_error_line}[/bold magenta]"
        )

        safe_broken_code = extract_pure_luau_code(broken_code)
        fix_guidance = self._generate_fix_guidance(compiler_error, error_type)

        base_prompt = (
            f"[ERROR CLASSIFICATION]: {error_type}\n"
            f"[ERROR MESSAGE COMPILER LUNE/ROJO]:\n{compiler_error}\n\n"
            f"[RECOMMENDED FIX STRATEGY]:\n{fix_guidance}\n\n"
        )
        if ecosystem_context:
            base_prompt += f"[MODUL ECOSYSTEM REFERENCE UNTUK IMPORT/REQUIRE]:\n{ecosystem_context}\n\n"

        console_terminal_interface.print(f"[dim cyan]   🔍 Menjalankan RAG Pipeline...[/dim cyan]")
        clean_error_q = LuauKnowledgeScraper._clean_error_query(compiler_error)
        clean_task_name = LuauKnowledgeScraper._clean_task_query(module_name)
        combined_query = f"{clean_task_name} {clean_error_q}"[:80]

        github_context = await LuauKnowledgeScraper.search_github_luau(combined_query)
        devforum_context = await LuauKnowledgeScraper.search_devforum(combined_query)
        reddit_context = await LuauKnowledgeScraper.search_reddit_robloxdev(combined_query)

        if github_context or devforum_context or reddit_context:
            base_prompt += "[KNOWLEDGE BASE (HASIL SCRAPING GITHUB RAW, DEVFORUM & REDDIT)]\n"
            if github_context:
                base_prompt += github_context + "\n"
            if devforum_context:
                base_prompt += devforum_context + "\n"
            if reddit_context:
                base_prompt += reddit_context + "\n"
            base_prompt += "DOKTRIN ADAPTASI: 1. Filter Standalone. 2. Musnahkan ID Aset. 3. Jangan salin bulat-bulat, adaptasikan!\n\n"

        base_prompt += (
            f"[KODE YANG RUSAK]:\n{MARKDOWN_BLOCK}lua\n{safe_broken_code}\n{MARKDOWN_BLOCK}\n\n"
            f"[INSTRUKSI BEDAH MUTLAK]:\n"
            f"1. Identifikasi EXACT baris penyebab error.\n"
            f"2. Pahami root cause dari error type '{error_type}'.\n"
            f"3. Ubah HANYA baris yang rusak tersebut. Jika perlu dirombak, rombak secara logis.\n"
            f"4. Pastikan semua variabel dan anotasi tipe (strict mode luau) 99% tidak mungkin eror.\n"
            f"5. Wajib mengembalikan file utuh (bukan diff) setelah diperbaiki.\n\n"
        )

        mcp_history_log = ""
        max_mcp_turns = 4 if ROBLOX_MCP_URL else 1

        for turn in range(max_mcp_turns):
            if ROBLOX_MCP_URL:
                console_terminal_interface.print(
                    f"[dim yellow]   [Iterative Debugging] Turn {turn+1}/{max_mcp_turns}...[/dim yellow]"
                )

            dynamic_prompt = base_prompt
            if mcp_history_log:
                dynamic_prompt += f"\n\n[RIWAYAT MCP TOOL EXECUTION (STUDIO LIVE)]:\n{mcp_history_log}\nPelajari log ini sebelum bertindak!"

            success, result_data = await execute_gemini_cli_pure(agent, self.sys_inst, dynamic_prompt)

            if not success:
                console_terminal_interface.print(f"[bold red]   [Aider CLI Error] {result_data[:200]}[/bold red]")
                return broken_code

            if '"mcp_tool_call"' in result_data:
                try:
                    tool_data = json.loads(result_data)["mcp_tool_call"]
                    tool_name = tool_data.get("tool_name", "unknown")
                    tool_args = tool_data.get("args", {})

                    console_terminal_interface.print(
                        f"[bold cyan]   🛠️ [MCP Action] AI Menjalankan Studio Tool: {tool_name}[/bold cyan]"
                    )

                    tool_response = await RobloxMCPBridge.execute_tool(tool_name, tool_args)
                    mcp_history_log += (
                        f"\n--- CALL: {tool_name} ---\n"
                        f"ARGS: {json.dumps(tool_args)}\n"
                        f"RESULT: {tool_response[:1000]}\n"
                    )
                    continue
                except Exception as e:
                    mcp_history_log += f"\n--- CALL FAILED ---\nERROR: {str(e)}\n"
                    continue
            else:
                return extract_pure_luau_code(result_data)

        return broken_code

    async def initialize_and_scan(self) -> None:
        """
        Membaca direktori proyek (FantasyExtraction_Roblox_TrueApex) sebelum
        memulai pekerjaan apapun untuk memahami modul yang sudah/belum dibangun.
        Dipanggil saat pertama kali sistem dijalankan dan sebelum publish ke Roblox Creator API.
        """
        console_terminal_interface.print(
            "[bold cyan][Healer Init] Memindai konteks proyek & referensi GitHub sebelum bekerja...[/bold cyan]"
        )
        try:
            _github_hitbox = await search_github_for_hitbox_armor()
            _project_ctx = await scan_existing_project()
            self._project_context_cache = _project_ctx
            self._github_hitbox_context = _github_hitbox
            console_terminal_interface.print(
                "[bold green][Healer Init] ✅ Scan proyek selesai. AI siap dengan konteks penuh.[/bold green]"
            )
            # Periksa & hapus file Lua lama yang isinya salah/tidak lengkap
            _repair_report = await scan_and_repair_invalid_files()
            if _repair_report:
                console_terminal_interface.print(
                    f"[bold yellow][Healer Init] Laporan perapian file:\n{_repair_report}[/bold yellow]"
                )
        except Exception as _e:
            console_terminal_interface.print(
                f"[bold yellow][Healer Init] Scan dilewati (non-fatal): {_e}[/bold yellow]"
            )
            self._project_context_cache = ""
            self._github_hitbox_context = ""


class OmniSynthesizerAgent:
    def __init__(self, healer_agent: AutoHealerAgent):
        self.healer_agent = healer_agent
        self.sys_inst = (
            "<|think|>\n"
            "BERPIKIRLAH SECARA MENDALAM DAN EKSTENSIF (REASON LONGER) SEBELUM MENJAWAB! Evaluasi setiap elemen fisika dan arsitektur sebelum Anda menulis kode. "
            "Anda adalah Arsitek Penyatuan Multiverse Luau tingkat militer. Tulis kode Luau Murni. "
            "PROTOKOL MUTLAK: Wajib Searching github untuk mengetahui kode lua nya eror atau tidak "
            "dan analisis menggunakan lune secara internal sebelum diberikan pada saya. "
            "Anda harus menerapkan pengujian tingkat militer di logika Anda sehingga "
            "kode yang dibuat 99% tidak mungkin eror. "
            "Wajib --!strict. Fokus pada efisiensi matematika dan pencegahan memory leak."
            "KESADARAN TIPE ASET: Baca nama task dengan cermat sebelum menulis kode. "
            "Jika nama task mengandung kata GUI/UI/HUD/MENU/SCREEN/INVENTORY/SHOP/BUTTON/FRAME/HOTBAR/COMPASS/MINIMAP/SCOREBOARD/HEALTH_BAR/STAMINA_BAR → "
            "kamu sedang membuat UI Roblox (LocalScript di dalam ScreenGui). "
            "Jika nama task mengandung kata MODEL/PROP/BUILDING/TREE/ROCK/VEHICLE/WEAPON_MODEL/ARMOR_MODEL/CHEST/BARREL/CRATE/DOOR → "
            "kamu sedang membuat logika 3D Model (Script di dalam Model di Workspace). "
            "Jika nama task mengandung kata WORLD/TERRAIN/LIGHTING/ATMOSPHERE/SKYBOX/SPAWN/ZONE/MAP/AMBIENT/FOG → "
            "kamu sedang membuat script lingkungan dunia (Script di ServerScriptService atau Workspace). "
            "Kode Luau yang kamu hasilkan akan OTOMATIS dibungkus ke file .rbxmx oleh Asset Engine. "
            "Tugas kamu: tulis HANYA logika Luau murni yang sesuai tipe task tersebut."
        )

    async def synthesize_handoff(
        self,
        agent: dict,
        target_filepath: str,
        module_name: str,
        task_description: str,
        req_keys: list,
        forb_keys: list,
        previous_error: str,
        previous_code: str,
    ) -> Tuple[bool, str, str]:
        comprehensive_prompt = (
            f"[KEYWORD WAJIB (SYARAT LULUS COMPILER)]: Anda HARUS menggunakan keyword/fungsi berikut dalam skrip Anda: {', '.join(req_keys) if req_keys else 'Tidak ada keyword khusus'}\n"
            f"[KEYWORD HARAM (AKAN DITOLAK COMPILER)]: JANGAN PERNAH menggunakan keyword berikut: {', '.join(forb_keys) if forb_keys else 'Tidak ada batasan khusus'}\n\n"
            f"[CHEAT SHEET 14 TITIK KEAMANAN MILITER (WAJIB DIPATUHI OLEH SYNTHESIZER!)]\n"
            f"01. Baris pertama skrip WAJIB mendeklarasikan `--!strict`.\n"
            f"02. HARAM & DILARANG KERAS menggunakan: `_G`, `shared`, `loadstring`, `getfenv`, `spawn()`, `delay()`.\n"
            f"03. JIKA ada loop `while true do`, Anda WAJIB mendefinisikan `local RunService = game:GetService(\"RunService\")` dan menggunakan `RunService.Heartbeat:Wait()` atau `task.wait()`.\n"
            f"04. ANTI-MEMORY LEAK: Setiap koneksi event WAJIB disimpan ke variabel!\n"
            f"05. FAULT-TOLERANCE: Operasi `GetAsync`, `SetAsync`, dll WAJIB 100% dibungkus dalam `pcall()`.\n"
            f"06. ZERO-TRUST EXPLOIT: Jika Anda membuat `.OnServerEvent:Connect`, Anda WAJIB memvalidasi variabel dari client menggunakan `typeof()`!\n\n"
            f"07. ATURAN ASET GUI (jika nama task = GUI/UI/HUD/MENU/SCREEN/INVENTORY dll):\n"
            f"    → Tulis kode sebagai LocalScript. Gunakan: game:GetService(\"Players\").LocalPlayer, "
            f"PlayerGui, ScreenGui, Frame, TextLabel, TextButton, ImageLabel, UDim2, Color3.\n"
            f"    → WAJIB membuat fungsi update UI dan menghubungkan ke event (misalnya RemoteEvent, ValueChanged).\n"
            f"    → DILARANG: game:GetService(\"Players\") tanpa .LocalPlayer di LocalScript.\n\n"
            f"08. ATURAN ASET MODEL/PROP (jika nama task = MODEL/PROP/BUILDING/TREE/ROCK/VEHICLE dll):\n"
            f"    → Tulis kode sebagai Script (bukan LocalScript). Parent akan menjadi Model di Workspace.\n"
            f"    → Gunakan: script.Parent untuk mengakses Model, weld/constraint untuk fisika bagian.\n"
            f"    → WAJIB: CanCollide = true pada semua BasePart, Anchored sesuai kebutuhan.\n\n"
            f"09. ATURAN ASET WORLD/LINGKUNGAN (jika nama task = WORLD/TERRAIN/LIGHTING/ATMOSPHERE dll):\n"
            f"    → Tulis kode sebagai Script yang jalan di server. Gunakan: game:GetService(\"Lighting\"), "
            f"game:GetService(\"TweenService\"), workspace.Terrain.\n"
            f"    → WAJIB menggunakan RunService.Heartbeat atau task.wait() untuk loop, BUKAN while true do tanpa wait.\n\n"
            f"10. VALIDASI ASSET ENGINE: Kode yang kamu hasilkan akan diuji validator XML dan remodel headless. "
            f"Pastikan --!strict di baris pertama, tidak ada syntax error, dan semua service di-GetService dengan benar.\n\n"
            "[INSTRUKSI TUGAS KHUSUS]:\n"
        )

        ecosystem_context = await retrieve_ecosystem_context()
        if ecosystem_context:
            comprehensive_prompt += f"[REFERENSI MODUL GLOBAL UNTUK REQUIRE()]:\n{ecosystem_context}\n\n"
        comprehensive_prompt += f"[INSTRUKSI TUGAS KHUSUS ({module_name})]:\n{task_description}\n\n"

        # Injeksi wajib HitboxSeparation untuk modul yang membutuhkannya
        _needs_hitbox = (
            any(_kw in module_name.upper() for _kw in ["ARMOR", "HELMET", "WEAPON", "FURNITURE", "BIOME", "TREE", "ROCK", "BUILDING"])
            or "HitboxSeparation" in req_keys
        )
        if _needs_hitbox:
            _hitbox_template = get_armor_hitbox_mandatory_template()
            comprehensive_prompt += _hitbox_template
            try:
                _github_ref = await search_github_for_hitbox_armor()
                if _github_ref:
                    comprehensive_prompt += _github_ref
            except Exception:
                pass
            console_terminal_interface.print(
                f"[bold magenta]  [HITBOX INJECT] Template HitboxSeparation + GitHub disuntikkan untuk: {module_name}[/bold magenta]"
            )

        if previous_error and previous_code:
            safe_code = extract_pure_luau_code(previous_code)
            comprehensive_prompt += (
                f"[CRITICAL ERROR DARI AGEN SEBELUMNYA - PERBAIKI MATEMATIS]:\n"
                f"{MARKDOWN_BLOCK}lua\n{safe_code}\n{MARKDOWN_BLOCK}\n"
                f"[ERROR LOG DARI COMPILER]:\n{previous_error}\n\n"
            )

        console_terminal_interface.print(
            f"[bold cyan]  [{agent['name']}] Memproses {module_name}... (Antri Sequential - Standar Militer)[/bold cyan]"
        )

        console_terminal_interface.print(
            f"[dim cyan]  🔍 Menjalankan RAG Pipeline: Membaca Kitab DevForum & Ekstrak Raw GitHub...[/dim cyan]"
        )
        clean_task_query = LuauKnowledgeScraper._clean_task_query(module_name)
        github_context = await LuauKnowledgeScraper.search_github_luau(clean_task_query)

        # Jika GitHub Code Search gagal, coba Repository Search
        if not github_context:
            github_context = await LuauKnowledgeScraper.search_github_repositories(clean_task_query)

        devforum_context = await LuauKnowledgeScraper.search_devforum(clean_task_query)
        reddit_context = await LuauKnowledgeScraper.search_reddit_robloxdev(clean_task_query)

        if github_context or devforum_context or reddit_context:
            live_rag_data = "[KNOWLEDGE BASE (HASIL SCRAPING GITHUB RAW, DEVFORUM & REDDIT)]\n"
            if github_context:
                live_rag_data += github_context + "\n"
            if devforum_context:
                live_rag_data += devforum_context + "\n"
            if reddit_context:
                live_rag_data += reddit_context + "\n"
            live_rag_data += "GUNAKAN TEKS KODE DAN DISKUSI DI ATAS SEBAGAI INSPIRASI/CONTEKAN CARA MENYELESAIKAN TUGAS INI.\n"
            comprehensive_prompt += live_rag_data
            console_terminal_interface.print(f"[dim green]  ✅ DevForum dan Raw GitHub disuntikkan ke prompt.[/dim green]")

        success, result_data = await execute_gemini_cli_pure(agent, self.sys_inst, comprehensive_prompt)

        if success:
            code_attempt = result_data
            if '"mcp_tool_call"' in code_attempt:
                return False, "Agent Error: Synthesizer tidak boleh memanggil Tool MCP. Hanya Healer yang diizinkan.", previous_code

            if previous_code and previous_error:
                safe_prev_code = extract_pure_luau_code(previous_code)
                similarity = difflib.SequenceMatcher(None, safe_prev_code, code_attempt).ratio()

                if similarity < 0.15:
                    console_terminal_interface.print(
                        f"[bold red]  [SANITY CHECK GAGAL]: Kode baru hanya {similarity*100:.1f}% mirip. File terindikasi kosong/halusinasi. DITOLAK.[/bold red]"
                    )
                    return False, f"SANITY_CHECK_FAILED: Kode baru terlalu berbeda ({similarity*100:.1f}% similarity).", previous_code

            omni_ok, omni_msg = AbsoluteOmniValidator.execute_validation(code_attempt, req_keys, forb_keys)
            if not omni_ok:
                console_terminal_interface.print(f"[bold red]  [OmniValidator] {omni_msg[:200]}[/bold red]")
                return False, omni_msg, code_attempt

            ast_ok, ast_msg = await NativeLuauCompiler.execute_native_ast_verification(code_attempt, module_name)
            if not ast_ok:
                console_terminal_interface.print(f"[bold yellow]  [AST] {ast_msg[:200]}[/bold yellow]")
                healed_code = await self.healer_agent.heal_code(
                    code_attempt, ast_msg, module_name, agent,
                    task_description=task_description,
                    ecosystem_context=ecosystem_context,
                    target_filepath=target_filepath,
                )

                healed_omni_ok, healed_omni_msg = AbsoluteOmniValidator.execute_validation(healed_code, req_keys, forb_keys)
                if not healed_omni_ok:
                    return False, healed_omni_msg, healed_code

                healed_ast_ok, healed_ast_msg = await NativeLuauCompiler.execute_native_ast_verification(healed_code, module_name)
                if not healed_ast_ok:
                    return False, healed_ast_msg, healed_code

                code_attempt = healed_code

            # Guard: pastikan target_filepath tidak kosong sebelum makedirs
            if not target_filepath:
                return False, "TARGET_FILEPATH_KOSONG: Tidak bisa menyimpan file tanpa path.", code_attempt
            parent_dir = os.path.dirname(target_filepath)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            with open(target_filepath, "w", encoding="utf-8") as f:
                f.write(code_attempt)

            await save_verified_module(module_name, target_filepath, code_attempt)
            console_terminal_interface.print(f"[bold green]  ✅ [{module_name}] SUKSES! Disimpan & Diverifikasi.[/bold green]")

            # === NEXUS ASSET ENGINE HOOK ===
            _asset_type = detect_asset_type(module_name)
            if _asset_type != "LUAU":
                try:
                    _asset_ok, _asset_path, _asset_err = await AssetOrchestrator.process_asset_task(module_name, code_attempt)
                    if _asset_ok:
                        console_terminal_interface.print(f"[bold green]  ✅ [Asset Engine] Aset tersimpan: {_asset_path}[/bold green]")
                    else:
                        if _asset_err != "BUKAN_ASET":
                            console_terminal_interface.print(f"[bold yellow]  ⚠️ [Asset Engine] {_asset_err[:150]}[/bold yellow]")
                except Exception as _ae:
                    console_terminal_interface.print(f"[dim yellow]  [Asset Engine] Exception (non-fatal): {_ae}[/dim yellow]")
            # === AKHIR ASSET ENGINE HOOK ===

            return True, "", code_attempt

        else:
            return False, result_data, previous_code
