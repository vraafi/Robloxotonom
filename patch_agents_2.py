import sys
with open("nexus_main.py", "r", encoding="utf-8") as f:
    data = f.read()

data = data.replace(
    "10. ProximityPrompt di tanah dengan ActionText = 'Equip'.",
    "10. ProximityPrompt di tanah dengan ActionText = 'Equip'. JIKA MENGGUNAKAN MESH ASSET ID (rbxassetid://), PASTIKAN MENULISKANNYA SECARA JELAS KARENA AKAN DIUNDUH OTOMATIS OLEH ASSET ENGINE."
)

data = data.replace(
    "WAJIB di-WeldConstraint ke tangan pemain saat dipakai.",
    "WAJIB di-WeldConstraint ke tangan pemain saat dipakai. GUNAKAN `rbxassetid://<id>` BILA PERLU AGAR DIUNDUH AI SECARA OTONOM."
)

with open("nexus_main.py", "w", encoding="utf-8") as f:
    f.write(data)
