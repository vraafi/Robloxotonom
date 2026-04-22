import sys
with open("nexus_asset_engine.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

new_lines = []
skip = False
for line in lines:
    if "return await cls._handle_mesh_part_from_luau" in line:
        new_lines.append(line)
        skip = True
        continue
    if skip and "    # ── Internal: Handle MESH task" in line:
        skip = False

    if not skip:
        new_lines.append(line)

with open("nexus_asset_engine.py", "w", encoding="utf-8") as f:
    f.writelines(new_lines)
