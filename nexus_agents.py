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

CLI_EXECUTION_SEMAPHORE = asyncio.Semaphore(len(ACTIVE_AGENTS))  # ⚡ PARALLEL: Semua agent berjalan bersamaan

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
    code = re.sub(r'\n*\s*`{3}.*$', '', code, flags=re.DOTALL)
    return code.strip()


class RobloxMCPBridge:
    """
    Jembatan HTTP ke PC Lokal Anda (Roblox Studio MCP).
    Membypass bug enum API dengan menembak langsung JSON-RPC ke server.
    """
    def __init__(self, mcp_url: str):
        self.mcp_url = mcp_url

    async def execute_tool(self, tool_name: str, arguments: dict) -> str:
        if not self.mcp_url:
            return "ERROR: ROBLOX_MCP_URL tidak dikonfigurasi di VPS Anda."

        payload = {
            "jsonrpc": "2.0",
            "method": tool_name,
            "params": arguments,
            "id": 1,
        }

        def _post():
            try:
                res = requests.post(f"{self.mcp_url}/jsonrpc", json=payload, timeout=45)
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
                "models/gemma-4-26b-a4b-it",
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




# =====================================================================
# AGENT MEMORY + ANTIGRAVITY ORCHESTRATOR (PATCH BARU)
# =====================================================================

# =====================================================================
# [KEMAMPUAN BARU]: SAKELAR OVERRIDE PRIORITAS TELEGRAM (MUTEX LOCK)
# =====================================================================
# (Definisi NexusGlobalState dipindahkan ke bawah dengan atribut lengkap)





class AgentMemory:
    """Menyimpan riwayat percakapan untuk konteks multi-turn."""
    def __init__(self, max_history: int = 10):
        self._history = []
        self._max = max_history

    def add_user_message(self, msg: str):
        self._history.append({"role": "user", "content": msg})
        if len(self._history) > self._max * 2:
            self._history = self._history[-(self._max * 2):]

    def add_ai_message(self, msg: str):
        self._history.append({"role": "ai", "content": msg})

    def get_context_string(self) -> str:
        if not self._history:
            return "(Tidak ada riwayat sebelumnya)"
        lines = []
        for item in self._history[-10:]:
            role = "User" if item["role"] == "user" else "AI"
            lines.append(f"{role}: {item['content'][:300]}")
        return "\n".join(lines)


    def clear(self) -> None:
        """Hapus seluruh riwayat percakapan (mulai sesi baru)."""
        self._history.clear()


global_agent_memory = AgentMemory()


def inject_antigravity_laws(prompt: str) -> str:
    """Menyuntikkan hukum-hukum dasar AI ke setiap prompt.
    
    Logika digabungkan dari:
    - Aturan dasar AI (versi asli)
    - Hukum fisika Roblox (nexus_agents.py v1.1)
    - SafeSpawnOrchestrator.lua (spawn hierarchy, anti-void, ForceField)
    - SpaceshipSpawnFloor_DailyReward.lua (platform, daily reward, DataStore pcall)
    - HUDResponsiveInjector.lua (Offset->Scale, close button, ScrollingFrame)
    """
    laws = (
        "[HUKUM ANTIGRAVITY - WAJIB DIPATUHI]:\n"
        "1. Selalu berikan kode yang lengkap dan bisa langsung dijalankan.\n"
        "2. Tambahkan komentar singkat dan jelas.\n"
        "3. Jangan memberikan kode palsu atau placeholder.\n"
        "4. Gunakan best practice bahasa pemrograman yang diminta.\n"
        "---\n"
        "\n"
        "[HUKUM FISIKA ROBLOX - LEVEL 9 ABSOLUT]:\n"
        "5. PENEMPATAN PRESISI: WAJIB workspace:Raycast(). Cek raycastResult.Normal:Dot(Vector3.new(0,1,0)) > 0.8. Gunakan Model:PivotTo(CFrame).\n"
        "6. SPATIAL OVERLAP: Cek ruang kosong dengan workspace:GetPartBoundsInBox (OverlapParams Exclude ActiveMap).\n"
        "7. NETWORK OWNERSHIP: Panggil part:SetNetworkOwner(nil) untuk NPC. Matikan state FallingDown.\n"
        "8. CONTINUOUS RESPAWN: Saat monster mati (Humanoid.Died), gunakan task.delay(10, function() spawnNew() end). Hancurkan mayat dengan Debris.\n"
        "9. SIKLUS 2.5 JAM: Sebelum ActiveMap:Destroy(), hancurkan SeatWeld, set Sit=false, teleport pemain ke SpaceshipSpawnFloor.\n"
        "10. ANTI-LAG: Jika Position.Y < -50, hancurkan NPC dan/atau teleport pemain ke titik aman.\n"
        "11. DATASTORE SAFETY: WAJIB gunakan pcall() untuk SEMUA operasi DataStore. Selalu cek success dan log error.\n"
        "12. REMOTE EVENTS: Selalu validasi typeof() semua argumen dari RemoteEvent/RemoteFunction di sisi Server (zero-trust).\n"
        "---\n"
        "\n"
        "[HUKUM SAFE SPAWN ORCHESTRATOR - PRIORITAS SPAWN]:\n"
        "13. HIERARKI SPAWN: (1) SpaceshipSpawnFloor di Y=1000+ -> (2) NexusUniversalBaseplate -> (3) CFrame darurat (0,100,0).\n"
        "14. JEDA SPAWN: WAJIB task.wait(0.5) setelah CharacterAdded sebelum PivotTo(). Tanpa ini Roblox engine menimpa koordinat.\n"
        "15. FORCE FIELD: Beri ForceField Visible=false saat spawn, hapus via task.delay(5, ...) agar tidak mati karena glitch awal.\n"
        "16. STOP MOMENTUM: Set AssemblyLinearVelocity=Vector3.zero DAN AssemblyAngularVelocity=Vector3.zero sebelum PivotTo().\n"
        "17. ANTI-VOID MONITOR: Pantau Position.Y < -50 dengan RunService.Heartbeat. Jika terjatuh, teleport ulang ke titik aman.\n"
        "18. TELEPORT COOLDOWN: Minimal 0.5 detik antar teleportasi untuk mencegah spam. Gunakan tick() untuk tracking.\n"
        "19. PLATFORM SPACESHIP: Size = Vector3.new(200, 5, 200) lebih aman daripada 100x100. Anchored=true, Locked=true.\n"
        "---\n"
        "\n"
        "[HUKUM DAILY REWARD & DATASTORE - SAFE PATTERN]:\n"
        "20. JANGAN TIMPA CASH: Gunakan player:FindFirstChild('leaderstats') terlebih dulu. Hanya buat Folder jika belum ada.\n"
        "21. DAILY REWARD UTC: math.floor(os.time() / 86400) untuk mendapatkan hari UTC absolut (reset 00:00 UTC).\n"
        "22. SAVE PATTERN: pcall SetAsync saat PlayerRemoving. pcall GetAsync saat PlayerAdded. Selalu cek success.\n"
        "23. NOTIFY CLIENT: Gunakan RemoteEvent:FireClient() untuk notifikasi ke pemain setelah memberi reward.\n"
        "24. EXISTING PLAYER: Handle pemain yang sudah join saat script diload via Players:GetPlayers() loop.\n"
        "25. DUPLICATE CHECK: Cek workspace:FindFirstChild('SpaceshipSpawnFloor') sebelum membuat platform baru.\n"
        "---\n"
        "\n"
        "[HUKUM HUD RESPONSIF & UI INJECTION]:\n"
        "26. OFFSET KE SCALE: Jika GuiObject.Size.X.Offset > 0, konversi ke Scale: newScale = currentScale + (offset / parentAbsoluteSize).\n"
        "27. TOMBOL X CLOSE: Inject TextButton 'NexusAutoCloseButton' ke Frame Visible=true dengan BackgroundTransparency < 1.\n"
        "28. JANGAN INJECT SCROLLINGFRAME: Hanya Frame (bukan ScrollingFrame) yang mendapat tombol X close.\n"
        "29. ASPEK RATIO: UIAspectRatioConstraint (AspectRatio=1) pada tombol X agar selalu kotak di semua resolusi.\n"
        "30. ZINDEX: Tombol close wajib ZIndex = frame.ZIndex + 1 agar selalu tampil di atas konten frame.\n"
        "31. TOUCH EVENT: Daftarkan closeBtn.TouchTap:Connect() untuk support layar sentuh mobile.\n"
        "32. MIN FRAME SIZE: Jangan inject tombol X ke Frame dengan AbsoluteSize < 100x100 pixel (frame terlalu kecil).\n"
        "---\n"
        "\n"
        "[HUKUM ESTETIKA TINGGI - WAJIB DIPATUHI]:\n"
        "33. DILARANG menggunakan Part kotak atau silinder dasar untuk NPC, kendaraan, monster, atau objek dunia yang penting. "
        "WAJIB gunakan MeshPart, SpecialMesh, UnionOperation, atau kombinasi Part dengan bentuk kompleks.\n"
        "34. Gunakan material yang tepat (SmoothPlastic, Neon, Glass, Fabric, Metal, Wood, Granite, Slate) dan warna yang "
        "kontras serta sesuai tema game. Hindari warna abu-abu polos (BrickColor 'Medium stone grey') untuk objek utama.\n"
        "35. Untuk objek organik (monster, creature, karakter, NPC): PRIORITASKAN penggunaan MeshPart dengan MeshId yang "
        "sudah diunggah, atau SpecialMesh dengan MeshType yang sesuai bentuk (Sphere untuk tubuh bulat, Cylinder untuk "
        "tiang/kaki, Wedge untuk sirip/tanduk). Jika tidak ada mesh tersedia, buat kombinasi Part yang kompleks.\n"
        "36. Pertimbangkan pencahayaan dan efek visual: gunakan PointLight atau SpotLight untuk objek yang bersinar, "
        "ParticleEmitter untuk efek api/asap/cahaya, dan SelectionBox/Highlight untuk pembeda visual.\n"
        "---\n"
        "\n"
        "[HUKUM MESHPART & SPECIALMESH - PANDUAN TEKNIS WAJIB]:\n"
        "37. DETEKSI TIPE ASET: Jika nama task mengandung MESHPART/MESH_PART/SPECIALMESH/SPECIAL_MESH/CREATURE/MONSTER/"
        "NPC_MESH/ORGANIC_MESH → sistem akan otomatis memanggil SmartUIAssetSelector untuk memilih mesh terbaik dari katalog.\n"
        "38. PEMILIHAN MESH CERDAS: SmartUIAssetSelector mencocokkan kata kunci task dengan MESH_ASSET_CATALOG. "
        "Jika cocok ditemukan → MeshPart/SpecialMesh yang sesuai digunakan. "
        "Jika TIDAK cocok → AURA FALLBACK digunakan dengan PERINGATAN KERAS di log.\n"
        "39. AURA SEBAGAI PEMBEDA VISUAL TERAKHIR: Aura (ParticleEmitter + SelectionBox) hanya digunakan sebagai "
        "pembeda visual ketika benar-benar tidak ada MeshPart/SpecialMesh yang cocok. "
        "⚠️ PERINGATAN KERAS: Aura bukan pengganti mesh. Segera tambahkan rbxassetid yang tepat ke "
        "MESH_ASSET_CATALOG di nexus_asset_engine.py jika aura fallback sering muncul.\n"
        "40. JENIS AURA YANG TERSEDIA (hanya jika fallback): "
        "AuraHitam (shadow/evil), AuraMerah (api/agresi), AuraBiru (air/es/sihir), AuraHijau (racun/alam/heal), "
        "AuraUngu (chaos/mistis), AuraEmas (divine/legend), AuraPutih (cahaya/suci), AuraPelangi (omni-element).\n"
        "---\n"
    )
    return laws + prompt


