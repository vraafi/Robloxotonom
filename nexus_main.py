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
from aiohttp import web
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
from nexus_healer import PreDeploymentValidator
from nexus_polyglot import start_telegram_polling


# ==============================
# TELEGRAM RATE LIMITING SYSTEM
# ==============================
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
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: requests.post(url, json=payload, timeout=10),
            )
        except Exception as e:
            console_terminal_interface.print(f"[dim yellow]Notifikasi Telegram gagal: {e}[/dim yellow]")


async def send_telegram_document(file_path: str, caption: str = ""):
    global _last_telegram_send

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        console_terminal_interface.print("[dim yellow]Kredensial Telegram kosong, pengiriman dokumen dilewati.[/dim yellow]")
        return

    if not os.path.exists(file_path):
        console_terminal_interface.print(
            f"[bold red]Gagal mengirim ke Telegram: File {file_path} tidak ditemukan![/bold red]"
        )
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
                with open(file_path, "rb") as f:
                    files = {"document": f}
                    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
                    res = requests.post(url, data=data, files=files, timeout=120)
                    if res.status_code == 200:
                        console_terminal_interface.print("[bold green]✅ File .rbxl sukses dievakuasi ke Telegram Anda![/bold green]")
                    else:
                        console_terminal_interface.print(f"[bold red]❌ Gagal kirim Telegram: {res.text}[/bold red]")
            except Exception as e:
                console_terminal_interface.print(f"[bold red]Exception Telegram Document: {e}[/bold red]")

        loop = asyncio.get_running_loop()
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
    app = web.Application()
    app.router.add_post("/telemetry", handle_roblox_telemetry)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "0.0.0.0", VPS_WEBHOOK_PORT)
        await site.start()
        console_terminal_interface.print(
            f"[bold cyan][Webhook] Memantau Roblox di Port {VPS_WEBHOOK_PORT}...[/bold cyan]"
        )
    except OSError as e:
        console_terminal_interface.print(
            f"[bold yellow][Webhook] Port {VPS_WEBHOOK_PORT} tidak tersedia: {e}. Webhook dinonaktifkan.[/bold yellow]"
        )


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
                console_terminal_interface.print(
                    f"[bold yellow][Rojo] Build gagal: {result.stderr.decode(errors='ignore')[:200]}[/bold yellow]"
                )
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

        console_terminal_interface.print(
            f"[bold cyan][Deploy] Mengirimkan file kompilasi ke Telegram (Evolusi {evolution_level})...[/bold cyan]"
        )
        await send_telegram_document(
            COMPILED_GAME_FILE,
            f"🚀 [NEXUS APEX] File Final Evolusi {evolution_level} siap! (Akan di-publish ke Roblox Creator API)",
        )

        if not ROBLOX_OPEN_CLOUD_API_KEY:
            console_terminal_interface.print(
                "[bold yellow][Deploy] ROBLOX_OPEN_CLOUD_API_KEY tidak dikonfigurasi. Berhenti setelah evakuasi Telegram.[/bold yellow]"
            )
            return

        url = f"https://apis.roblox.com/universes/v1/{ROBLOX_UNIVERSE_ID}/places/{ROBLOX_PLACE_ID}/versions"
        headers = {
            "x-api-key": ROBLOX_OPEN_CLOUD_API_KEY,
            "Content-Type": "application/xml",
        }
        try:
            console_terminal_interface.print("[bold cyan][Deploy] Mengunggah ke Roblox Open Cloud API...[/bold cyan]")
            loop = asyncio.get_running_loop()

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
                console_terminal_interface.print(
                    f"[bold green]✅ Deployment Roblox Berhasil! (Versi {version_number})[/bold green]"
                )
                success_caption = (
                    f"✅ DEPLOYMENT BERHASIL! (Evolusi {evolution_level})\n"
                    f"🎮 Versi Roblox: {version_number}\n"
                    f"📦 File ini adalah versi final yang sudah aktif di server Roblox.\n"
                    f"🔗 Buka game kamu di Roblox untuk memverifikasi."
                )
                await send_telegram_notification(success_caption, important=True)
                await send_telegram_document(COMPILED_GAME_FILE, success_caption)
            else:
                # ── DEPLOYMENT GAGAL: Kirim file .rbxl ke Telegram untuk upload manual ──
                error_detail = response.text[:300] if response.text else "Tidak ada detail"
                fail_caption = (
                    f"❌ DEPLOYMENT ROBLOX GAGAL (Evolusi {evolution_level})\n"
                    f"Status: {response.status_code}\n"
                    f"Error: {error_detail}\n\n"
                    f"📥 File ini untuk UPLOAD MANUAL ke Roblox Studio:\n"
                    f"1. Download file .rbxl di atas\n"
                    f"2. Buka Roblox Studio → File → Open from File\n"
                    f"3. Publish manual via File → Publish to Roblox"
                )
                console_terminal_interface.print(f"[bold red]❌ [Deploy] Gagal! Status: {response.status_code}[/bold red]")
                await send_telegram_notification(fail_caption, important=True)
                # Kirim ulang file .rbxl dengan caption GAGAL yang jelas
                await send_telegram_document(
                    COMPILED_GAME_FILE,
                    fail_caption,
                )
        except Exception as e:
            # ── EXCEPTION saat upload: Kirim file ke Telegram ──
            exc_caption = (
                f"💥 EXCEPTION saat Deployment (Evolusi {evolution_level})\n"
                f"Error: {type(e).__name__}: {str(e)[:200]}\n\n"
                f"📥 Upload file ini secara MANUAL ke Roblox Studio."
            )
            console_terminal_interface.print(f"[bold red][Deploy] Exception: {e}[/bold red]")
            try:
                await send_telegram_notification(exc_caption, important=True)
                await send_telegram_document(COMPILED_GAME_FILE, exc_caption)
            except Exception:
                pass


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
            "ServerScriptService": {"$path": "src/ServerScriptService"},
            "StarterPlayer": {
                "$className": "StarterPlayer",
                "StarterPlayerScripts": {"$path": "src/StarterPlayerScripts"},
                "StarterCharacterScripts": {"$path": "src/StarterCharacterScripts"},
            },
            "StarterGui": {"$path": "src/StarterGui"},
            "ReplicatedStorage": {"$path": "src/ReplicatedStorage"},
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
    """Arsitek AI Otonom yang membaca file selesai dan mencari gap di DevForum/Github untuk men-generate tugas baru."""

    @staticmethod
    async def analyze_and_plan_next_evolution(evolution_level: int, agent: dict) -> list:
        console_terminal_interface.print(
            f"\n[bold magenta]🔍 [Architect] Menganalisis File Ekosistem & Scraping RAG untuk Evolusi {evolution_level}...[/bold magenta]"
        )

        db = establish_database_connection()
        cur = db.cursor()
        cur.execute("SELECT module_name FROM verified_modules")
        rows = cur.fetchall()
        db.close()
        existing_modules = [r[0] for r in rows]

        devforum_data = await LuauKnowledgeScraper.search_devforum(
            "core systems needed for full roblox extraction game"
        )
        github_data = await LuauKnowledgeScraper.search_github_luau(
            "roblox game architecture framework complete"
        )
        # Tambahan: cari juga via Repository Search & Topics sebagai pelengkap
        if not github_data:
            github_data = await LuauKnowledgeScraper.search_github_repositories(
                "roblox extraction game framework"
            )
        topics_data = await LuauKnowledgeScraper.search_github_topics("roblox")

        sys_inst = (
            "Anda adalah Game Director & Arsitek Roblox Tingkat Militer. "
            "Tugas Anda: Analisis daftar modul yang sudah berhasil dibuat oleh tim programmer, "
            "baca data referensi (RAG) dari Github/DevForum tentang arsitektur game extraction end-to-end, "
            "lalu hasilkan JSON daftar tugas baru (fitur/sistem) yang BELUM ADA di ekosistem game ini."
        )

        schema = (
            "{\n"
            '  "new_tasks": [\n'
            '    {\n'
            '      "cat": "NAMA_KATEGORI_TUGAS_HURUF_BESAR",\n'
            '      "target_folder": "ServerScriptService",\n'
            '      "desc": "Instruksi spesifik dan detail tentang bentuk, warna (wajib neon/cerah), dan logika",\n'
            '      "req": ["DataStoreService"],\n'
            '      "forb": ["_G"]\n'
            '    }\n'
            '  ]\n'
            "}"
        )

        prompt_payload = (
            f"[DAFTAR MODUL YANG SUDAH SELESAI DIBUAT (JANGAN MENYURUH MEMBUAT INI LAGI)]:\n"
            f"{', '.join(existing_modules) if existing_modules else 'Belum ada modul.'}\n\n"
            f"[LIVE RAG KNOWLEDGE BASE (DEVFORUM & GITHUB)]:\n{devforum_data}\n{github_data}\n{topics_data}\n\n"
            f"BERDASARKAN DATA DI ATAS, rancang maksimal 5 sistem/tugas krusial yang HILANG atau BELUM ADA untuk melengkapi game extraction ini agar menjadi 100% End-to-End. "
            f"WAJIB KELUARKAN JSON MURNI SESUAI SCHEMA INI:\n{schema}"
        )

        env_vars = os.environ.copy()
        env_vars["GEMINI_API_KEY"] = agent["api_key"]
        env_vars["CI"] = "true"
        env_vars["TERM"] = "dumb"
        env_vars["NO_COLOR"] = "1"

        command = [
            GEMINI_CLI_PATH, "-m", "models/gemma-4-31b-it", "-y",
            "-p", "Baca stdin. Berikan output JSON murni tanpa markdown text.",
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env_vars,
            )

            full_input = f"[SYSTEM]:\n{sys_inst}\n\n[PROMPT]:\n{prompt_payload}"
            stdout_data, stderr_data = await asyncio.wait_for(
                process.communicate(input=full_input.encode("utf-8")),
                timeout=180.0,
            )

            raw_output = stdout_data.decode("utf-8", errors="ignore")

            start_idx = raw_output.find("{")
            end_idx = raw_output.rfind("}")
            if start_idx != -1 and end_idx != -1:
                clean_json = raw_output[start_idx:end_idx + 1]
                data = json.loads(clean_json)

                new_tasks = []
                valid_folders = [
                    "ServerScriptService", "StarterPlayerScripts",
                    "StarterCharacterScripts", "StarterGui", "ReplicatedStorage",
                ]
                for t in data.get("new_tasks", []):
                    cat = t.get("cat", f"SYS_{random.randint(100, 999)}")
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

                    if any(keyword in cat.upper() for keyword in ["WEAPON", "ARMOR", "ITEM", "GEAR", "TOOL", "AMMUNITION", "BAIT"]):
                        for r in ["ItemCategory", "BasePrice", "VisualEquip", "ProximityPrompt"]:
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
                        "desc": t.get("desc", f"Implementasi sistem {cat}"),
                    })
                console_terminal_interface.print(
                    f"[bold green]✅ Architect menemukan {len(new_tasks)} tugas baru yang harus dikerjakan![/bold green]"
                )
                return new_tasks

        except Exception as e:
            console_terminal_interface.print(f"[bold red]Architect Agent Error: {e}[/bold red]")

        # FIX: hapus return [] duplikat yang tidak pernah tercapai.
        # Sekarang ada satu titik return yang benar di sini.
        return []


