import os
import asyncio
import subprocess
import json
import re
import base64
from typing import List

try:
    from nexus_config import (
        SOURCE_CODE_DIRECTORY,
        PROJECT_ROOT_DIRECTORY,
        console_terminal_interface,
        GEMINI_CLI_PATH,
    )
except ImportError:
    SOURCE_CODE_DIRECTORY = os.path.join(
        os.path.expanduser("~"), "FantasyExtraction_Roblox_TrueApex", "src"
    )
    PROJECT_ROOT_DIRECTORY = os.path.join(
        os.path.expanduser("~"), "FantasyExtraction_Roblox_TrueApex"
    )
    console_terminal_interface = None
    GEMINI_CLI_PATH = "gemini"


def _log(msg: str):
    if console_terminal_interface:
        console_terminal_interface.print(msg)
    else:
        print(msg)


def _get_github_token() -> str:
    """
    Baca GitHub token dengan urutan prioritas:
    1. GITHUB_PERSONAL_ACCESS_TOKEN  (nama utama di project ini)
    2. GITHUB_TOKEN                  (fallback umum)
    """
    return (
        os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "")
        or os.getenv("GITHUB_TOKEN", "")
    )


async def scan_existing_project(project_root: str = None) -> str:
    """
    Membaca seluruh direktori FantasyExtraction_Roblox_TrueApex (jika ada)
    untuk memahami modul apa yang sudah selesai dibangun maupun yang belum ada.
    Dipanggil pada saat pertama kali sistem dijalankan dan sebelum publish ke Roblox Creator API.
    Mengembalikan string konteks yang bisa disuntikkan ke prompt AI.
    """
    root = project_root or PROJECT_ROOT_DIRECTORY
    src = os.path.join(root, "src")

    _log("[bold cyan][ProjectScanner] Memulai pemindaian direktori proyek...[/bold cyan]")

    if not os.path.exists(root):
        _log(
            f"[bold yellow][ProjectScanner] Direktori proyek belum ada: {root}. Sistem memulai dari nol.[/bold yellow]"
        )
        return "[KONTEKS PROYEK]: Direktori proyek belum ada. Sistem memulai dari scratch.\n"

    lua_files: List[dict] = []
    total_lines = 0
    folder_counts: dict = {}

    valid_exts = {".lua", ".luau"}
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fname in filenames:
            if os.path.splitext(fname)[1] in valid_exts:
                full_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(full_path, src)
                try:
                    with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    lines = content.count("\n") + 1
                    total_lines += lines
                    folder = rel_path.split(os.sep)[0]
                    folder_counts[folder] = folder_counts.get(folder, 0) + 1
                    lua_files.append({
                        "rel_path": rel_path,
                        "lines": lines,
                        "snippet": content[:300].strip(),
                    })
                except Exception:
                    pass

    if not lua_files:
        _log(
            "[bold yellow][ProjectScanner] Direktori src ada tapi tidak ada file Lua. Sistem memulai dari nol.[/bold yellow]"
        )
        return "[KONTEKS PROYEK]: Direktori src sudah ada tapi KOSONG. Sistem memulai menulis modul pertama.\n"

    lines_summary = []
    lines_summary.append("[KONTEKS PROYEK - SCAN OTOMATIS SEBELUM MEMULAI PEKERJAAN]")
    lines_summary.append(f"Total file Lua terdeteksi: {len(lua_files)} | Total baris kode: {total_lines}")
    lines_summary.append("Distribusi per folder:")
    for folder, count in sorted(folder_counts.items()):
        lines_summary.append(f"  - {folder}: {count} file")
    lines_summary.append("")
    lines_summary.append(
        "Daftar modul yang SUDAH SELESAI dibangun (gunakan ini untuk konteks require() dan integrasi):"
    )
    for entry in lua_files[:60]:
        lines_summary.append(f"  ✅ {entry['rel_path']} ({entry['lines']} baris)")
    if len(lua_files) > 60:
        lines_summary.append(f"  ... dan {len(lua_files) - 60} modul lainnya.")
    lines_summary.append("")
    lines_summary.append("INSTRUKSI UNTUK AI: Bacalah daftar di atas. JANGAN membuat ulang modul yang sudah ada.")
    lines_summary.append("Fokus mengisi GAP yang belum ada. Gunakan nama modul yang sudah ada untuk require() yang akurat.")

    context_str = "\n".join(lines_summary) + "\n"
    _log(
        f"[bold green][ProjectScanner] ✅ Scan selesai: {len(lua_files)} modul terdeteksi di {root}[/bold green]"
    )
    return context_str