async def decompose_complex_prompt(complex_prompt: str, model: str) -> list:
    context_history = global_agent_memory.get_context_string()
    planner_prompt = f"""
Anda Orchestrator AI. Pecah permintaan pengguna menjadi array JSON berisi sub-tugas.
Baca konteks ini jika pengguna memberi perintah lanjutan:
--- RIWAYAT PERCAKAPAN ---
{context_history}
--------------------------
Permintaan: {complex_prompt}
Output WAJIB JSON array murni tanpa format markdown.
"""
    command = [GEMINI_CLI_PATH, "generate", "--model", model, "--prompt", planner_prompt]
    try:
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            process.kill()
            console_terminal_interface.print("[bold red]Orchestrator timeout! Menggunakan prompt asli.[/bold red]")
            return [complex_prompt]
        if process.returncode == 0:
            clean_json = re.sub(r"```json\n|\n```|```", "", stdout.decode('utf-8').strip()).strip()
            try:
                task_list = json.loads(clean_json)
                if isinstance(task_list, list): return task_list
            except json.JSONDecodeError:
                pass
    except Exception:
        pass
    return [complex_prompt]


async def execute_antigravity_simple(prompt: str, model: str, max_retries: int = 3) -> str:
    """Eksekutor CLI sederhana untuk bot Telegram (tanpa agent dict)."""
    antigravity_prompt = inject_antigravity_laws(prompt)
    context_history = global_agent_memory.get_context_string()
    antigravity_prompt = f"Konteks Sebelumnya:\n{context_history}\n\nInstruksi:\n{antigravity_prompt}"

    if "WEAPON_CUSTOMIZATION_ENGINE" in prompt or "Armor" in prompt:
        antigravity_prompt += "\n\n[FATAL SYSTEM DIRECTIVE]: WAJIB sertakan '-- HitboxSeparation' di awal file."

    # Deteksi keyword spawn/teleport -> inject aturan SafeSpawnOrchestrator
    if any(kw in prompt for kw in ["Spawn", "spawn", "CharacterAdded", "Teleport", "PivotTo", "SpaceshipFloor"]):
        antigravity_prompt += "\n\n[SPAWN DIRECTIVE]: WAJIB task.wait(0.5) setelah CharacterAdded. Gunakan hierarki: SpaceshipSpawnFloor -> NexusUniversalBaseplate -> CFrame(0,100,0). Set AssemblyLinearVelocity=Vector3.zero sebelum PivotTo()."

    # Deteksi keyword DataStore -> inject aturan safe DataStore
    if any(kw in prompt for kw in ["DataStore", "datastore", "GetAsync", "SetAsync", "DailyReward"]):
        antigravity_prompt += "\n\n[DATASTORE DIRECTIVE]: WAJIB pcall() untuk SEMUA DataStore:GetAsync() dan DataStore:SetAsync(). Cek pemain masih online sebelum memberi reward."

    # Deteksi keyword HUD/UI -> inject aturan HUDResponsiveInjector
    if any(kw in prompt for kw in ["PlayerGui", "HUD", "Frame", "TextButton", "UDim2", "GuiObject"]):
        antigravity_prompt += "\n\n[HUD DIRECTIVE]: Konversi Offset ke Scale (newScale = currentScale + offset/parentAbsoluteSize). Jangan inject tombol X ke ScrollingFrame. ZIndex tombol = parent.ZIndex + 1."

    command = [GEMINI_CLI_PATH, "generate", "--model", model, "--prompt", antigravity_prompt]

    for attempt in range(max_retries):
        try:
            process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60.0)
            except asyncio.TimeoutError:
                process.kill()
                console_terminal_interface.print(f"[bold yellow]Timeout percobaan {attempt + 1}/{max_retries}. Mencoba ulang...[/bold yellow]")
                await asyncio.sleep(2)
                continue
            if process.returncode == 0:
                result = stdout.decode('utf-8').strip()
                if result:
                    return result
            else:
                err = stderr.decode('utf-8').strip()[:200]
                console_terminal_interface.print(f"[bold red]Model error kode {process.returncode}: {err}[/bold red]")
        except FileNotFoundError:
            console_terminal_interface.print(f"[bold red]FATAL: Gemini CLI tidak ditemukan di: {GEMINI_CLI_PATH}[/bold red]")
            return ""
        except Exception as e:
            console_terminal_interface.print(f"[bold red]Exception percobaan {attempt + 1}: {e}[/bold red]")
        # Backoff eksponensial: 2s, 4s, 8s
        if attempt < max_retries - 1:
            await asyncio.sleep((2 ** attempt) * 2)
    return ""


# execute_antigravity_fleet VERSI BARU ada di bawah (~L2017).
# Versi lama (dengan signature berbeda) dihapus untuk menghindari konflik.

# =====================================================================
# KELAS ASLI OmniSynthesizerAgent, AutoHealerAgent BERADA DI BAWAH INI
# =====================================================================

class AutoHealerAgent:
    def __init__(self, mcp_bridge=None):
        self.mcp_bridge = mcp_bridge
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
                console_terminal_interface.print(f"[bold red]   [Gemini CLI Error] {result_data[:200]}[/bold red]")
                return broken_code

            if '"mcp_tool_call"' in result_data:
                try:
                    tool_data = json.loads(result_data)["mcp_tool_call"]
                    tool_name = tool_data.get("tool_name", "unknown")
                    tool_args = tool_data.get("args", {})

                    console_terminal_interface.print(
                        f"[bold cyan]   🛠️ [MCP Action] AI Menjalankan Studio Tool: {tool_name}[/bold cyan]"
                    )

                    if self.mcp_bridge:
                        tool_response = await self.mcp_bridge.execute_tool(tool_name, tool_args)
                    else:
                        tool_response = "MCP Bridge Not Available"

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
            _project_ctx = await scan_existing_project(mcp_bridge=self.mcp_bridge)
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


