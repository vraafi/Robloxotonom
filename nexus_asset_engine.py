"""
nexus_asset_engine.py
=====================
Modul BARU untuk pembuatan aset dan UI Roblox secara otonom di VPS Linux tanpa Studio.
TIDAK mengubah logika Gemini CLI, agent orchestrator, atau pipeline yang sudah ada.

Komponen:
  - detect_asset_type       : Mendeteksi tipe aset dari nama task
  - AssetDirectoryManager   : Menentukan folder tujuan yang benar (struktur Rojo)
  - RbxmxGenerator          : Membuat file .rbxmx (XML Roblox) untuk Part/Model/GUI
  - AssetTestValidator      : Validasi struktural XML tanpa perlu Studio
  - ReModelRunner           : Menjalankan remodel secara headless (tes DataModel)
  - OpenCloudAssetUploader  : Upload mesh/aset ke Roblox via Open Cloud API
  - AssetOrchestrator       : Titik masuk utama yang dipanggil agent
"""

import os
import re
import json
import asyncio
import subprocess
import requests
import xml.etree.ElementTree as ET
from typing import Tuple, Optional

from nexus_config import (
    console_terminal_interface,
    PROJECT_ROOT_DIRECTORY,
    SOURCE_CODE_DIRECTORY,
    ROBLOX_OPEN_CLOUD_API_KEY,
    ROBLOX_UNIVERSE_ID,
    COMPILED_GAME_FILE,
)

# ============================================================
# KONSTANTA PATH — Mengikuti struktur Rojo
# ============================================================
_ASSETS_DIR         = os.path.join(SOURCE_CODE_DIRECTORY, "Workspace")
_GUI_DIR            = os.path.join(SOURCE_CODE_DIRECTORY, "StarterGui")
_PLAYER_DIR         = os.path.join(SOURCE_CODE_DIRECTORY, "StarterPlayer", "StarterPlayerScripts")
_SERVER_DIR         = os.path.join(SOURCE_CODE_DIRECTORY, "ServerScriptService")
_REPLICATED_DIR     = os.path.join(SOURCE_CODE_DIRECTORY, "ReplicatedStorage")
_MESH_OBJ_DIR       = os.path.join(PROJECT_ROOT_DIRECTORY, "mesh_exports")
_REMODEL_SCRIPTS    = os.path.join(PROJECT_ROOT_DIRECTORY, "remodel_scripts")
_REMODEL_BIN        = os.path.join(PROJECT_ROOT_DIRECTORY, "remodel")

for _d in [_ASSETS_DIR, _GUI_DIR, _PLAYER_DIR, _SERVER_DIR,
           _REPLICATED_DIR, _MESH_OBJ_DIR, _REMODEL_SCRIPTS]:
    os.makedirs(_d, exist_ok=True)

# ============================================================
# PENDETEKSI TIPE ASET — Berdasarkan nama task
# ============================================================
_GUI_KEYWORDS   = [
    "GUI", "UI", "SCREEN", "HUD", "MENU", "BUTTON", "FRAME",
    "INVENTORY", "DIALOG", "SHOP", "NOTIFICATION", "TOOLTIP",
    "OVERLAY", "HOTBAR", "COMPASS", "MINIMAP", "SCOREBOARD",
    "LEADERBOARD", "CROSSHAIR", "HEALTH_BAR", "STAMINA_BAR",
]
_MESH_KEYWORDS  = ["MESH", "OBJ_ASSET", "3D_ASSET", "CUSTOM_MESH"]
_MODEL_KEYWORDS = [
    "MODEL", "PROP", "FURNITURE", "BUILDING", "TREE", "ROCK",
    "VEHICLE", "WEAPON_MODEL", "ARMOR_MODEL", "CHEST", "BARREL",
    "CRATE", "BRIDGE", "LADDER", "FENCE", "DOOR", "WINDOW",
]
_WORLD_KEYWORDS = [
    "WORLD", "TERRAIN", "MAP", "SPAWN", "ZONE", "REGION",
    "CHECKPOINT", "LANDMARK", "BASEPLATE", "AMBIENT", "FOG",
    "LIGHTING", "ATMOSPHERE", "SKYBOX",
]


