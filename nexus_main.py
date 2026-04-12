import asyncio
import os
import json
import subprocess
import requests
import aiofiles
import signal
import sys
import random
import time
from typing import Tuple

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

from rich.panel import Panel

from nexus_config import (
    console_terminal_interface,
    ROBLOX_UNIVERSE_ID,
    ROBLOX_PLACE_ID,
    ROBLOX_OPEN_CLOUD_API_KEY,
    PROJECT_ROOT_DIRECTORY,
    SOURCE_CODE_DIRECTORY,
    COMPILED_GAME_FILE,
    VPS_WEBHOOK_PORT,
    LIVE_JIT_MESSAGING_TOPIC,
    ACTIVE_AGENTS,
    TELEGRAM_CHAT_ID,
    TELEGRAM_BOT_TOKEN,
    GEMINI_CLI_PATH,
)
from nexus_database import (
    initialize_system_ledger,
    establish_database_connection,
    log_roblox_telemetry,
    get_unanalyzed_telemetry,
)
from nexus_compiler import NativeLuauCompiler
from nexus_agents import OmniSynthesizerAgent, AutoHealerAgent, LuauKnowledgeScraper


_telegram_semaphore = asyncio.Semaphore(1)
_last_telegram_send = 0.0
_min_interval_between_messages = 2.0


async def send_telegram_notification(message: str, important: bool = False):
    global _last_telegram_send

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    async with _telegram_semaphore:
        now = time.time()
        elapsed = now - _last_telegram_send

        if not important and elapsed < _min_interval_between_messages:
            await asyncio.sleep(_min_interval_between_messages - elapsed)

        _last_telegram_send = time.time()

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: requests.post(url, json=payload, timeout=10)
            )
        except Exception as e:
            console_terminal_interface.print(f"[dim yellow]Notifikasi Telegram gagal: {e}[/dim yellow]")


async def send_telegram_document(file_path: str, caption: str = ""):
    global _last_telegram_send

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    if not os.path.exists(file_path):
        console_terminal_interface.print(f"[bold red]Gagal mengirim ke Telegram: File {file_path} tidak ditemukan![/bold red]")
        return

    async with _telegram_semaphore:
        now = time.time()
        elapsed = now - _last_telegram_send
        if elapsed < _min_interval_between_messages:
            await asyncio.sleep(_min_interval_between_messages - elapsed)

        _last_telegram_send = time.time()

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"

        def _send():
            try:
                with open(file_path, 'rb') as f:
                    files = {'document': f}
                    data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption}
                    res = requests.post(url, data=data, files=files, timeout=120)
                    if res.status_code == 200:
                        console_terminal_interface.print("[bold green]✅ File .rbxl sukses dievakuasi ke Telegram![/bold green]")
                    else:
                        console_terminal_interface.print(f"[bold red]❌ Gagal kirim Telegram: {res.text}[/bold red]")
            except Exception as e:
                console_terminal_interface.print(f"[bold red]Exception Telegram Document: {e}[/bold red]")

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _send)


async def handle_roblox_telemetry(request):
    try:
        data = await request.json()
        await log_roblox_telemetry(
            data.get("server_id", "UNKNOWN"),
            data.get("event_type", "UNKNOWN"),
            data.get("event_data", {}),
        )
        return web.Response(text="TELEMETRY_LOGGED", status=200)
    except Exception as e:
        return web.Response(text=str(e), status=400)


async def start_telemetry_webhook():
    if not AIOHTTP_AVAILABLE:
        console_terminal_interface.print("[bold yellow][Webhook] aiohttp tidak tersedia. Webhook dinonaktifkan.[/bold yellow]")
        return

    app = web.Application()
    app.router.add_post("/telemetry", handle_roblox_telemetry)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "0.0.0.0", VPS_WEBHOOK_PORT)
        await site.start()
        console_terminal_interface.print(f"[bold cyan][Webhook] Memantau Roblox di Port {VPS_WEBHOOK_PORT}...[/bold cyan]")
    except OSError as e:
        console_terminal_interface.print(f"[bold yellow][Webhook] Port {VPS_WEBHOOK_PORT} tidak tersedia: {e}. Webhook dinonaktifkan.[/bold yellow]")


