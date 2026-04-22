import re
luau_code = "local mesh = Instance.new('SpecialMesh')\nmesh.MeshId = 'rbxassetid://12345678'\n"
asset_id_match = re.search(r"rbxassetid://(\d+)", luau_code)
if asset_id_match:
    print(asset_id_match.group(1))
else:
    print("NO MATCH")