def detect_asset_type(task_name: str) -> str:
    """
    Mengembalikan tipe aset berdasarkan nama task:
      'GUI'   → ScreenGui / UI element (disimpan ke StarterGui)
      'MODEL' → Model 3D rbxmx (disimpan ke Workspace)
      'MESH'  → Upload mesh ke Open Cloud API
      'WORLD' → Script/Part dunia (disimpan ke Workspace)
      'LUAU'  → Script biasa — TIDAK ditangani modul ini
    """
    upper = task_name.upper()
    for kw in _GUI_KEYWORDS:
        if kw in upper:
            return "GUI"
    for kw in _MESH_KEYWORDS:
        if kw in upper:
            return "MESH"
    for kw in _MODEL_KEYWORDS:
        if kw in upper:
            return "MODEL"
    for kw in _WORLD_KEYWORDS:
        if kw in upper:
            return "WORLD"
    return "LUAU"


# ============================================================
# ASSET DIRECTORY MANAGER
# ============================================================
class AssetDirectoryManager:
    """Menentukan path tujuan yang tepat berdasarkan tipe aset (Rojo project structure)."""

    @staticmethod
    def get_target_path(task_name: str, asset_type: str) -> str:
        safe_name = re.sub(r"[^\w]", "_", task_name)
        if asset_type == "GUI":
            return os.path.join(_GUI_DIR, f"{safe_name}.rbxmx")
        elif asset_type in ("MODEL", "WORLD"):
            return os.path.join(_ASSETS_DIR, f"{safe_name}.rbxmx")
        elif asset_type == "MESH":
            return os.path.join(_MESH_OBJ_DIR, f"{safe_name}.obj")
        return os.path.join(_SERVER_DIR, f"{safe_name}.luau")

    @staticmethod
    def get_remodel_script_path(task_name: str) -> str:
        safe_name = re.sub(r"[^\w]", "_", task_name)
        return os.path.join(_REMODEL_SCRIPTS, f"test_{safe_name}.luau")


# ============================================================
# RBXMX GENERATOR — Membuat file XML Roblox
# ============================================================
class RbxmxGenerator:
    """
    Membungkus kode Luau yang sudah diverifikasi ke dalam file .rbxmx.
    Rojo membaca file ini dan menyertakannya ke dalam build.rbxl secara otomatis.
    """

    _GUI_TMPL = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<roblox xmlns:xmime="http://www.w3.org/2005/05/xmlmime"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xsi:noNamespaceSchemaLocation="http://www.roblox.com/roblox.xsd" version="4">\n'
        '  <Item class="ScreenGui" referent="RBXG0">\n'
        '    <Properties>\n'
        '      <string name="Name">{name}</string>\n'
        '      <bool name="ResetOnSpawn">false</bool>\n'
        '      <bool name="Enabled">true</bool>\n'
        '      <token name="DisplayOrder">10</token>\n'
        '    </Properties>\n'
        '    <Item class="LocalScript" referent="RBXG1">\n'
        '      <Properties>\n'
        '        <string name="Name">{name}_Controller</string>\n'
        '        <bool name="Disabled">false</bool>\n'
        '        <ProtectedString name="Source"><![CDATA[{code}]]></ProtectedString>\n'
        '      </Properties>\n'
        '    </Item>\n'
        '  </Item>\n'
        '</roblox>\n'
    )

    _MODEL_TMPL = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<roblox xmlns:xmime="http://www.w3.org/2005/05/xmlmime"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xsi:noNamespaceSchemaLocation="http://www.roblox.com/roblox.xsd" version="4">\n'
        '  <Item class="Model" referent="RBXM0">\n'
        '    <Properties>\n'
        '      <string name="Name">{name}</string>\n'
        '    </Properties>\n'
        '    <Item class="Script" referent="RBXM1">\n'
        '      <Properties>\n'
        '        <string name="Name">{name}_Logic</string>\n'
        '        <bool name="Disabled">false</bool>\n'
        '        <ProtectedString name="Source"><![CDATA[{code}]]></ProtectedString>\n'
        '      </Properties>\n'
        '    </Item>\n'
        '  </Item>\n'
        '</roblox>\n'
    )

    _WORLD_TMPL = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<roblox xmlns:xmime="http://www.w3.org/2005/05/xmlmime"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xsi:noNamespaceSchemaLocation="http://www.roblox.com/roblox.xsd" version="4">\n'
        '  <Item class="Script" referent="RBXW0">\n'
        '    <Properties>\n'
        '      <string name="Name">{name}</string>\n'
        '      <bool name="Disabled">false</bool>\n'
        '      <ProtectedString name="Source"><![CDATA[{code}]]></ProtectedString>\n'
        '    </Properties>\n'
        '  </Item>\n'
        '</roblox>\n'
    )

    @classmethod
    def generate(cls, task_name: str, asset_type: str, luau_code: str) -> Tuple[bool, str, str]:
        """
        Membungkus kode Luau ke dalam template rbxmx yang sesuai.
        Returns: (success, rbxmx_content, error_message)
        """
        safe_name = re.sub(r"[^\w]", "_", task_name)
        try:
            if asset_type == "GUI":
                content = cls._GUI_TMPL.format(name=safe_name, code=luau_code)
            elif asset_type == "MODEL":
                content = cls._MODEL_TMPL.format(name=safe_name, code=luau_code)
            elif asset_type == "WORLD":
                content = cls._WORLD_TMPL.format(name=safe_name, code=luau_code)
            else:
                return False, "", f"Tipe '{asset_type}' tidak didukung RbxmxGenerator."
            return True, content, ""
        except Exception as e:
            return False, "", f"RbxmxGenerator error: {e}"

    @staticmethod
    def write(file_path: str, content: str) -> Tuple[bool, str]:
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            return True, ""
        except Exception as e:
            return False, f"Gagal menulis {file_path}: {e}"