# ============================================================
# INSTRUKSI SPESIFIK PER KATEGORI TASK
# Kunci: prefix kategori task (huruf kapital, tanpa nomor)
# Nilai: instruksi teknis Luau yang presisi untuk AI
# ============================================================
TASK_SPECIFIC_INSTRUCTIONS: dict = {

    "GATHERING_TOOLS": (
        "[KATEGORI: GATHERING TOOL - KAPAK & BELIUNG]\n"
        "Anda sedang membuat ALAT PANEN Roblox (Kapak/Beliung).\n"
        "Ini adalah instance Tool yang DIPAKAI pemain. BUKAN script server biasa!\n"
        "WAJIB IKUTI ARSITEKTUR INI:\n"
        "1. Script ini berjalan di dalam Tool (LocalScript di Tool untuk client-side input).\n"
        "2. Gunakan Tool.Activated:Connect() untuk mendeteksi serangan pemain.\n"
        "3. Tembakkan workspace:Raycast() dari HumanoidRootPart ke arah CFrame.LookVector (jarak 6 stud).\n"
        "4. Kapak HANYA mengenai Node ber-tag CollectionService 'Tree'. Beliung HANYA mengenai tag 'Rock'.\n"
        "5. Saat terkena: kirim hit ke server via RemoteEvent bernama 'HarvestHit' dengan argumen (hitPart, tagName).\n"
        "6. Server WAJIB memvalidasi: typeof(hitObject) == 'Instance' dan CollectionService:HasTag(hitObject, tagName).\n"
        "7. Jika Health <= 0: server hancurkan objek (Destroy()) dan spawn RAW_MATERIAL_ITEM di posisi tersebut.\n"
        "8. CONTOH POLA (MUTLAK IKUTI INI):\n"
        "--!strict\n"
        "local Players = game:GetService('Players')\n"
        "local CollectionService = game:GetService('CollectionService')\n"
        "local ReplicatedStorage = game:GetService('ReplicatedStorage')\n"
        "local tool = script.Parent\n"
        "local HIT_TAG = 'Tree'\n"
        "local HIT_DISTANCE = 6\n"
        "local hitEvent = ReplicatedStorage:WaitForChild('HarvestHit')\n"
        "local conn = tool.Activated:Connect(function()\n"
        "    local player = Players.LocalPlayer\n"
        "    local char = player.Character\n"
        "    if not char then return end\n"
        "    local hrp = char:FindFirstChild('HumanoidRootPart')\n"
        "    if not hrp then return end\n"
        "    local rayParams = RaycastParams.new()\n"
        "    rayParams.FilterDescendantsInstances = {char}\n"
        "    rayParams.FilterType = Enum.RaycastFilterType.Exclude\n"
        "    local result = workspace:Raycast(hrp.Position, hrp.CFrame.LookVector * HIT_DISTANCE, rayParams)\n"
        "    if result and result.Instance then\n"
        "        local hit = result.Instance\n"
        "        if CollectionService:HasTag(hit, HIT_TAG) or (hit.Parent and CollectionService:HasTag(hit.Parent, HIT_TAG)) then\n"
        "            hitEvent:FireServer(hit, HIT_TAG)\n"
        "        end\n"
        "    end\n"
        "end)\n"
    ),

    "MONSTER": (
        "[KATEGORI: MONSTER / NPC MUSUH - AI TEMPUR]\n"
        "Anda sedang membuat Script Monster/Hewan menggunakan PathfindingService Roblox.\n"
        "WAJIB IKUTI ARSITEKTUR AI MONSTER:\n"
        "1. Gunakan PathfindingService:CreatePath() dan path:ComputeAsync() untuk navigasi.\n"
        "2. Gunakan RunService.Heartbeat:Connect() untuk update AI loop (BUKAN while true do tanpa wait).\n"
        "3. Variabel WAJIB ADA:\n"
        "   local Diet: string = 'Carnivore'  -- atau 'Herbivore' atau 'Omnivore'\n"
        "   local SocialBehavior: string = 'Solitary'  -- atau 'Pack' atau 'Herd'\n"
        "   local SpawnWeight: number = 3\n"
        "   local Habitat: string = 'Forest'  -- atau 'Desert', 'Ocean', 'Snow'\n"
        "   local PerceptionRadius: number = 40\n"
        "4. DropTable: tabel item yang di-spawn saat monster mati: {['Daging'] = 2, ['Tulang'] = 1}.\n"
        "5. Saat mati (Humanoid.Died): loop DropTable → spawn fisik Part kecil di posisi monster.\n"
        "6. Untuk CARNIVORE/OMNIVORE: scan radius untuk item ber-tag CollectionService 'Bait'.\n"
        "7. PATTERN PATHFINDING WAJIB:\n"
        "   local path = PathfindingService:CreatePath({AgentRadius=2, AgentHeight=5, AgentCanJump=true})\n"
        "   path:ComputeAsync(hrp.Position, target)\n"
        "   if path.Status == Enum.PathStatus.Success then\n"
        "       for _, waypoint in ipairs(path:GetWaypoints()) do\n"
        "           humanoid:MoveTo(waypoint.Position)\n"
        "           humanoid.MoveToFinished:Wait()\n"
        "       end\n"
        "   end\n"
    ),

    "RAW_MATERIAL_ITEM": (
        "[KATEGORI: RAW MATERIAL - BAHAN MENTAH]\n"
        "Anda sedang membuat sistem spawn ITEM FISIK yang jatuh ke tanah.\n"
        "HUKUM RAW MATERIAL (MUTLAK):\n"
        "1. DILARANG keras memiliki atribut: Recipe, Durability, ArmorTier.\n"
        "2. WAJIB: local ItemCategory: string = 'Material'\n"
        "3. WAJIB: local BasePrice: number = 150 (contoh harga)\n"
        "4. Buat wujud fisik kecil (Part) di tanah dengan ProximityPrompt ActionText = 'Ambil'.\n"
        "5. Saat dipungut: hapus dari workspace, tambah ke inventory via RemoteEvent ke server.\n" "6. Spawn Rate dinamis berdasarkan Rarity dan Map drop rates.\n"
        "6. CollectionService.AddTag(part, 'WorldItem') untuk tracking.\n"
        "7. CanCollide = false pada item, Anchored = false (bisa jatuh ke tanah).\n"
        "8. Fungsi spawn wajib seperti ini:\n"
        "   local function spawnMaterial(itemName: string, pos: Vector3, amount: number)\n"
        "       for i = 1, amount do\n"
        "           local part = Instance.new('Part')\n"
        "           part.Name = itemName\n"
        "           part.Size = Vector3.new(0.5, 0.5, 0.5)\n"
        "           part.CanCollide = false\n"
        "           part.Position = pos + Vector3.new(math.random(-2,2), 1, math.random(-2,2))\n"
        "           local prompt = Instance.new('ProximityPrompt')\n"
        "           prompt.ActionText = 'Ambil'\n"
        "           prompt.ObjectText = itemName\n"
        "           prompt.Parent = part\n"
        "           part.Parent = workspace\n"
        "       end\n"
        "   end\n"
    ),

    "MODERN_WEAPON": (
        "[KATEGORI: SENJATA API MODERN - ASSAULT RIFLE/SNIPER]\n"
        "Anda sedang membuat Senjata Api yang MENEMBAKKAN PELURU FISIK via Raycast.\n"
        "HUKUM MODERN WEAPON (MUTLAK):\n"
        "1. HARAM memiliki variabel BaseDamage! Damage ditentukan oleh AMMUNITION_CALIBER modul.\n"
        "2. WAJIB: local CompatibleCaliber: string = '5.56x45mm'\n"
        "3. WAJIB: local FireRate: number = 750  -- RPM\n"
        "4. WAJIB: local Recoil: number = 0.3\n"
        "5. WAJIB Recipe: local Recipe = {['Iron Ingot'] = 5, ['Wood'] = 2}\n"
        "6. local ItemCategory: string = 'Weapon'\n"
        "7. Mekanisme tembak: Tool.Activated → Raycast dari kamera ke arah depan.\n"
        "8. HitboxSeparation: buat Part transparan CanCollide=true sebagai hitbox.\n"
        "9. VisualEquip: WeldConstraint senjata ke RightHand karakter saat diequip. Drop rate menyesuaikan (Pistol 90%, Heavy Weapon 0.01%).\n"
        "10. ProximityPrompt di tanah dengan ActionText = 'Equip'. JIKA MENGGUNAKAN MESH ASSET ID (rbxassetid://), PASTIKAN MENULISKANNYA SECARA JELAS KARENA AKAN DIUNDUH OTOMATIS OLEH ASSET ENGINE.\n"
    ),

    "FANTASY_WEAPON": (
        "[KATEGORI: SENJATA FANTASY - PEDANG/TONGKAT SIHIR]\n"
        "Anda sedang membuat Senjata Melee atau Magic Fantasy.\n"
        "HUKUM FANTASY WEAPON:\n"
        "1. Melee: Tool.Activated → workspace:Raycast() atau magnitude check sekitar karakter.\n"
        "2. Magic: buat Projectile (BasePart) bergerak menggunakan LinearVelocity atau BodyVelocity.\n"
        "3. WAJIB Recipe: local Recipe = {['Crystal'] = 3, ['Dragon Bone'] = 1}\n"
        "4. local ItemCategory: string = 'Weapon'\n"
        "5. VisualEquip: WeldConstraint ke RightHand karakter.\n"
        "6. ProximityPrompt ActionText = 'Equip'.\n"
        "7. DILARANG: CompatibleCaliber (ini bukan senjata api). Armor langka drop 0.01%, Armor murah 70%.\n"
    ),

    "MODERN_ARMOR_HELMET": (
        "[KATEGORI: ARMOR MODERN - ROMPI/HELM TAKTIS]\n"
        "Anda sedang membuat Armor yang bisa dipakai karakter.\n"
        "HUKUM ARMOR MODERN (MUTLAK):\n"
        "\n"
        "PERINGATAN KERAS — RECIPE WAJIB DIISI, JANGAN SAMPAI KOSONG!\n"
        "VALIDATOR AKAN MENOLAK TERUS JIKA RECIPE KOSONG!\n"
        "GUNAKAN PERSIS SALAH SATU FORMAT INI (SALIN LANGSUNG):\n"
        "\n"
        "FORMAT A (WAJIB GUNAKAN INI jika tidak yakin):\n"
        "local Recipe = {Kevlar = 3, Iron = 2, Ceramic = 1}\n"
        "\n"
        "FORMAT B (Alternatif yang juga valid):\n"
        "local Recipe = {}\n"
        "Recipe.Kevlar = 3\n"
        "Recipe.Iron = 2\n"
        "\n"
        "JANGAN PERNAH MENULIS: local Recipe = {}  -- INI AKAN SELALU DITOLAK!\n"
        "\n"
        "VARIABEL LAIN YANG WAJIB ADA:\n"
        "local Durability: number = 100\n"
        "local ArmorTier: number = 3\n"
        "local MaterialType: string = 'Ceramic'\n"
        "local ItemCategory: string = 'Armor'\n"
        "local BasePrice: number = 2500\n"
        "\n"
        "7. HitboxSeparation: Part transparan CanCollide=true sebagai hitbox.\n"
        "8. VisualEquip: WeldConstraint armor ke UpperTorso atau Head pemain saat diequip.\n"
        "9. ProximityPrompt ActionText = 'Gunakan'.\n"
    ),

    "FANTASY_ARMOR_HELMET": (
        "[KATEGORI: ARMOR FANTASY - ZIRAH/JUBAH KSATRIA]\n"
        "Anda sedang membuat Armor Fantasy yang bisa dipakai karakter.\n"
        "HUKUM ARMOR FANTASY (MUTLAK):\n"
        "\n"
        "PERINGATAN KERAS — RECIPE WAJIB DIISI, JANGAN SAMPAI KOSONG!\n"
        "GUNAKAN PERSIS FORMAT INI (SALIN LANGSUNG):\n"
        "local Recipe = {DragonScale = 5, Mithril = 3, Crystal = 1}\n"
        "JANGAN MENULIS: local Recipe = {}  -- SELALU DITOLAK VALIDATOR!\n"
        "\n"
        "VARIABEL WAJIB:\n"
        "local Durability: number = 100\n"
        "local ArmorTier: number = 4\n"
        "local MaterialType: string = 'Mithril'\n"
        "local ItemCategory: string = 'Armor'\n"
        "local BasePrice: number = 5000\n"
        "\n"
        "7. HitboxSeparation + VisualEquip: WeldConstraint ke UpperTorso atau Head.\n"
        "8. ProximityPrompt ActionText = 'Gunakan'.\n"
    ),

    "AMMUNITION_CALIBER": (
        "[KATEGORI: AMUNISI - KALIBER PELURU]\n"
        "Anda sedang membuat modul data Amunisi (mirip Arena Breakout).\n"
        "HUKUM BALISTIK (MUTLAK):\n"
        "1. DILARANG keras memiliki Recipe, Durability, ArmorTier.\n"
        "2. WAJIB: local BaseDamage: number = 35\n"
        "3. WAJIB: local PenetrationLevel: number = 3  -- nilai 1-6\n"
        "4. local ItemCategory: string = 'Ammunition'\n"
        "5. local BasePrice: number = 200\n"
        "6. Buat wujud fisik kotak amunisi (Part kecil) dengan ProximityPrompt ActionText = 'Ambil'.\n"
        "7. Anchored = false, CanCollide = false.\n"
        "8. Tabel kaliber yang harus ada: '5.56x45mm', '7.62x39mm', '9mm', '.308 Win', '12 gauge'.\n"
    ),

    "CORE_INVENTORY_SYSTEM": (
        "[KATEGORI: INVENTORY SYSTEM - CUSTOM EXTRACTION]\n"
        "Anda sedang membuat Sistem Inventory TANPA Backpack bawaan Roblox.\n"
        "HUKUM INVENTORY (MUTLAK):\n"
        "1. DILARANG StarterGear atau Backpack bawaan Roblox.\n"
        "2. 3 Kompartemen (tabel server per-player):\n"
        "   - MainBackpack: HILANG SEMUA saat mati.\n"
        "   - SafeContainer: TIDAK hilang saat mati (maks 3 slot).\n"
        "   - LobbyStorage: Gudang permanen, TIDAK bisa dibawa ke arena.\n"
        "3. DataStoreService wajib untuk SafeContainer dan LobbyStorage.\n"
        "4. Players.PlayerAdded:Connect() → pcall(DataStore:GetAsync()) untuk load.\n"
        "5. Players.PlayerRemoving:Connect() → pcall(DataStore:SetAsync()) untuk save.\n"
        "6. Saat Humanoid.Died: hapus semua MainBackpack, pertahankan SafeContainer.\n"
        "7. Kirim state inventory ke client via RemoteEvent saat ada perubahan.\n"
    ),

    "NPC_TRADER": (
        "[KATEGORI: NPC TRADER - PEDAGANG HIDUP]\n"
        "Anda sedang membuat NPC Trader yang AKTIF bergerak (bukan patung statis).\n"
        "HUKUM NPC TRADER (MUTLAK):\n"
        "1. Pasang alat kerja di tangan NPC via WeldConstraint:\n"
        "   Blacksmith → Palu di RightHand. Woodworker → Gergaji. Gunsmith → Kunci Inggris.\n"
        "2. Harga Jual = BasePrice * 2.0. Harga Beli dari Pemain = BasePrice * 0.4.\n"
        "3. ProximityPrompt ActionText = 'Buka Toko' untuk interaksi.\n"
        "4. Saat dipicu → FireClient ke pemain untuk buka UI Shop.\n"
        "5. Server validasi transaksi via RemoteFunction (ZERO-TRUST: typeof() semua argumen).\n"
        "6. 8 NPC terspesialisasi: Blacksmith, Woodworker, Stonemason, Gunsmith, Medic, Chef, Scientist, Black Market.\n"
    ),

    "LOBBY_SPACESHIP": (
        "[KATEGORI: LOBBY PESAWAT LUAR ANGKASA]\n"
        "Lobby ini ada di LUAR ANGKASA (Y = 10000+). BUKAN di bumi.\n"
        "HUKUM FISIKA LOBBY (MUTLAK):\n"
        "1. DILARANG KERAS workspace:Raycast() ke tanah. Tidak ada tanah di angkasa!\n"
        "2. DILARANG: workspace.Terrain.\n"
        "3. Bangun pesawat dengan Instance.new('Part') di posisi Y = 10000.\n"
        "4. Semua lantai/dinding: CanCollide = true, Anchored = true.\n"
        "5. Teleport pemain ke Y=10000+ saat PlayerAdded.\n"
        "6. Tambahkan Lighting gelap dan Atmosphere untuk nuansa luar angkasa.\n"
    ),

    "FURNITURE": (
        "[KATEGORI: FURNITUR LOBBY PESAWAT]\n"
        "Furnitur ini ADA DI DALAM LOBBY PESAWAT (Y=10000+). Bukan di bumi!\n"
        "HUKUM FURNITUR (MUTLAK):\n"
        "1. DILARANG KERAS workspace:Raycast() ke bawah. Tidak ada tanah di sini.\n"
        "2. Posisi furnitur relatif terhadap lantai pesawat (Y=10000+).\n"
        "3. HitboxSeparation: Part transparan CanCollide=true sebagai hitbox.\n"
        "4. Anchored = true, CanCollide = true pada hitbox.\n"
        "5. Warna WAJIB neon/cerah: BrickColor.new('Bright white') atau neon colors.\n"
    ),

    "CORE_WORLD_GENERATION": (
        "[KATEGORI: PROCEDURAL WORLD GENERATION]\n"
        "Anda men-generate dunia secara prosedural.\n"
        "HUKUM PENEMPATAN AKURAT (MUTLAK):\n"
        "1. Baseplate berukuran 2048x64x2048 Parts.\n"
        "2. Setiap objek di-Raycast ke bawah sebelum diletakkan:\n"
        "   local params = RaycastParams.new()\n"
        "   params.FilterType = Enum.RaycastFilterType.Include\n"
        "   params.FilterDescendantsInstances = {workspace.Terrain}\n"
        "   local hit = workspace:Raycast(Vector3.new(x,1000,z), Vector3.new(0,-2000,0), params)\n"
        "   if hit then part.Position = hit.Position + Vector3.new(0, part.Size.Y/2, 0) end\n"
        "3. Semua BasePart: CanCollide = true, Anchored = true.\n"
        "4. DILARANG: floating objects tanpa Raycast ke tanah.\n"
        "5. Warna neon/cerah sesuai bioma.\n"
    ),

    "BIOME_SYSTEM": (
        "[KATEGORI: BIOME SYSTEM - EKOSISTEM]\n"
        "Anda membuat sistem Bioma (Hutan, Desert, Snow, Ocean).\n"
        "HUKUM HITBOX SEPARATION (MUTLAK):\n"
        "1. Saat generate Pohon/Batu: WAJIB Hitbox Separation:\n"
        "   - Part transparan (Transparency=1) CanCollide=true = hitbox.\n"
        "   - MeshPart visual CanCollide=false.\n"
        "2. RaycastParams sebelum meletakkan objek (cegah floating).\n"
        "3. FilterDescendantsInstances exclude objek sejenis.\n"
        "4. CollectionService.AddTag(treePart, 'Tree') untuk setiap pohon.\n"
        "5. CollectionService.AddTag(rockPart, 'Rock') untuk setiap batu.\n"
        "6. Warna unik per bioma: Hutan=Neon Hijau, Desert=Oranye, Snow=Biru muda, Ocean=Biru.\n"
    ),

    "DAY_NIGHT_CYCLE": (
        "[KATEGORI: DAY/NIGHT CYCLE]\n"
        "Anda membuat siklus siang-malam.\n"
        "HUKUM SIKLUS:\n"
        "1. local Lighting = game:GetService('Lighting')\n"
        "2. local RunService = game:GetService('RunService')\n"
        "3. local TweenService = game:GetService('TweenService')\n"
        "4. Update ClockTime via RunService.Heartbeat:Connect() (BUKAN while true do).\n"
        "5. Siang (6-18): Brightness=2, FogEnd=1000. Malam (18-24 & 0-6): Brightness=0.1, FogEnd=100.\n"
        "6. TweenService untuk transisi mulus.\n"
        "7. Expose BindableEvent 'TimeChanged' agar Monster AI bisa membaca waktu.\n"
    ),

    "WEATHER_DISASTER": (
        "[KATEGORI: WEATHER SYSTEM]\n"
        "Anda membuat cuaca ekstrem (Hujan, Badai Pasir, Salju).\n"
        "HUKUM CUACA:\n"
        "1. Hujan: ParticleEmitter biru di atas pemain, Lighting.FogEnd = 50.\n"
        "2. Badai Pasir: FogColor oranye/cokelat, FogEnd = 30.\n"
        "3. Salju: ParticleEmitter putih, FogEnd = 70.\n"
        "4. TweenService untuk transisi mulus.\n"
        "5. Random cuaca setiap 5-10 menit via task.wait().\n"
        "6. RemoteEvent broadcast ke client saat cuaca berubah.\n"
    ),

    "SMELTING_FURNACE": (
        "[KATEGORI: SMELTING FURNACE]\n"
        "Anda membuat Mesin Peleburan Logam di Lobby Pesawat.\n"
        "HUKUM SMELTING:\n"
        "1. Buat Part 3D Furnace di posisi dalam Lobby (Y=10000+).\n"
        "2. ProximityPrompt ActionText = 'Lebur Logam'.\n"
        "3. Saat diaktifkan: cek inventory pemain via RemoteFunction.\n"
        "4. Jika ada 'Iron Ore': hapus dari inventory, jalankan ParticleEmitter api.\n"
        "5. task.wait(3) untuk simulasi proses.\n"
        "6. Setelah selesai: spawn 'Iron Ingot' dengan ProximityPrompt 'Ambil'.\n"
        "7. ZERO-TRUST: typeof() check semua argumen dari client.\n"
    ),

    "RESOURCE_NODE_MANAGER": (
        "[KATEGORI: RESOURCE NODE MANAGER]\n"
        "Anda membuat sistem manajemen HP untuk Pohon dan Batu.\n"
        "HUKUM NODE:\n"
        "1. Scan CollectionService:GetTagged('Tree') dan GetTagged('Rock').\n"
        "2. Setiap node punya IntValue 'Health' = 100.\n"
        "3. Event listener: saat Health berubah, cek apakah <= 0.\n"
        "4. Jika <= 0: Destroy() node, spawn RAW_MATERIAL_ITEM di posisinya.\n"
        "5. Kayu untuk Tree. Besi Mentah/Batu untuk Rock.\n"
        "6. Respawn node baru di posisi sama setelah 30 detik.\n"
        "7. DILARANG script per-node. Gunakan SATU script server yang mengatur semua node.\n"
    ),

    "ITEM_CATEGORY_DATABASE": (
        "[KATEGORI: ITEM DATABASE - MODULE SCRIPT]\n"
        "Anda membuat ModuleScript sentral di ReplicatedStorage.\n"
        "HUKUM DATABASE:\n"
        "1. Ini adalah ModuleScript (bukan Script atau LocalScript).\n"
        "2. Return tabel yang bisa di-require:\n"
        "   return {\n"
        "       Weapon = {MaxStack=1, CanEquip=true, IsConsumable=false, Encumbrance=3},\n"
        "       Ammunition = {MaxStack=60, CanEquip=false, IsConsumable=true, Encumbrance=1},\n"
        "       Armor = {MaxStack=1, CanEquip=true, IsConsumable=false, Encumbrance=4},\n"
        "       Medical = {MaxStack=5, CanEquip=false, IsConsumable=true, Encumbrance=1},\n"
        "       Material = {MaxStack=20, CanEquip=false, IsConsumable=false, Encumbrance=1},\n"
        "       Valuable = {MaxStack=10, CanEquip=false, IsConsumable=false, Encumbrance=2},\n"
        "       Bait = {MaxStack=10, CanEquip=false, IsConsumable=true, Encumbrance=1},\n"
        "       Tool = {MaxStack=1, CanEquip=true, IsConsumable=false, Encumbrance=2},\n"
        "   }\n"
        "3. Tambahkan fungsi getCategory(itemName: string): string.\n"
    ),

    "CORE_MISSION_SYSTEM": (
        "[KATEGORI: MISSION SYSTEM]\n"
        "Anda membuat Sistem Quest Harian dan Event Mingguan.\n"
        "HUKUM MISI:\n"
        "1. DataStoreService untuk progress misi per player.\n"
        "2. Misi Harian reset setiap 24 jam (os.time() tracking).\n"
        "3. Setiap misi: {Id, Title, Description, Type='Kill'/'Gather'/'Craft', Target, Amount, Reward}.\n"
        "4. Saat selesai: kirim reward ke Inbox pemain via InboxService.\n"
        "5. PlayerAdded: load misi dari DataStore via pcall().\n"
        "6. RemoteEvent broadcast progress ke client.\n"
    ),

    "CORE_MONETIZATION_SYSTEM": (
        "[KATEGORI: MONETIZATION - PEMBAYARAN ROBUX]\n"
        "Anda membuat sistem pembelian Robux. HUKUM ANTI-P2W MUTLAK:\n"
        "1. MarketplaceService.ProcessReceipt HANYA di SATU skrip ini.\n"
        "2. DILARANG jual: Sword, Gun, Armor, Weapon.\n"
        "3. Yang boleh dijual: Cosmetic, Extra Slot, XP Boost, Currency.\n"
        "4. pcall() untuk semua DataStore di dalam ProcessReceipt.\n"
        "5. return Enum.ProductPurchaseDecision.PurchaseGranted setelah berhasil.\n"
        "6. Simpan receipt ID ke DataStore untuk cegah double-purchase.\n"
    ),

    "AUDIO_SYSTEM": (
        "[KATEGORI: AUDIO SYSTEM - BGM DAN SFX]\n"
        "Anda membuat sistem audio client (LocalScript di StarterPlayerScripts).\n"
        "HUKUM AUDIO:\n"
        "1. ContentProvider:PreloadAsync() untuk semua Sound sebelum play.\n"
        "2. BGM: loop=true, Volume=0.3, fade in/out via TweenService.\n"
        "3. SFX: loop=false, Volume=0.7.\n"
        "4. Dengarkan RemoteEvent dari server untuk ganti BGM.\n"
        "5. ANTI-MEMORY LEAK: simpan semua koneksi event ke variabel.\n"
    ),

    "CORE_INBOX_SYSTEM": (
        "[KATEGORI: INBOX SYSTEM - KOTAK MASUK]\n"
        "Anda membuat Sistem Kotak Masuk player.\n"
        "HUKUM INBOX:\n"
        "1. DataStoreService per player untuk list messages/items.\n"
        "2. Format item inbox: {Id=string, Type='item'/'reward', Content=string, ExpiresAt=number}.\n"
        "3. Claim item: pindah ke SafeContainer atau LobbyStorage.\n"
        "4. Expired items hapus saat join (cek os.time()).\n"
        "5. PlayerAdded: load dari DataStore via pcall().\n"
        "6. PlayerRemoving: save ke DataStore via pcall().\n"
    ),

    "FLEA_MARKET_BACKEND": (
        "[KATEGORI: FLEA MARKET BACKEND]\n"
        "Anda membuat Backend Server pasar jual beli antar pemain.\n"
        "HUKUM FLEA MARKET:\n"
        "1. RemoteFunction (bukan RemoteEvent) untuk transaksi.\n"
        "2. pcall() untuk semua DataStore.\n"
        "3. ZERO-TRUST: typeof() dan range check semua argumen dari client.\n"
        "4. Listing: {Id, SellerId, ItemName, Quantity, Price, CreatedAt}.\n"
        "5. Saat beli: kurangi currency pembeli, tambah currency penjual via Inbox.\n"
        "6. Cegah self-purchase: if listing.SellerId == buyer.UserId then reject.\n"
        "7. OrderedDataStore untuk sort listing by harga.\n"
    ),
      "GUI_HUD": (
          "[KATEGORI: GUI / HUD / ANTARMUKA PENGGUNA ROBLOX]\n"
          "Anda sedang membuat UI Roblox (ScreenGui + LocalScript).\n"
          "HUKUM MUTLAK PEMBUATAN UI (WAJIB DIPATUHI 100%):\n"
          "1. WAJIB BUAT SEMUA ELEMEN UI VIA KODE LUAU MURNI menggunakan Instance.new().\n"
          "   DILARANG KERAS menggunakan rbxassetid://, rbxthumb://, Content URL, atau ID aset apapun dari GitHub/internet.\n"
          "   Semua visual (warna, bentuk, teks, ikon) WAJIB dibuat via properti Roblox: BackgroundColor3, TextColor3, UICorner, UIStroke, dll.\n"
          "2. STRUKTUR WAJIB:\n"
          "   local Players = game:GetService('Players')\n"
          "   local player = Players.LocalPlayer\n"
          "   local playerGui = player:WaitForChild('PlayerGui')\n"
          "   local screenGui = Instance.new('ScreenGui')\n"
          "   screenGui.Name = 'NamaUI'\n"
          "   screenGui.ResetOnSpawn = false\n"
          "   screenGui.Parent = playerGui\n"
          "3. SEMUA FRAME/BUTTON WAJIB ADA BackgroundColor3 (jangan transparansi semua).\n"
          "4. Gunakan UDim2.new() untuk Size dan Position — WAJIB gunakan Scale (0-1) bukan hanya Offset.\n"
          "5. Tombol WAJIB memiliki event handler: TextButton.MouseButton1Click:Connect() atau .Activated:Connect().\n"
          "6. UICorner, UIStroke, UIListLayout WAJIB menggunakan Instance.new() dan set Parent ke parent frame/button.\n"
          "7. DILARANG menggunakan ImageLabel dengan Image = 'rbxassetid://ANGKA'. Ganti dengan warna solid atau TextLabel.\n"
          "8. Update dinamis UI: gunakan RemoteEvent atau ValueObject:GetPropertyChangedSignal().\n"
          "CONTOH POLA MINIMAL YANG BENAR (SALIN TEMPLATE INI):\n"
          "--!strict\n"
          "local Players = game:GetService('Players')\n"
          "local player = Players.LocalPlayer\n"
          "local playerGui = player:WaitForChild('PlayerGui')\n"
          "local screenGui = Instance.new('ScreenGui')\n"
          "screenGui.Name = 'MyHUD'\n"
          "screenGui.ResetOnSpawn = false\n"
          "screenGui.Parent = playerGui\n"
          "local mainFrame = Instance.new('Frame')\n"
          "mainFrame.Size = UDim2.new(0.3, 0, 0.1, 0)\n"
          "mainFrame.Position = UDim2.new(0.35, 0, 0.02, 0)\n"
          "mainFrame.BackgroundColor3 = Color3.fromRGB(20, 20, 30)\n"
          "mainFrame.BorderSizePixel = 0\n"
          "mainFrame.Parent = screenGui\n"
          "local corner = Instance.new('UICorner')\n"
          "corner.CornerRadius = UDim.new(0, 8)\n"
          "corner.Parent = mainFrame\n"
          "local label = Instance.new('TextLabel')\n"
          "label.Size = UDim2.new(1, 0, 1, 0)\n"
          "label.BackgroundTransparency = 1\n"
          "label.Text = 'STATUS'\n"
          "label.TextColor3 = Color3.fromRGB(255, 255, 255)\n"
          "label.Font = Enum.Font.GothamBold\n"
          "label.TextScaled = true\n"
          "label.Parent = mainFrame\n"
      ),

      "SHOP_UI": (
          "[KATEGORI: SHOP / TOKO / INVENTORY UI]\n"
          "Anda membuat UI Toko/Inventory sebagai LocalScript di ScreenGui.\n"
          "HUKUM SHOP UI:\n"
          "1. DILARANG rbxassetid:// — gunakan HANYA kode Luau murni.\n"
          "2. Buat ScrollingFrame untuk list item, isi dengan Frame per item menggunakan for loop.\n"
          "3. Tombol Beli/Jual WAJIB ada event handler dan RemoteFunction ke server untuk validasi.\n"
          "4. Tampilkan currency/gold pemain via RemoteFunction atau ValueObject.\n"
          "5. Semua item menggunakan TextLabel untuk nama + harga, BUKAN ImageLabel dengan asset ID.\n"
          "6. Server WAJIB validasi: pcall DataStore, cek currency cukup, cek item ada.\n"
      ),

      "INVENTORY_UI": (
          "[KATEGORI: INVENTORY UI]\n"
          "Anda membuat UI Inventory sebagai LocalScript di ScreenGui.\n"
          "HUKUM INVENTORY UI:\n"
          "1. DILARANG rbxassetid:// — gunakan HANYA kode Luau murni.\n"
          "2. Grid layout menggunakan UIGridLayout di ScrollingFrame.\n"
          "3. Setiap slot = Frame + UICorner + TextLabel (nama item) + TextLabel (jumlah).\n"
          "4. Data inventory diminta ke server via RemoteFunction.\n"
          "5. Update realtime: daftarkan listener ke RemoteEvent 'InventoryUpdated'.\n"
          "6. JANGAN gunakan Image apapun — semua visual via BackgroundColor3 dan Text.\n"
      ),

      "HEALTH_BAR": (
          "[KATEGORI: HEALTH BAR / STAMINA BAR / STATUS BAR UI]\n"
          "Anda membuat status bar (HP/Stamina/Energy) sebagai LocalScript.\n"
          "HUKUM STATUS BAR:\n"
          "1. DILARANG rbxassetid:// — gunakan HANYA kode Luau murni.\n"
          "2. Background frame (abu gelap) + fill frame (warna) menggunakan Size dengan Scale.\n"
          "3. Update bar: humanoid.HealthChanged atau ValueObject:GetPropertyChangedSignal().\n"
          "4. Animasi bar dengan TweenService untuk transisi mulus.\n"
          "5. Contoh update HP bar:\n"
          "   humanoid.HealthChanged:Connect(function(hp)\n"
          "       local pct = hp / humanoid.MaxHealth\n"
          "       local tween = TweenService:Create(fillFrame, TweenInfo.new(0.2), {Size = UDim2.new(pct, 0, 1, 0)})\n"
          "       tween:Play()\n"
          "   end)\n"
      ),

      "LEADERBOARD": (
          "[KATEGORI: LEADERBOARD / SCOREBOARD UI]\n"
          "Anda membuat papan skor sebagai LocalScript di ScreenGui.\n"
          "HUKUM LEADERBOARD:\n"
          "1. DILARANG rbxassetid:// — gunakan HANYA kode Luau murni.\n"
          "2. Data dari server via RemoteFunction atau RemoteEvent.\n"
          "3. Tampilkan daftar menggunakan UIListLayout + loop frame per pemain.\n"
          "4. Update berkala menggunakan task.spawn + task.wait(interval).\n"
          "5. Hapus entry lama sebelum populate ulang: for _, c in ipairs(listContainer:GetChildren()) do if c:IsA('Frame') then c:Destroy() end end.\n"
      ),

      "NOTIFICATION": (
          "[KATEGORI: NOTIFICATION / TOAST / POPUP UI]\n"
          "Anda membuat sistem notifikasi sebagai LocalScript di ScreenGui.\n"
          "HUKUM NOTIFICATION UI:\n"
          "1. DILARANG rbxassetid:// — gunakan HANYA kode Luau murni.\n"
          "2. Animasi masuk/keluar menggunakan TweenService (Position atau Transparency).\n"
          "3. Notifikasi baru spawn di atas, geser ke bawah jika ada antrian.\n"
          "4. Notif otomatis hilang setelah N detik: task.delay(N, function() tween keluar, kemudian Destroy() end).\n"
          "5. Subscribe ke RemoteEvent 'ShowNotification' dari server.\n"
      ),

      "MINIMAP": (
          "[KATEGORI: MINIMAP / COMPASS UI]\n"
          "Anda membuat minimap/kompas sebagai LocalScript di ScreenGui.\n"
          "HUKUM MINIMAP:\n"
          "1. DILARANG rbxassetid:// — gunakan HANYA kode Luau murni.\n"
          "2. Frame bulat menggunakan UICorner CornerRadius = UDim.new(1, 0).\n"
          "3. Titik pemain: Frame kecil berwarna yang posisinya diupdate tiap frame via RunService.Heartbeat.\n"
          "4. Skala posisi dunia ke posisi UI: (worldPos - centerWorld) / worldRadius * frameRadius.\n"
          "5. Clamp posisi titik agar tidak keluar frame minimap.\n"
      ),

  
}

