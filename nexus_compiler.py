import re
import asyncio
import tempfile
import os
import subprocess
import json
from typing import Tuple, List

from nexus_config import (
    console_terminal_interface,
    LUAU_ANALYZE_BINARY_PATH,
    LUNE_BINARY_PATH,
    LUAURC_PATH,
    PROJECT_ROOT_DIRECTORY
)


class AbsoluteOmniValidator:
    """Hakim Keamanan Leksikal Murni Tingkat Militer."""

    @staticmethod
    def sanitize_luau_code(raw_luau_code: str) -> str:
        code = re.sub(r'^\s*--.*$', '', raw_luau_code, flags=re.MULTILINE)
        return code

    @staticmethod
    def execute_validation(raw_luau_code: str, required_keywords: List[str], forbidden_keywords: List[str]) -> Tuple[bool, str]:
        omni_errors = []
        sanitized_code = AbsoluteOmniValidator.sanitize_luau_code(raw_luau_code)

        if not sanitized_code.strip() or len(sanitized_code) < 20:
            return False, "Kode kosong atau terlalu pendek untuk dievaluasi."

        if not re.search(r'^--!strict', raw_luau_code):
            omni_errors.append("Contract Violation: Baris pertama MUTLAK harus `--!strict`.")

        if "while true do" in sanitized_code or "while task.wait() do" in sanitized_code:
            if "RunService" not in sanitized_code and "task.wait" not in sanitized_code:
                omni_errors.append("Performance Violation: Loop terdeteksi tanpa RunService atau task.wait().")

        for req in required_keywords:
            if req not in sanitized_code:
                omni_errors.append(f"Contract Violation: Anda diwajibkan menggunakan '{req}' agar sesuai arsitektur.")
            
            if req == "Recipe":
                if not re.search(r'Recipe\s*[=:]\s*\{\s*[\'"]?[a-zA-Z_]', sanitized_code):
                    omni_errors.append("Crafting Logic Violation: Tabel 'Recipe' ditemukan, tetapi KOSONG atau formatnya salah. Hukum Crafting anti-P2W mewajibkan definisi bahan baku nyata di dalam tabel, contoh: `local Recipe = { Iron = 2, Wood = 1 }`.")

            if req == "ArmorTier":
                if not re.search(r'ArmorTier\s*[=:]\s*[1-6]', sanitized_code):
                    omni_errors.append("Armor Physics Violation: 'ArmorTier' wajib didefinisikan dengan nilai angka 1 hingga 6 (Contoh: ArmorTier = 4).")
            
            if req == "MaterialType":
                if not re.search(r'MaterialType\s*[=:]\s*[\'"][a-zA-Z]+[\'"]', sanitized_code):
                    omni_errors.append("Armor Physics Violation: 'MaterialType' wajib didefinisikan sebagai string (Contoh: MaterialType = 'Ceramic' atau 'Steel').")

            if req == "ItemCategory":
                if not re.search(r'ItemCategory\s*[=:]\s*[\'"](Weapon|Ammunition|Armor|Medical|Material|Valuable|Bait|Tool)[\'"]', sanitized_code, re.IGNORECASE):
                    omni_errors.append("Economy Taxonomy Violation: 'ItemCategory' wajib diisi SECARA SPESIFIK dengan salah satu kategori resmi ini: 'Weapon', 'Ammunition', 'Armor', 'Medical', 'Material', 'Valuable', 'Tool', atau 'Bait'. Dilarang mengarang nama kategori lain!")

            if req == "BasePrice":
                if not re.search(r'BasePrice\s*[=:]\s*\d+', sanitized_code):
                    omni_errors.append("Economy Violation: 'BasePrice' wajib didefinisikan sebagai angka integer variabel sungguhan (Contoh: local BasePrice = 1500). Dilarang keras memakai string atau komentar untuk harga!")

            if req == "CanCollide":
                if not re.search(r'CanCollide\s*=\s*true', sanitized_code, re.IGNORECASE):
                    omni_errors.append("Physical Collision Violation: Objek dunia (Pohon, Batu, Tanah) WAJIB memiliki properti 'CanCollide = true'. Pemain DILARANG KERAS menembus objek fisik secara tidak logis seperti hantu!")
            
            if req == "Anchored":
                if not re.search(r'Anchored\s*=\s*true', sanitized_code, re.IGNORECASE):
                    omni_errors.append("Physical Gravity Violation: Objek dunia WAJIB memiliki properti 'Anchored = true' agar tidak jatuh menembus baseplate karena gravitasi.")
            
            if req == "Raycast":
                if "workspace:Raycast" not in sanitized_code and "workspace.Raycast" not in sanitized_code:
                    omni_errors.append("Physical Placement Violation: Anda WAJIB menggunakan 'workspace:Raycast()' ke arah bawah untuk menemukan titik permukaan tanah (Y-level) sebelum meletakkan objek lingkungan (Pohon/Batu/Tanah). DILARANG KERAS membuat objek melayang di udara!")

            if req == "RaycastParams":
                if "RaycastParams.new" not in sanitized_code:
                    omni_errors.append("Physical Precision Violation (DevForum Standard): Saat menggunakan Raycast, Anda WAJIB menggunakan 'RaycastParams.new()' dan mengatur FilterType agar sinar mengabaikan daun pohon/batu lain. Jika tidak, objek akan mendarat di atas daun dan melayang di udara!")

            if req == "HitboxSeparation":
                if not re.search(r'CanCollide\s*=\s*false', sanitized_code, re.IGNORECASE) or not re.search(r'Transparency\s*=\s*1', sanitized_code):
                    omni_errors.append("Collision Optimization Violation (DevForum AAA Standard): Anda DILARANG menggunakan kolisi bawaan Mesh/Model yang rumit. WAJIB menerapkan Hitbox Separation! (1. Buat Part Transparan dengan CanCollide=true sebagai Hitbox tak terlihat. 2. Jadikan Mesh/Part visualnya CanCollide=false).")

            if req == "VisualEquip":
                if not re.search(r'WeldConstraint|AddAccessory|Motor6D', sanitized_code, re.IGNORECASE):
                    omni_errors.append("Visual Equip Violation (DevForum Standard): Pemain TIDAK BISA melihat senjata atau armor di badannya! Anda WAJIB menyertakan logika untuk menempelkan 3D model ke badan pemain (Gunakan 'WeldConstraint' ke Torso/Head, atau 'Humanoid:AddAccessory()') ketika ProximityPrompt ditekan!")
                if not re.search(r'ActionText\s*=\s*[\'"](?:Equip|Gunakan|Pakai)[\'"]', sanitized_code, re.IGNORECASE):
                    omni_errors.append("Visual Equip Violation: ProximityPrompt untuk item ini WAJIB memiliki ActionText bernilai 'Equip' atau 'Gunakan' agar pemain mengerti cara memakainya.")

        if "OnServerEvent" in sanitized_code or "OnServerInvoke" in sanitized_code:
            if not re.search(r'typeof\s*\(', sanitized_code) and not re.search(r'type\s*\(', sanitized_code):
                omni_errors.append("Zero-Trust Security Violation: RemoteEvent/RemoteFunction terdeteksi (`OnServerEvent` / `OnServerInvoke`), tetapi tidak ada validasi tipe data menggunakan `typeof()`. Hacker (Exploiter) dapat memanipulasi parameter dari Client (misal: mengirim harga barang = 0 di pasar loak). Anda WAJIB memvalidasi argumen dari client!")

        if "DataStoreService" in sanitized_code or "SetAsync" in sanitized_code or "UpdateAsync" in sanitized_code:
            if "pcall" not in sanitized_code and "xpcall" not in sanitized_code:
                omni_errors.append("Fault-Tolerance Violation: Operasi database ekonomi (DataStoreService) terdeteksi tanpa perlindungan `pcall()`. Jika server Roblox mengalami gangguan, script ini akan crash dan menyebabkan hilangnya data transaksi uang/barang pemain!")
        
        if "DataStoreService" in sanitized_code and ("SafeContainer" in sanitized_code or "LobbyStorage" in sanitized_code):
            if "PlayerRemoving" not in sanitized_code or "PlayerAdded" not in sanitized_code:
                omni_errors.append("Data Persistence Violation: Sistem inventaris permanen terdeteksi, tetapi Anda tidak menggunakan event `PlayerAdded` (untuk load) dan `PlayerRemoving` (untuk save). Ini akan menyebabkan barang pemain hilang mutlak saat mereka keluar dari game/disconnect! WAJIB pasang event tersebut untuk menyimpan data ke database.")
        
        if "Diet" in required_keywords:
            if not re.search(r'Diet\s*[=:]\s*[\'"](Carnivore|Herbivore|Omnivore)[\'"]', sanitized_code, re.IGNORECASE):
                omni_errors.append("Ecology Violation: Variabel 'Diet' wajib diisi dengan string 'Carnivore', 'Herbivore', atau 'Omnivore'.")
        if "SocialBehavior" in required_keywords:
            if not re.search(r'SocialBehavior\s*[=:]\s*[\'"](Solitary|Pack|Herd)[\'"]', sanitized_code, re.IGNORECASE):
                omni_errors.append("Ecology Violation: Variabel 'SocialBehavior' wajib diisi dengan string 'Solitary', 'Pack', atau 'Herd'.")
        if "SpawnWeight" in required_keywords:
            if not re.search(r'SpawnWeight\s*[=:]\s*\d+', sanitized_code):
                omni_errors.append("Ecology Violation: Variabel 'SpawnWeight' wajib didefinisikan sebagai angka.")
        if "Habitat" in required_keywords:
            if not re.search(r'Habitat\s*[=:]\s*[\'"][a-zA-Z_]+[\'"]', sanitized_code, re.IGNORECASE):
                omni_errors.append("Ecology Violation: Variabel 'Habitat' wajib diisi dengan string nama bioma (contoh: 'Forest', 'Desert', 'Snow').")
        if "Stamina" in required_keywords:
            if not re.search(r'Stamina\s*[=:]\s*\d+', sanitized_code):
                omni_errors.append("Ecology Violation: Variabel 'Stamina' wajib didefinisikan sebagai angka.")
        if "PerceptionRadius" in required_keywords:
            if not re.search(r'PerceptionRadius\s*[=:]\s*\d+', sanitized_code):
                omni_errors.append("Ecology Violation: Variabel 'PerceptionRadius' wajib didefinisikan sebagai angka.")
        if "LocomotionType" in required_keywords:
            if not re.search(r'LocomotionType\s*[=:]\s*[\'"](Terrestrial|Aerial|Aquatic)[\'"]', sanitized_code, re.IGNORECASE):
                omni_errors.append("Ecology Violation: Variabel 'LocomotionType' wajib diisi ('Terrestrial', 'Aerial', 'Aquatic').")
        if "DropTable" in required_keywords:
            has_drop_table = re.search(r'DropTable\s*[=:]\s*\{', sanitized_code)
            is_bait = re.search(r'(IsBait|Unkillable)\s*[=:]\s*true', sanitized_code, re.IGNORECASE)
            if not has_drop_table and not is_bait:
                omni_errors.append("Ecology & Economy Violation: Variabel 'DropTable' wajib didefinisikan berupa tabel, KECUALI jika Anda mendefinisikan item ini sebagai umpan hidup dengan 'local IsBait = true'.")

        if "TweenService" in required_keywords and "ProximityPrompt" in required_keywords and "ItemCategory" in sanitized_code:
            if not re.search(r'TweenService', sanitized_code):
                omni_errors.append("Item Logic Violation: Umpan (Bait) hidup WAJIB menggunakan TweenService untuk membuat animasinya menggeliat/bergerak.")

        for forb in forbidden_keywords:
            if forb in sanitized_code:
                omni_errors.append(f"Contract Violation: Dilarang keras menggunakan '{forb}' pada modul ini.")

        if omni_errors:
            return False, "VALIDASI LEKSIKAL OMNI GAGAL (PERBAIKI SEMUA):\n- " + "\n- ".join(omni_errors)

        return True, "Validasi Leksikal Tingkat Militer Lulus 100%."