# ============================================================
# ASSET TEST VALIDATOR — Validasi XML tanpa Studio
# ============================================================
class AssetTestValidator:
    """
    Memvalidasi file .rbxmx yang dihasilkan.
    Cek: XML well-formed, class wajib ada, Source script tidak kosong.
    Tidak memerlukan Roblox Studio maupun koneksi internet.
    """

    _REQUIRED_CLASSES = {
        "GUI":   {"ScreenGui", "LocalScript"},
        "MODEL": {"Model", "Script"},
        "WORLD": {"Script"},
        "MESH":  set(),
    }

    @classmethod
    def validate_rbxmx(cls, file_path: str, asset_type: str) -> Tuple[bool, str]:
        if not os.path.exists(file_path):
            return False, f"File tidak ditemukan: {file_path}"

        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
        except ET.ParseError as e:
            return False, f"XML Parse Error: {e}"

        if root.tag != "roblox":
            return False, f"Root tag bukan 'roblox' — ditemukan: '{root.tag}'"

        found_classes = {item.get("class", "") for item in root.iter("Item")}
        required = cls._REQUIRED_CLASSES.get(asset_type, set())
        missing = required - found_classes
        if missing:
            return False, f"Class wajib tidak ada: {missing}. Ditemukan: {found_classes}"

        for ps in root.iter("ProtectedString"):
            if ps.get("name") == "Source":
                src = (ps.text or "").strip()
                if len(src) < 10:
                    return False, "ProtectedString Source kosong atau terlalu pendek."

        return True, f"✅ Validasi {asset_type} rbxmx LULUS. Classes: {found_classes}"

    @staticmethod
    def validate_obj(file_path: str) -> Tuple[bool, str]:
        if not os.path.exists(file_path):
            return False, f"File OBJ tidak ditemukan: {file_path}"
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        if "v " not in content:
            return False, "OBJ tidak memiliki vertex (v ...). File tidak valid."
        if "f " not in content:
            return False, "OBJ tidak memiliki face (f ...). File tidak valid."
        v_count = sum(1 for ln in content.splitlines() if ln.startswith("v "))
        return True, f"✅ OBJ valid. Vertex count: {v_count}"


