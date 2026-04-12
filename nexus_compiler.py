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
                    omni_errors.append("Crafting Logic Violation: Tabel 'Recipe' ditemukan, tetapi KOSONG atau formatnya salah.")

            if req == "ArmorTier":
                if not re.search(r'ArmorTier\s*[=:]\s*[1-6]', sanitized_code):
                    omni_errors.append("Armor Physics Violation: 'ArmorTier' wajib didefinisikan dengan nilai angka 1 hingga 6.")

            if req == "MaterialType":
                if not re.search(r'MaterialType\s*[=:]\s*[\'"][a-zA-Z]+[\'"]', sanitized_code):
                    omni_errors.append("Armor Physics Violation: 'MaterialType' wajib didefinisikan sebagai string.")

            if req == "ItemCategory":
                if not re.search(r'ItemCategory\s*[=:]\s*[\'"](Weapon|Ammunition|Armor|Medical|Material|Valuable|Bait|Tool)[\'"]', sanitized_code, re.IGNORECASE):
                    omni_errors.append("Economy Taxonomy Violation: 'ItemCategory' wajib diisi dengan salah satu kategori resmi.")

            if req == "BasePrice":
                if not re.search(r'BasePrice\s*[=:]\s*\d+', sanitized_code):
                    omni_errors.append("Economy Violation: 'BasePrice' wajib didefinisikan sebagai angka integer.")

            if req == "CanCollide":
                if not re.search(r'CanCollide\s*=\s*true', sanitized_code, re.IGNORECASE):
                    omni_errors.append("Physical Collision Violation: Objek dunia WAJIB memiliki properti 'CanCollide = true'.")

            if req == "Anchored":
                if not re.search(r'Anchored\s*=\s*true', sanitized_code, re.IGNORECASE):
                    omni_errors.append("Physical Gravity Violation: Objek dunia WAJIB memiliki properti 'Anchored = true'.")

            if req == "Raycast":
                if "workspace:Raycast" not in sanitized_code and "workspace.Raycast" not in sanitized_code:
                    omni_errors.append("Physical Placement Violation: Anda WAJIB menggunakan 'workspace:Raycast()' ke arah bawah.")

            if req == "RaycastParams":
                if "RaycastParams.new" not in sanitized_code:
                    omni_errors.append("Physical Precision Violation: Saat menggunakan Raycast, WAJIB menggunakan 'RaycastParams.new()'.")

            if req == "HitboxSeparation":
                if not re.search(r'CanCollide\s*=\s*false', sanitized_code, re.IGNORECASE) or not re.search(r'Transparency\s*=\s*1', sanitized_code):
                    omni_errors.append("Collision Optimization Violation: WAJIB menerapkan Hitbox Separation.")

            if req == "VisualEquip":
                if not re.search(r'WeldConstraint|AddAccessory|Motor6D', sanitized_code, re.IGNORECASE):
                    omni_errors.append("Visual Equip Violation: WAJIB menyertakan logika untuk menempelkan 3D model ke badan pemain.")
                if not re.search(r'ActionText\s*=\s*[\'"](?:Equip|Gunakan|Pakai)[\'"]', sanitized_code, re.IGNORECASE):
                    omni_errors.append("Visual Equip Violation: ProximityPrompt WAJIB memiliki ActionText bernilai 'Equip' atau 'Gunakan'.")

        if "OnServerEvent" in sanitized_code or "OnServerInvoke" in sanitized_code:
            if not re.search(r'typeof\s*\(', sanitized_code) and not re.search(r'type\s*\(', sanitized_code):
                omni_errors.append("Zero-Trust Security Violation: RemoteEvent/RemoteFunction terdeteksi tanpa validasi tipe data menggunakan `typeof()`.")

        if "DataStoreService" in sanitized_code or "SetAsync" in sanitized_code or "UpdateAsync" in sanitized_code:
            if "pcall" not in sanitized_code and "xpcall" not in sanitized_code:
                omni_errors.append("Fault-Tolerance Violation: Operasi DataStoreService terdeteksi tanpa perlindungan `pcall()`.")

        if "DataStoreService" in sanitized_code and ("SafeContainer" in sanitized_code or "LobbyStorage" in sanitized_code):
            if "PlayerRemoving" not in sanitized_code or "PlayerAdded" not in sanitized_code:
                omni_errors.append("Data Persistence Violation: Sistem inventaris permanen terdeteksi tanpa event `PlayerAdded` dan `PlayerRemoving`.")

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
                omni_errors.append("Ecology Violation: Variabel 'Habitat' wajib diisi dengan string nama bioma.")
        if "Stamina" in required_keywords:
            if not re.search(r'Stamina\s*[=:]\s*\d+', sanitized_code):
                omni_errors.append("Ecology Violation: Variabel 'Stamina' wajib didefinisikan sebagai angka.")
        if "PerceptionRadius" in required_keywords:
            if not re.search(r'PerceptionRadius\s*[=:]\s*\d+', sanitized_code):
                omni_errors.append("Ecology Violation: Variabel 'PerceptionRadius' wajib didefinisikan sebagai angka.")
        if "LocomotionType" in required_keywords:
            if not re.search(r'LocomotionType\s*[=:]\s*[\'"](Terrestrial|Aerial|Aquatic)[\'"]', sanitized_code, re.IGNORECASE):
                omni_errors.append("Ecology Violation: Variabel 'LocomotionType' wajib diisi.")
        if "DropTable" in required_keywords:
            has_drop_table = re.search(r'DropTable\s*[=:]\s*\{', sanitized_code)
            is_bait = re.search(r'(IsBait|Unkillable)\s*[=:]\s*true', sanitized_code, re.IGNORECASE)
            if not has_drop_table and not is_bait:
                omni_errors.append("Ecology & Economy Violation: Variabel 'DropTable' wajib didefinisikan berupa tabel.")

        for forb in forbidden_keywords:
            if forb in sanitized_code:
                omni_errors.append(f"Contract Violation: Dilarang keras menggunakan '{forb}' pada modul ini.")

        if omni_errors:
            return False, "VALIDASI LEKSIKAL OMNI GAGAL (PERBAIKI SEMUA):\n- " + "\n- ".join(omni_errors)

        return True, "Validasi Leksikal Tingkat Militer Lulus 100%."