# ============================================================
# QUERY RAG YANG LEBIH SPESIFIK PER KATEGORI TASK
# ============================================================
TASK_RAG_QUERIES: dict = {
    "GATHERING_TOOLS": "roblox luau tool activated raycast axe pickaxe CollectionService IntValue harvest",
    "MONSTER": "roblox PathfindingService NPC AI monster combat strict luau waypoint",
    "RAW_MATERIAL_ITEM": "roblox luau item drop spawn ProximityPrompt ground pickup CollectionService",
    "MODERN_WEAPON": "roblox luau gun firearm raycast bullet tool activated recoil strict server",
    "FANTASY_WEAPON": "roblox luau sword melee magic projectile LinearVelocity tool activated strict",
    "MODERN_ARMOR_HELMET": "roblox luau armor weld UpperTorso character equip ProximityPrompt strict",
    "FANTASY_ARMOR_HELMET": "roblox luau armor fantasy weld equip character strict luau",
    "AMMUNITION_CALIBER": "roblox luau ammunition bullet caliber damage penetration ModuleScript strict",
    "CORE_INVENTORY_SYSTEM": "roblox luau inventory extraction game datastore PlayerAdded PlayerRemoving strict",
    "CORE_INBOX_SYSTEM": "roblox luau mailbox inbox datastore player message reward claim strict",
      "GUI_HUD": "roblox luau LocalScript ScreenGui Frame TextLabel TextButton UDim2 Color3 UICorner HUD strict",
      "SHOP_UI": "roblox luau LocalScript ScreenGui shop store buy sell RemoteFunction ScrollingFrame strict",
      "INVENTORY_UI": "roblox luau LocalScript ScreenGui inventory grid UIGridLayout slot item strict",
      "HEALTH_BAR": "roblox luau LocalScript ScreenGui health bar TweenService humanoid HealthChanged strict",
      "LEADERBOARD": "roblox luau LocalScript ScreenGui leaderboard scoreboard UIListLayout strict",
      "NOTIFICATION": "roblox luau LocalScript ScreenGui notification toast popup TweenService strict",
      "MINIMAP": "roblox luau LocalScript ScreenGui minimap compass RunService Heartbeat strict",
    "NPC_TRADER": "roblox luau NPC trader shop ProximityPrompt RemoteFunction weld constraint server",
    "LOBBY_SPACESHIP": "roblox luau lobby floating Part anchored CanCollide strict no raycast",
    "FURNITURE": "roblox luau furniture Part anchored hitbox separation no raycast strict",
    "SMELTING_FURNACE": "roblox luau crafting smelting ProximityPrompt ParticleEmitter task wait strict",
    "CORE_MISSION_SYSTEM": "roblox luau quest mission datastore daily reset reward strict server",
    "CORE_MONETIZATION_SYSTEM": "roblox luau MarketplaceService ProcessReceipt datastore strict receipt",
    "AUDIO_SYSTEM": "roblox luau Sound BGM SFX TweenService ContentProvider PreloadAsync strict",
    "CORE_WORLD_GENERATION": "roblox luau procedural terrain generation raycast baseplate anchored strict",
    "BIOME_SYSTEM": "roblox luau biome system CollectionService tag tree rock raycast hitbox strict",
    "DAY_NIGHT_CYCLE": "roblox luau day night Lighting ClockTime TweenService RunService Heartbeat strict",
    "WEATHER_DISASTER": "roblox luau weather rain storm fog Lighting ParticleEmitter TweenService strict",
    "RESOURCE_NODE_MANAGER": "roblox luau resource node IntValue Health CollectionService respawn strict",
    "ITEM_CATEGORY_DATABASE": "roblox luau ModuleScript item database ReplicatedStorage category return",
    "FLEA_MARKET_BACKEND": "roblox luau trading DataStore RemoteFunction zero trust strict server",
    "CORE_INBOX_UI": "roblox luau UI LocalScript ScreenGui Frame TextLabel RemoteEvent strict",
    "NPC_SHOP_UI": "roblox luau shop UI LocalScript ScrollingFrame RemoteFunction strict",
    "PLAYER_FLEA_MARKET_UI": "roblox luau flea market UI LocalScript TextBox RemoteFunction strict",
    "CORE_MISSION_UI": "roblox luau quest UI LocalScript ScreenGui RemoteEvent strict",
    "DAILY_LOG_SYSTEM": "roblox luau daily log DataStore timestamp player server strict",
    "CORE_WORLD_SETTINGS": "roblox luau camera lock first person LockFirstPerson gravity Lighting strict",
}


