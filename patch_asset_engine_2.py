import sys
with open("nexus_asset_engine.py", "r", encoding="utf-8") as f:
    data = f.read()

data = data.replace(
    'obj_path = f"/tmp/{asset_id}.obj"',
    'obj_path = f"/root/Robloxotonom/roblox_asset/{asset_id}.obj"'
)

with open("nexus_asset_engine.py", "w", encoding="utf-8") as f:
    f.write(data)