def _build_task_queue():
    dynamic_tasks = [
        ("CORE_WORLD_SETTINGS", 1, "StarterPlayerScripts", "WAJIB membuat LocalScript yang mengunci kamera pemain ke First-Person (`Enum.CameraMode.LockFirstPerson`) selamanya tanpa bisa di-zoom out. Atur juga parameter gravitasi dan Lighting agar terasa seperti survival yang keras.", ["CameraMode", "LockFirstPerson"], []),
        ("DAY_NIGHT_CYCLE", 1, "ServerScriptService", "Rancang Sistem Siklus Siang, Sore, dan Malam yang dinamis. Waktu di dalam game WAJIB berputar. Lighting harus berubah drastis (gelap gulita di malam hari). Modul ini akan memengaruhi PerceptionRadius monster.", ["Lighting", "ClockTime"], []),
        ("WEATHER_DISASTER", 5, "ServerScriptService", "Rancang sistem cuaca ekstrem (Hujan Badai, Badai Pasir, Salju). WAJIB memengaruhi jarak pandang pemain dan memengaruhi atribut lingkungan.", [], []),
        ("CORE_WORLD_GENERATION", 1, "ServerScriptService", "WAJIB membuat script Procedural Generation untuk membangun Baseplate dasar berukuran 2048x64x2048 dan meng-generate tanah/terrain. Gunakan warna PALING CERAH dan NEON. HUKUM PENEMPATAN AKURAT & BENTURAN: Semua tanah dan rintangan yang digenerate WAJIB diletakkan di permukaan menggunakan 'workspace:Raycast()', lalu diatur 'CanCollide = true' dan 'Anchored = true'.", ["Instance.new", "CanCollide", "Anchored", "Raycast", "RaycastParams"], []),
        ("BIOME_SYSTEM", 5, "ServerScriptService", "Rancang bioma lingkungan ekstrem (Banjir, Pasir, Hutan, Snow, Ocean). Bioma ini akan dibaca oleh sistem Monster sebagai 'Habitat'. HUKUM PENEMPATAN AKURAT & BENTURAN (DEVFORUM STANDARD): Saat meng-generate Pohon atau Batu, DILARANG mengandalkan kolisi bawaan MeshPart. Anda WAJIB menggunakan teknik 'Hitbox Separation'. Anda WAJIB menembakkan sinar 'workspace:Raycast()' ke arah bawah dengan 'RaycastParams' (mode Exclude pohon lain) untuk menemukan titik permukaan tanah sebelum meletakkan Hitbox.", ["Instance.new", "CanCollide", "Anchored", "Raycast", "RaycastParams", "HitboxSeparation"], []),
        ("RESOURCE_NODE_MANAGER", 1, "ServerScriptService", "Rancang Sistem Manajer Pohon dan Batu. HUKUM NODE: Semua Pohon dan Batu yang di-generate oleh BIOME_SYSTEM WAJIB ditambahkan 'IntValue' bernama 'Health' (misal: 100). Buat fungsi global yang mendengarkan pengurangan Health. Jika Health <= 0, hancurkan objek pohon/batu tersebut dan spawn wujud 3D 'RAW_MATERIAL_ITEM' (Kayu untuk Pohon, Besi Mentah/Batu untuk Rock) di posisi tersebut menggunakan teknik jatuh fisika ringan.", ["IntValue", "Health", "Instance.new"], []),
        ("ITEM_CATEGORY_DATABASE", 1, "ReplicatedStorage", "Rancang Database Kategori Item sentral. WAJIB membuat modul struktur data yang mendaftarkan Kategori resmi: 'Weapon', 'Ammunition', 'Armor', 'Medical', 'Material', 'Valuable', 'Bait', 'Tool'.", ["Weapon", "Ammunition", "Armor", "Bait", "Material", "Tool"], []),
        ("GATHERING_TOOLS", 2, "ServerScriptService", "Rancang ALAT PANEN: Kapak (Axe) dan Beliung (Pickaxe). HUKUM ALAT PANEN: Ini adalah 'Tool' yang bisa di-equip pemain. WAJIB menggunakan event '.Activated' dan menembakkan 'workspace:Raycast()' jarak dekat ke depan pemain. Kapak HANYA melukai objek ber-tag 'Tree'. Beliung HANYA melukai objek ber-tag 'Rock'. Kurangi nilai 'Health' (IntValue) dari objek tersebut saat dipukul. Set 'ItemCategory' = 'Tool'.", ["Tool", "Activated", "Raycast", "ItemCategory"], []),
        ("RAW_MATERIAL_ITEM", 100, "ServerScriptService", "Rancang RAW MATERIAL / BAHAN MENTAH (Contoh: Daging, Tulang, Besi Mentah, Kayu, Ulat). HUKUM RAW MATERIAL: Karena ini bahan mentah dari alam, item ini HARAM memiliki atribut 'Recipe', 'Durability', atau 'ArmorTier'! Atur 'ItemCategory' = 'Material' (atau 'Bait' untuk Ulat). Item ini hanya boleh dijatuhkan dari Monster atau dihancurkan dari Pohon/Batu. WAJIB buat fisik 3D kecil di tanah yang bisa dipungut pemain dengan ProximityPrompt (ActionText = 'Ambil').", ["ItemCategory", "BasePrice", "ProximityPrompt"], ["Recipe", "Durability", "ArmorTier", "Weapon"]),
        ("AMMUNITION_CALIBER", 30, "ReplicatedStorage", "Rancang modul Kaliber Peluru meniru 100% statistik Arena Breakout. HUKUM BALISTIK: Amunisi WAJIB mendefinisikan BaseDamage, PenetrationLevel (Tier 1-6). WAJIB punya wujud fisik 3D kotak amunisi dengan ProximityPrompt (ActionText='Ambil').", ["BaseDamage", "PenetrationLevel", "ItemCategory", "BasePrice", "ProximityPrompt", "Anchored"], ["Recipe", "Weapon"]),
        ("MODERN_ARMOR_HELMET", 25, "ServerScriptService", "Rancang BARANG JADI: Helm Taktis Militer & Rompi Anti-Peluru Modern (Kevlar/Ceramic). HUKUM ARMOR MODERN: WAJIB memiliki 'Recipe' (Bahan mentah dari RAW_MATERIAL_ITEM untuk merakitnya), 'Durability' (100/100), 'ArmorTier' (1-6), dan 'MaterialType' ('Ceramic'/'Steel'). Set 'ItemCategory' = 'Armor'. HUKUM VISUAL EQUIP: Model 3D di tanah WAJIB dipasangkan 'ProximityPrompt' (ActionText='Gunakan'). Saat ditekan, Armor 3D WAJIB di-WeldConstraint ke UpperTorso karakter pemain agar terlihat jelas visualnya!", ["Recipe", "Durability", "ArmorTier", "MaterialType", "ItemCategory", "BasePrice", "ProximityPrompt", "HitboxSeparation", "VisualEquip"], []),
        ("FANTASY_ARMOR_HELMET", 25, "ServerScriptService", "Rancang BARANG JADI: Jubah Penyihir & Zirah Ksatria Kuno (Fantasy Theme). HUKUM ARMOR FANTASY: Tetap WAJIB memiliki 'Recipe', 'Durability', 'ArmorTier', dan 'MaterialType' ('Leather'/'Mithril'). Set 'ItemCategory' = 'Armor'. HUKUM VISUAL EQUIP: Wujud 3D di tanah dipasangkan 'ProximityPrompt' (ActionText='Gunakan'/'Equip'). WAJIB di-weld ke badan pemain saat dipungut.", ["Recipe", "Durability", "ArmorTier", "MaterialType", "ItemCategory", "BasePrice", "ProximityPrompt", "HitboxSeparation", "VisualEquip"], []),
        ("MODERN_WEAPON", 20, "ServerScriptService", "Rancang BARANG JADI: Senjata Api Modern (Assault Rifle, Sniper) meniru Arena Breakout. HUKUM MODERN WEAPON: HARAM memiliki variabel Damage! Senjata ini menembakkan peluru fisik (Raycast). WAJIB mengatur 'CompatibleCaliber' (contoh: 5.56x45mm), 'FireRate' (RPM), dan 'Recoil'. WAJIB punya 'Recipe' dan 'ItemCategory' = 'Weapon'. HUKUM VISUAL EQUIP: Pasang ProximityPrompt (Equip). WAJIB di-WeldConstraint ke tangan pemain saat dipakai.", ["Raycast", "Recipe", "CompatibleCaliber", "ItemCategory", "BasePrice", "ProximityPrompt", "HitboxSeparation", "VisualEquip"], ["BaseDamage"]),
        ("FANTASY_WEAPON", 20, "ServerScriptService", "Rancang BARANG JADI: Senjata Sihir/Pedang Ksatria (Fantasy Theme). HUKUM FANTASY WEAPON: Menggunakan serangan Melee atau Tembakan Mana. WAJIB punya 'Recipe' dan 'ItemCategory' = 'Weapon'. HUKUM VISUAL EQUIP: Pasang ProximityPrompt (Equip). WAJIB di-WeldConstraint ke tangan karakter pemain.", ["Recipe", "ItemCategory", "BasePrice", "ProximityPrompt", "HitboxSeparation", "VisualEquip"], ["CompatibleCaliber"]),
        ("CORE_INVENTORY_SYSTEM", 1, "ServerScriptService", "Rancang Sistem Inventory Kustom khusus Extraction Game. DILARANG KERAS menggunakan Backpack bawaan Roblox. WAJIB membagi inventory menjadi 3 kompartemen struktur data di Server: 'MainBackpack' (Tas tempur, hilang 100% saat mati), 'SafeContainer' (Tas kecil aman saat mati), dan 'LobbyStorage' (Gudang Stash permanen di Lobby yang menampung barang beli/jual, TIDAK BISA dibawa ke arena tempur). HUKUM PERSISTENSI DATA MUTLAK: Barang di SafeContainer dan LobbyStorage TIDAK BOLEH HILANG saat pemain keluar game. WAJIB menggunakan event `Players.PlayerAdded` untuk me-load data dan `Players.PlayerRemoving` untuk me-save data ke DataStoreService dengan pcall().", ["DataStoreService", "pcall", "MainBackpack", "SafeContainer", "LobbyStorage", "PlayerRemoving", "PlayerAdded"], ["StarterGear"]),
        ("CORE_INBOX_SYSTEM", 1, "ServerScriptService", "Rancang Sistem Kotak Masuk (Inbox/Mailbox) mirip Arena Breakout. Bertindak sebagai penampung sementara dan aman untuk pemain. Data Inbox WAJIB disimpan di DataStoreService.", ["DataStoreService", "pcall", "Inbox"], []),
        ("CORE_INBOX_UI", 1, "StarterGui", "Rancang UI untuk Kotak Masuk (Inbox) dengan ikon Amplop.", ["RemoteFunction"], []),
        ("MONSTER", 50, "ServerScriptService", "Rancang monster/hewan unik. HUKUM EKOLOGI DUNIA NYATA: WAJIB mendefinisikan 'Diet', 'SocialBehavior', 'SpawnWeight', 'Habitat', 'LocomotionType' (Terrestrial, Aerial, Aquatic), dan 'DropTable' (Jika mati, harus men-spawn wujud fisik dari RAW_MATERIAL_ITEM yang terdaftar agar pemain bisa memungutnya). HUKUM RANTAI MAKANAN: Omnivora/Karnivora WAJIB memindai radius sekitarnya untuk mencari item fisik berlabel 'Bait' untuk dimakan. HUKUM MOTORIK: Gunakan PathfindingService.", ["PathfindingService", "Humanoid", "Diet", "SocialBehavior", "SpawnWeight", "Habitat", "DropTable", "Stamina", "PerceptionRadius", "LocomotionType"], ["Motor6D", "Scavenger"]),
        ("LOBBY_SPACESHIP", 1, "ServerScriptService", "Rancang lobby di pesawat luar angkasa besar dengan domain investor. HUKUM FISIKA LOBBY: Lobby ini BUKAN Bioma! Bangun pesawat menggunakan blok Part biasa di langit/luar angkasa (Y = 10000). DILARANG KERAS menggunakan workspace:Raycast() ke tanah karena ini di angkasa. Namun lantai/dinding pesawat WAJIB CanCollide = true dan Anchored = true.", ["Anchored", "CanCollide"], ["Raycast", "Terrain"]),
        ("FURNITURE", 50, "ServerScriptService", "Rancang furnitur lobby pesawat. Warna wajib putih cerah atau neon. HUKUM FISIKA: Furnitur diletakkan di dalam Lobby Pesawat (Y=10000), DILARANG Raycast ke tanah bumi. Furnitur WAJIB menggunakan 'HitboxSeparation', 'CanCollide = true' (di hitbox), dan 'Anchored = true'.", ["Anchored", "CanCollide", "HitboxSeparation"], ["Raycast"]),
        ("SMELTING_FURNACE", 1, "ServerScriptService", "Rancang Mesin Peleburan Logam (Furnace) di Lobby. HUKUM SMELTING: Mesin ini memiliki wujud fisik 3D dan 'ProximityPrompt' (ActionText='Lebur Besi'). Jika pemain membawa 'Besi Mentah' (Raw Iron) di inventory, mesin akan menghapusnya dari inventory, memutar animasi/partikel api selama beberapa detik menggunakan 'task.wait()', lalu men-spawn 'Iron Ingot' (Besi Matang) di depan mesin agar bisa dipungut pemain.", ["ProximityPrompt", "task.wait", "ParticleEmitter"], []),
        ("NPC_TRADER", 8, "ServerScriptService", "Rancang Skrip Server 8 NPC Trader terspesialisasi: 1. Blacksmith (Dekat Furnace, jual Besi/Armor), 2. Woodworker (Jual Kayu/Kapak), 3. Stonemason (Jual Batu/Beliung), 4. Gunsmith (Jual Senjata Api/Peluru), 5. Medic (Medical), 6. Chef (Daging/Makanan), 7. Scientist (Material langka), 8. Black Market (Valuable). HUKUM NPC HIDUP: NPC DILARANG menjadi patung statis! Mereka WAJIB dipasangkan alat kerja (Palu, Gergaji, dll) di tangan mereka menggunakan `WeldConstraint`. HARGA JUAL NPC = BasePrice * 2.0. HARGA BELI DARI PEMAIN = BasePrice * 0.4.", ["Recipe", "ProximityPrompt", "BasePrice", "ItemCategory", "RemoteEvent", "WeldConstraint"], ["TakeDamage"]),
        ("NPC_SHOP_UI", 1, "StarterGui", "Rancang UI Katalog Belanja untuk NPC Trader.", ["RemoteEvent", "RemoteFunction"], []),
        ("FLEA_MARKET_BACKEND", 1, "ServerScriptService", "Rancang Backend Server Keamanan untuk Pasar Loak (Shopee pemain) menggunakan pcall.", ["RemoteFunction", "DataStoreService", "pcall", "Inbox"], []),
        ("PLAYER_FLEA_MARKET_UI", 1, "StarterGui", "Rancang UI Pasar Loak (Flea Market / Shopee antar pemain).", ["ItemCategory", "TextBox", "RemoteFunction"], []),
        ("CORE_MISSION_SYSTEM", 1, "ServerScriptService", "Rancang Sistem Misi Harian dan Event Mingguan (Quest System).", ["Inbox", "Mission"], []),
        ("CORE_MISSION_UI", 1, "StarterGui", "Rancang UI Daftar Misi mendengarkan RemoteEvent.", ["RemoteEvent"], []),
        ("CORE_MONETIZATION_SYSTEM", 1, "ServerScriptService", "Rancang sistem monetisasi sentral. HUKUM ANTI-P2W MUTLAK: `MarketplaceService.ProcessReceipt` HANYA BOLEH dideklarasikan di SATU skrip ini.", ["MarketplaceService", "ProcessReceipt", "DataStoreService", "pcall"], ["Sword", "Gun", "Armor", "Weapon"]),
        ("AUTONOMOUS_GAP_ANALYSIS", 1, "ServerScriptService", "Analisis ekosistem game saat ini. Secara otonom bangun sistem fundamental tambahan.", [], []),
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



# ══════════════════════════════════════════════════════════════════════════════
# ⚡ ANTIGRAVITY-STYLE PARALLEL EXECUTION SYSTEM
# Setiap task mendapat agent sendiri, berjalan bersamaan, tidak saling tunggu.
# File-lock mencegah dua agent menulis file yang sama secara bersamaan.
# ══════════════════════════════════════════════════════════════════════════════

_FILE_LOCKS: dict = {}
_FILE_LOCKS_MUTEX = asyncio.Lock()

async def _get_file_lock(path: str) -> asyncio.Lock:
    """Satu Lock per path file — mencegah tabrakan tulis antar agent."""
    async with _FILE_LOCKS_MUTEX:
        if path not in _FILE_LOCKS:
            _FILE_LOCKS[path] = asyncio.Lock()
        return _FILE_LOCKS[path]


async def _run_task_parallel(
    task_num: int,
    total_tasks: int,
    task: dict,
    dedicated_agent: dict,
    synthesizer,
    generation_counter: int,
    evolution_level: int,
) -> tuple:
    """
    ⚡ PARALLEL WORKER — Satu task, satu agent dedicated, berjalan bebas.
    Tidak berbagi agent dengan task lain = tidak ada antrian.
    File-lock mencegah tabrakan jika dua task menulis path yang sama.
    Healer berjalan per-task secara independen.
    """
    task_name = task["name"]
    task_path = task["path"]

    # ── Resume check ──────────────────────────────────────────────────────
    if os.path.exists(task_path) and os.path.getsize(task_path) > 50:
        _db_resume = establish_database_connection()
        _cur_resume = _db_resume.cursor()
        _cur_resume.execute(
            "SELECT module_name FROM verified_modules WHERE module_name = ?",
            (task_name,)
        )
        _row_resume = _cur_resume.fetchone()
        _db_resume.close()

        if _row_resume:
            console_terminal_interface.print(
                f"[dim green]⏭️  [RESUME] '{task_name}' sudah selesai. Dilewati.[/dim green]"
            )
            return (True, task_name, "resumed")
        else:
            try:
                with open(task_path, "r", encoding="utf-8") as _f_r:
                    _existing_code = _f_r.read()
                if len(_existing_code.strip()) > 50:
                    await save_verified_module(task_name, task_path, _existing_code)
                    console_terminal_interface.print(
                        f"[dim cyan]🔄 [RESUME-SYNC] '{task_name}' disinkronkan ke DB.[/dim cyan]"
                    )
                    return (True, task_name, "synced")
            except Exception:
                pass

    # ── Eksekusi dengan dedicated agent + file-lock ───────────────────────
    completed = False
    prev_err = ""
    prev_code = ""
    real_attempt_count = 0

    file_lock = await _get_file_lock(task_path)

    while not completed:
        console_terminal_interface.print(
            f"[bold cyan]  [{dedicated_agent['name']}] "
            f"Task {task_num}/{total_tasks}: {task_name} — "
            f"Percobaan {real_attempt_count + 1}/∞[/bold cyan]"
        )
        try:
            async with file_lock:
                completed, prev_err, prev_code = await synthesizer.synthesize_handoff(
                    dedicated_agent,
                    task_path,
                    task_name,
                    task["desc"],
                    task["req"],
                    task["forb"],
                    prev_err,
                    prev_code,
                )
        except Exception as exc:
            prev_err = f"EXCEPTION: {str(exc)}"
            completed = False

        if completed:
            # Notifikasi Telegram per task selesai
            await send_telegram_notification(
                f"✅ [{dedicated_agent['name']}] TASK SELESAI\n"
                f"📄 {task_name}\n"
                f"🔁 Evolusi {evolution_level} | Siklus {generation_counter}",
                important=False,
            )
            return (True, task_name, "done")

        if "RATE_LIMIT" in prev_err:
            # ⚡ Dedicated agent kena rate limit → retry cepat tanpa nunggu 60s
            console_terminal_interface.print(
                f"[bold yellow]  [{dedicated_agent['name']}] Rate limit → retry 5s...[/bold yellow]"
            )
            await asyncio.sleep(5)
        else:
            real_attempt_count += 1
            backoff_delay = min(real_attempt_count * 2, 10)
            if real_attempt_count > 0:
                await asyncio.sleep(backoff_delay)

    return (False, task_name, prev_err[:120])

async def run_orchestrator():
    try:
        await initialize_system_ledger()
        setup_rojo()
        NativeLuauCompiler.ensure_compiler_exists()

        asyncio.create_task(start_telemetry_webhook())
        asyncio.create_task(start_telegram_polling())  # Polyglot Telegram Listener (Non-Blocking)

        healer = AutoHealerAgent()
        await healer.initialize_and_scan()
        synthesizer = OmniSynthesizerAgent(healer)

        evolution_level = 1
        generation_counter = 1

        agent_idx = random.randint(0, len(ACTIVE_AGENTS) - 1) if ACTIVE_AGENTS else 0

        while True:
            console_terminal_interface.print(
                Panel(f"[bold magenta]=== EVOLUSI LEVEL {evolution_level} - SIKLUS KE-{generation_counter} ===[/bold magenta]")
            )

            if evolution_level == 1:
                task_queue = _build_task_queue()
            else:
                current_agent_architect = ACTIVE_AGENTS[agent_idx % len(ACTIVE_AGENTS)]
                agent_idx += 1
                task_queue = await DynamicTaskArchitect.analyze_and_plan_next_evolution(
                    evolution_level, current_agent_architect
                )

                if not task_queue:
                    console_terminal_interface.print(
                        "[bold yellow]Architect menyimpulkan game sudah lengkap atau terjadi limit. Memuat ulang ekspansi kosmetik...[/bold yellow]"
                    )
                    task_queue = [
                        {
                            "name": f"AUTONOMOUS_EXPANSION_{evolution_level}",
                            "path": os.path.join(
                                SOURCE_CODE_DIRECTORY, "StarterPlayerScripts",
                                f"EXPANSION_{evolution_level}.client.lua",
                            ),
                            "req": [],
                            "forb": ["_G", "shared"],
                            "desc": "Berdasarkan ekosistem yang ada, ciptakan sistem kosmetik atau optimasi baru yang membuat game ini mencapai level AAA.",
                        }
                    ]

            total_tasks = len(task_queue)

            console_terminal_interface.print(
            console_terminal_interface.print(
                Panel(
                    f"[bold cyan]⚡ PARALLEL EXECUTION AKTIF\n"
                    f"({total_tasks} tasks × {len(ACTIVE_AGENTS)} agents dedicated — tidak antri)\n"
                    f"Evolusi {evolution_level} | Siklus {generation_counter}[/bold cyan]"
                )
            )

            # Notifikasi Telegram: evolusi dimulai
            await send_telegram_notification(
                f"🚀 EVOLUSI {evolution_level} DIMULAI\n"
                f"⚡ {total_tasks} task berjalan PARALEL\n"
                f"👥 {len(ACTIVE_AGENTS)} agent dedicated (tidak ada antrian)\n"
                f"🔁 Siklus ke-{generation_counter}",
                important=True,
            )

            # Buat worker per task — masing-masing dapat agent sendiri (index-locked)
            _parallel_workers = [
                _run_task_parallel(
                    task_num=i + 1,
                    total_tasks=total_tasks,
                    task=task,
                    dedicated_agent=ACTIVE_AGENTS[i % len(ACTIVE_AGENTS)],
                    synthesizer=synthesizer,
                    generation_counter=generation_counter,
                    evolution_level=evolution_level,
                )
                for i, task in enumerate(task_queue)
            ]

            # ⚡ Semua task berjalan bersamaan — tidak saling tunggu
            _parallel_results = await asyncio.gather(*_parallel_workers, return_exceptions=True)

            # Hitung hasil
            tasks_done = sum(1 for r in _parallel_results if isinstance(r, tuple) and r[0])
            tasks_failed = total_tasks - tasks_done
            failed_tasks = [
                f"• {r[1]}: {r[2]}"
                for r in _parallel_results
                if isinstance(r, tuple) and not r[0]
            ]

            # Ringkasan evolusi ke Telegram
            _evo_summary = (
                f"📊 EVOLUSI {evolution_level} SELESAI\n"
                f"✅ Berhasil: {tasks_done}/{total_tasks}\n"
                f"❌ Gagal: {tasks_failed}/{total_tasks}\n"
                f"🔁 Siklus ke-{generation_counter}"
            )
            if failed_tasks:
                _evo_summary += "\n\n📋 Task gagal:\n" + "\n".join(failed_tasks[:5])
            await send_telegram_notification(_evo_summary, important=True)
                f"\n[bold magenta]Siklus {generation_counter} Selesai. "
                f"Berhasil: {tasks_done}/{total_tasks}, Gagal: {tasks_failed}/{total_tasks}. "
                f"Sinkronisasi File...[/bold magenta]"
            )
            await dump_ssd()

            if evolution_level >= 50:
                console_terminal_interface.print(
                    f"[bold green]🎉 Siklus {generation_counter} (Evolusi {evolution_level}) Selesai! Deploy ke Roblox... Sistem akan lanjut otomatis.[/bold green]"
                )

                # ══════════════════════════════════════════════════════════════
                # PRE-DEPLOYMENT VALIDATOR
                # Pastikan 100% file sudah ada dan valid sebelum upload ke Roblox.
                # Jika ada yang hilang → regenerasi dulu, deployment menunggu.
                # Deployment DIBATALKAN jika masih ada file yang tidak bisa dibuat.
                # ══════════════════════════════════════════════════════════════
                _validator = PreDeploymentValidator()
                _deploy_agent = ACTIVE_AGENTS[agent_idx % len(ACTIVE_AGENTS)]
                _validation_task_queue = _build_task_queue()

                console_terminal_interface.print(
                    "[bold yellow]🛡️  [PRE-DEPLOY] Memverifikasi kelengkapan semua file game...[/bold yellow]"
                )
                _deploy_safe = await _validator.validate_and_complete(
                    task_queue=_validation_task_queue,
                    synthesizer=synthesizer,
                    agent=_deploy_agent,
                    notify_fn=send_telegram_notification,
                )

                if not _deploy_safe:
                    validator_fail_msg = (
                        "🚫 DEPLOYMENT DIBATALKAN\n"
                        "Pre-Deployment Validator menemukan file yang tidak bisa di-generate.\n"
                        "Periksa log VPS: tail -f nohup.out\n\n"
                        "Kemungkinan penyebab:\n"
                        "• Rate limit API Gemini habis\n"
                        "• Task tertentu selalu gagal compiler check\n"
                        "• Disk VPS penuh"
                    )
                    console_terminal_interface.print(
                        "[bold red]🚫 DEPLOYMENT DIBATALKAN: File tidak 100% lengkap setelah regenerasi.[/bold red]"
                    )
                    await send_telegram_notification(validator_fail_msg, important=True)
                    # Jika file .rbxl hasil build sebelumnya masih ada, kirim juga
                    if os.path.exists(COMPILED_GAME_FILE):
                        await send_telegram_document(
                            COMPILED_GAME_FILE,
                            f"📦 File .rbxl terakhir yang tersimpan (mungkin tidak lengkap)\n"
                            f"JANGAN upload ke Roblox sebelum memeriksa kelengkapan file!",
                        )
                    # ⚡ INFINITE: Jangan stop, reset ke evolusi 1 dan lanjut
                    evolution_level = 1
                    generation_counter += 1
                    await asyncio.sleep(15)
                    continue

                # ══════════════════════════════════════════════════════════════
                # Semua file sudah 100% valid → lanjutkan deployment
                # ══════════════════════════════════════════════════════════════
                rojo_ok = RobloxDeployer.compile_rojo()
                if not rojo_ok:
                    # Rojo build gagal → notif Telegram + kirim src sebagai arsip
                    rojo_fail_msg = (
                        f"🔨 ROJO BUILD GAGAL (Evolusi {evolution_level})\n"
                        f"File .rbxl tidak berhasil dibuat oleh Rojo.\n"
                        f"Kemungkinan ada syntax error di file .lua.\n\n"
                        f"Periksa log VPS: tail -f nexus_healer.log"
                    )
                    console_terminal_interface.print(f"[bold red]{rojo_fail_msg}[/bold red]")
                    await send_telegram_notification(rojo_fail_msg, important=True)
                else:
                    await healer.initialize_and_scan()
                    await RobloxDeployer.publish(evolution_level)
                # ⚡ INFINITE: Siklus selesai → reset ke evolusi 1, lanjut otomatis
                await send_telegram_notification(
                    f"♾️ Siklus {generation_counter} selesai! Memulai siklus {generation_counter + 1}...",
                    important=True
                )
                evolution_level = 1
                generation_counter += 1
                await asyncio.sleep(15)
                continue

            evolution_level += 1
            generation_counter += 1

            await asyncio.sleep(10)

    except Exception as e:
        error_msg = f"FATAL ERROR di Orchestrator: {type(e).__name__}: {e}"
        console_terminal_interface.print(f"[bold red]{error_msg}[/bold red]")
        try:
            await send_telegram_notification(f"❌ {error_msg}")
        except Exception:
            pass
        raise


def _shutdown_handler(signum, frame):
    console_terminal_interface.print("[bold red]\nSistem dihentikan oleh pengguna (SIGINT/SIGTERM).[/bold red]")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    console_terminal_interface.print(
        Panel("[bold cyan]NEXUS TIER ABSOLUTE APEX - SELF-HEALING AUTONOMOUS AGENT INITIALIZING...[/bold cyan]")
    )
    try:
        asyncio.run(run_orchestrator())
    except SystemExit:
        pass