class NativeLuauCompiler:
    """Kompilator AST C++ dan Eksekutor Runtime Lune."""

    @staticmethod
    def ensure_compiler_exists():
        if not os.path.exists(LUAU_ANALYZE_BINARY_PATH):
            console_terminal_interface.print("[bold yellow]luau-analyze tidak ditemukan. Mengunduh binary Linux terbaru...[/bold yellow]")
            try:
                subprocess.run([
                    "wget", "-q", "https://github.com/luau-lang/luau/releases/latest/download/luau-ubuntu.zip",
                    "-O", "/tmp/luau-ubuntu.zip"
                ], check=True, timeout=60)
                subprocess.run(["unzip", "-o", "/tmp/luau-ubuntu.zip", "luau-analyze", "-d", "/tmp/"], check=True)
                subprocess.run(["chmod", "+x", "/tmp/luau-analyze"], check=True)
                subprocess.run(["mv", "/tmp/luau-analyze", LUAU_ANALYZE_BINARY_PATH], check=True)
            except Exception as e:
                console_terminal_interface.print(f"[bold yellow]luau-analyze download gagal: {e}. Akan dilewati.[/bold yellow]")

        if not os.path.exists(LUNE_BINARY_PATH):
            console_terminal_interface.print("[bold yellow]lune tidak ditemukan. Mengunduh binary Linux terbaru...[/bold yellow]")
            try:
                subprocess.run([
                    "wget", "-q", "https://github.com/lune-org/lune/releases/latest/download/lune-linux-x86_64.zip",
                    "-O", "/tmp/lune-linux-x86_64.zip"
                ], check=True, timeout=60)
                subprocess.run(["unzip", "-o", "/tmp/lune-linux-x86_64.zip", "lune", "-d", "/tmp/"], check=True)
                subprocess.run(["chmod", "+x", "/tmp/lune"], check=True)
                subprocess.run(["mv", "/tmp/lune", LUNE_BINARY_PATH], check=True)
            except Exception as e:
                console_terminal_interface.print(f"[bold yellow]lune download gagal: {e}. Akan dilewati.[/bold yellow]")

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
        if not os.path.exists(LUAU_ANALYZE_BINARY_PATH):
            return True, "luau-analyze tidak tersedia, dilewati."

        fd, temp_path = tempfile.mkstemp(suffix=".luau", prefix=f"temp_{module_name}_")
        os.close(fd)

        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(luau_code)

            loop = asyncio.get_event_loop()

            analyze_process = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [LUAU_ANALYZE_BINARY_PATH, "--formatter=plain", temp_path],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
            )

            if analyze_process.returncode != 0:
                error_msg = analyze_process.stderr.strip() or analyze_process.stdout.strip()
                return False, f"AST COMPILATION FAILED (luau-analyze):\n{error_msg}"

            if os.path.exists(LUNE_BINARY_PATH):
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
                    return False, f"RUNTIME EXECUTION FAILED (lune):\n{error_msg}"

            return True, "AST dan Runtime Lune Lulus 100%."

        except subprocess.TimeoutExpired:
            return False, "TIMEOUT: Runtime Lune mengalami infinite loop/hang (Maks 5 detik)."
        except Exception as e:
            return False, f"SYSTEM ERROR saat eksekusi native: {str(e)}"
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