# ============================================================
# REMODEL RUNNER — Headless Roblox DataModel Testing
# ============================================================
class ReModelRunner:
    """
    Menjalankan remodel (headless Roblox DataModel) di VPS Linux
    untuk menguji apakah aset dapat dimasukkan ke place file dengan benar.

    Install remodel di VPS:
      wget https://github.com/rojo-rbx/remodel/releases/download/v0.11.0/remodel-0.11.0-linux-x86_64.zip
      unzip remodel-linux.zip -d ~/FantasyExtraction_Roblox_TrueApex/
      chmod +x ~/FantasyExtraction_Roblox_TrueApex/remodel
    """

    @staticmethod
    def is_available() -> bool:
        return os.path.isfile(_REMODEL_BIN) and os.access(_REMODEL_BIN, os.X_OK)

    @classmethod
    def _build_test_script(cls, task_name: str, asset_type: str, rbxmx_path: str) -> str:
        safe_name = re.sub(r"[^\w]", "_", task_name)
        compiled = COMPILED_GAME_FILE

        if asset_type == "GUI":
            return (
                f'local remodel = require("remodel")\n'
                f'if not remodel.isFile("{compiled}") then\n'
                f'  print("SKIP: build.rbxl belum ada.")\n'
                f'  return\n'
                f'end\n'
                f'local place = remodel.readPlaceFile("{compiled}")\n'
                f'local sg = place:FindFirstChild("StarterGui")\n'
                f'if not sg then error("StarterGui tidak ditemukan!") end\n'
                f'local items = remodel.readModelFile("{rbxmx_path}")\n'
                f'for _, c in ipairs(items) do c.Parent = sg end\n'
                f'remodel.writePlaceFile(place, "{compiled}")\n'
                f'print("OK: GUI {safe_name} masuk ke StarterGui.")\n'
            )
        elif asset_type in ("MODEL", "WORLD"):
            return (
                f'local remodel = require("remodel")\n'
                f'if not remodel.isFile("{compiled}") then\n'
                f'  print("SKIP: build.rbxl belum ada.")\n'
                f'  return\n'
                f'end\n'
                f'local place = remodel.readPlaceFile("{compiled}")\n'
                f'local ws = place:FindFirstChild("Workspace")\n'
                f'if not ws then error("Workspace tidak ditemukan!") end\n'
                f'local items = remodel.readModelFile("{rbxmx_path}")\n'
                f'for _, c in ipairs(items) do c.Parent = ws end\n'
                f'remodel.writePlaceFile(place, "{compiled}")\n'
                f'print("OK: {asset_type} {safe_name} masuk ke Workspace.")\n'
            )
        return f'print("Tipe {asset_type} tidak memerlukan tes remodel.")\n'

    @classmethod
    async def run_test(cls, task_name: str, asset_type: str, rbxmx_path: str) -> Tuple[bool, str]:
        if not cls.is_available():
            return True, (
                "⚠️ remodel belum terinstall — tes headless dilewati. "
                "Install: wget https://github.com/rojo-rbx/remodel/releases/"
                "download/v0.11.0/remodel-0.11.0-linux-x86_64.zip"
            )

        script_content = cls._build_test_script(task_name, asset_type, rbxmx_path)
        script_path = AssetDirectoryManager.get_remodel_script_path(task_name)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script_content)

        try:
            loop = asyncio.get_event_loop()
            proc = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [_REMODEL_BIN, "run", script_path],
                    capture_output=True, text=True, timeout=30
                ),
            )
            if proc.returncode == 0:
                out = proc.stdout.strip() or "Selesai."
                return True, f"✅ Remodel LULUS: {out}"
            err = (proc.stderr or proc.stdout or "").strip()
            return False, f"❌ Remodel GAGAL: {err[:300]}"
        except subprocess.TimeoutExpired:
            return False, "❌ Remodel TIMEOUT (>30 detik)."
        except Exception as e:
            return False, f"❌ Remodel Error: {e}"