class RobloxDeployer:
    @staticmethod
    def compile_rojo() -> bool:
        console_terminal_interface.print("[bold yellow][Rojo] Mengompilasi Realitas ke .rbxl...[/bold yellow]")
        try:
            result = subprocess.run(
                ["rojo", "build", PROJECT_ROOT_DIRECTORY, "-o", COMPILED_GAME_FILE],
                capture_output=True,
                timeout=120,
            )
            if result.returncode != 0:
                console_terminal_interface.print(f"[bold yellow][Rojo] Build gagal: {result.stderr.decode(errors='ignore')[:200]}[/bold yellow]")
            return result.returncode == 0
        except FileNotFoundError:
            console_terminal_interface.print("[bold yellow][Rojo] Tidak terinstall. Tahap build dilewati.[/bold yellow]")
            return False
        except subprocess.TimeoutExpired:
            console_terminal_interface.print("[bold yellow][Rojo] Build timeout.[/bold yellow]")
            return False
        except Exception as e:
            console_terminal_interface.print(f"[bold yellow][Rojo] Error: {e}[/bold yellow]")
            return False

    @staticmethod
    async def publish(evolution_level: int):
        if not os.path.exists(COMPILED_GAME_FILE):
            console_terminal_interface.print("[bold yellow][Deploy] File .rbxl tidak ditemukan. Publish dilewati.[/bold yellow]")
            return

        console_terminal_interface.print(f"[bold cyan][Deploy] Mengirimkan file kompilasi ke Telegram (Evolusi {evolution_level})...[/bold cyan]")
        await send_telegram_document(
            COMPILED_GAME_FILE,
            f"🚀 [NEXUS APEX] File Final Evolusi {evolution_level} siap!"
        )

        if not ROBLOX_OPEN_CLOUD_API_KEY:
            console_terminal_interface.print("[bold yellow][Deploy] ROBLOX_OPEN_CLOUD_API_KEY tidak dikonfigurasi.[/bold yellow]")
            return

        url = f"https://apis.roblox.com/universes/v1/{ROBLOX_UNIVERSE_ID}/places/{ROBLOX_PLACE_ID}/versions"
        headers = {
            "x-api-key": ROBLOX_OPEN_CLOUD_API_KEY,
            "Content-Type": "application/xml",
        }
        try:
            console_terminal_interface.print("[bold cyan][Deploy] Mengunggah ke Roblox Open Cloud API...[/bold cyan]")
            loop = asyncio.get_event_loop()

            with open(COMPILED_GAME_FILE, "rb") as f:
                file_data = f.read()

            def _do_publish():
                return requests.post(
                    url,
                    headers=headers,
                    data=file_data,
                    params={"versionType": "Published"},
                    timeout=120,
                )

            response = await loop.run_in_executor(None, _do_publish)

            if response.status_code == 200:
                version_number = response.json().get("versionNumber", "Unknown")
                console_terminal_interface.print(f"[bold green]✅ Deployment Roblox Berhasil! (Versi {version_number})[/bold green]")
                await send_telegram_notification(f"✅ Deployment ke Roblox Server berhasil! Versi Place: {version_number}")
            else:
                msg = f"❌ Deployment Roblox Gagal! Status: {response.status_code}, Respon: {response.text[:200]}"
                console_terminal_interface.print(f"[bold red]{msg}[/bold red]")
                await send_telegram_notification(msg)
        except Exception as e:
            console_terminal_interface.print(f"[bold red][Deploy] Exception: {e}[/bold red]")


def setup_rojo():
    dirs = [
        "src/ServerScriptService",
        "src/StarterPlayerScripts",
        "src/StarterCharacterScripts",
        "src/StarterGui",
        "src/ReplicatedStorage",
    ]
    for d in dirs:
        os.makedirs(os.path.join(PROJECT_ROOT_DIRECTORY, d), exist_ok=True)

    project_config = {
        "name": "ApexAbsolut",
        "tree": {
            "$className": "DataModel",
            "ServerScriptService": {
                "$path": "src/ServerScriptService"
            },
            "StarterPlayer": {
                "$className": "StarterPlayer",
                "StarterPlayerScripts": {
                    "$path": "src/StarterPlayerScripts"
                },
                "StarterCharacterScripts": {
                    "$path": "src/StarterCharacterScripts"
                }
            },
            "StarterGui": {
                "$path": "src/StarterGui"
            },
            "ReplicatedStorage": {
                "$path": "src/ReplicatedStorage"
            }
        },
    }
    config_path = os.path.join(PROJECT_ROOT_DIRECTORY, "default.project.json")
    with open(config_path, "w") as f:
        json.dump(project_config, f, indent=4)