class OmniSynthesizerAgent:
    def __init__(self, healer_agent: AutoHealerAgent, mcp_bridge=None):
        self.healer_agent = healer_agent
        self.mcp_bridge = mcp_bridge
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
            "Jika nama task mengandung kata MESHPART/MESH_PART/SPECIALMESH/SPECIAL_MESH/CREATURE/MONSTER/NPC_MESH/ORGANIC_MESH → "
            "kamu sedang membuat objek 3D berbasis MeshPart atau SpecialMesh. "
            "Sistem akan otomatis memanggil SmartUIAssetSelector untuk memilih mesh terbaik dari katalog. "
            "Jika tidak ada mesh yang cocok, aura fallback (ParticleEmitter + SelectionBox) digunakan sebagai pembeda visual TERAKHIR. "
            "Ini HANYA terjadi jika benar-benar tidak ada MeshPart/SpecialMesh yang cocok — bukan pilihan utama. "
            "Jika nama task mengandung kata MODEL/PROP/BUILDING/TREE/ROCK/VEHICLE/WEAPON_MODEL/ARMOR_MODEL/CHEST/BARREL/CRATE/DOOR → "
            "kamu sedang membuat logika 3D Model (Script di dalam Model di Workspace). "
            "Jika nama task mengandung kata WORLD/TERRAIN/LIGHTING/ATMOSPHERE/SKYBOX/SPAWN/ZONE/MAP/AMBIENT/FOG → "
            "kamu sedang membuat script lingkungan dunia (Script di ServerScriptService atau Workspace). "
            "Kode Luau yang kamu hasilkan akan OTOMATIS dibungkus ke file .rbxmx oleh Asset Engine. "
            "Tugas kamu: tulis HANYA logika Luau murni yang sesuai tipe task tersebut.\n"
            "PENTING: JANGAN mengambil jalan pintas. Buat aset yang SEBENARNYA.\n"
            "CONTOH STRUKTUR ASET YANG BENAR:\n"
            "- GUI: Harus ada Instance.new('ScreenGui'), Frame, TextLabel, dll. dengan properti visual lengkap (Size, Position, Color3, Font).\n"
            "- MODEL: Harus ada Instance.new('Model'), Part (sebagai PrimaryPart), dan Script di dalamnya.\n"
            "- MESH: Gunakan SpecialMesh atau MeshPart dengan rbxassetid yang valid (atau placeholder visual yang kuat).\n"
            "DILARANG hanya menulis komentar '-- asset code here'. Kamu WAJIB mengimplementasikan visualnya."
        )

    @staticmethod
    def _build_error_augmentation(previous_error: str, forb_keys: list) -> str:
        """
        Bangun augmentasi prompt berdasarkan analisis mendalam error sebelumnya.
        TIDAK mengurangi prompt yang sudah ada — hanya MENAMBAHKAN konteks error-spesifik.
        AI healer bisa self-diagnose dan generate instruksi perbaikan yang akurat.
        """
        augment_parts = []

        # --- Deteksi: Forbidden keyword (_G, shared, dll) ---
        for fk in forb_keys:
            if fk in previous_error and ("Dilarang keras" in previous_error or "Contract Violation" in previous_error):
                part = (
                    f"\n[PERINGATAN KRITIS - PERCOBAAN SEBELUMNYA DITOLAK KARENA KEYWORD TERLARANG: '{fk}']\n"
                    f"Kode kamu DITOLAK karena mengandung '{fk}'. Ini TIDAK BOLEH TERJADI.\n"
                )
                if fk == "_G":
                    part += (
                        "SOLUSI WAJIB — Ganti _G dengan pola ModuleScript + require():\n"
                        "  SALAH  : _G.PlayerData = {}\n"
                        "  BENAR  : Buat ModuleScript di ReplicatedStorage, lalu:\n"
                        "           local M = require(game.ReplicatedStorage.GameData)\n"
                        "           M.PlayerData = {}\n"
                        "Pastikan string \"_G\" tidak muncul sama sekali dalam kode output.\n"
                    )
                elif fk == "shared":
                    part += "SOLUSI: Ganti \"shared\" dengan ModuleScript di ReplicatedStorage.\n"
                augment_parts.append(part)

        # --- Deteksi: Rojo Property Type Mismatch ---
        if "Rojo Type Violation" in previous_error or "Property type mismatch" in previous_error or "DisplayOrder" in previous_error:
            augment_parts.append(
                "\n[ROJO TYPE MISMATCH TERDETEKSI — ATURAN WAJIB PROPERTI ROBLOX]:\n"
                "DisplayOrder, ZIndex, LayoutOrder, TextSize = angka integer (Int32), BUKAN Enum.\n"
                "SALAH: gui.DisplayOrder = Enum.ZIndexBehavior.Global\n"
                "BENAR: gui.DisplayOrder = 5\n"
                "ZIndexBehavior adalah properti BERBEDA: gui.ZIndexBehavior = Enum.ZIndexBehavior.Global\n"
            )

        # --- Deteksi: Contract Violation — keyword wajib tidak digunakan ---
        if "Anda diwajibkan menggunakan" in previous_error:
            augment_parts.append(
                "\n[KEYWORD WAJIB BELUM DITEMUKAN]:\n"
                f"Error sebelumnya: {previous_error[:300]}\n"
                "Pastikan semua keyword dari daftar [KEYWORD WAJIB] benar-benar hadir dalam kode.\n"
            )

        # --- Deteksi: Syntax error Lune runtime ---
        if "syntax error" in previous_error.lower() or "RUNTIME EXECUTION FAILED" in previous_error:
            import re as _re2
            _lm = _re2.search(r':(\d+):', previous_error)
            _line_hint = f" di baris {_lm.group(1)}" if _lm else ""
            augment_parts.append(
                f"\n[SYNTAX ERROR{_line_hint} — PERCOBAAN SEBELUMNYA GAGAL RUNTIME]:\n"
                f"Error: {previous_error[:250]}\n"
                "Periksa semua statement: tidak boleh terpotong, tidak ada expression tanpa assignment.\n"
            )

        if not augment_parts:
            augment_parts.append(
                f"\n[AUTONOMOUS SELF-DIAGNOSIS — ERROR TIDAK DIKENALI PATTERN MATCHER]:\n"
                f"Error dari percobaan sebelumnya:\n{previous_error[:500]}\n\n"
                f"INSTRUKSI UNTUK AI HEALER (SELF-DIAGNOSE):\n"
                f"1. Baca error message di atas secara LITERAL kata per kata.\n"
                f"2. Identifikasi: apakah ini syntax error, runtime error, type mismatch, atau contract violation?\n"
                f"3. Cari BARIS KODE SPESIFIK yang menyebabkan error tersebut.\n"
                f"4. Tulis ulang SELURUH kode dengan perbaikan pada baris yang bermasalah.\n"
                f"5. Pastikan kode baru TIDAK mengandung pola yang sama yang menyebabkan error.\n"
                f"6. VERIFIKASI MENTAL: jalankan kode secara mental baris per baris sebelum output.\n"
                f"JANGAN mengulangi kode yang sama. Jika error SAMA dengan sebelumnya,\n"
                f"ambil pendekatan BERBEDA TOTAL untuk menyelesaikan tugas ini.\n"
            )

        return (
            "\n[ANALISIS MENDALAM ERROR PERCOBAAN SEBELUMNYA — BACA DAN PERBAIKI SEBELUM MENULIS KODE]:\n"
            + "".join(augment_parts)
            + "\n"
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
            f"07b. ATURAN ASET MESHPART/SPECIALMESH (jika nama task = MESHPART/CREATURE/MONSTER/NPC_MESH/ORGANIC_MESH dll):\n"
            f"    → Sistem OTOMATIS memanggil SmartUIAssetSelector untuk memilih mesh terbaik dari MESH_ASSET_CATALOG.\n"
            f"    → PRIORITAS: MeshPart (rbxassetid spesifik) > SpecialMesh (built-in Roblox) > Aura fallback.\n"
            f"    → AURA FALLBACK (ParticleEmitter + SelectionBox) hanya digunakan jika benar-benar tidak ada mesh yang cocok.\n"
            f"    → ⚠️ PERINGATAN KERAS: Jika aura fallback aktif, muncul log merah di terminal. "
            f"Tambahkan rbxassetid yang tepat ke MESH_ASSET_CATALOG segera.\n"
            f"    → Kode Luau yang dihasilkan OTOMATIS dimasukkan ke dalam Model rbxmx oleh Asset Engine.\n\n"
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

        # === INJEKSI INSTRUKSI SPESIFIK PER KATEGORI ===
        _task_category = "_".join(module_name.split("_")[:-1]) if "_" in module_name else module_name
        _specific_inst = TASK_SPECIFIC_INSTRUCTIONS.get(_task_category, "")
        if _specific_inst:
            comprehensive_prompt += (
                f"[PANDUAN TEKNIS KHUSUS UNTUK KATEGORI '{_task_category}']:\n"
                f"{_specific_inst}\n\n"
            )
            console_terminal_interface.print(
                f"[bold magenta]  [TASK-PROMPT] Instruksi '{_task_category}' disuntikkan.[/bold magenta]"
            )
        # ===============================================

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

            # ── ESKALASI KHUSUS: Recipe kosong ─────────────────────────────────
            if "Recipe" in previous_error or "Crafting Logic Violation" in previous_error:
                comprehensive_prompt += (
                    "[DARURAT — RECIPE KOSONG TERDETEKSI]:\n"
                    "Kamu terus mengirim Recipe kosong! Validator MENOLAK Recipe = {} tanpa isi.\n"
                    "SEKARANG JUGA, GUNAKAN PERSIS BARIS INI di dalam skripmu (SALIN PERSIS):\n"
                    "\n"
                    "local Recipe = {Kevlar = 3, Iron = 2, Wood = 1}\n"
                    "\n"
                    "PENJELASAN ATURAN VALIDATOR:\n"
                    "- DITOLAK : local Recipe = {}\n"
                    "- DITOLAK : local Recipe = {\n}\n"
                    "- DITERIMA: local Recipe = {Kevlar = 3, Iron = 2}\n"
                    "- DITERIMA: local Recipe = {DragonScale = 5, Mithril = 3}\n"
                    "Tulis bahan baku sesederhana mungkin dengan kata tanpa spasi.\n"
                    "Contoh nama bahan: Iron, Wood, Kevlar, Crystal, Leather, Stone, Bone\n\n"
                )
                console_terminal_interface.print(
                    f"[bold red]  [ESCALATION] Recipe kosong terdeteksi — menyuntikkan instruksi DARURAT![/bold red]"
                )

            # ── ESKALASI KHUSUS: ArmorTier salah ───────────────────────────────
            if "ArmorTier" in previous_error:
                comprehensive_prompt += (
                    "[DARURAT — ArmorTier SALAH]:\n"
                    "ArmorTier HARUS berupa angka 1 sampai 6. Gunakan persis ini:\n"
                    "local ArmorTier: number = 3\n\n"
                )

            # ── ESKALASI KHUSUS: MaterialType salah ────────────────────────────
            if "MaterialType" in previous_error:
                comprehensive_prompt += (
                    "[DARURAT — MaterialType SALAH]:\n"
                    "MaterialType HARUS string tanpa spasi. Pilih salah satu:\n"
                    "local MaterialType: string = 'Ceramic'\n"
                    "-- atau: 'Steel', 'Kevlar', 'Mithril', 'Leather', 'Dragonscale'\n\n"
                )

            # ── ESKALASI KHUSUS: ItemCategory salah ────────────────────────────
            if "ItemCategory" in previous_error or "Economy Taxonomy" in previous_error:
                comprehensive_prompt += (
                    "[DARURAT — ItemCategory SALAH]:\n"
                    "ItemCategory HARUS persis salah satu string ini:\n"
                    "local ItemCategory: string = 'Armor'\n"
                    "-- Pilihan resmi: 'Weapon', 'Ammunition', 'Armor', 'Medical', 'Material', 'Valuable', 'Bait', 'Tool'\n\n"
                )
            # ───────────────────────────────────────────────────────────────────

        console_terminal_interface.print(
            f"[bold cyan]  [{agent['name']}] Memproses {module_name}... (Antri Sequential - Standar Militer)[/bold cyan]"
        )

        console_terminal_interface.print(
            f"[dim cyan]  🔍 Menjalankan RAG Pipeline: Membaca Kitab DevForum & Ekstrak Raw GitHub...[/dim cyan]"
        )
        _rag_cat = "_".join(module_name.split("_")[:-1]) if "_" in module_name else module_name
        clean_task_query = TASK_RAG_QUERIES.get(
            _rag_cat,
            LuauKnowledgeScraper._clean_task_query(module_name)
        )
        console_terminal_interface.print(f"[dim cyan]  RAG Query: '{clean_task_query}'[/dim cyan]")
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
            live_rag_data += (
                  "DOKTRIN ADAPTASI RAG (WAJIB DIPATUHI):\n"
                  "1. FILTER STANDALONE: Kode dari GitHub mungkin standalone — adaptasikan ke arsitektur game ini.\n"
                  "2. MUSNAHKAN ASSET ID: DILARANG KERAS menyalin atau menggunakan rbxassetid://, rbxthumb://, "
                  "   Content ID, atau angka ID aset apapun dari kode GitHub. SEMUA aset visual WAJIB dibuat via "
                  "   Instance.new() dengan properti BackgroundColor3, TextColor3, UICorner, UIStroke, dll.\n"
                  "3. JANGAN SALIN BULAT-BULAT: Gunakan sebagai referensi logika saja, bukan template visual.\n"
                  "4. PRIORITASKAN INSTRUKSI TASK DI ATAS: Aturan task lebih penting dari contoh GitHub.\n"
              )
            comprehensive_prompt += live_rag_data
            console_terminal_interface.print(f"[dim green]  ✅ DevForum dan Raw GitHub disuntikkan ke prompt.[/dim green]")


        # === AUGMENTASI PROMPT DINAMIS BERDASARKAN ANALISIS ERROR SEBELUMNYA ===
        if previous_error:
            _aug = OmniSynthesizerAgent._build_error_augmentation(previous_error, forb_keys)
            if _aug:
                comprehensive_prompt += _aug

        success, result_data = await execute_gemini_cli_pure(agent, self.sys_inst, comprehensive_prompt)

        if success:
            code_attempt = result_data
            if '"mcp_tool_call"' in code_attempt:
                return False, "Agent Error: Synthesizer tidak boleh memanggil Tool MCP. Hanya Healer yang diizinkan.", previous_code

            if previous_code and previous_error:
                safe_prev_code = extract_pure_luau_code(previous_code)
                similarity = difflib.SequenceMatcher(None, safe_prev_code, code_attempt).ratio()

                # Threshold diturunkan ke 0.03 (3%) — kode yang diperbaiki total wajar sangat berbeda.
                # 0.15 terlalu agresif dan menolak perbaikan valid sehingga healer terlihat tidak bekerja.
                if similarity < 0.03 or (len(code_attempt.strip()) < 20):
                    console_terminal_interface.print(
                        f"[bold red]  [SANITY CHECK GAGAL]: Kode baru hanya {similarity*100:.1f}% mirip DAN sangat pendek — terindikasi kosong/halusinasi. DITOLAK.[/bold red]"
                    )
                    return False, f"SANITY_CHECK_FAILED: Kode baru hampir kosong ({similarity*100:.1f}% similarity, {len(code_attempt.strip())} char).", previous_code

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


import threading as _threading

# Pause event yang di-set oleh nexus_telegram_bot.py via import.
# Didefinisikan di sini (nexus_agents) sebagai sumber kebenaran tunggal,
# agar tidak terjadi circular import dan kedua modul memakai OBJEK YANG SAMA.
_roblox_agent_paused = _threading.Event()
_roblox_agent_paused.set()  # Default: aktif (not paused)


class NexusGlobalState:
    """State global agent — diakses oleh telegram bot untuk stop/continue."""
    is_running: bool = True
    current_task: str = ""
    total_tasks_done: int = 0
    total_tasks_failed: int = 0
    # Sakelar override: True = semua agen otonom berhenti/tidur sementara
    TELEGRAM_OVERRIDE_ACTIVE: bool = False
    # Flag graceful shutdown
    BOT_SHUTTING_DOWN: bool = False


# Memory global antar sesi
# global_agent_memory remaped ke bawah — gunakan instance AgentMemory() dari atas (L626)


async def execute_with_persistent_retry(
    task_func,
    task_name: str,
    send_telegram_fn,
    max_attempts: int = 0,
    github_search_fn=None,
    extra_context: str = "",
):
    """
    Wrapper retry tanpa batas (infinite) — task WAJIB selesai 100%, tidak pernah di-skip.

    Pipeline per percobaan:
      1. Cek pause (/stop) — tunggu /continue jika di-pause
      2. Jalankan task_func(extra_context=...)
      3. Jika gagal >= 3x: cari panduan di GitHub lalu retry
      4. Tidak ada batas percobaan — terus sampai berhasil
    Catatan: max_attempts dipertahankan sebagai parameter untuk kompatibilitas,
    tapi nilainya diabaikan (selalu infinite).
    """
    last_error = ""
    github_context = extra_context
    attempt = 0

    while True:
        attempt += 1
        NexusGlobalState.current_task = task_name

        # === CEK PAUSE ===
        if not _roblox_agent_paused.is_set():
            print(f"[Agent PAUSE] Task '{task_name}' dijeda. Menunggu /continue...")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _roblox_agent_paused.wait)
            print(f"[Agent RESUME] Task '{task_name}' dilanjutkan.")

        # === CEK STOP GLOBAL ===
        if not NexusGlobalState.is_running:
            return None

        try:
            result = await task_func(extra_context=github_context)
            NexusGlobalState.total_tasks_done += 1
            return result

        except asyncio.CancelledError:
            raise  # Propagate CancelledError agar /stop bisa bekerja

        except Exception as e:
            last_error = str(e)
            print(f"[Retry {attempt}] '{task_name}': {last_error[:100]}")

        # Setelah 3x gagal: cari panduan di GitHub (sekali, tidak berulang setiap 3x)
        if attempt == 3 and github_search_fn:
            query = f"roblox luau {task_name} {last_error[:40]}"
            try:
                github_result = await github_search_fn(query)
                if github_result:
                    github_context = github_result
                    print(f"[GitHub Context] +{len(github_context)} char konteks ditambahkan untuk '{task_name}'")
            except Exception:
                pass

        # Tunggu sebelum retry (maks 60 detik)
        wait = min(10 * attempt, 60)
        await asyncio.sleep(wait)