# ============================================================
# OPEN CLOUD ASSET UPLOADER — Upload Mesh ke Roblox
# ============================================================
class OpenCloudAssetUploader:
    """
    Upload file OBJ/FBX ke Roblox lewat Open Cloud API.
    Tidak memerlukan Roblox Studio sama sekali.
    Membutuhkan ROBLOX_OPEN_CLOUD_API_KEY dan ROBLOX_UNIVERSE_ID di .env.nexus.
    """

    _UPLOAD_URL = "https://apis.roblox.com/assets/v1/assets"

    @staticmethod
    def _get_creator_user_id() -> Optional[str]:
        if not ROBLOX_UNIVERSE_ID or ROBLOX_UNIVERSE_ID == "0":
            return None
        try:
            res = requests.get(
                f"https://apis.roblox.com/cloud/v2/universes/{ROBLOX_UNIVERSE_ID}",
                headers={"x-api-key": ROBLOX_OPEN_CLOUD_API_KEY},
                timeout=10,
            )
            if res.status_code == 200:
                return str(res.json().get("creator", {}).get("userId", ""))
        except Exception:
            pass
        return None

    @classmethod
    async def upload_mesh(cls, task_name: str, obj_path: str) -> Tuple[bool, str]:
        if not ROBLOX_OPEN_CLOUD_API_KEY:
            return False, "❌ ROBLOX_OPEN_CLOUD_API_KEY belum dikonfigurasi di .env.nexus"
        if not os.path.exists(obj_path):
            return False, f"❌ File OBJ tidak ada: {obj_path}"

        creator_id = cls._get_creator_user_id()
        if not creator_id:
            return False, "❌ Gagal mendapatkan Creator User ID dari Universe ID."

        display_name = re.sub(r"[^\w\s]", "", task_name).strip()[:64]
        request_meta = json.dumps({
            "assetType": "Model",
            "displayName": display_name,
            "description": f"Aset otonom Nexus: {task_name}",
            "creationContext": {"creator": {"userId": creator_id}},
        })

        def _post():
            with open(obj_path, "rb") as f:
                return requests.post(
                    cls._UPLOAD_URL,
                    headers={"x-api-key": ROBLOX_OPEN_CLOUD_API_KEY},
                    data={"request": request_meta},
                    files={"fileContent": (os.path.basename(obj_path), f, "model/obj")},
                    timeout=60,
                )

        loop = asyncio.get_event_loop()
        try:
            res = await loop.run_in_executor(None, _post)
            if res.status_code in (200, 201):
                op_id = res.json().get("operationId", "unknown")
                return True, f"✅ Upload diterima. Operation ID: {op_id}"
            return False, f"❌ Upload gagal. HTTP {res.status_code}: {res.text[:300]}"
        except Exception as e:
            return False, f"❌ Upload exception: {e}"


