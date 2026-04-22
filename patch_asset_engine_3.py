import sys
with open("nexus_asset_engine.py", "r", encoding="utf-8") as f:
    data = f.read()

data = data.replace(
    'return await cls._handle_mesh(task_name, downloaded_obj, obj_path)',
    'luau = cls.generate_mesh_part_luau(task_name, {"id": f"rbxassetid://{asset_id}", "mesh_type": "FileMesh", "is_special_mesh": False})\n                    return await cls._handle_mesh_part_from_luau(task_name, target_path, luau)'
)

with open("nexus_asset_engine.py", "w", encoding="utf-8") as f:
    f.write(data)