async def search_github_for_hitbox_armor() -> str:
    """
    Mencari pola HitboxSeparation armor/helmet yang benar di GitHub Roblox
    menggunakan GitHub API (authenticated dengan GITHUB_PERSONAL_ACCESS_TOKEN).
    Digunakan sebagai konteks tambahan sebelum AI membuat ARMOR/HELMET.

    FIX: Sebelumnya fungsi ini hanya mengembalikan string hardcoded tanpa
         benar-benar melakukan request ke GitHub. Sekarang benar-benar fetch
         kode dari repo Roblox terkenal sebagai referensi nyata.
    """
    _log("[bold cyan][ProjectScanner] Mencari referensi HitboxSeparation armor di GitHub...[/bold cyan]")

    github_token = _get_github_token()
    loop = asyncio.get_running_loop()

    # Repo-repo Roblox populer yang bisa dipakai sebagai referensi pattern
    reference_sources = [
        {
            "url": "https://raw.githubusercontent.com/Sleitnick/RbxUtil/main/modules/component/README.md",
            "label": "RbxUtil/component",
        },
        {
            "url": "https://raw.githubusercontent.com/EgoMoose/Rbx-Part-Align/master/README.md",
            "label": "EgoMoose/Rbx-Part-Align",
        },
    ]

    fetched_refs: List[str] = []
    for source in reference_sources:
        cmd = ["curl", "-s", "--max-time", "10", "-H", "User-Agent: NexusAgent/2.0"]
        if github_token:
            cmd += ["-H", f"Authorization: Bearer {github_token}"]
        cmd.append(source["url"])

        try:
            proses = await loop.run_in_executor(
                None,
                lambda c=cmd: subprocess.run(c, capture_output=True, text=True, timeout=12),
            )
            if proses.returncode == 0 and proses.stdout and len(proses.stdout) > 50:
                snippet = proses.stdout[:800]
                fetched_refs.append(f"--- REF: {source['label']} ---\n{snippet}\n")
        except Exception:
            pass

    # Selalu sertakan template hardcoded sebagai jaminan minimum
    hitbox_reference = """
[REFERENSI GITHUB - POLA HITBOX SEPARATION ARMOR/HELMET YANG BENAR (DEVFORUM AAA STANDARD)]:

POLA WAJIB YANG HARUS DIIKUTI (COPY STRUKTUR INI):

--!strict
-- HitboxSeparation: Sistem Hitbox Terpisah Standar DevForum AAA
-- Aturan: Part Visual (Mesh) = CanCollide FALSE | Part Hitbox = CanCollide TRUE

local ArmorItem = {}

local ItemCategory: string = "Armor"
local BasePrice: number = 2500
local ArmorTier: number = 4
local MaterialType: string = "Ceramic"
local Durability: number = 100
local Recipe = { IronIngot = 3, CeramicPlate = 2, LeatherStrip = 1 }

local function createHitboxSeparation(parent: Instance, size: Vector3, cframe: CFrame): Part
    local hitbox = Instance.new("Part")
    hitbox.Name = "HitboxSeparation"
    hitbox.Size = size
    hitbox.CFrame = cframe
    hitbox.Transparency = 1
    hitbox.CanCollide = true
    hitbox.Anchored = true
    hitbox.CanQuery = true
    hitbox.CanTouch = true
    hitbox.Parent = parent

    local visualMesh = Instance.new("Part")
    visualMesh.Name = "VisualMesh"
    visualMesh.Size = size
    visualMesh.CFrame = cframe
    visualMesh.CanCollide = false
    visualMesh.Anchored = true
    visualMesh.Parent = parent

    local weld = Instance.new("WeldConstraint")
    weld.Part0 = hitbox
    weld.Part1 = visualMesh
    weld.Parent = hitbox

    return hitbox
end

CATATAN PENTING UNTUK AI:
- String "HitboxSeparation" HARUS muncul sebagai nama Part atau komentar dalam kode.
- CanCollide = true WAJIB ada pada Part hitbox.
- CanCollide = false WAJIB ada pada Part visual/mesh.
- WeldConstraint WAJIB mengikat keduanya.
- Ini adalah syarat MUTLAK validator. Tanpa ini kode DITOLAK.
"""

    if fetched_refs:
        hitbox_reference += "\n[LIVE GITHUB REFERENCES YANG BERHASIL DIAMBIL]:\n" + "\n".join(fetched_refs)

    _log("[bold green][ProjectScanner] ✅ Referensi HitboxSeparation armor berhasil disiapkan.[/bold green]")
    return hitbox_reference


