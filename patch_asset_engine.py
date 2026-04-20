import sys
with open("nexus_asset_engine.py", "r", encoding="utf-8") as f:
    data = f.read()

data = data.replace(
    'if is_aura_fallback:',
    'if is_aura_fallback:\n            import re\n            asset_id_match = re.search(r"rbxassetid://(\\d+)", luau_code)\n            if asset_id_match:\n                asset_id = asset_id_match.group(1)\n                console_terminal_interface.print(f"  [Asset Engine] [bold cyan]Mendeteksi Asset ID {asset_id} di Luau fallback. Mencoba mengunduh mesh...[/bold cyan]")\n                obj_path = f"/tmp/{asset_id}.obj"\n                downloaded_obj = cls._download_and_convert_mesh(asset_id)\n                if downloaded_obj and "v " in downloaded_obj:\n                    with open(obj_path, "w", encoding="utf-8") as f:\n                        f.write(downloaded_obj)\n                    return await cls._handle_mesh(task_name, downloaded_obj, obj_path)\n'
)

with open("nexus_asset_engine.py", "w", encoding="utf-8") as f:
    f.write(data)
