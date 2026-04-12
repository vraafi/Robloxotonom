import asyncio
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
from typing import Tuple

from nexus_healer import ApexKeyRotator

from rich.progress import Progress, SpinnerColumn, TextColumn

from nexus_config import (
    console_terminal_interface,
    TEMP_IO_DIRECTORY,
    ACTIVE_AGENTS,
    APIKeyRotator,
    GEMINI_CLI_PATH,
    ROBLOX_MCP_URL,
)
from nexus_database import retrieve_ecosystem_context, save_verified_module
from nexus_compiler import AbsoluteOmniValidator, NativeLuauCompiler

_key_rotator = ApexKeyRotator([a["api_key"] for a in ACTIVE_AGENTS if a["api_key"]])

CLI_EXECUTION_SEMAPHORE = asyncio.Semaphore(1)

MARKDOWN_BLOCK = chr(96) * 3

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
            "id": 1
        }
        
        def _post():
            try:
                res = requests.post(f"{ROBLOX_MCP_URL}/jsonrpc", json=payload, timeout=45)
                if res.status_code == 200:
                    return res.text
                return f"MCP_ERROR: Kode {res.status_code} | Pesan: {res.text}"
            except Exception as e:
                return f"MCP_CONNECTION_FAILED: Pastikan ngrok aktif di PC Lokal Anda. Detail: {str(e)}"

        loop = asyncio.get_event_loop()
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

    @staticmethod
    async def search_github_luau(query: str) -> str:
        try:
            encoded_query = query.replace(" ", "+")
            url = f"https://api.github.com/search/code?q={encoded_query}+roblox+pushed:>2024-01-01&per_page=2"
            
            command = [
                "curl", "-s", "--max-time", "15",
                "-H", "Accept: application/vnd.github.v3+json",
                "-H", "User-Agent: NexusAgent/1.0"
            ]
            
            github_token = os.getenv("GITHUB_TOKEN", "")
            if github_token:
                command.extend(["-H", f"Authorization: Bearer {github_token}"])
                
            command.append(url)

            loop = asyncio.get_event_loop()
            proses = await loop.run_in_executor(None, lambda: subprocess.run(command, capture_output=True, text=True, timeout=20))
            if proses.returncode == 0 and proses.stdout:
                data = json.loads(proses.stdout)
                items = data.get("items", [])[:2]
                if items:
                    res = "GITHUB ROBLOX KNOWLEDGE (RAW FILE EXTRACT):\n"
                    for item in items:
                        repo_name = item.get('repository', {}).get('full_name', '')
                        file_name = item.get('name', '')
                        file_api_url = item.get('url', '')
                        
                        if file_api_url:
                            raw_cmd = [
                                "curl", "-s", "--max-time", "10",
                                "-H", "Accept: application/vnd.github.v3.raw",
                                "-H", "User-Agent: NexusAgent/1.0"
                            ]
                            if github_token:
                                raw_cmd.extend(["-H", f"Authorization: Bearer {github_token}"])
                            raw_cmd.append(file_api_url)
                            
                            raw_proses = await loop.run_in_executor(None, lambda: subprocess.run(raw_cmd, capture_output=True, text=True, timeout=15))
                            if raw_proses.returncode == 0 and raw_proses.stdout:
                                raw_code = raw_proses.stdout[:4000]
                                res += f"--- FULL/RAW FILE: {repo_name}/{file_name} ---\n{raw_code}\n\n"
                    return res
        except Exception:
            pass
        return ""

    @staticmethod
    async def search_devforum(query: str) -> str:
        try:
            encoded_query = query.replace(" ", "+")
            url = f"https://devforum.roblox.com/search/query.json?q={encoded_query}"
            
            command = [
                "curl", "-s", "--max-time", "15",
                "-H", "User-Agent: NexusAgent/1.0",
                url
            ]
            loop = asyncio.get_event_loop()
            proses = await loop.run_in_executor(None, lambda: subprocess.run(command, capture_output=True, text=True, timeout=20))
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
                                "-H", "User-Agent: NexusAgent/1.0",
                                raw_post_url
                            ]
                            raw_proses = await loop.run_in_executor(None, lambda: subprocess.run(raw_cmd, capture_output=True, text=True, timeout=15))
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

    @staticmethod
    async def search_reddit_robloxdev(query: str) -> str:
        try:
            encoded_query = query.replace(" ", "+")
            url = f"https://www.reddit.com/r/robloxdev/search.json?q={encoded_query}&restrict_sr=1&limit=3"
            command = [
                "curl", "-s", "--max-time", "15",
                "-H", "User-Agent: NexusAgent/1.0",
                url
            ]
            loop = asyncio.get_event_loop()
            proses = await loop.run_in_executor(None, lambda: subprocess.run(command, capture_output=True, text=True, timeout=20))
            if proses.returncode == 0 and proses.stdout:
                data = json.loads(proses.stdout)
                posts = data.get("data", {}).get("children", [])[:3]
                if posts:
                    res = "REDDIT r/robloxdev DISCUSSIONS:\n"
                    for p in posts:
                        post_data = p.get("data", {})
                        title = post_data.get('title', '')
                        body_text = post_data.get('selftext', '')[:600]
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
                        "--temp", "1.0",
                        "--top-p", "0.95",
                        "--top-k", "64",
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
                    markdown_match = re.search(f'{MARKDOWN_BLOCK}(?:json)?\n(.*?)\n{MARKDOWN_BLOCK}', raw_output, re.DOTALL | re.IGNORECASE)
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
                    return False, f"GEMINI_CLI_NOT_FOUND: CLI tidak ditemukan."
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
        self.heal_history = {} 

    def _analyze_error_type(self, error_msg: str) -> str:
        error_lower = error_msg.lower()
        if "but got" in error_lower or "expected" in error_lower: return "TYPE_MISMATCH"
        elif "unknown" in error_lower and ("global" in error_lower or "type" in error_lower): return "UNDEFINED_REFERENCE"
        elif "syntax" in error_lower or "unexpected symbol" in error_lower: return "SYNTAX_ERROR"
        elif "cannot assign" in error_lower or "function only returns" in error_lower: return "ASSIGNMENT_ERROR"
        elif "unknown property" in error_lower or "not found" in error_lower: return "PROPERTY_ERROR"
        else: return "GENERIC_ERROR"

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
        target_filepath: str = ""
    ) -> str:
        last_error_line = compiler_error.splitlines()[-1] if compiler_error else "Unknown"
        error_type = self._analyze_error_type(compiler_error)
        
        if module_name not in self.heal_history:
            self.heal_history[module_name] = []
        self.heal_history[module_name].append(error_type)
        
        console_terminal_interface.print(f"[bold magenta]   [Auto-Healer] Membedah {module_name} ({error_type}): {last_error_line}[/bold magenta]")
        
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
            if github_context: base_prompt += github_context + "\n"
            if devforum_context: base_prompt += devforum_context + "\n"
            if reddit_context: base_prompt += reddit_context + "\n"
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
                console_terminal_interface.print(f"[dim yellow]   [Iterative Debugging] Turn {turn+1}/{max_mcp_turns}...[/dim yellow]")
            
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
                    
                    console_terminal_interface.print(f"[bold cyan]   🛠️ [MCP Action] AI Menjalankan Studio Tool: {tool_name}[/bold cyan]")
                    
                    tool_response = await RobloxMCPBridge.execute_tool(tool_name, tool_args)
                    mcp_history_log += f"\n--- CALL: {tool_name} ---\nARGS: {json.dumps(tool_args)}\nRESULT: {tool_response[:1000]}\n"
                    continue 
                except Exception as e:
                    mcp_history_log += f"\n--- CALL FAILED ---\nERROR: {str(e)}\n"
                    continue
            else:
                return extract_pure_luau_code(result_data)
        
        return broken_code


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
            "[INSTRUKSI TUGAS KHUSUS]:\n"
        )

        ecosystem_context = await retrieve_ecosystem_context()
        if ecosystem_context:
            comprehensive_prompt += f"[REFERENSI MODUL GLOBAL UNTUK REQUIRE()]:\n{ecosystem_context}\n\n"
        comprehensive_prompt += f"[INSTRUKSI TUGAS KHUSUS ({module_name})]:\n{task_description}\n\n"

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
        
        console_terminal_interface.print(f"[dim cyan]  🔍 Menjalankan RAG Pipeline: Membaca Kitab DevForum & Ekstrak Raw GitHub...[/dim cyan]")
        clean_task_query = LuauKnowledgeScraper._clean_task_query(module_name)
        github_context = await LuauKnowledgeScraper.search_github_luau(clean_task_query)
        devforum_context = await LuauKnowledgeScraper.search_devforum(clean_task_query)
        reddit_context = await LuauKnowledgeScraper.search_reddit_robloxdev(clean_task_query)
        
        if github_context or devforum_context or reddit_context:
            live_rag_data = "[KNOWLEDGE BASE (HASIL SCRAPING GITHUB RAW, DEVFORUM & REDDIT)]\n"
            if github_context: live_rag_data += github_context + "\n"
            if devforum_context: live_rag_data += devforum_context + "\n"
            if reddit_context: live_rag_data += reddit_context + "\n"
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
                    console_terminal_interface.print(f"[bold red]  [SANITY CHECK GAGAL]: Kode baru hanya {similarity*100:.1f}% mirip. File terindikasi kosong/halusinasi. DITOLAK.[/bold red]")
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
                    target_filepath=target_filepath
                )
                
                healed_omni_ok, healed_omni_msg = AbsoluteOmniValidator.execute_validation(healed_code, req_keys, forb_keys)
                if not healed_omni_ok:
                    return False, healed_omni_msg, healed_code
                
                healed_ast_ok, healed_ast_msg = await NativeLuauCompiler.execute_native_ast_verification(healed_code, module_name)
                if not healed_ast_ok:
                    return False, healed_ast_msg, healed_code

                code_attempt = healed_code

            os.makedirs(os.path.dirname(target_filepath), exist_ok=True)
            with open(target_filepath, "w", encoding="utf-8") as f:
                f.write(code_attempt)

            await save_verified_module(module_name, target_filepath, code_attempt)
            console_terminal_interface.print(f"[bold green]  ✅ [{module_name}] SUKSES! Disimpan & Diverifikasi.[/bold green]")
            return True, "", code_attempt

        else:
            return False, result_data, previous_code