async def dump_ssd():
    """Dump semua modul terverifikasi ke file di disk."""
    try:
        db = establish_database_connection()
        cur = db.cursor()
        cur.execute("SELECT filepath, code_content FROM verified_modules")
        rows = cur.fetchall()
        db.close()

        for row in rows:
            filepath = row[0]
            code_content = row[1]
            if not filepath or filepath == "memory":
                continue
            try:
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
                    await f.write(code_content)
            except Exception as e:
                console_terminal_interface.print(f"[bold yellow]Gagal dump {filepath}: {e}[/bold yellow]")
    except Exception as e:
        console_terminal_interface.print(f"[bold yellow]Gagal dump_ssd: {e}[/bold yellow]")


class DynamicTaskArchitect:
    """Arsitek AI Otonom yang membaca file selesai dan mencari gap untuk men-generate tugas baru."""

    @staticmethod
    async def analyze_and_plan_next_evolution(evolution_level: int, agent: dict) -> list:
        console_terminal_interface.print(f"\n[bold magenta]🔍 [Architect] Menganalisis Ekosistem untuk Evolusi {evolution_level}...[/bold magenta]")

        db = establish_database_connection()
        cur = db.cursor()
        cur.execute("SELECT module_name FROM verified_modules")
        rows = cur.fetchall()
        db.close()
        existing_modules = [r[0] for r in rows]

        devforum_data = await LuauKnowledgeScraper.search_devforum("core systems needed for full roblox extraction game")
        github_data = await LuauKnowledgeScraper.search_github_luau("roblox game architecture framework complete")

        sys_inst = (
            "Anda adalah Game Director & Arsitek Roblox Tingkat Militer. "
            "Analisis daftar modul yang sudah selesai, "
            "lalu hasilkan JSON daftar tugas baru yang BELUM ADA."
        )

        schema = (
            "{\n"
            '  "new_tasks": [\n'
            '    {\n'
            '      "cat": "NAMA_KATEGORI_TUGAS_HURUF_BESAR",\n'
            '      "target_folder": "ServerScriptService",\n'
            '      "desc": "Instruksi spesifik",\n'
            '      "req": ["DataStoreService"],\n'
            '      "forb": ["_G"]\n'
            '    }\n'
            '  ]\n'
            "}"
        )

        prompt_payload = (
            f"[DAFTAR MODUL YANG SUDAH SELESAI]:\n"
            f"{', '.join(existing_modules) if existing_modules else 'Belum ada modul.'}\n\n"
            f"[LIVE RAG KNOWLEDGE BASE]:\n{devforum_data}\n{github_data}\n\n"
            f"Rancang maksimal 5 sistem krusial yang BELUM ADA. "
            f"WAJIB KELUARKAN JSON MURNI SESUAI SCHEMA INI:\n{schema}"
        )

        env_vars = os.environ.copy()
        env_vars["GEMINI_API_KEY"] = agent["api_key"]
        env_vars["CI"] = "true"
        env_vars["TERM"] = "dumb"
        env_vars["NO_COLOR"] = "1"

        command = [
            GEMINI_CLI_PATH, "-m", "models/gemini-2.5-flash", "-y",
            "-p", "Baca stdin. Berikan output JSON murni tanpa markdown text."
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env_vars
            )

            full_input = f"[SYSTEM]:\n{sys_inst}\n\n[PROMPT]:\n{prompt_payload}"
            stdout_data, stderr_data = await asyncio.wait_for(
                process.communicate(input=full_input.encode("utf-8")),
                timeout=180.0
            )

            raw_output = stdout_data.decode("utf-8", errors="ignore")

            start_idx = raw_output.find('{')
            end_idx = raw_output.rfind('}')
            if start_idx != -1 and end_idx != -1:
                clean_json = raw_output[start_idx:end_idx+1]
                data = json.loads(clean_json)

                new_tasks = []
                valid_folders = ["ServerScriptService", "StarterPlayerScripts", "StarterCharacterScripts", "StarterGui", "ReplicatedStorage"]
                for t in data.get("new_tasks", []):
                    cat = t.get("cat", f"SYS_{random.randint(100,999)}")
                    target_folder = t.get("target_folder", "ServerScriptService")

                    if target_folder not in valid_folders:
                        target_folder = "ServerScriptService"

                    req_list = t.get("req", [])

                    if any(keyword in cat.upper() for keyword in ["WEAPON", "ARMOR", "ITEM", "GEAR", "TOOL", "FURNITURE"]) and "Recipe" not in req_list:
                        req_list.append("Recipe")

                    if any(keyword in cat.upper() for keyword in ["ARMOR", "HELMET"]):
                        for r in ["Durability", "ArmorTier", "MaterialType"]:
                            if r not in req_list:
                                req_list.append(r)

                    if target_folder == "ServerScriptService":
                        ext = ".server.lua"
                    elif target_folder in ["StarterPlayerScripts", "StarterCharacterScripts", "StarterGui"]:
                        ext = ".client.lua"
                    elif target_folder == "ReplicatedStorage":
                        ext = ".lua"
                    else:
                        ext = ".server.lua"

                    new_tasks.append({
                        "name": f"{cat}_1",
                        "path": os.path.join(SOURCE_CODE_DIRECTORY, target_folder, cat, f"{cat}_1{ext}"),
                        "req": req_list,
                        "forb": t.get("forb", ["_G", "shared", "loadstring", "getfenv"]),
                        "desc": t.get("desc", f"Implementasi sistem {cat}")
                    })
                console_terminal_interface.print(f"[bold green]✅ Architect menemukan {len(new_tasks)} tugas baru![/bold green]")
                return new_tasks
        except Exception as e:
            console_terminal_interface.print(f"[bold red]Architect Agent Error: {e}[/bold red]")
            return []
        return []