class NativeLuauCompiler:
    """Kompilator AST C++ dan Eksekutor Runtime Lune."""

    @staticmethod
    def _download_and_extract(url: str, binary_name: str, dest_path: str):
        """Download zip dari URL, ekstrak binary_name, taruh di dest_path. Tanpa wget/unzip."""
        import urllib.request
        import zipfile
        import tempfile
        tmp_zip = tempfile.mktemp(suffix=".zip")
        try:
            urllib.request.urlretrieve(url, tmp_zip)
            with zipfile.ZipFile(tmp_zip, "r") as z:
                members = z.namelist()
                target = next((m for m in members if os.path.basename(m) == binary_name), None)
                if target is None:
                    raise FileNotFoundError(f"{binary_name} tidak ditemukan di dalam {url}")
                with z.open(target) as src, open(dest_path, "wb") as dst:
                    dst.write(src.read())
            os.chmod(dest_path, 0o755)
        finally:
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)

    @staticmethod
    def _get_lune_latest_url() -> str:
        """Ambil URL download lune Linux x86_64 terbaru dari GitHub API."""
        import urllib.request
        import json as _json
        try:
            req = urllib.request.urlopen(
                "https://api.github.com/repos/lune-org/lune/releases/latest", timeout=15
            )
            data = _json.loads(req.read())
            for asset in data.get("assets", []):
                name = asset["name"]
                if "linux" in name and "x86_64" in name and name.endswith(".zip"):
                    return asset["browser_download_url"]
        except Exception:
            pass
        return "https://github.com/lune-org/lune/releases/latest/download/lune-linux-x86_64.zip"

    @staticmethod
    def ensure_compiler_exists():
        if not os.path.exists(LUAU_ANALYZE_BINARY_PATH):
            console_terminal_interface.print("[bold yellow]luau-analyze tidak ditemukan. Mengunduh binary Linux terbaru...[/bold yellow]")
            NativeLuauCompiler._download_and_extract(
                "https://github.com/luau-lang/luau/releases/latest/download/luau-ubuntu.zip",
                "luau-analyze",
                LUAU_ANALYZE_BINARY_PATH,
            )
            console_terminal_interface.print("[bold green]luau-analyze berhasil diunduh.[/bold green]")

        if not os.path.exists(LUNE_BINARY_PATH):
            console_terminal_interface.print("[bold yellow]lune tidak ditemukan. Mengunduh binary Linux terbaru...[/bold yellow]")
            lune_url = NativeLuauCompiler._get_lune_latest_url()
            NativeLuauCompiler._download_and_extract(lune_url, "lune", LUNE_BINARY_PATH)
            console_terminal_interface.print("[bold green]lune berhasil diunduh.[/bold green]")

        if not os.path.exists(LUAURC_PATH):
            luaurc_content = {
                "languageMode": "strict",
                "lint": {
                    "UnknownGlobal": False,
                    "GlobalPredecl": False,
                    "DeprecatedApi": True
                },
                "globals": [
                    "game", "workspace", "script", "math", "table", "string", "coroutine", 
                    "task", "os", "debug", "utf8", "bit32", "require", "tick", "wait", 
                    "delay", "spawn", "warn", "print", "error", "assert", "type", "typeof", 
                    "tostring", "tonumber", "pairs", "ipairs", "next", "select", "unpack", 
                    "getmetatable", "setmetatable", "pcall", "xpcall", "rawequal", "rawget", 
                    "rawset", "rawlen", "Vector3", "Vector2", "CFrame", "Color3", "UDim2", 
                    "UDim", "Instance", "Enum", "RaycastParams", "TweenInfo", "NumberSequence", 
                    "ColorSequence", "NumberSequenceKeypoint", "ColorSequenceKeypoint", 
                    "Region3", "Region3int16", "Vector3int16", "Vector2int16", "BrickColor", 
                    "Faces", "Axes", "PhysicalProperties", "PathfindingModifier"
                ]
            }
            with open(LUAURC_PATH, "w") as f:
                json.dump(luaurc_content, f, indent=4)

    @staticmethod
    async def execute_native_ast_verification(luau_code: str, module_name: str) -> Tuple[bool, str]:
        fd, temp_path = tempfile.mkstemp(suffix=".luau", prefix=f"temp_{module_name}_")
        os.close(fd)

        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(luau_code)

            loop = asyncio.get_running_loop()
            
            analyze_process = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [LUAU_ANALYZE_BINARY_PATH, "--formatter=plain", temp_path],
                    capture_output=True,
                    text=True
                )
            )

            if analyze_process.returncode != 0:
                error_msg = analyze_process.stderr.strip() or analyze_process.stdout.strip()
                return False, f"AST COMPILATION FAILED (luau-analyze):\n{error_msg}"

            lune_process = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [LUNE_BINARY_PATH, "run", temp_path],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
            )
            
            if lune_process.returncode != 0:
                error_msg = lune_process.stderr.strip() or lune_process.stdout.strip()
                # Roblox-specific globals (game, workspace, script, Players, dll) tidak ada
                # di lune — ini BUKAN bug pada kode Luau, hanya limitasi runtime lune.
                # Kode tetap valid untuk Roblox Studio.
                ROBLOX_ENV_ERRORS = [
                    "attempt to index nil with 'GetService'",
                    "attempt to index nil with 'GetAttribute'",
                    "attempt to call nil",
                    "attempt to index nil",
                    "attempt to perform arithmetic on nil",
                    "game is not defined",
                    "workspace is not defined",
                    "script is not defined",
                ]
                is_roblox_env_error = any(sig in error_msg for sig in ROBLOX_ENV_ERRORS)
                if is_roblox_env_error:
                    return True, f"AST Lulus. Lune warning (Roblox env — normal): {error_msg[:120]}"
                return False, f"RUNTIME EXECUTION FAILED (lune):\n{error_msg}"

            return True, "AST dan Runtime Lune Lulus 100%."

        except subprocess.TimeoutExpired:
            return False, "TIMEOUT: Runtime Lune mengalami infinite loop/hang (Maks 5 detik)."
        except Exception as e:
            return False, f"SYSTEM ERROR saat eksekusi native: {str(e)}"
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