async def execute_antigravity_fleet(
    user_report: str,
    status_message,
    bot_instance,
    chat_id: str,
):
    """
    Entry point dari nexus_telegram_bot.py untuk Mode Roblox.

    Arsitektur Worker Pool:
    - N worker berjalan paralel (N = jumlah API key aktif, maks 10)
    - Setiap worker mengambil 1 task dari antrian, mengerjakannya sampai LULUS validasi,
      baru kemudian mengambil task berikutnya
    - Task TIDAK PERNAH di-skip — infinite retry sampai 100% berhasil
    - Jika 3x gagal, ambil panduan dari GitHub sebelum retry berikutnya
    """
    from nexus_project_scanner import scan_existing_project, scan_deep_validate

    n_workers = len(ACTIVE_AGENTS)  # Biasanya 10

    # ── Progress tracking ──────────────────────────────────────────────────
    progress = {
        "done": 0,
        "total": 0,
        "worker_status": ["idle"] * n_workers,
        "stopped": False,
    }

    async def send_fn(text: str):
        try:
            await status_message.edit_text(text[:4000])
        except Exception:
            pass

    # ── 1. Scan mendalam isi file ──────────────────────────────────────────
    await send_fn("Scanning isi file project (deep scan)...")
    project_context = await scan_existing_project()
    validate_result = await scan_deep_validate(auto_fix=True)

    if validate_result.get("fixed", 0) > 0:
        await send_fn(
            "Auto-fix " + str(validate_result["fixed"]) + " file bermasalah selesai.\n"
            "Melanjutkan ke analisis laporan..."
        )

    # ── 2. Analisis laporan → daftar task (Alur Terstruktur) ───────────────
    await send_fn("🔍 Menganalisis laporan & Menyusun Daftar Tugas (Google Antigravity Mode)...")

    try:
        fleet_leader = OmniSynthesizerAgent(
            agent_id="fleet_leader",
            api_key=ACTIVE_AGENTS[0]["api_key"],
        )
        tasks = await fleet_leader.analyze_user_report(user_report, project_context)
        
        # Pastikan tugas mencakup poin-poin yang diminta pengguna jika relevan
        if not tasks:
            raise Exception("Gagal menghasilkan daftar tugas.")
            
    except Exception as e:
        # Fallback ke daftar tugas terstruktur sesuai permintaan pengguna
        tasks = [
            {"id": 1, "title": f"Membaca repository github https://github.com/vraafi/Robloxotonom", "detail": "Analisis struktur dan kode sumber repositori.", "status": "pending"},
            {"id": 2, "title": "Mencari solusi dan memberikan solusi dari kasus pengguna", "detail": user_report, "status": "pending"},
            {"id": 3, "title": "Menyimpulkan dan memperbaiki/menambahkan logika fitur", "detail": "Integrasi solusi ke dalam project.", "status": "pending"},
            {"id": 4, "title": "Memberikan instruksi token akses atau hasil akhir", "detail": "Finalisasi dan serah terima.", "status": "pending"}
        ]

    def _generate_antigravity_display(current_tasks, done_count, total_count):
        display = "🚀 **NEXUS ANTIGRAVITY TASK LIST**\n"
        display += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        for i, t in enumerate(current_tasks, 1):
            status_icon = "⏳"
            if t.get("status") == "completed": status_icon = "✅"
            elif t.get("status") == "running": status_icon = "🔄"
            elif t.get("status") == "failed": status_icon = "❌"
            
            display += f"{i}. {status_icon} {t.get('title', 'Tugas ' + str(i))}\n"
            if t.get("status") == "running":
                display += f"   └─ ⚡ *Sedang dikerjakan...*\n"
        display += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        display += f"📊 Progress: {done_count}/{total_count} Selesai | ♾️ Infinity Retry: AKTIF\n"
        return display

    await send_fn(_generate_antigravity_display(tasks, 0, len(tasks)))
    await asyncio.sleep(3)

    progress["total"] = len(tasks)

    # ── 3. Antrian task ───────────────────────────────────────────────────
    task_queue: asyncio.Queue = asyncio.Queue()
    for t in tasks:
        await task_queue.put(t)

    # ── 4. Status updater (update Telegram setiap 20 detik) ───────────────
    async def status_updater():
        while not progress["stopped"]:
            await asyncio.sleep(20)
            if progress["stopped"]:
                break
            lines = [
                "STATUS NEXUS FLEET",
                "Progress: " + str(progress["done"]) + "/" + str(progress["total"]),
                "",
            ]
            for i, ws in enumerate(progress["worker_status"]):
                lines.append("Worker " + str(i + 1) + ": " + ws)
            try:
                await status_message.edit_text("\n".join(lines)[:4000])
            except Exception:
                pass

    # ── 5. Worker function ────────────────────────────────────────────────
    async def worker(worker_id: int):
        """
        Satu worker = satu API key.
        Ambil task → kerjakan sampai lulus validasi → ambil task berikutnya.
        """
        agent_cfg = ACTIVE_AGENTS[worker_id % len(ACTIVE_AGENTS)]

        while True:
            # Ambil task berikutnya dari antrian
            try:
                task = task_queue.get_nowait()
            except asyncio.QueueEmpty:
                progress["worker_status"][worker_id] = "idle (selesai)"
                return

            task_name = task.get("title", "Task " + str(task.get("id", "?")))
            attempt = 0
            github_context = ""

            # Retry tanpa batas sampai task lulus validasi
            while True:
                attempt += 1

                # Cek pause (/stop dari Telegram)
                if not _roblox_agent_paused.is_set():
                    progress["worker_status"][worker_id] = "[PAUSE] " + task_name
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, _roblox_agent_paused.wait)

                # Cek stop global
                if not NexusGlobalState.is_running:
                    progress["stopped"] = True
                    task_queue.task_done()
                    return

                progress["worker_status"][worker_id] = (
                    "[" + agent_cfg.get("name", "W" + str(worker_id)) + "] "
                    "Percobaan " + str(attempt) + " — " + task_name[:50]
                )

                # Setelah 3x gagal: ambil panduan dari GitHub
                if attempt == 3:
                    try:
                        q = "roblox luau " + task_name + " " + github_context[:30]
                        gh = await LuauKnowledgeScraper.search_github_luau(q)
                        if gh:
                            github_context = gh
                    except Exception:
                        pass

                try:
                    task["status"] = "running"
                    await send_fn(_generate_antigravity_display(tasks, progress["done"], progress["total"]))
                    
                    success, result_msg = await _execute_one_task_validated(
                        task, agent_cfg, github_context
                    )

                    if success:
                        task["status"] = "completed"
                        progress["done"] += 1
                        NexusGlobalState.total_tasks_done += 1
                        progress["worker_status"][worker_id] = (
                            "DONE (" + str(progress["done"]) + "/" + str(progress["total"]) + ") " + task_name[:40]
                        )
                        await send_fn(_generate_antigravity_display(tasks, progress["done"], progress["total"]))
                        break  # Lanjut ke task berikutnya

                    else:
                        # Validasi gagal — jangan lanjut, retry task yang sama
                        task["status"] = "failed"
                        progress["worker_status"][worker_id] = (
                            "[Retry " + str(attempt) + "] VALIDASI GAGAL — " + result_msg[:60]
                        )
                        wait = min(10 * attempt, 60)
                        await asyncio.sleep(wait)

                except asyncio.CancelledError:
                    raise

                except Exception as e:
                    progress["worker_status"][worker_id] = (
                        "[Retry " + str(attempt) + "] ERROR — " + str(e)[:60]
                    )
                    wait = min(10 * attempt, 60)
                    await asyncio.sleep(wait)

            task_queue.task_done()

    # ── 6. Jalankan workers + status updater secara paralel ───────────────
    updater_task = asyncio.create_task(status_updater())
    try:
        await asyncio.gather(*[worker(i) for i in range(n_workers)])
    finally:
        progress["stopped"] = True
        updater_task.cancel()
        try:
            await updater_task
        except asyncio.CancelledError:
            pass

    # ── 7. Build Rojo setelah semua task selesai ──────────────────────────
    final_msg = (
        "FLEET SELESAI!\n\n"
        "Berhasil: " + str(progress["done"]) + "/" + str(progress["total"]) + " task\n\n"
        "Memulai Rojo build..."
    )
    await send_fn(final_msg)

    try:
        from nexus_main import RobloxDeployer
        build_ok, stderr = RobloxDeployer.compile_rojo()
        if build_ok:
            await send_fn("Build berhasil! File .rbxl siap dipakai.")
        else:
            await send_fn("Build gagal:\n" + stderr[:300])
    except Exception as e:
        await send_fn("Build error: " + str(e)[:200])


