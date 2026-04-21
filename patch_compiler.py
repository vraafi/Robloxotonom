import sys
with open("nexus_compiler.py", "r", encoding="utf-8") as f:
    data = f.read()

data = data.replace(
    'if re.search(r"\\b" + re.escape(forb) + r"\\b", sanitized_code):',
    'if forb == "_G":\n                if re.search(r"\\b_G\\b", sanitized_code) or "_G." in sanitized_code:\n                    omni_errors.append(f"Contract Violation: Dilarang keras menggunakan \'{forb}\' pada modul ini.")\n            elif re.search(r"\\b" + re.escape(forb) + r"\\b", sanitized_code):'
)

with open("nexus_compiler.py", "w", encoding="utf-8") as f:
    f.write(data)
