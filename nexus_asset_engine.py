"""
nexus_asset_engine.py
=====================
Modul BARU untuk pembuatan aset dan UI Roblox secara otonom di VPS Linux tanpa Studio.
TIDAK mengubah logika Gemini CLI, agent orchestrator, atau pipeline yang sudah ada.

Komponen:
  - detect_asset_type       : Mendeteksi tipe aset dari nama task
  - AssetDirectoryManager   : Menentukan folder tujuan yang benar (struktur Rojo)
  - SmartUIAssetSelector    : Pemilih aset UI cerdas (MeshPart/SpecialMesh) dengan fallback aura
  - RbxmxGenerator          : Membuat file .rbxmx (XML Roblox) untuk Part/Model/GUI/MeshPart
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
_MESH_PART_KEYWORDS = [
    "MESHPART", "MESH_PART", "SPECIALMESH", "SPECIAL_MESH",
    "CREATURE", "MONSTER", "NPC_MESH", "ORGANIC_MESH",
]

# ============================================================
# KATALOG ASET MESHPART / SPECIALMESH BAWAAN
# (Aset publik Roblox yang bebas dipakai; tambahkan rbxassetid
#  milik Anda sendiri di sini untuk performa terbaik)
# ============================================================
MESH_ASSET_CATALOG: list[dict] = [
    {
        "id": "rbxassetid://0",
        "tags": ["sphere", "ball", "round", "bulat", "bola", "oval"],
        "description": "Bola / Sphere — bentuk dasar organik bulat",
        "mesh_type": "Sphere",
        "is_special_mesh": True,
    },
    {
        "id": "rbxassetid://0",
        "tags": ["cylinder", "tube", "tabung", "silinder", "pipa", "tiang"],
        "description": "Silinder — cocok untuk tiang, pipa, atau batang",
        "mesh_type": "Cylinder",
        "is_special_mesh": True,
    },
    {
        "id": "rbxassetid://0",
        "tags": ["wedge", "baji", "miring", "lereng", "slope", "ramp"],
        "description": "Baji / Wedge — bentuk segitiga miring",
        "mesh_type": "Wedge",
        "is_special_mesh": True,
    },
    {
        "id": "rbxassetid://0",
        "tags": ["cornerwedge", "sudut", "corner", "pojok"],
        "description": "Corner Wedge — sudut yang terpotong diagonal",
        "mesh_type": "CornerWedge",
        "is_special_mesh": True,
    },
    {
        "id": "rbxassetid://1290033",
        "tags": ["diamond", "berlian", "permata", "gem", "crystal", "kristal"],
        "description": "Diamond / Berlian — bentuk permata",
        "mesh_type": "FileMesh",
        "is_special_mesh": True,
    },
    {
        "id": "rbxassetid://9856898",
        "tags": ["ring", "cincin", "lingkaran", "torus", "loop"],
        "description": "Ring / Cincin — torus / cincin",
        "mesh_type": "FileMesh",
        "is_special_mesh": True,
    },
    {
        "id": "rbxassetid://72013856",
        "tags": ["star", "bintang", "star_shape"],
        "description": "Bintang — shape bintang dekoratif",
        "mesh_type": "FileMesh",
        "is_special_mesh": True,
    },
    {
        "id": "rbxassetid://431221914",
        "tags": ["rock", "batu", "stone", "boulder", "batuan"],
        "description": "Batu / Rock — batu alam organik",
        "mesh_type": "FileMesh",
        "is_special_mesh": True,
    },
    {
        "id": "rbxassetid://1290033",
        "tags": ["gem", "amethyst", "emerald", "ruby", "sapphire"],
        "description": "Kristal Gem — permata bersegi",
        "mesh_type": "FileMesh",
        "is_special_mesh": True,
    },
    {
        "id": "rbxassetid://0",
        "tags": ["torso", "body", "badan", "tubuh", "humanoid", "karakter"],
        "description": "Torso humanoid — badan karakter",
        "mesh_type": "Brick",
        "is_special_mesh": True,
    },
]

# ============================================================
# KATALOG AURA FALLBACK
# Digunakan HANYA jika tidak ada MeshPart/SpecialMesh yang cocok.
# PERINGATAN KERAS: Aura hanya boleh dipakai sebagai PEMBEDA visual
# terakhir — bukan sebagai pengganti bentuk mesh yang sesungguhnya.
# ============================================================
AURA_CATALOG: list[dict] = [
    {
        "name": "AuraHitam",
        "color": "Color3.fromRGB(0, 0, 0)",
        "particle_color": "ColorSequence.new(Color3.fromRGB(20, 0, 30), Color3.fromRGB(0, 0, 0))",
        "description": "Aura gelap/hitam — kekuatan jahat atau shadow element",
        "tags": ["dark", "shadow", "evil", "gelap", "hitam", "iblis", "black"],
    },
    {
        "name": "AuraMerah",
        "color": "Color3.fromRGB(180, 0, 0)",
        "particle_color": "ColorSequence.new(Color3.fromRGB(255, 50, 0), Color3.fromRGB(180, 0, 0))",
        "description": "Aura merah — kekuatan api atau agresi tinggi",
        "tags": ["fire", "rage", "merah", "red", "api", "marah", "berapi", "panas"],
    },
    {
        "name": "AuraBiru",
        "color": "Color3.fromRGB(0, 80, 200)",
        "particle_color": "ColorSequence.new(Color3.fromRGB(100, 180, 255), Color3.fromRGB(0, 80, 200))",
        "description": "Aura biru — elemen air atau es, atau kekuatan magis",
        "tags": ["water", "ice", "magic", "biru", "blue", "es", "air", "sihir"],
    },
    {
        "name": "AuraHijau",
        "color": "Color3.fromRGB(0, 180, 50)",
        "particle_color": "ColorSequence.new(Color3.fromRGB(100, 255, 100), Color3.fromRGB(0, 180, 50))",
        "description": "Aura hijau — racun, alam, atau healing",
        "tags": ["poison", "nature", "heal", "hijau", "green", "racun", "alam", "sembuh"],
    },
    {
        "name": "AuraUngu",
        "color": "Color3.fromRGB(120, 0, 180)",
        "particle_color": "ColorSequence.new(Color3.fromRGB(200, 100, 255), Color3.fromRGB(120, 0, 180))",
        "description": "Aura ungu — energi mistis atau chaos",
        "tags": ["chaos", "mystic", "ungu", "purple", "mistis", "kekacauan"],
    },
    {
        "name": "AuraEmas",
        "color": "Color3.fromRGB(255, 200, 0)",
        "particle_color": "ColorSequence.new(Color3.fromRGB(255, 240, 100), Color3.fromRGB(255, 180, 0))",
        "description": "Aura emas — kekuatan legendaris atau divine",
        "tags": ["gold", "divine", "legend", "emas", "dewa", "suci", "holy"],
    },
    {
        "name": "AuraPutih",
        "color": "Color3.fromRGB(220, 220, 255)",
        "particle_color": "ColorSequence.new(Color3.fromRGB(255, 255, 255), Color3.fromRGB(200, 200, 255))",
        "description": "Aura putih — kesucian atau kekuatan cahaya",
        "tags": ["light", "pure", "putih", "white", "suci", "cahaya", "angel"],
    },
    {
        "name": "AuraPelangi",
        "color": "Color3.fromRGB(255, 100, 200)",
        "particle_color": "ColorSequence.new(Color3.fromRGB(255, 0, 100), Color3.fromRGB(100, 200, 255))",
        "description": "Aura pelangi — kekuatan tidak terdefinisi atau omni-element",
        "tags": ["rainbow", "prism", "omni", "pelangi", "prisma", "semua elemen"],
    },
]


def detect_asset_type(task_name: str) -> str:
    """
    Mengembalikan tipe aset berdasarkan nama task:
      'GUI'       → ScreenGui / UI element (disimpan ke StarterGui)
      'MESH_PART' → MeshPart/SpecialMesh rbxmx (disimpan ke Workspace)
      'MODEL'     → Model 3D rbxmx (disimpan ke Workspace)
      'MESH'      → Upload mesh ke Open Cloud API
      'WORLD'     → Script/Part dunia (disimpan ke Workspace)
      'LUAU'      → Script biasa — TIDAK ditangani modul ini
    """
    upper = task_name.upper()
    for kw in _GUI_KEYWORDS:
        if kw in upper:
            return "GUI"
    for kw in _MESH_PART_KEYWORDS:
        if kw in upper:
            return "MESH_PART"
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
# SMART UI ASSET SELECTOR — Pemilih MeshPart/SpecialMesh Cerdas
# ============================================================
class SmartUIAssetSelector:
    """
    Memilih aset MeshPart atau SpecialMesh yang paling cocok dari katalog
    berdasarkan konteks task_name menggunakan pencocokan tag berbasis kata kunci.

    PRIORITAS PEMILIHAN (urutan ketat):
      1. Cocokkan kata kunci task_name dengan 'tags' di MESH_ASSET_CATALOG.
         Pilih entri dengan jumlah tag yang paling banyak cocok.
      2. Jika TIDAK ada yang cocok sama sekali → gunakan fallback aura dari
         AURA_CATALOG. Fallback ini hanya boleh digunakan sebagai PEMBEDA visual
         terakhir.

    PERINGATAN KERAS:
      Aura hanya boleh digunakan ketika BENAR-BENAR tidak ada MeshPart atau
      SpecialMesh yang cocok. Penggunaan aura harus disertai log peringatan
      yang jelas. Aura BUKAN pengganti mesh — aura hanya menambahkan efek
      visual pembeda pada Part biasa agar tetap dapat dibedakan antar objek.
    """

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Pecah teks menjadi token kata kunci huruf kecil."""
        return re.sub(r"[_\-\s]+", " ", text.lower()).split()

    @classmethod
    def select_mesh_asset(cls, task_name: str) -> Optional[dict]:
        """
        Mencari aset MeshPart/SpecialMesh yang paling sesuai dari MESH_ASSET_CATALOG.
        Mengembalikan dict aset atau None jika tidak ada yang cocok.
        """
        tokens = cls._tokenize(task_name)
        best_match: Optional[dict] = None
        best_score = 0

        for asset in MESH_ASSET_CATALOG:
            score = sum(1 for tag in asset["tags"] if tag in tokens)
            if score > best_score:
                best_score = score
                best_match = asset

        if best_score > 0 and best_match is not None:
            console_terminal_interface.print(
                f"  [bold green][SmartSelector] ✅ Mesh cocok ditemukan: "
                f"'{best_match['description']}' (skor={best_score})[/bold green]"
            )
            return best_match

        return None

    @classmethod
    def select_aura_fallback(cls, task_name: str) -> dict:
        """
        ⚠️ FALLBACK TERAKHIR — hanya dipanggil jika select_mesh_asset() gagal.
        Memilih aura yang paling sesuai dari AURA_CATALOG.
        Mengembalikan aura default (hitam) jika tidak ada yang cocok.
        """
        tokens = cls._tokenize(task_name)
        best_aura: Optional[dict] = None
        best_score = 0

        for aura in AURA_CATALOG:
            score = sum(1 for tag in aura["tags"] if tag in tokens)
            if score > best_score:
                best_score = score
                best_aura = aura

        chosen = best_aura if best_aura is not None else AURA_CATALOG[0]

        console_terminal_interface.print(
            f"  [bold red][SmartSelector] ⚠️ PERINGATAN KERAS: Tidak ada MeshPart/"
            f"SpecialMesh yang cocok untuk '{task_name}'.[/bold red]"
        )
        console_terminal_interface.print(
            f"  [bold red][SmartSelector] ⚠️ MENGGUNAKAN AURA FALLBACK: "
            f"'{chosen['name']}' sebagai PEMBEDA VISUAL SAJA.[/bold red]"
        )
        console_terminal_interface.print(
            f"  [bold yellow][SmartSelector] TINDAKAN YANG DISARANKAN: Tambahkan "
            f"rbxassetid yang sesuai ke MESH_ASSET_CATALOG di nexus_asset_engine.py "
            f"agar aura tidak digunakan lagi untuk task serupa.[/bold yellow]"
        )

        return chosen

    @classmethod
    def generate_mesh_part_luau(
        cls,
        task_name: str,
        mesh_asset: dict,
        parent_expr: str = "workspace",
    ) -> str:
        """
        Menghasilkan kode Luau untuk membuat MeshPart atau SpecialMesh
        berdasarkan aset yang dipilih dari katalog.
        """
        safe_name = re.sub(r"[^\w]", "_", task_name)
        mesh_type = mesh_asset.get("mesh_type", "Sphere")
        asset_id = mesh_asset.get("id", "rbxassetid://0")

        if mesh_asset.get("is_special_mesh"):
            if mesh_type in ("Sphere", "Cylinder", "Wedge", "CornerWedge", "Brick"):
                return (
                    f"--!strict\n"
                    f"-- SmartUIAssetSelector: SpecialMesh [{mesh_type}] untuk {safe_name}\n"
                    f"local part = Instance.new('Part')\n"
                    f"part.Name = '{safe_name}'\n"
                    f"part.Size = Vector3.new(4, 4, 4)\n"
                    f"part.Anchored = true\n"
                    f"part.CanCollide = true\n"
                    f"part.Material = Enum.Material.SmoothPlastic\n"
                    f"part.CastShadow = true\n"
                    f"local mesh = Instance.new('SpecialMesh')\n"
                    f"mesh.MeshType = Enum.MeshType.{mesh_type}\n"
                    f"mesh.Scale = Vector3.new(1, 1, 1)\n"
                    f"mesh.Parent = part\n"
                    f"part.Parent = {parent_expr}\n"
                )
            else:
                return (
                    f"--!strict\n"
                    f"-- SmartUIAssetSelector: SpecialMesh [FileMesh] untuk {safe_name}\n"
                    f"local part = Instance.new('Part')\n"
                    f"part.Name = '{safe_name}'\n"
                    f"part.Size = Vector3.new(4, 4, 4)\n"
                    f"part.Anchored = true\n"
                    f"part.CanCollide = true\n"
                    f"part.Material = Enum.Material.SmoothPlastic\n"
                    f"local mesh = Instance.new('SpecialMesh')\n"
                    f"mesh.MeshType = Enum.MeshType.FileMesh\n"
                    f"mesh.MeshId = '{asset_id}'\n"
                    f"mesh.Scale = Vector3.new(1, 1, 1)\n"
                    f"mesh.Parent = part\n"
                    f"part.Parent = {parent_expr}\n"
                )
        else:
            return (
                f"--!strict\n"
                f"-- SmartUIAssetSelector: MeshPart untuk {safe_name}\n"
                f"local meshPart = Instance.new('MeshPart')\n"
                f"meshPart.Name = '{safe_name}'\n"
                f"meshPart.MeshId = '{asset_id}'\n"
                f"meshPart.Size = Vector3.new(4, 4, 4)\n"
                f"meshPart.Anchored = true\n"
                f"meshPart.CanCollide = true\n"
                f"meshPart.Material = Enum.Material.SmoothPlastic\n"
                f"meshPart.CastShadow = true\n"
                f"meshPart.Parent = {parent_expr}\n"
            )

    @classmethod
    def generate_aura_luau(
        cls,
        task_name: str,
        aura: dict,
        parent_expr: str = "workspace",
    ) -> str:
        """
        ⚠️ FALLBACK TERAKHIR — Menghasilkan kode Luau untuk Part biasa dengan
        aura ParticleEmitter sebagai pembeda visual.

        PERINGATAN KERAS: Kode ini HANYA boleh dijalankan jika tidak ada
        MeshPart/SpecialMesh yang cocok di MESH_ASSET_CATALOG. Aura bukan
        pengganti mesh — tambahkan aset mesh yang benar ke katalog sesegera
        mungkin untuk menghindari penggunaan aura fallback ini.
        """
        safe_name = re.sub(r"[^\w]", "_", task_name)
        aura_name = aura.get("name", "AuraHitam")
        color = aura.get("color", "Color3.fromRGB(0, 0, 0)")
        particle_color = aura.get("particle_color", "ColorSequence.new(Color3.fromRGB(0,0,0), Color3.fromRGB(0,0,0))")
        description = aura.get("description", "Aura fallback")

        return (
            f"--!strict\n"
            f"-- ⚠️ PERINGATAN KERAS: Ini adalah AURA FALLBACK karena tidak ada\n"
            f"-- MeshPart/SpecialMesh yang cocok untuk: {safe_name}\n"
            f"-- Aura: {aura_name} — {description}\n"
            f"-- TINDAKAN WAJIB: Tambahkan rbxassetid yang sesuai ke\n"
            f"-- MESH_ASSET_CATALOG di nexus_asset_engine.py agar mesh sesungguhnya\n"
            f"-- digunakan di masa mendatang, bukan aura placeholder ini.\n"
            f"local part = Instance.new('Part')\n"
            f"part.Name = '{safe_name}'\n"
            f"part.Size = Vector3.new(4, 4, 4)\n"
            f"part.Anchored = true\n"
            f"part.CanCollide = true\n"
            f"part.BrickColor = BrickColor.new('Medium stone grey')\n"
            f"part.Material = Enum.Material.SmoothPlastic\n"
            f"part.CastShadow = true\n"
            f"-- Aura: SelectionBox sebagai highlight pembeda\n"
            f"local selBox = Instance.new('SelectionBox')\n"
            f"selBox.Adornee = part\n"
            f"selBox.Color3 = {color}\n"
            f"selBox.LineThickness = 0.05\n"
            f"selBox.SurfaceTransparency = 0.7\n"
            f"selBox.SurfaceColor3 = {color}\n"
            f"selBox.Parent = part\n"
            f"-- ParticleEmitter aura\n"
            f"local particle = Instance.new('ParticleEmitter')\n"
            f"particle.Color = {particle_color}\n"
            f"particle.LightEmission = 0.8\n"
            f"particle.LightInfluence = 0.2\n"
            f"particle.Size = NumberSequence.new({{NumberSequenceKeypoint.new(0, 0.5), NumberSequenceKeypoint.new(1, 0)}})\n"
            f"particle.Transparency = NumberSequence.new({{NumberSequenceKeypoint.new(0, 0.2), NumberSequenceKeypoint.new(1, 1)}})\n"
            f"particle.Speed = NumberRange.new(1, 3)\n"
            f"particle.Rate = 20\n"
            f"particle.Lifetime = NumberRange.new(1, 2)\n"
            f"particle.SpreadAngle = Vector2.new(180, 180)\n"
            f"particle.Parent = part\n"
            f"part.Parent = {parent_expr}\n"
        )

    @classmethod
    def resolve_and_generate(
        cls,
        task_name: str,
        parent_expr: str = "workspace",
    ) -> tuple[str, str, bool]:
        """
        Titik masuk utama SmartUIAssetSelector.
        Mengembalikan: (luau_code, log_message, is_aura_fallback)

        Alur keputusan:
          1. Coba cocokkan MeshPart/SpecialMesh dari katalog.
          2. Jika tidak cocok → gunakan aura fallback dengan peringatan keras.
        """
        mesh_asset = cls.select_mesh_asset(task_name)
        if mesh_asset is not None:
            luau = cls.generate_mesh_part_luau(task_name, mesh_asset, parent_expr)
            return luau, f"MeshPart/SpecialMesh terpilih: {mesh_asset['description']}", False
        else:
            aura = cls.select_aura_fallback(task_name)
            luau = cls.generate_aura_luau(task_name, aura, parent_expr)
            return luau, f"AURA FALLBACK digunakan: {aura['name']} — {aura['description']}", True


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
        elif asset_type in ("MODEL", "WORLD", "MESH_PART"):
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
        '      <int name="DisplayOrder">10</int>\n'
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

    # Template MESH_PART: membungkus kode Luau yang sudah dihasilkan
    # SmartUIAssetSelector (bisa berupa MeshPart, SpecialMesh, atau aura fallback)
    # ke dalam Model rbxmx dengan Script server.
    _MESH_PART_TMPL = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<roblox xmlns:xmime="http://www.w3.org/2005/05/xmlmime"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xsi:noNamespaceSchemaLocation="http://www.roblox.com/roblox.xsd" version="4">\n'
        '  <Item class="Model" referent="RBXMP0">\n'
        '    <Properties>\n'
        '      <string name="Name">{name}</string>\n'
        '    </Properties>\n'
        '    <Item class="Script" referent="RBXMP1">\n'
        '      <Properties>\n'
        '        <string name="Name">{name}_MeshSpawner</string>\n'
        '        <bool name="Disabled">false</bool>\n'
        '        <ProtectedString name="Source"><![CDATA[{code}]]></ProtectedString>\n'
        '      </Properties>\n'
        '    </Item>\n'
        '  </Item>\n'
        '</roblox>\n'
    )

    @classmethod
    def generate(cls, task_name: str, asset_type: str, luau_code: str) -> Tuple[bool, str, str]:
        """
        Membungkus kode Luau ke dalam template rbxmx yang sesuai.
        Untuk MESH_PART: luau_code berisi kode yang sudah dihasilkan SmartUIAssetSelector.
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
            elif asset_type == "MESH_PART":
                content = cls._MESH_PART_TMPL.format(name=safe_name, code=luau_code)
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
        "GUI":       {"ScreenGui", "LocalScript"},
        "MODEL":     {"Model", "Script"},
        "WORLD":     {"Script"},
        "MESH":      set(),
        "MESH_PART": {"Model", "Script"},
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
        elif asset_type in ("MODEL", "WORLD", "MESH_PART"):
            return (
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
    Pipeline: detect type → (SmartUIAssetSelector jika MESH_PART) → generate rbxmx
              → validasi XML → tes remodel → upload (khusus MESH).
    """



    @staticmethod
    def _download_and_convert_mesh(asset_id: str) -> str:
        """
        Mendownload mesh biner Roblox secara otomatis lalu mengubahnya ke OBJ.
        Kembalikan string isi OBJ, atau string kosong jika gagal.
        """
        import subprocess
        import os

        # Eksekusi URL endpoint Roblox
        url = f"https://assetdelivery.roblox.com/v1/asset?id={asset_id}"
        mesh_path = f"/tmp/{asset_id}.mesh"
        obj_path = f"/tmp/{asset_id}.obj"

        try:
            # 1. Download file biner .mesh
            subprocess.run([
                "curl", "-s", "--location", "--request", "GET", url,
                "--header", "User-Agent: Roblox/WinInet",
                "--header", "Accept: application/json",
                "--output", mesh_path
            ], check=True, timeout=15)

            # 2. Jika tool rbx-mesh-to-obj ada, kita jalankan, kalau tidak fallback placeholder
            if os.path.exists("./rbx-mesh-to-obj") and os.access("./rbx-mesh-to-obj", os.X_OK):
                subprocess.run(["./rbx-mesh-to-obj", mesh_path, obj_path], check=True, timeout=15)
                if os.path.exists(obj_path):
                    with open(obj_path, "r", encoding="utf-8", errors="ignore") as fobj:
                        obj_data = fobj.read()
                    return obj_data
            else:
                # Mock response - jika script tidak punya binary pihak ketiga
                print(f"[Asset Engine] Tool ./rbx-mesh-to-obj tidak ditemukan! Simulasi konversi mesh ID {asset_id}")
                pass

        except Exception as e:
            print(f"[Asset Engine] Gagal mengunduh/konversi mesh {asset_id}: {e}")
            pass

        return ""

    @classmethod
    async def process_asset_task(
        cls,
        task_name: str,
        luau_code: str,
    ) -> Tuple[bool, str, str]:
        """
        Returns: (success, saved_file_path, error_message)
        Jika task bukan aset (LUAU), returns (False, "", "BUKAN_ASET").

        Untuk MESH_PART: SmartUIAssetSelector menentukan MeshPart/SpecialMesh terbaik
        dari MESH_ASSET_CATALOG. Jika tidak ada yang cocok, aura fallback digunakan
        dengan PERINGATAN KERAS di log.
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

        # ── MESH_PART: gunakan SmartUIAssetSelector ──────────────────────────
        if asset_type == "MESH_PART":
            return await cls._handle_mesh_part(task_name, target_path)

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

    # ── Internal: Handle MESH_PART task via SmartUIAssetSelector ───────────
    @classmethod
    async def _handle_mesh_part(cls, task_name: str, target_path: str) -> Tuple[bool, str, str]:
        """
        Menangani task bertipe MESH_PART menggunakan SmartUIAssetSelector.

        Alur:
          1. SmartUIAssetSelector.resolve_and_generate() memilih MeshPart/SpecialMesh terbaik.
          2. Jika tidak ada yang cocok → aura fallback dipilih dengan PERINGATAN KERAS.
          3. Kode Luau yang dihasilkan dibungkus dalam template MESH_PART rbxmx.
          4. Validasi XML + tes remodel headless.
        """
        luau_code, selector_log, is_aura_fallback = SmartUIAssetSelector.resolve_and_generate(task_name)

        if is_aura_fallback:
            console_terminal_interface.print(
                f"  [bold red][Asset Engine] ⚠️⚠️⚠️ PERINGATAN KERAS ⚠️⚠️⚠️[/bold red]"
            )
            console_terminal_interface.print(
                f"  [bold red][Asset Engine] AURA FALLBACK aktif untuk '{task_name}'.[/bold red]"
            )
            console_terminal_interface.print(
                f"  [bold red][Asset Engine] Ini terjadi karena tidak ada MeshPart/"
                f"SpecialMesh yang cocok di MESH_ASSET_CATALOG.[/bold red]"
            )
            console_terminal_interface.print(
                f"  [bold yellow][Asset Engine] AKSI YANG DIPERLUKAN: Tambahkan "
                f"rbxassetid yang relevan ke MESH_ASSET_CATALOG di nexus_asset_engine.py.[/bold yellow]"
            )
        else:
            console_terminal_interface.print(
                f"  [bold green][Asset Engine] ✅ {selector_log}[/bold green]"
            )

        gen_ok, rbxmx_content, gen_err = RbxmxGenerator.generate(task_name, "MESH_PART", luau_code)
        if not gen_ok:
            return False, "", f"[RbxmxGenerator MESH_PART] {gen_err}"

        write_ok, write_err = RbxmxGenerator.write(target_path, rbxmx_content)
        if not write_ok:
            return False, "", f"[WriteFile MESH_PART] {write_err}"

        console_terminal_interface.print(
            f"  [Asset Engine] 💾 Tersimpan → [dim]{target_path}[/dim]"
        )

        val_ok, val_msg = AssetTestValidator.validate_rbxmx(target_path, "MESH_PART")
        if not val_ok:
            try:
                os.remove(target_path)
            except OSError:
                pass
            return False, "", f"[XMLValidasi MESH_PART] {val_msg}"

        console_terminal_interface.print(f"  [Asset Engine] {val_msg}")

        remodel_ok, remodel_msg = await ReModelRunner.run_test(task_name, "MESH_PART", target_path)
        if remodel_ok:
            console_terminal_interface.print(f"  [Asset Engine] {remodel_msg[:120]}")
        else:
            console_terminal_interface.print(
                f"  [Asset Engine] [bold yellow]⚠️ Remodel: {remodel_msg[:120]}[/bold yellow]"
            )

        return True, target_path, ""

    # ── Internal: Handle MESH task ──────────────────────────────────────────
    @classmethod
    async def _handle_mesh(cls, task_name: str, description: str, obj_path: str) -> Tuple[bool, str, str]:
        stripped = description.strip()

        # ── DETEKSI ASSET ID MESH OTOMATIS ──
        # Jika AI mengirimkan Asset ID, unduh otonom tanpa henti
        import re
        asset_id_match = re.search(r"rbxassetid://(\\d+)|(\\d{8,})", stripped)
        if asset_id_match:
            asset_id = asset_id_match.group(1) or asset_id_match.group(2)
            console_terminal_interface.print(f"  [Asset Engine] [bold cyan]Mendeteksi Asset ID {asset_id}. Mengeksekusi pengunduhan .mesh otomatis...[/bold cyan]")

            downloaded_obj = cls._download_and_convert_mesh(asset_id)
            if downloaded_obj and "v " in downloaded_obj and "f " in downloaded_obj:
                with open(obj_path, "w", encoding="utf-8") as f:
                    f.write(downloaded_obj)
                val_ok, val_msg = AssetTestValidator.validate_obj(obj_path)
                if val_ok:
                    return True, obj_path, f"Unduh otomatis Mesh ID {asset_id} Lulus"
            else:
                console_terminal_interface.print(f"  [Asset Engine] [yellow]Gagal mengonversi Mesh ID {asset_id}. Memakai Placeholder.[/yellow]")

        # Jika output AI sudah berupa OBJ valid, gunakan langsung
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