def _build_task_queue():
    """Bangun antrian tugas fundamental statis untuk Evolusi 1."""
    dynamic_tasks = [
        ("CORE_WORLD_SETTINGS", 1, "StarterPlayerScripts", "WAJIB membuat LocalScript yang mengunci kamera pemain ke First-Person (`Enum.CameraMode.LockFirstPerson`) selamanya tanpa bisa di-zoom out.", ["CameraMode", "LockFirstPerson"], []),
        ("DAY_NIGHT_CYCLE", 1, "ServerScriptService", "Rancang Sistem Siklus Siang, Sore, dan Malam yang dinamis.", ["Lighting", "ClockTime"], []),
        ("ITEM_CATEGORY_DATABASE", 1, "ReplicatedStorage", "Rancang Database Kategori Item sentral.", ["Weapon", "Ammunition", "Armor", "Bait", "Material", "Tool"], []),
        ("RAW_MATERIAL_ITEM", 5, "ServerScriptService", "Rancang RAW MATERIAL / BAHAN MENTAH. HARAM memiliki atribut 'Recipe', 'Durability', atau 'ArmorTier'! Atur 'ItemCategory' = 'Material'.", ["ItemCategory", "BasePrice", "ProximityPrompt"], ["Recipe", "Durability", "ArmorTier", "Weapon"]),
        ("CORE_INVENTORY_SYSTEM", 1, "ServerScriptService", "Rancang Sistem Inventory Kustom. WAJIB DataStoreService dengan pcall().", ["DataStoreService", "pcall", "MainBackpack", "SafeContainer", "LobbyStorage", "PlayerRemoving", "PlayerAdded"], ["StarterGear"]),
        ("DAILY_LOG_SYSTEM", 1, "ServerScriptService", "Rancang sistem log harian.", [], []),
        ("AUDIO_SYSTEM", 1, "StarterPlayerScripts", "Rancang sistem audio client untuk BGM dan SFX.", [], []),
    ]

    task_queue = []
    for cat, amt, target_folder, desc, req, forb in dynamic_tasks:
        for i in range(1, amt + 1):
            if target_folder == "ServerScriptService":
                ext = ".server.lua"
            elif target_folder in ["StarterPlayerScripts", "StarterCharacterScripts", "StarterGui"]:
                ext = ".client.lua"
            elif target_folder == "ReplicatedStorage":
                ext = ".lua"
            else:
                ext = ".server.lua"

            task_queue.append({
                "name": f"{cat}_{i}",
                "path": os.path.join(SOURCE_CODE_DIRECTORY, target_folder, cat, f"{cat}_{i}{ext}"),
                "req": req,
                "forb": forb,
                "desc": desc,
            })
    return task_queue


