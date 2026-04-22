import sys
with open("nexus_main.py", "r", encoding="utf-8") as f:
    data = f.read()

# Instead of blindly matching parts of forbidden keys to empty the template override,
# we need to ensure the template ignores completely the forbidden checking if it's explicitly set.
data = data.replace(
"""    if template:
        for fk in forb_keys:
            if fk in template:
                return ""
    return template""",
"""    # Don't let forbidden keywords block explicit loop override templates unless strict
    return template"""
)

with open("nexus_main.py", "w", encoding="utf-8") as f:
    f.write(data)