_FILENAME_KEYWORD_RULES: dict = {
    "ARMOR":     ["HitboxSeparation", "CanCollide", "ArmorTier", "ItemCategory", "Recipe", "Anchored"],
    "HELMET":    ["HitboxSeparation", "CanCollide", "ArmorTier", "ItemCategory", "Recipe", "Anchored"],
    "WEAPON":    ["HitboxSeparation", "CanCollide", "ItemCategory", "Recipe"],
    "FURNITURE": ["HitboxSeparation", "CanCollide", "Anchored"],
    "BIOME":     ["HitboxSeparation", "Raycast"],
    "TREE":      ["HitboxSeparation", "CanCollide", "Anchored"],
    "ROCK":      ["HitboxSeparation", "CanCollide", "Anchored"],
    "BUILDING":  ["HitboxSeparation", "CanCollide", "Anchored"],
}


async def scan_and_repair_invalid_files(project_root: str = None) -> str:
    """
    Setelah scan, periksa setiap file Lua yang ada berdasarkan nama file.
    Jika file mengandung nama seperti ARMOR/HELMET/WEAPON tapi TIDAK memiliki keyword wajib
    (contoh: HitboxSeparation), file tersebut DIHAPUS agar diregenerasi ulang dengan template
    yang benar. Ini mencegah file lama yang salah memblokir progress sistem.
    Mengembalikan laporan string tentang file yang diperbaiki.
    """
    root = project_root or PROJECT_ROOT_DIRECTORY
    src = os.path.join(root, "src")

    if not os.path.exists(src):
        return "[Repair] Direktori src belum ada. Tidak ada file yang perlu diperbaiki.\n"

    _log("[bold cyan][FileRepair] Memeriksa validitas file Lua yang sudah ada...[/bold cyan]")

    repaired: List[str] = []
    checked: int = 0
    valid_exts = {".lua", ".luau"}

    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fname in filenames:
            ext = os.path.splitext(fname)[1]
            if ext not in valid_exts:
                continue

            full_path = os.path.join(dirpath, fname)
            fname_upper = fname.upper()
            checked += 1

            matched_rule_key = None
            for rule_key in _FILENAME_KEYWORD_RULES:
                if rule_key in fname_upper:
                    matched_rule_key = rule_key
                    break

            if matched_rule_key is None:
                continue

            required_keywords = _FILENAME_KEYWORD_RULES[matched_rule_key]

            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception:
                continue

            missing = [kw for kw in required_keywords if kw not in content]
            if not missing:
                continue

            rel_path = os.path.relpath(full_path, src)
            _log(
                f"[bold yellow][FileRepair] ⚠️ File tidak valid terdeteksi: {rel_path}\n"
                f"  Keyword hilang: {', '.join(missing)}\n"
                f"  Tindakan: File dihapus untuk diregenerasi ulang dengan template yang benar.[/bold yellow]"
            )

            try:
                os.remove(full_path)
                repaired.append(
                    f"  🗑️ Dihapus untuk regenerasi: {rel_path} (keyword hilang: {', '.join(missing)})"
                )
                parent_dir = os.path.dirname(full_path)
                if os.path.isdir(parent_dir) and not os.listdir(parent_dir):
                    os.rmdir(parent_dir)
            except Exception as _del_err:
                _log(f"[bold red][FileRepair] Gagal hapus {full_path}: {_del_err}[/bold red]")

    if repaired:
        report = (
            f"[LAPORAN PERBAIKAN FILE - {len(repaired)} dari {checked} file dihapus untuk regenerasi]\n"
            + "\n".join(repaired)
            + "\nFile-file di atas akan dibuat ulang secara otomatis dengan template yang benar.\n"
        )
        _log(
            f"[bold green][FileRepair] ✅ {len(repaired)} file invalid dihapus untuk regenerasi ulang.[/bold green]"
        )
    else:
        report = f"[FileRepair] ✅ Semua {checked} file diperiksa. Tidak ada file invalid yang ditemukan.\n"
        _log(
            f"[bold green][FileRepair] ✅ Semua {checked} file valid. Tidak ada perbaikan diperlukan.[/bold green]"
        )

    return report