async def run_orchestrator():
    try:
        await initialize_system_ledger()
        setup_rojo()
        NativeLuauCompiler.ensure_compiler_exists()

        asyncio.create_task(start_telemetry_webhook())

        healer = AutoHealerAgent()
        synthesizer = OmniSynthesizerAgent(healer)

        evolution_level = 1
        generation_counter = 1

        agent_idx = random.randint(0, len(ACTIVE_AGENTS) - 1) if ACTIVE_AGENTS else 0

        while True:
            console_terminal_interface.print(
                Panel(f"[bold magenta]=== EVOLUSI LEVEL {evolution_level}/50 - SIKLUS KE-{generation_counter} ===[/bold magenta]")
            )

            if evolution_level == 1:
                task_queue = _build_task_queue()
            else:
                current_agent_architect = ACTIVE_AGENTS[agent_idx % len(ACTIVE_AGENTS)]
                agent_idx += 1
                task_queue = await DynamicTaskArchitect.analyze_and_plan_next_evolution(evolution_level, current_agent_architect)

                if not task_queue:
                    console_terminal_interface.print("[bold yellow]Architect menyimpulkan game sudah lengkap atau terjadi limit. Memuat ulang ekspansi kosmetik...[/bold yellow]")
                    task_queue = [
                        {
                            "name": f"AUTONOMOUS_EXPANSION_{evolution_level}",
                            "path": os.path.join(SOURCE_CODE_DIRECTORY, "StarterPlayerScripts", f"EXPANSION_{evolution_level}.client.lua"),
                            "req": [],
                            "forb": ["_G", "shared"],
                            "desc": "Berdasarkan ekosistem yang ada, ciptakan sistem kosmetik atau optimasi baru."
                        }
                    ]

            total_tasks = len(task_queue)

            console_terminal_interface.print(
                Panel(
                    f"[bold magenta]=== SEQUENTIAL QUEUE HANDOFF "
                    f"({total_tasks} tasks, {len(ACTIVE_AGENTS)} agents aktif) ===[/bold magenta]"
                )
            )

            tasks_done = 0
            tasks_failed = 0
            failed_tasks = []

            for task_num, task in enumerate(task_queue, start=1):
                console_terminal_interface.print(
                    f"\n[bold blue]--- Task {task_num}/{total_tasks}: {task['name']} ---[/bold blue]"
                )

                task_start_time = time.time()
                completed = False
                prev_err = ""
                prev_code = ""
                real_attempt_count = 0
                error_history = []

                while not completed:
                    current_agent = ACTIVE_AGENTS[agent_idx % len(ACTIVE_AGENTS)]
                    agent_idx += 1

                    console_terminal_interface.print(
                        f"[bold cyan]  Percobaan {real_attempt_count + 1}/∞ → [{current_agent['name']}] (Estafet 24/7 Tanpa Batas)[/bold cyan]"
                    )

                    try:
                        completed, prev_err, prev_code = await synthesizer.synthesize_handoff(
                            current_agent,
                            task["path"],
                            task["name"],
                            task["desc"],
                            task["req"],
                            task["forb"],
                            prev_err,
                            prev_code,
                        )
                    except Exception as e:
                        prev_err = f"EXCEPTION: {str(e)}"
                        completed = False

                    if completed:
                        tasks_done += 1
                        break

                    if "RATE_LIMIT" in prev_err:
                        console_terminal_interface.print(
                            f"[bold yellow]  Rate limit terdeteksi, menunggu 60 detik...[/bold yellow]"
                        )
                        await asyncio.sleep(60)
                    elif "GEMINI_CLI_NOT_FOUND" in prev_err:
                        console_terminal_interface.print(
                            f"[bold red]  Gemini CLI tidak ditemukan! Menunggu 30 detik dan lanjutkan...[/bold red]"
                        )
                        await asyncio.sleep(30)
                        tasks_failed += 1
                        failed_tasks.append(task["name"])
                        break
                    else:
                        real_attempt_count += 1

                        error_key = prev_err.split(":")[0][:50]
                        error_history.append(error_key)

                        backoff_delay = min(2 ** real_attempt_count, 30)
                        if real_attempt_count > 0:
                            await asyncio.sleep(backoff_delay)

                        if real_attempt_count >= 10:
                            console_terminal_interface.print(
                                f"[bold red]  Task {task['name']} gagal setelah 10 percobaan. Lanjutkan ke task berikutnya.[/bold red]"
                            )
                            tasks_failed += 1
                            failed_tasks.append(task["name"])
                            break

            console_terminal_interface.print(
                Panel(
                    f"[bold green]✅ EVOLUSI {evolution_level} SELESAI!\n"
                    f"Berhasil: {tasks_done}/{total_tasks} | Gagal: {tasks_failed}/{total_tasks}\n"
                    f"Tasks Gagal: {', '.join(failed_tasks) if failed_tasks else 'Tidak ada'}[/bold green]"
                )
            )

            await dump_ssd()

            rojo_success = RobloxDeployer.compile_rojo()
            if rojo_success:
                await RobloxDeployer.publish(evolution_level)
                await send_telegram_notification(
                    f"✅ Evolusi {evolution_level} Selesai!\n"
                    f"Berhasil: {tasks_done}/{total_tasks} tugas.\n"
                    f"File .rbxl telah dikirim!",
                    important=True
                )

            evolution_level += 1
            generation_counter += 1

            await asyncio.sleep(5)

    except KeyboardInterrupt:
        console_terminal_interface.print("\n[bold red]Sistem dihentikan oleh pengguna.[/bold red]")
    except Exception as e:
        console_terminal_interface.print(f"[bold red]FATAL ERROR di Orchestrator: {e}[/bold red]")
        import traceback
        traceback.print_exc()
        raise


