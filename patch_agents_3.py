import sys
with open("nexus_main.py", "r", encoding="utf-8") as f:
    data = f.read()

data = data.replace(
    'GUNAKAN LUAU DAN `rbxassetid://<id>` SEBAGAI MESHID AGAR DIUNDUH AI SECARA OTONOM DAN MENGHASILKAN ASET MODEL NYATA BUKAN KUBUS.',
    'TULISKAN `rbxassetid://<id>` BILA PERLU AGAR DIUNDUH AI SECARA OTONOM.'
)

data = data.replace(
    '1. SmartUIAssetSelector.resolve_and_generate() memilih MeshPart/SpecialMesh terbaik.\n          2. Jika tidak ada yang cocok → aura fallback dipilih dengan PERINGATAN KERAS.',
    '1. SmartUIAssetSelector.resolve_and_generate() memilih MeshPart/SpecialMesh terbaik.\n          2. Jika tidak ada yang cocok, sistem akan mencoba mengekstrak rbxassetid:// secara langsung.'
)

with open("nexus_main.py", "w", encoding="utf-8") as f:
    f.write(data)