async def _execute_one_task_validated(
    task: dict,
    agent_cfg: dict,
    extra_context: str = "",
) -> tuple:
    """
    Eksekusi satu task Roblox menggunakan API key spesifik dari worker yang mengerjakan.

    Wajib lulus dua lapis validasi sebelum file disimpan:
      1. AbsoluteOmniValidator — cek keyword wajib/terlarang + aturan Luau
      2. NativeLuauCompiler   — verifikasi AST/syntax Luau native

    Return:
      (True,  "")          jika kode lulus semua validasi dan berhasil disimpan
      (False, error_msg)   jika validasi gagal (caller wajib retry)
    """
    import re as _re
    from nexus_config import SOURCE_CODE_DIRECTORY as _SRC

    hint = task.get("target_file_hint", "")
    folder = task.get("target_folder", "")
    detail = task.get("detail", "")
    action = task.get("action", "fix_bug")
    task_title = task.get("title", "unknown")

    # ── Tentukan path file output ────────────────────────────────────────
    file_path = None
    if hint and hint != "unknown":
        for root, dirs, files in os.walk(_SRC):
            for fname in files:
                if hint.lower() in fname.lower() and fname.endswith((".lua", ".luau", ".rbxmx")):
                    file_path = os.path.join(root, fname)
                    break

    if file_path and os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            original_code = f.read()
    else:
        original_code = ""
        safe_name = _re.sub(r"[^\w]", "_", task_title).upper()
        if folder == "ServerScriptService":
            fname = safe_name + ".server.lua"
        elif folder in ("StarterGui", "StarterPlayerScripts", "StarterCharacterScripts"):
            fname = safe_name + ".client.lua"
        else:
            fname = safe_name + ".lua"
        file_path = os.path.join(_SRC, folder, fname)

    code_context = original_code[:4000] if original_code else "(File baru)"
    ctx_extra = ("\nKONTEKS DARI GITHUB:\n" + extra_context) if extra_context else ""

    # ── Tentukan file type ───────────────────────────────────────────────
    if folder == "ServerScriptService":
        file_type = "Server Script (Script)"
    elif folder in ("StarterGui", "StarterPlayerScripts", "StarterCharacterScripts"):
        file_type = "Client Script (LocalScript)"
    else:
        file_type = "ModuleScript"

    # ── Buat prompt untuk Gemini ─────────────────────────────────────────
    sys_inst = (
        "Kamu adalah senior Roblox Luau developer ahli.\n"
        "Output HANYA kode Luau murni — tidak ada penjelasan, tidak ada markdown fence.\n"
        "Baris pertama WAJIB --!strict\n"
        "Jangan gunakan Enum untuk DisplayOrder, ZIndex, LayoutOrder (pakai integer).\n"
        "Semua koneksi event WAJIB disimpan ke variabel (anti memory leak).\n"
        "Operasi DataStore WAJIB dibungkus pcall()."
    )

    prompt = (
        "TIPE FILE: " + file_type + "\n"
        "NAMA TUGAS: " + task_title + "\n"
        "AKSI: " + action + "\n"
        "DETAIL TUGAS:\n" + detail + "\n\n"
        "KODE SAAT INI:\n" + code_context
        + ctx_extra + "\n\n"
        "Tulis kode " + file_type + " yang lengkap dan benar untuk task ini."
    )

    # ── Panggil Gemini dengan API key worker ini ─────────────────────────
    success, raw_output = await execute_gemini_cli_pure(agent_cfg, sys_inst, prompt)

    if not success:
        return False, "Gemini gagal: " + raw_output[:150]

    generated_code = extract_pure_luau_code(raw_output)

    if not generated_code or len(generated_code.strip()) < 10:
        return False, "Output Gemini kosong atau terlalu pendek"

    # ── Validasi Lapis 1: AbsoluteOmniValidator ──────────────────────────
    # req_keys dan forb_keys kosong untuk task umum (validator tetap cek
    # aturan dasar --!strict, rbxassetid, dll.)
    omni_ok, omni_msg = AbsoluteOmniValidator.execute_validation(generated_code, [], [])
    if not omni_ok:
        return False, "OmniValidator gagal: " + omni_msg[:200]

    # ── Validasi Lapis 2: NativeLuauCompiler AST ─────────────────────────
    module_name = _re.sub(r"[^\w]", "_", task_title).upper()
    ast_ok, ast_msg = await NativeLuauCompiler.execute_native_ast_verification(
        generated_code, module_name
    )
    if not ast_ok:
        return False, "AST/Syntax gagal: " + ast_msg[:200]

    # ── Kedua validasi lulus → simpan file ──────────────────────────────
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(generated_code)

    return True, "OK: " + os.path.basename(file_path)


async def _execute_one_task(task: dict, extra_context: str = "") -> str:
    """
    Wrapper kompatibilitas backward — memanggil _execute_one_task_validated
    dengan agent pertama. Hanya dipakai oleh execute_with_persistent_retry lama.
    """
    ok, msg = await _execute_one_task_validated(task, ACTIVE_AGENTS[0], extra_context)
    if not ok:
        raise Exception(msg)
    return msg