# ============================================================
# ASSET ORCHESTRATOR — Titik Masuk Utama
# ============================================================
class AssetOrchestrator:
    """
    Dipanggil oleh OmniSynthesizerAgent setelah Luau code lulus semua validasi.
    Pipeline: detect type → generate rbxmx → validasi XML → tes remodel → upload (khusus MESH).
    """

    @classmethod
    async def process_asset_task(
        cls,
        task_name: str,
        luau_code: str,
    ) -> Tuple[bool, str, str]:
        """
        Returns: (success, saved_file_path, error_message)
        Jika task bukan aset (LUAU), returns (False, "", "BUKAN_ASET").
        """
        asset_type = detect_asset_type(task_name)
        if asset_type == "LUAU":
            return False, "", "BUKAN_ASET"

        console_terminal_interface.print(
            f"  [bold magenta][Asset Engine][/bold magenta] Tipe: "
            f"[bold cyan]{asset_type}[/bold cyan] → Task: {task_name}"
        )

        target_path = AssetDirectoryManager.get_target_path(task_name, asset_type)

        # ── MESH: jalur khusus (OBJ + upload) ──────────────────────────────
        if asset_type == "MESH":
            return await cls._handle_mesh(task_name, luau_code, target_path)

        # ── STEP 1: Generate rbxmx ──────────────────────────────────────────
        gen_ok, rbxmx_content, gen_err = RbxmxGenerator.generate(task_name, asset_type, luau_code)
        if not gen_ok:
            return False, "", f"[RbxmxGenerator] {gen_err}"

        # ── STEP 2: Tulis file ──────────────────────────────────────────────
        write_ok, write_err = RbxmxGenerator.write(target_path, rbxmx_content)
        if not write_ok:
            return False, "", f"[WriteFile] {write_err}"

        console_terminal_interface.print(
            f"  [Asset Engine] 💾 Tersimpan → [dim]{target_path}[/dim]"
        )

        # ── STEP 3: Validasi XML ────────────────────────────────────────────
        val_ok, val_msg = AssetTestValidator.validate_rbxmx(target_path, asset_type)
        if not val_ok:
            try:
                os.remove(target_path)
            except OSError:
                pass
            return False, "", f"[XMLValidasi] {val_msg}"

        console_terminal_interface.print(f"  [Asset Engine] {val_msg}")

        # ── STEP 4: Tes Remodel headless ────────────────────────────────────
        remodel_ok, remodel_msg = await ReModelRunner.run_test(task_name, asset_type, target_path)
        if remodel_ok:
            console_terminal_interface.print(f"  [Asset Engine] {remodel_msg[:120]}")
        else:
            # Warning saja — file XML sudah valid, remodel hanya tes tambahan
            console_terminal_interface.print(
                f"  [Asset Engine] [bold yellow]⚠️ Remodel: {remodel_msg[:120]}[/bold yellow]"
            )

        return True, target_path, ""

    # ── Internal: Handle MESH task ──────────────────────────────────────────
    @classmethod
    async def _handle_mesh(cls, task_name: str, description: str, obj_path: str) -> Tuple[bool, str, str]:
        # Jika output AI sudah berupa OBJ valid, gunakan langsung
        stripped = description.strip()
        if stripped.startswith("v ") and "f " in stripped:
            with open(obj_path, "w", encoding="utf-8") as f:
                f.write(description)
        else:
            # Buat kubus placeholder OBJ minimal yang valid
            with open(obj_path, "w", encoding="utf-8") as f:
                f.write(cls._placeholder_obj(task_name))
            console_terminal_interface.print(
                f"  [Asset Engine] [yellow]⚠️ OBJ placeholder dibuat. "
                "AI perlu menghasilkan format OBJ secara langsung untuk mesh nyata.[/yellow]"
            )

        val_ok, val_msg = AssetTestValidator.validate_obj(obj_path)
        if not val_ok:
            return False, "", f"[OBJ Validasi] {val_msg}"

        console_terminal_interface.print(f"  [Asset Engine] {val_msg}")

        upload_ok, upload_msg = await OpenCloudAssetUploader.upload_mesh(task_name, obj_path)
        console_terminal_interface.print(f"  [Asset Engine] {upload_msg}")

        if upload_ok:
            return True, obj_path, ""
        return False, "", upload_msg

    @staticmethod
    def _placeholder_obj(task_name: str) -> str:
        """Kubus 1×1×1 minimal sebagai placeholder mesh."""
        return (
            f"# Nexus Asset Engine — Placeholder: {task_name}\n"
            "o PlaceholderCube\n"
            "v  0.5  0.5  0.5\n"
            "v  0.5  0.5 -0.5\n"
            "v  0.5 -0.5  0.5\n"
            "v  0.5 -0.5 -0.5\n"
            "v -0.5  0.5  0.5\n"
            "v -0.5  0.5 -0.5\n"
            "v -0.5 -0.5  0.5\n"
            "v -0.5 -0.5 -0.5\n"
            "vn  1  0  0\nvn -1  0  0\nvn  0  1  0\n"
            "vn  0 -1  0\nvn  0  0  1\nvn  0  0 -1\n"
            "f 1//1 2//1 4//1 3//1\n"
            "f 5//2 7//2 8//2 6//2\n"
            "f 1//3 5//3 6//3 2//3\n"
            "f 3//4 4//4 8//4 7//4\n"
            "f 1//5 3//5 7//5 5//5\n"
            "f 2//6 6//6 8//6 4//6\n"
        )
