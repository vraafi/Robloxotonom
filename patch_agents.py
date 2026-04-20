import sys
with open("nexus_agents.py", "r", encoding="utf-8") as f:
    data = f.read()

data = data.replace(
    "10. ProximityPrompt di tanah dengan ActionText = 'Equip'.\\n",
    "10. ProximityPrompt di tanah dengan ActionText = 'Equip'. JIKA MENGGUNAKAN MESH ASSET ID (rbxassetid://), PASTIKAN MENULISKANNYA SECARA JELAS KARENA AKAN DIUNDUH OTOMATIS OLEH ASSET ENGINE.\\n"
)

with open("nexus_agents.py", "w", encoding="utf-8") as f:
    f.write(data)