async def test_roblox_api():
    """Uji coba Roblox Open Cloud API."""
    console_terminal_interface.print("\n[bold cyan]=== UJI COBA ROBLOX OPEN CLOUD API ===[/bold cyan]")

    if not ROBLOX_OPEN_CLOUD_API_KEY:
        console_terminal_interface.print("[bold red]❌ ROBLOX_OPEN_CLOUD_API_KEY tidak ditemukan![/bold red]")
        return False

    console_terminal_interface.print(f"[dim]Universe ID: {ROBLOX_UNIVERSE_ID}[/dim]")
    console_terminal_interface.print(f"[dim]Place ID: {ROBLOX_PLACE_ID}[/dim]")

    url = f"https://apis.roblox.com/cloud/v2/universes/{ROBLOX_UNIVERSE_ID}"
    headers = {
        "x-api-key": ROBLOX_OPEN_CLOUD_API_KEY,
    }

    try:
        loop = asyncio.get_event_loop()

        def _test_get():
            return requests.get(url, headers=headers, timeout=15)

        response = await loop.run_in_executor(None, _test_get)
        console_terminal_interface.print(f"[dim]Response Status: {response.status_code}[/dim]")

        if response.status_code == 200:
            data = response.json()
            console_terminal_interface.print(f"[bold green]✅ Roblox Open Cloud API BERHASIL![/bold green]")
            console_terminal_interface.print(f"[green]   Universe Name: {data.get('displayName', 'N/A')}[/green]")
            console_terminal_interface.print(f"[green]   Privacy: {data.get('visibility', 'N/A')}[/green]")
            return True
        elif response.status_code == 401:
            console_terminal_interface.print(f"[bold red]❌ API Key tidak valid atau tidak memiliki izin! Status: 401[/bold red]")
            console_terminal_interface.print(f"[dim]Response: {response.text[:300]}[/dim]")
            return False
        elif response.status_code == 403:
            console_terminal_interface.print(f"[bold red]❌ Akses ditolak! Universe tidak ditemukan atau API Key tidak punya izin ke universe ini. Status: 403[/bold red]")
            console_terminal_interface.print(f"[dim]Response: {response.text[:300]}[/dim]")
            return False
        elif response.status_code == 404:
            console_terminal_interface.print(f"[bold yellow]⚠️ Universe ID {ROBLOX_UNIVERSE_ID} tidak ditemukan. Status: 404[/bold yellow]")
            return False
        else:
            console_terminal_interface.print(f"[bold yellow]⚠️ Roblox API Status: {response.status_code}[/bold yellow]")
            console_terminal_interface.print(f"[dim]Response: {response.text[:300]}[/dim]")
            return False
    except Exception as e:
        console_terminal_interface.print(f"[bold red]❌ Exception saat test Roblox API: {e}[/bold red]")
        return False


if __name__ == "__main__":
    async def main():
        console_terminal_interface.print(Panel("[bold green]🚀 NEXUS AI SYSTEM STARTING...[/bold green]"))

        api_ok = await test_roblox_api()
        if api_ok:
            console_terminal_interface.print("[bold green]✅ Roblox API Test: PASSED[/bold green]")
        else:
            console_terminal_interface.print("[bold yellow]⚠️ Roblox API Test: GAGAL (Sistem tetap berjalan)[/bold yellow]")

        await run_orchestrator()

    asyncio.run(main())
