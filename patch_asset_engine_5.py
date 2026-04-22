import sys
with open("nexus_asset_engine.py", "r", encoding="utf-8") as f:
    data = f.read()

data = data.replace(
    'return await cls._handle_mesh_part_from_luau(task_name, target_path, luau_code)\n        if not gen_ok:\n            return False, "", f"[RbxmxGenerator MESH_PART] {gen_err}"\n\n        write_ok, write_err = RbxmxGenerator.write(target_path, rbxmx_content)\n        if not write_ok:\n            return False, "", f"[WriteFile MESH_PART] {write_err}"\n\n        console_terminal_interface.print(\n            f"  [Asset Engine] 💾 Tersimpan → [dim]{target_path}[/dim]"\n        )\n\n        val_ok, val_msg = AssetTestValidator.validate_rbxmx(target_path, "MESH_PART")\n        if not val_ok:\n            try:\n                os.remove(target_path)\n            except Exception:\n                pass\n            return False, "", f"[XML Validasi] {val_msg}"\n\n        remodel_ok, remodel_msg = await AssetTestValidator.validate_remodel(task_name, target_path)\n        if remodel_ok:\n            console_terminal_interface.print(f"  [Asset Engine] {remodel_msg[:120]}")\n        else:\n            console_terminal_interface.print(\n                f"  [Asset Engine] [bold yellow]⚠️ Remodel: {remodel_msg[:120]}[/bold yellow]"\n            )\n\n        return True, target_path, ""',
    'return await cls._handle_mesh_part_from_luau(task_name, target_path, luau_code)'
)

with open("nexus_asset_engine.py", "w", encoding="utf-8") as f:
    f.write(data)
