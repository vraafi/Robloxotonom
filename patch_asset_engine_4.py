import sys
with open("nexus_asset_engine.py", "r", encoding="utf-8") as f:
    data = f.read()

# Make a new helper method specifically to handle downloaded meshes directly wrapping it via rbxmx
data = data.replace(
    'gen_ok, rbxmx_content, gen_err = RbxmxGenerator.generate(task_name, "MESH_PART", luau_code)',
    'return await cls._handle_mesh_part_from_luau(task_name, target_path, luau_code)'
)

data = data.replace(
    '    @classmethod\n    async def _handle_mesh(cls',
    '    @classmethod\n    async def _handle_mesh_part_from_luau(cls, task_name: str, target_path: str, luau_code: str) -> tuple[bool, str, str]:\n        gen_ok, rbxmx_content, gen_err = RbxmxGenerator.generate(task_name, "MESH_PART", luau_code)\n        if not gen_ok:\n            return False, "", f"[RbxmxGenerator MESH_PART] {gen_err}"\n        write_ok, write_err = RbxmxGenerator.write(target_path, rbxmx_content)\n        if not write_ok:\n            return False, "", f"[WriteFile MESH_PART] {write_err}"\n        console_terminal_interface.print(f"  [Asset Engine] 💾 Tersimpan → [dim]{target_path}[/dim]")\n        val_ok, val_msg = AssetTestValidator.validate_rbxmx(target_path, "MESH_PART")\n        if not val_ok:\n            try:\n                os.remove(target_path)\n            except Exception:\n                pass\n            return False, "", f"[XML Validasi] {val_msg}"\n        return True, target_path, ""\n\n    @classmethod\n    async def _handle_mesh(cls'
)

with open("nexus_asset_engine.py", "w", encoding="utf-8") as f:
    f.write(data)