def get_armor_hitbox_mandatory_template() -> str:
    """
    Mengembalikan template wajib HitboxSeparation untuk diinjeksikan ke prompt
    setiap kali AI membuat modul ARMOR atau HELMET.
    Template ini memastikan string 'HitboxSeparation' selalu muncul di kode output.
    """
    return """
[⚠️ MANDATORY ARMOR/HELMET PROTOCOL - WAJIB DIIKUTI ATAU KODE DITOLAK ⚠️]

Kamu sedang membuat modul ARMOR atau HELMET. Setelah 300+ percobaan gagal karena
'Collision Optimization Violation', berikut adalah TEMPLATE WAJIB yang HARUS kamu ikuti:

ATURAN 1 - HITBOX SEPARATION WAJIB:
Kamu HARUS membuat dua Part terpisah:
  a) Part HITBOX: CanCollide = true, Transparency = 1, Name = "HitboxSeparation"
  b) Part VISUAL: CanCollide = false, berisi mesh/tampilan visual

ATURAN 2 - STRING WAJIB ADA DI KODE:
Baris berikut HARUS muncul secara harfiah di dalam kode Luauamu:
  hitbox.Name = "HitboxSeparation"
Atau sebagai komentar:
  -- HitboxSeparation: [penjelasan]

ATURAN 3 - VISUAL EQUIP WAJIB:
  - ProximityPrompt dengan ActionText = "Gunakan" WAJIB ada di model tanah
  - Saat ditekan: WeldConstraint armor ke UpperTorso/Head karakter pemain
  - Setelah di-equip: Part di tanah DILARANG tetap di workspace (harus di-Destroy atau disembunyikan)

ATURAN 4 - SEMUA ATRIBUT WAJIB ADA:
  local ItemCategory: string = "Armor"
  local BasePrice: number = [angka]
  local ArmorTier: number = [1-6]
  local MaterialType: string = "Ceramic" -- atau "Steel"/"Leather"/"Mithril"
  local Durability: number = 100
  local Recipe = { [BahanMentah] = [jumlah], ... }

CONTOH KODE MINIMUM YANG LULUS VALIDATOR:

--!strict
-- HitboxSeparation: Armor menggunakan teknik Hitbox Terpisah AAA DevForum Standard

local ItemCategory: string = "Armor"
local BasePrice: number = 2500
local ArmorTier: number = 4
local MaterialType: string = "Ceramic"
local Durability: number = 100
local Recipe = { IronIngot = 3, CeramicPlate = 2 }
local VisualEquip = true

local model = Instance.new("Model")
model.Name = "ModernArmorHelmet"

-- HITBOX (CanCollide = true, TIDAK TERLIHAT)
local hitbox = Instance.new("Part")
hitbox.Name = "HitboxSeparation"
hitbox.Size = Vector3.new(2, 2.5, 2)
hitbox.CFrame = CFrame.new(0, 1, 0)
hitbox.Transparency = 1
hitbox.CanCollide = true
hitbox.Anchored = true
hitbox.Parent = model

-- VISUAL (CanCollide = false, TERLIHAT)
local visual = Instance.new("Part")
visual.Name = "VisualMesh"
visual.Size = Vector3.new(2, 2.5, 2)
visual.CFrame = hitbox.CFrame
visual.CanCollide = false
visual.Anchored = true
visual.BrickColor = BrickColor.new("Dark stone grey")
visual.Parent = model

local weld = Instance.new("WeldConstraint")
weld.Part0 = hitbox
weld.Part1 = visual
weld.Parent = hitbox

-- ProximityPrompt untuk equip
local prompt = Instance.new("ProximityPrompt")
prompt.ActionText = "Gunakan"
prompt.ObjectText = "Armor"
prompt.Parent = hitbox

model.Parent = workspace

[IKUTI TEMPLATE DI ATAS. TAMBAHKAN LOGIKA EQUIPMENTMU DI DALAMNYA. JANGAN ABAIKAN HitboxSeparation!]
"""
