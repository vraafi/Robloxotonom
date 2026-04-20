import asyncio
import os
import json
import re
import subprocess
import requests
import aiofiles
import signal
import sys
import random
import time
from aiohttp import web
from rich.panel import Panel

from nexus_config import (
    console_terminal_interface,
    ROBLOX_UNIVERSE_ID,
    ROBLOX_PLACE_ID,
    ROBLOX_OPEN_CLOUD_API_KEY,
    PROJECT_ROOT_DIRECTORY,
    SOURCE_CODE_DIRECTORY,
    COMPILED_GAME_FILE,
    VPS_WEBHOOK_PORT,
    LIVE_JIT_MESSAGING_TOPIC,
    ACTIVE_AGENTS,
    TELEGRAM_CHAT_ID,
    TELEGRAM_BOT_TOKEN,
    GEMINI_CLI_PATH,
)
from nexus_database import (
    initialize_system_ledger,
    establish_database_connection,
    log_roblox_telemetry,
    get_unanalyzed_telemetry,
    save_verified_module,
)
from nexus_compiler import NativeLuauCompiler
from nexus_agents import (
    OmniSynthesizerAgent, AutoHealerAgent, LuauKnowledgeScraper,
    execute_gemini_cli_pure, extract_pure_luau_code,
    NexusGlobalState, _roblox_agent_paused,
)
from nexus_healer import PreDeploymentValidator
from nexus_polyglot import start_telegram_polling
from nexus_project_scanner import scan_existing_project, scan_deep_validate



# ==============================
# TELEGRAM RATE LIMITING SYSTEM
# ==============================
_telegram_semaphore = asyncio.Semaphore(1)
_last_telegram_send = 0.0
_min_interval_between_messages = 2.0



class RojoBuildAutoHealer:
    """
    Auto-healer otonom untuk SEMUA error Rojo build.
    Pipeline: Parse error → Cari file → Auto-fix (pattern) → Gemini heal → Retry build.
    Healer membangun prompt sendiri berdasarkan analisis error, tanpa mengurangi konteks yang ada.
    Mendukung semua tipe error Rojo:
      - Property type mismatch (Int32/Float/Enum salah tipe)
      - Unknown property (properti tidak dikenal untuk class tertentu)
      - Malformed file / JSON parse error
      - Missing file / path error
      - Invalid class name
      - Duplicate instance name
      - Rbxm/Rbxmx deserialization error
      - Dan error umum lainnya
    """

    ROJO_ERROR_PATTERNS = {
        "type_mismatch": re.compile(
            r"Property type mismatch: Expected\s+(\S+)\s+to be of type\s+(\w+),\s+but it was of type\s+(\w+)\s+on instance\s+(\S+)",
            re.IGNORECASE
        ),
        "unknown_property": re.compile(
            r"(?:Unknown|Unsupported)\s+property\s+['\"]?(\w+)['\"]?\s+on\s+(?:class\s+)?['\"]?(\w+)['\"]?.*?(?:instance\s+)?(\S+)?",
            re.IGNORECASE
        ),
        "invalid_value": re.compile(
            r"(?:Invalid|Unexpected)\s+(?:value|type)\s+(?:for\s+)?(?:property\s+)?['\"]?(\w+)['\"]?\s*.*?(?:on\s+instance\s+)?(\S+)?",
            re.IGNORECASE
        ),
        "file_not_found": re.compile(
            r"(?:File|Path)\s+(?:not found|does not exist|missing):\s*['\"]?(.+?)['\"]?(?:\s|$)",
            re.IGNORECASE
        ),
        "json_parse": re.compile(
            r"(?:JSON|Parse|Syntax)\s+(?:error|failed|invalid).*?(?:in\s+)?['\"]?(.+?\.\w+)['\"]?",
            re.IGNORECASE
        ),
        "invalid_class": re.compile(
            r"(?:Invalid|Unknown)\s+class(?:Name)?[:\s]+['\"]?(\w+)['\"]?\s*(?:on\s+instance\s+)?(\S+)?",
            re.IGNORECASE
        ),
        "generic_error": re.compile(
            r"\[ERROR\s+rojo\]\s+(.+)",
            re.IGNORECASE
        ),
    }

    INT32_PROPERTIES = {
        "DisplayOrder", "ZIndex", "LayoutOrder", "TextSize",
        "MaxVisibleGraphemes", "SortOrder", "Position",
        "LineHeight", "TextWrapped",
    }

    FLOAT_PROPERTIES = {
        "BackgroundTransparency", "TextTransparency", "ImageTransparency",
        "GroupTransparency", "ScrollBarThickness", "Transparency",
        "Reflectance",
    }

    BOOL_PROPERTIES = {
        "Visible", "Active", "ClipsDescendants", "Draggable",
        "Selectable", "AutoLocalize", "ResetOnSpawn",
        "Enabled", "Archivable", "Locked", "CanCollide", "Anchored",
    }

    ENUM_VALUE_MAP = {
        "SortOrder": {"LayoutOrder": 0, "Name": 1, "Custom": 2},
        "HorizontalAlignment": {"Center": 0, "Left": 1, "Right": 2},
        "VerticalAlignment": {"Center": 0, "Top": 1, "Bottom": 2},
        "TextXAlignment": {"Center": 0, "Left": 1, "Right": 2},
        "TextYAlignment": {"Center": 0, "Top": 1, "Bottom": 2},
        "ScaleType": {"Stretch": 0, "Slice": 1, "Tile": 2, "Fit": 3, "Crop": 4},
        "AutomaticSize": {"None": 0, "X": 1, "Y": 2, "XY": 3},
        "BorderMode": {"Outline": 0, "Middle": 1, "Inset": 2},
        "ZIndexBehavior": {"Global": 0, "Sibling": 1},
        "FillDirection": {"Horizontal": 0, "Vertical": 1},
        "SizeConstraint": {"RelativeXY": 0, "RelativeXX": 1, "RelativeYY": 2},
    }

    KNOWN_BAD_PROPERTIES = {
        "ScreenGui": {"DisplayOrder", "ZIndex", "LayoutOrder"},
        "Frame": {"ZIndex", "LayoutOrder", "BackgroundTransparency"},
        "TextLabel": {"ZIndex", "LayoutOrder", "TextSize", "MaxVisibleGraphemes"},
        "TextButton": {"ZIndex", "LayoutOrder", "TextSize"},
        "ImageLabel": {"ZIndex", "LayoutOrder", "ImageTransparency"},
        "ImageButton": {"ZIndex", "LayoutOrder"},
        "ScrollingFrame": {"ZIndex", "LayoutOrder", "ScrollBarThickness"},
        "UIListLayout": {"SortOrder"},
        "UIGridLayout": {"SortOrder"},
    }

    @staticmethod
    def parse_rojo_errors(stderr: str) -> list:
        """Extract SEMUA error Rojo dari stderr — semua tipe error didukung."""
        errors = []
        seen_keys = set()

        for m in RojoBuildAutoHealer.ROJO_ERROR_PATTERNS["type_mismatch"].finditer(stderr):
            full_prop = m.group(1)
            expected_type = m.group(2)
            actual_type = m.group(3)
            instance_path = m.group(4)
            prop_name = full_prop.split(".")[-1]
            file_name = instance_path.split(".")[-1]
            key = f"type_mismatch:{file_name}:{prop_name}"
            if key not in seen_keys:
                seen_keys.add(key)
                errors.append({
                    "error_type": "type_mismatch",
                    "full_prop": full_prop,
                    "prop_name": prop_name,
                    "expected_type": expected_type,
                    "actual_type": actual_type,
                    "instance_path": instance_path,
                    "file_name": file_name,
                    "raw_message": m.group(0),
                })

        for m in RojoBuildAutoHealer.ROJO_ERROR_PATTERNS["unknown_property"].finditer(stderr):
            prop_name = m.group(1)
            class_name = m.group(2)
            instance_path = m.group(3) or ""
            file_name = instance_path.split(".")[-1] if instance_path else class_name
            key = f"unknown_property:{file_name}:{prop_name}"
            if key not in seen_keys:
                seen_keys.add(key)
                errors.append({
                    "error_type": "unknown_property",
                    "prop_name": prop_name,
                    "class_name": class_name,
                    "instance_path": instance_path,
                    "file_name": file_name,
                    "raw_message": m.group(0),
                    "full_prop": f"{class_name}.{prop_name}",
                    "expected_type": "remove",
                    "actual_type": "unknown",
                })

        for m in RojoBuildAutoHealer.ROJO_ERROR_PATTERNS["invalid_value"].finditer(stderr):
            prop_name = m.group(1)
            instance_path = m.group(2) or ""
            file_name = instance_path.split(".")[-1] if instance_path else prop_name
            key = f"invalid_value:{file_name}:{prop_name}"
            if key not in seen_keys:
                seen_keys.add(key)
                errors.append({
                    "error_type": "invalid_value",
                    "prop_name": prop_name,
                    "instance_path": instance_path,
                    "file_name": file_name,
                    "raw_message": m.group(0),
                    "full_prop": prop_name,
                    "expected_type": "auto",
                    "actual_type": "invalid",
                })

        for m in RojoBuildAutoHealer.ROJO_ERROR_PATTERNS["json_parse"].finditer(stderr):
            file_ref = m.group(1)
            key = f"json_parse:{file_ref}"
            if key not in seen_keys:
                seen_keys.add(key)
                errors.append({
                    "error_type": "json_parse",
                    "file_ref": file_ref,
                    "file_name": os.path.basename(file_ref).replace(".json", "").replace(".lua", ""),
                    "raw_message": m.group(0),
                    "full_prop": "",
                    "prop_name": "",
                    "expected_type": "json_fix",
                    "actual_type": "parse_error",
                    "instance_path": file_ref,
                })

        for m in RojoBuildAutoHealer.ROJO_ERROR_PATTERNS["invalid_class"].finditer(stderr):
            class_name = m.group(1)
            instance_path = m.group(2) or ""
            file_name = instance_path.split(".")[-1] if instance_path else class_name
            key = f"invalid_class:{file_name}:{class_name}"
            if key not in seen_keys:
                seen_keys.add(key)
                errors.append({
                    "error_type": "invalid_class",
                    "class_name": class_name,
                    "instance_path": instance_path,
                    "file_name": file_name,
                    "raw_message": m.group(0),
                    "full_prop": "",
                    "prop_name": "",
                    "expected_type": "class_fix",
                    "actual_type": "invalid_class",
                })

        if not errors:
            for m in RojoBuildAutoHealer.ROJO_ERROR_PATTERNS["generic_error"].finditer(stderr):
                raw_msg = m.group(1).strip()
                if any(raw_msg in e.get("raw_message", "") for e in errors):
                    continue
                key = f"generic:{raw_msg[:80]}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    file_guess = ""
                    file_match = re.search(r'(?:instance|file|path)[:\s]+(\S+)', raw_msg, re.IGNORECASE)
                    if file_match:
                        file_guess = file_match.group(1).split(".")[-1]
                    errors.append({
                        "error_type": "generic",
                        "raw_message": raw_msg,
                        "file_name": file_guess,
                        "full_prop": "",
                        "prop_name": "",
                        "expected_type": "generic_fix",
                        "actual_type": "error",
                        "instance_path": "",
                    })

        return errors

    @staticmethod
    def find_lua_file(file_name: str) -> str:
        """Cari file .lua/.luau di seluruh project berdasarkan nama (tanpa ekstensi)."""
        if not file_name:
            return None
        for root, dirs, files in os.walk(PROJECT_ROOT_DIRECTORY):
            for fname in files:
                if fname.startswith(file_name) and fname.endswith((".lua", ".luau")):
                    return os.path.join(root, fname)
        return None

    @staticmethod
    def find_all_lua_files() -> list:
        """Cari SEMUA file .lua/.luau di project."""
        results = []
        for root, dirs, files in os.walk(PROJECT_ROOT_DIRECTORY):
            for fname in files:
                if fname.endswith((".lua", ".luau")):
                    results.append(os.path.join(root, fname))
        return results

    @staticmethod
    def auto_fix_type_mismatch(file_path: str, prop_name: str, expected_type: str) -> bool:
        """
        Perbaiki otomatis type mismatch yang sudah diketahui (tanpa Gemini).
        Mendukung: Int32, Float, Bool, String, dan Enum mapping.
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                code = f.read()
            original = code

            if expected_type == "Int32" or prop_name in RojoBuildAutoHealer.INT32_PROPERTIES:
                code = re.sub(
                    r"(\.\s*" + re.escape(prop_name) + r"\s*=\s*)Enum\.[A-Za-z0-9_.]+",
                    r"\g<1>0",
                    code
                )
                code = re.sub(
                    r"(?<![\w.])(" + re.escape(prop_name) + r"\s*=\s*)Enum\.[A-Za-z0-9_.]+",
                    r"\g<1>0",
                    code
                )
                code = re.sub(
                    r"(\.\s*" + re.escape(prop_name) + r"\s*=\s*)['\"][^'\"]*['\"]",
                    r"\g<1>0",
                    code
                )
                code = re.sub(
                    r"(\.\s*" + re.escape(prop_name) + r"\s*=\s*)true",
                    r"\g<1>1",
                    code,
                    flags=re.IGNORECASE
                )
                code = re.sub(
                    r"(\.\s*" + re.escape(prop_name) + r"\s*=\s*)false",
                    r"\g<1>0",
                    code,
                    flags=re.IGNORECASE
                )

            elif expected_type in ("Float32", "Float64", "Float", "float"):
                code = re.sub(
                    r"(\.\s*" + re.escape(prop_name) + r"\s*=\s*)Enum\.[A-Za-z0-9_.]+",
                    r"\g<1>0",
                    code
                )
                code = re.sub(
                    r"(\.\s*" + re.escape(prop_name) + r"\s*=\s*)['\"][^'\"]*['\"]",
                    r"\g<1>0",
                    code
                )
                code = re.sub(
                    r"(\.\s*" + re.escape(prop_name) + r"\s*=\s*)true",
                    r"\g<1>1",
                    code,
                    flags=re.IGNORECASE
                )
                code = re.sub(
                    r"(\.\s*" + re.escape(prop_name) + r"\s*=\s*)false",
                    r"\g<1>0",
                    code,
                    flags=re.IGNORECASE
                )

            elif expected_type == "Bool" or expected_type == "bool":
                code = re.sub(
                    r"(\.\s*" + re.escape(prop_name) + r"\s*=\s*)Enum\.[A-Za-z0-9_.]+",
                    r"\g<1>true",
                    code
                )
                code = re.sub(
                    r"(\.\s*" + re.escape(prop_name) + r"\s*=\s*)\d+",
                    lambda m: m.group(1) + ("true" if int(m.group(0).split("=")[-1].strip()) != 0 else "false"),
                    code
                )

            elif expected_type == "String" or expected_type == "string":
                code = re.sub(
                    r"(\.\s*" + re.escape(prop_name) + r"\s*=\s*)(\d+)",
                    r'\g<1>"\g<2>"',
                    code
                )

            elif expected_type == "Enum":
                if prop_name in RojoBuildAutoHealer.ENUM_VALUE_MAP:
                    pass

            if expected_type == "remove":
                code = re.sub(
                    r"[^\n]*\.\s*" + re.escape(prop_name) + r"\s*=\s*[^\n]+\n?",
                    "",
                    code
                )

            if code != original:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(code)
                return True
            return False
        except Exception as e:
            console_terminal_interface.print(
                f"[bold yellow][RojoBuildHealer] Exception di auto_fix: {e}[/bold yellow]"
            )
            return False

    @staticmethod
    def auto_fix_all_known_issues(file_path: str) -> int:
        """
        Scan dan perbaiki SEMUA masalah tipe properti yang diketahui dalam satu file.
        Return jumlah fix yang diterapkan.
        """
        fix_count = 0
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                code = f.read()
            original = code

            for prop in RojoBuildAutoHealer.INT32_PROPERTIES:
                code = re.sub(
                    r"(\.\s*" + re.escape(prop) + r"\s*=\s*)Enum\.[A-Za-z0-9_.]+",
                    r"\g<1>0",
                    code
                )

            for prop in RojoBuildAutoHealer.FLOAT_PROPERTIES:
                code = re.sub(
                    r"(\.\s*" + re.escape(prop) + r"\s*=\s*)Enum\.[A-Za-z0-9_.]+",
                    r"\g<1>0",
                    code
                )

            for prop in RojoBuildAutoHealer.BOOL_PROPERTIES:
                code = re.sub(
                    r"(\.\s*" + re.escape(prop) + r"\s*=\s*)Enum\.[A-Za-z0-9_.]+",
                    r"\g<1>true",
                    code
                )

            if code != original:
                fix_count = sum(1 for a, b in zip(original.splitlines(), code.splitlines()) if a != b)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(code)

            return fix_count
        except Exception:
            return 0

    @staticmethod
    def fix_all_json_meta_files() -> int:
        """
        Scan dan perbaiki SEMUA file .json di project Rojo — termasuk:
          - .meta.json     (metadata instance)
          - .model.json    (data model)
          - *.project.json (konfigurasi project, termasuk default.project.json)
          - .json lainnya

        Jika DisplayOrder di JSON berformat {"Enum": X} atau {"Type": "Enum"}
        maka Rojo akan SELALU error meski Lua sudah diperbaiki.
        Fungsi ini memperbaiki SEMUA file JSON yang berpotensi salah tipe.
        """
        INT32_PROPS = {
            "DisplayOrder", "ZIndex", "LayoutOrder", "TextSize",
            "MaxVisibleGraphemes", "BorderSizePixel",
        }
        FLOAT_PROPS = {
            "BackgroundTransparency", "TextTransparency", "ImageTransparency",
            "GroupTransparency", "ScrollBarThickness", "Transparency",
            "Reflectance", "BackgroundTransparency",
        }
        BOOL_PROPS = {
            "Visible", "Active", "ClipsDescendants", "Draggable",
            "Selectable", "AutoLocalize", "ResetOnSpawn", "Enabled",
            "Modal", "IgnoreGuiInset",
        }

        fix_count = 0

        for root, dirs, files in os.walk(PROJECT_ROOT_DIRECTORY):
            for fname in files:
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        raw = f.read()
                    data = json.loads(raw)
                except Exception:
                    continue

                changed = False

                def fix_properties_dict(props: dict) -> bool:
                    """Fix properti dalam dict — return True jika ada perubahan."""
                    modified = False
                    for prop_name in list(props.keys()):
                        val = props[prop_name]

                        if prop_name in INT32_PROPS:
                            if isinstance(val, dict):
                                if "Enum" in val or val.get("Type") in ("Enum", "enum"):
                                    props[prop_name] = 0
                                    modified = True
                                elif "Int32" not in val and "Type" not in val:
                                    props[prop_name] = 0
                                    modified = True
                            elif isinstance(val, str):
                                props[prop_name] = 0
                                modified = True

                        elif prop_name in FLOAT_PROPS:
                            if isinstance(val, dict):
                                if "Enum" in val or val.get("Type") in ("Enum", "enum"):
                                    props[prop_name] = 0.0
                                    modified = True

                        elif prop_name in BOOL_PROPS:
                            if isinstance(val, dict):
                                if "Enum" in val or val.get("Type") in ("Enum", "enum"):
                                    props[prop_name] = True
                                    modified = True

                    return modified

                def fix_node(node) -> bool:
                    """Rekursif fix semua node JSON Rojo."""
                    mod = False
                    if not isinstance(node, dict):
                        return mod
                    for key in ["$properties", "Properties", "properties"]:
                        if key in node and isinstance(node[key], dict):
                            if fix_properties_dict(node[key]):
                                mod = True
                    for key in ["$children", "Children", "children"]:
                        if key in node and isinstance(node[key], (list, dict)):
                            children = node[key]
                            if isinstance(children, list):
                                for child in children:
                                    if fix_node(child):
                                        mod = True
                            elif isinstance(children, dict):
                                for child in children.values():
                                    if fix_node(child):
                                        mod = True
                    for k, v in node.items():
                        if isinstance(v, dict) and k not in ("$properties", "Properties", "properties"):
                            if fix_node(v):
                                mod = True
                    return mod

                if fix_node(data):
                    changed = True

                if changed:
                    try:
                        with open(fpath, "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=2, ensure_ascii=False)
                        fix_count += 1
                        console_terminal_interface.print(
                            f"[bold green][JsonFix] Tipe properti diperbaiki di: {fname}[/bold green]"
                        )
                    except Exception:
                        pass

        return fix_count

    @staticmethod
    def fix_json_for_instance(instance_name: str) -> int:
        """
        Cari dan perbaiki file JSON yang terkait dengan nama instance tertentu.
        Dipanggil saat healer mendeteksi error spesifik pada instance tertentu.
        """
        fix_count = 0
        search_terms = [instance_name, instance_name.lower(), instance_name.replace("_", "")]
        for root, dirs, files in os.walk(PROJECT_ROOT_DIRECTORY):
            for fname in files:
                if not fname.endswith(".json"):
                    continue
                base = fname.replace(".meta.json", "").replace(".model.json", "").replace(".json", "")
                if any(t.lower() in base.lower() or base.lower() in t.lower() for t in search_terms):
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except Exception:
                        continue

                    INT32_PROPS = {
                        "DisplayOrder", "ZIndex", "LayoutOrder", "TextSize",
                        "MaxVisibleGraphemes", "BorderSizePixel",
                    }

                    def _fix(node):
                        changed = False
                        if isinstance(node, dict):
                            for props_key in ["$properties", "Properties", "properties"]:
                                props = node.get(props_key, {})
                                if isinstance(props, dict):
                                    for pname in list(props.keys()):
                                        if pname in INT32_PROPS:
                                            v = props[pname]
                                            if isinstance(v, dict) and (
                                                "Enum" in v or v.get("Type") in ("Enum", "enum")
                                            ):
                                                props[pname] = 0
                                                changed = True
                                            elif isinstance(v, str):
                                                props[pname] = 0
                                                changed = True
                            for k, v in node.items():
                                if isinstance(v, (dict, list)) and k not in ("$properties", "Properties", "properties"):
                                    if isinstance(v, dict):
                                        if _fix(v):
                                            changed = True
                                    elif isinstance(v, list):
                                        for item in v:
                                            if isinstance(item, dict) and _fix(item):
                                                changed = True
                        return changed

                    if _fix(data):
                        try:
                            with open(fpath, "w", encoding="utf-8") as f:
                                json.dump(data, f, indent=2, ensure_ascii=False)
                            fix_count += 1
                            console_terminal_interface.print(
                                f"[bold green][JsonFix] Instance fix di: {fname}[/bold green]"
                            )
                        except Exception:
                            pass
        return fix_count


    @staticmethod
    def fix_all_rbxmx_files() -> int:
        """
        Scan dan perbaiki SEMUA file .rbxmx di project Rojo.
        Dalam format XML Roblox, <token> adalah tag untuk tipe Enum, sedangkan
        DisplayOrder wajib menggunakan tag <int> (Int32).
        Bug umum: generator menggunakan <token name="DisplayOrder"> padahal
        Rojo mengharapkan <int name="DisplayOrder">.
        """
        INT32_RBXMX_PROPS = {"DisplayOrder", "ZIndex", "LayoutOrder", "TextSize", "BorderSizePixel"}
        fix_count = 0
        for root, dirs, files in os.walk(PROJECT_ROOT_DIRECTORY):
            for fname in files:
                if not fname.endswith(".rbxmx"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        xml_content = f.read()
                    original = xml_content
                    for prop in INT32_RBXMX_PROPS:
                        xml_content = re.sub(
                            r'<token\s+name="' + re.escape(prop) + r'">([\d]+)</token>',
                            '<int name="' + prop + r'">\1</int>',
                            xml_content,
                        )
                    if xml_content != original:
                        with open(fpath, "w", encoding="utf-8") as f:
                            f.write(xml_content)
                        fix_count += 1
                        console_terminal_interface.print(
                            f"[bold green][RbxmxFix] Diperbaiki: {fname}[/bold green]"
                        )
                except Exception:
                    pass
        return fix_count

    @staticmethod
    def fix_rbxmx_for_instance(instance_name: str) -> int:
        """
        Cari dan perbaiki file .rbxmx yang terkait dengan nama instance tertentu.
        Dipanggil saat healer mendeteksi type_mismatch pada instance spesifik.
        """
        INT32_RBXMX_PROPS = {"DisplayOrder", "ZIndex", "LayoutOrder", "TextSize", "BorderSizePixel"}
        fix_count = 0
        search_terms = [instance_name, instance_name.lower(), instance_name.replace("_", "")]
        for root, dirs, files in os.walk(PROJECT_ROOT_DIRECTORY):
            for fname in files:
                if not fname.endswith(".rbxmx"):
                    continue
                base = fname.replace(".rbxmx", "")
                if not any(t.lower() in base.lower() or base.lower() in t.lower() for t in search_terms):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        xml_content = f.read()
                    original = xml_content
                    for prop in INT32_RBXMX_PROPS:
                        xml_content = re.sub(
                            r'<token\s+name="' + re.escape(prop) + r'">([\d]+)</token>',
                            '<int name="' + prop + r'">\1</int>',
                            xml_content,
                        )
                    if xml_content != original:
                        with open(fpath, "w", encoding="utf-8") as f:
                            f.write(xml_content)
                        fix_count += 1
                        console_terminal_interface.print(
                            f"[bold green][RbxmxFix] Instance fix di: {fname}[/bold green]"
                        )
                except Exception:
                    pass
        return fix_count


    @staticmethod
    def scan_project_files_summary() -> dict:
        """
        Inventarisasi SEMUA file di project Rojo dan validasi struktur dasarnya.
        Healer memanggil ini agar tidak 'buta' — tahu persis file apa saja yang ada
        dan apakah ada masalah className yang tidak valid di file .meta.json.

        File yang diperiksa:
          .lua / .luau        → Skrip Lua
          .meta.json          → Metadata instance Rojo
          .model.json         → Data model Rojo
          .rbxmx              → File XML model Roblox
          *.project.json      → File konfigurasi project Rojo
          .json (lainnya)     → File JSON umum
        """
        VALID_ROBLOX_CLASSES = {
            "ScreenGui", "Frame", "TextLabel", "TextButton", "ImageLabel",
            "ImageButton", "ScrollingFrame", "TextBox", "ViewportFrame",
            "UIListLayout", "UIGridLayout", "UIAspectRatioConstraint",
            "UISizeConstraint", "UITextSizeConstraint", "UIPadding", "UICorner",
            "UIGradient", "UIStroke", "UIScale", "UIPageLayout",
            "LocalScript", "Script", "ModuleScript",
            "Model", "Part", "MeshPart", "SpecialMesh", "UnionOperation",
            "Folder", "Configuration", "RemoteEvent", "RemoteFunction",
            "BindableEvent", "BindableFunction",
            "StringValue", "IntValue", "NumberValue", "BoolValue", "ObjectValue",
            "Weld", "WeldConstraint", "Motor6D", "DataModel", "StarterPlayer",
            "StarterGui", "StarterPlayerScripts", "StarterCharacterScripts",
            "ServerScriptService", "ReplicatedStorage", "Workspace",
        }
        summary = {
            "lua": [], "meta_json": [], "model_json": [],
            "rbxmx": [], "project_json": [], "other_json": [],
            "invalid_classname": [], "missing_classname": [],
        }
        for root, dirs, files in os.walk(PROJECT_ROOT_DIRECTORY):
            for fname in files:
                fpath = os.path.join(root, fname)
                if fname.endswith((".lua", ".luau")):
                    summary["lua"].append(fpath)
                elif fname.endswith(".meta.json"):
                    summary["meta_json"].append(fpath)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        cls_name = data.get("className") or data.get("ClassName") or data.get("class")
                        if cls_name is None:
                            summary["missing_classname"].append(fpath)
                        elif cls_name not in VALID_ROBLOX_CLASSES:
                            summary["invalid_classname"].append((fpath, cls_name))
                    except Exception:
                        pass
                elif fname.endswith(".model.json"):
                    summary["model_json"].append(fpath)
                elif fname.endswith(".rbxmx"):
                    summary["rbxmx"].append(fpath)
                elif fname.endswith(".project.json") or fname == "default.project.json":
                    summary["project_json"].append(fpath)
                elif fname.endswith(".json"):
                    summary["other_json"].append(fpath)

        total_files = sum(len(v) for k, v in summary.items() if k not in ("invalid_classname", "missing_classname"))
        console_terminal_interface.print(
            f"[bold blue][Healer Scan] Inventarisasi project selesai:[/bold blue]\n"
            f"  Skrip Lua/Luau   : {len(summary['lua'])} file\n"
            f"  Meta JSON        : {len(summary['meta_json'])} file\n"
            f"  Model JSON       : {len(summary['model_json'])} file\n"
            f"  RBXMX            : {len(summary['rbxmx'])} file\n"
            f"  Project JSON     : {len(summary['project_json'])} file\n"
            f"  JSON lainnya     : {len(summary['other_json'])} file\n"
            f"  ─────────────────────────────\n"
            f"  Total            : {total_files} file dipindai"
        )
        if summary["invalid_classname"]:
            for fp, cls in summary["invalid_classname"]:
                console_terminal_interface.print(
                    f"[bold yellow][Healer Scan] ⚠️  className tidak dikenal '{cls}' di: {os.path.basename(fp)}[/bold yellow]"
                )
        return summary

    @staticmethod
    def fix_invalid_classnames_in_meta(summary: dict = None) -> int:
        """
        Periksa dan perbaiki className yang tidak valid di file .meta.json.
        Jika className salah eja atau tidak dikenal, Rojo akan menolak seluruh build.
        Perbaikan: jika className mirip dengan class valid (fuzzy match sederhana),
        ganti otomatis. Jika tidak ada yang cocok, hapus property yang salah tipe saja.
        """
        CLASSNAME_ALIASES = {
            "screengui": "ScreenGui", "screen_gui": "ScreenGui",
            "localscript": "LocalScript", "local_script": "LocalScript",
            "modulescript": "ModuleScript", "module_script": "ModuleScript",
            "script": "Script",
            "textlabel": "TextLabel", "text_label": "TextLabel",
            "textbutton": "TextButton", "text_button": "TextButton",
            "imagebutton": "ImageButton", "image_button": "ImageButton",
            "imagelabel": "ImageLabel", "image_label": "ImageLabel",
            "frame": "Frame",
            "scrollingframe": "ScrollingFrame", "scrolling_frame": "ScrollingFrame",
            "textbox": "TextBox", "text_box": "TextBox",
            "folder": "Folder",
            "model": "Model",
            "remotevent": "RemoteEvent", "remoteevent": "RemoteEvent",
            "remotefunction": "RemoteFunction",
            "uilistlayout": "UIListLayout", "ui_list_layout": "UIListLayout",
        }
        fix_count = 0
        if summary is None:
            summary = RojoBuildAutoHealer.scan_project_files_summary()
        for fpath, bad_cls in summary.get("invalid_classname", []):
            normalized = bad_cls.lower().replace(" ", "").replace("-", "").replace("_", "")
            correct_cls = CLASSNAME_ALIASES.get(normalized) or CLASSNAME_ALIASES.get(bad_cls.lower())
            if correct_cls:
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    for key in ("className", "ClassName", "class"):
                        if key in data:
                            data[key] = correct_cls
                    with open(fpath, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                    fix_count += 1
                    console_terminal_interface.print(
                        f"[bold green][ClassFix] '{bad_cls}' → '{correct_cls}': {os.path.basename(fpath)}[/bold green]"
                    )
                except Exception:
                    pass
        return fix_count

    @staticmethod
    def proactive_scan_and_fix() -> int:
        """
        Proaktif scan DAN perbaiki SEMUA tipe file project Rojo sebelum build:
          1. Inventarisasi semua file (.lua, .meta.json, .rbxmx, .project.json, dsb.)
          2. Perbaiki className yang salah di .meta.json
          3. Perbaiki type mismatch di semua file JSON (termasuk default.project.json)
          4. Perbaiki tag <token> yang salah di file .rbxmx
          5. Perbaiki type mismatch di kode Lua

        Healer tidak buta — semua tipe file yang dikenal Rojo diperiksa dan diperbaiki.
        """
        total_fixes = 0

        # ── Inventarisasi semua file sebelum mulai (healer tidak buta)
        summary = RojoBuildAutoHealer.scan_project_files_summary()

        # ── Perbaiki className yang salah di .meta.json
        class_fixes = RojoBuildAutoHealer.fix_invalid_classnames_in_meta(summary)
        if class_fixes > 0:
            console_terminal_interface.print(
                f"[bold green][ProactiveFix] {class_fixes} className diperbaiki di file .meta.json[/bold green]"
            )
            total_fixes += class_fixes

        json_fixes = RojoBuildAutoHealer.fix_all_json_meta_files()
        if json_fixes > 0:
            console_terminal_interface.print(
                f"[bold green][ProactiveFix] {json_fixes} masalah diperbaiki di file JSON metadata Rojo[/bold green]"
            )
            total_fixes += json_fixes

        rbxmx_fixes = RojoBuildAutoHealer.fix_all_rbxmx_files()
        if rbxmx_fixes > 0:
            console_terminal_interface.print(
                f"[bold green][ProactiveFix] {rbxmx_fixes} masalah diperbaiki di file .rbxmx Rojo[/bold green]"
            )
            total_fixes += rbxmx_fixes

        all_files = RojoBuildAutoHealer.find_all_lua_files()
        for fpath in all_files:
            fixes = RojoBuildAutoHealer.auto_fix_all_known_issues(fpath)
            if fixes > 0:
                console_terminal_interface.print(
                    f"[bold green][ProactiveFix] {fixes} masalah diperbaiki di {os.path.basename(fpath)}[/bold green]"
                )
                total_fixes += fixes
        return total_fixes

    @staticmethod
    async def heal_with_gemini(agent: dict, file_path: str, error_info: dict) -> bool:
        """
        Healer membangun prompt sendiri berdasarkan error Rojo dan memperbaiki file via Gemini CLI.
        Prompt dibangun secara dinamis — mendukung SEMUA tipe error.
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                original_code = f.read()
        except Exception:
            return False

        error_type = error_info.get("error_type", "generic")

        sys_inst = (
            "Kamu adalah spesialis debug Roblox Luau dan Rojo project structure level APEX.\n"
            "TUGAS: Perbaiki kode Luau yang menyebabkan Rojo build gagal.\n"
            "OUTPUT: HANYA kode Luau murni yang sudah diperbaiki. TIDAK ADA teks lain.\n"
            "Baris pertama WAJIB --!strict.\n"
            "JANGAN menambahkan komentar atau penjelasan — HANYA kode Luau.\n"
            "Kamu WAJIB memahami perbedaan antara tipe data Roblox:\n"
            "  - Int32: angka integer (0, 1, 5, 10)\n"
            "  - Float/Float32: angka desimal (0.5, 1.0)\n"
            "  - Bool: true/false\n"
            "  - String: teks dalam tanda kutip\n"
            "  - Enum: Enum.NamaEnum.Nilai (HANYA untuk properti yang memang bertipe Enum)\n"
            "  - Color3: Color3.fromRGB(r, g, b) atau Color3.new(r, g, b)\n"
            "  - UDim2: UDim2.new(sx, ox, sy, oy) atau UDim2.fromScale(sx, sy)\n"
            "  - Vector2/Vector3: Vector2.new(x, y) / Vector3.new(x, y, z)\n"
        )

        if error_type == "type_mismatch":
            heal_prompt = (
                f"[ROJO BUILD ERROR]:\n{error_info['raw_message']}\n\n"
                f"[DIAGNOSA]:\n"
                f"Properti '{error_info['prop_name']}' diisi dengan tipe {error_info['actual_type']}, "
                f"padahal Rojo mengharuskan tipe {error_info['expected_type']}.\n\n"
                f"[ATURAN TIPE PROPERTI ROBLOX PENTING]:\n"
                f"  DisplayOrder  = 5             (Int32)\n"
                f"  ZIndex        = 1             (Int32)\n"
                f"  LayoutOrder   = 0             (Int32)\n"
                f"  TextSize      = 14            (Int32)\n"
                f"  MaxVisibleGraphemes = -1      (Int32)\n"
                f"  BackgroundTransparency = 0.5  (Float)\n"
                f"  TextTransparency = 0          (Float)\n"
                f"  Visible       = true          (Bool)\n"
                f"  Active        = true          (Bool)\n"
                f"  SortOrder     = Enum.SortOrder.LayoutOrder (Enum — INI BENAR PAKAI ENUM)\n"
                f"  ZIndexBehavior = Enum.ZIndexBehavior.Global (Enum)\n"
                f"  FillDirection = Enum.FillDirection.Vertical (Enum)\n\n"
                f"[INSTRUKSI]:\n"
                f"  Perbaiki assignment '{error_info['prop_name']}' dari {error_info['actual_type']} ke {error_info['expected_type']}.\n\n"
            )
        elif error_type == "unknown_property":
            heal_prompt = (
                f"[ROJO BUILD ERROR]:\n{error_info['raw_message']}\n\n"
                f"[DIAGNOSA]:\n"
                f"Properti '{error_info['prop_name']}' TIDAK ADA di class {error_info.get('class_name', 'Unknown')}.\n\n"
                f"[INSTRUKSI]:\n"
                f"  1. Hapus atau komentari baris yang mengatur properti '{error_info['prop_name']}'.\n"
                f"  2. Atau ganti dengan properti yang benar untuk class tersebut.\n"
                f"  3. Pastikan semua properti yang digunakan valid untuk class Roblox yang bersangkutan.\n\n"
            )
        elif error_type == "invalid_value":
            heal_prompt = (
                f"[ROJO BUILD ERROR]:\n{error_info['raw_message']}\n\n"
                f"[DIAGNOSA]:\n"
                f"Nilai yang diberikan untuk properti '{error_info['prop_name']}' tidak valid.\n\n"
                f"[INSTRUKSI]:\n"
                f"  1. Periksa tipe data yang benar untuk properti tersebut.\n"
                f"  2. Ganti dengan nilai yang sesuai tipe datanya.\n"
                f"  3. Rujuk aturan tipe di SYSTEM INSTRUCTION.\n\n"
            )
        elif error_type == "invalid_class":
            heal_prompt = (
                f"[ROJO BUILD ERROR]:\n{error_info['raw_message']}\n\n"
                f"[DIAGNOSA]:\n"
                f"Class '{error_info.get('class_name', 'Unknown')}' tidak dikenal oleh Rojo/Roblox.\n\n"
                f"[INSTRUKSI]:\n"
                f"  1. Ganti $className atau Instance.new() dengan class yang valid.\n"
                f"  2. Class yang umum: Frame, TextLabel, TextButton, ImageLabel, ScrollingFrame, ScreenGui, etc.\n"
                f"  3. Pastikan tidak ada typo di nama class.\n\n"
            )
        else:
            heal_prompt = (
                f"[ROJO BUILD ERROR]:\n{error_info.get('raw_message', 'Unknown error')}\n\n"
                f"[INSTRUKSI UMUM]:\n"
                f"  1. Analisis error di atas dan perbaiki kode.\n"
                f"  2. Pastikan semua properti menggunakan tipe data yang benar.\n"
                f"  3. Pastikan semua class name valid.\n"
                f"  4. Pastikan tidak ada duplikasi instance name.\n\n"
            )

        heal_prompt += (
            f"[KODE YANG PERLU DIPERBAIKI ({os.path.basename(file_path)})]:\n"
            f"```lua\n{original_code}\n```\n\n"
            f"Kembalikan kode Luau LENGKAP yang sudah diperbaiki."
        )

        success, result_data = await execute_gemini_cli_pure(agent, sys_inst, heal_prompt)
        if not success or not result_data:
            return False

        fixed_code = extract_pure_luau_code(result_data)
        if not fixed_code or len(fixed_code.strip()) < 20:
            return False

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(fixed_code)
            return True
        except Exception:
            return False

    @staticmethod
    async def heal_loop(rojo_stderr: str, agent: dict) -> bool:
        """
        Orchestrate full auto-heal pipeline:
        1. Proaktif scan & fix semua file yang sudah diketahui pattern-nya
        2. Parse semua Rojo error dari stderr (semua tipe)
        3. Untuk tiap error: cari file → auto-fix pattern → Gemini heal
        4. Kembalikan True jika semua berhasil diperbaiki
        """
        proactive_fixes = RojoBuildAutoHealer.proactive_scan_and_fix()
        if proactive_fixes > 0:
            console_terminal_interface.print(
                f"[bold green][RojoBuildHealer] ProactiveFix: {proactive_fixes} masalah diperbaiki sebelum parsing error.[/bold green]"
            )

        errors = RojoBuildAutoHealer.parse_rojo_errors(rojo_stderr)
        if not errors:
            if proactive_fixes > 0:
                console_terminal_interface.print(
                    "[bold green][RojoBuildHealer] Tidak ada error Rojo tersisa setelah proactive fix.[/bold green]"
                )
                return True
            console_terminal_interface.print(
                "[bold yellow][RojoBuildHealer] Tidak bisa parse error Rojo. Melewati auto-heal.[/bold yellow]"
            )
            return False

        console_terminal_interface.print(
            f"[bold cyan][RojoBuildHealer] {len(errors)} error Rojo terdeteksi (tipe: "
            f"{', '.join(set(e['error_type'] for e in errors))}). Memulai pipeline auto-heal...[/bold cyan]"
        )

        all_fixed = True
        for idx, err in enumerate(errors, 1):
            error_type = err.get("error_type", "generic")
            file_name = err.get("file_name", "")
            prop_name = err.get("prop_name", "")
            expected_type = err.get("expected_type", "")

            console_terminal_interface.print(
                f"[bold cyan][RojoBuildHealer] [{idx}/{len(errors)}] Healing {error_type}: "
                f"{file_name} | {prop_name or 'general'} → {expected_type}[/bold cyan]"
            )

            if error_type == "json_parse":
                file_ref = err.get("file_ref", "")
                if file_ref and os.path.exists(file_ref):
                    if await RojoBuildAutoHealer.heal_with_gemini(agent, file_ref, err):
                        console_terminal_interface.print(
                            f"[bold green][RojoBuildHealer] ✅ JSON fix berhasil: {file_ref}[/bold green]"
                        )
                        continue
                all_fixed = False
                continue

            # ── LANGKAH 0: Fix file JSON metadata Rojo terlebih dahulu ──────────
            # Rojo membaca properti dari file .meta.json / .model.json, BUKAN dari kode Lua.
            # Ini adalah penyebab loop yang terus berulang meski Lua sudah diperbaiki.
            if error_type == "type_mismatch" and file_name:
                json_fixes = RojoBuildAutoHealer.fix_json_for_instance(file_name)
                if json_fixes > 0:
                    console_terminal_interface.print(
                        f"[bold green][RojoBuildHealer] ✅ JSON metadata fix berhasil ({json_fixes} file): {file_name}[/bold green]"
                    )
                rbxmx_fixes = RojoBuildAutoHealer.fix_rbxmx_for_instance(file_name)
                if rbxmx_fixes > 0:
                    console_terminal_interface.print(
                        f"[bold green][RojoBuildHealer] ✅ RBXMX fix berhasil ({rbxmx_fixes} file): {file_name}[/bold green]"
                    )

            # ── LANGKAH 1: Cari file Lua ─────────────────────────────────────────
            file_path = RojoBuildAutoHealer.find_lua_file(file_name)
            if not file_path:
                # Tidak ada file Lua — tapi JSON mungkin sudah diperbaiki
                if error_type == "type_mismatch" and file_name:
                    json_fixes = RojoBuildAutoHealer.fix_json_for_instance(file_name)
                    if json_fixes > 0:
                        console_terminal_interface.print(
                            f"[bold green][RojoBuildHealer] ✅ JSON-only fix: {file_name} (tidak ada file Lua)[/bold green]"
                        )
                        continue
                console_terminal_interface.print(
                    f"[bold yellow][RojoBuildHealer] File '{file_name}' tidak ditemukan.[/bold yellow]"
                )
                all_fixed = False
                continue

            # ── LANGKAH 2: Pattern auto-fix di Lua (cepat, tanpa API) ───────────
            if prop_name and expected_type and RojoBuildAutoHealer.auto_fix_type_mismatch(file_path, prop_name, expected_type):
                console_terminal_interface.print(
                    f"[bold green][RojoBuildHealer] ✅ Pattern auto-fix (Lua) berhasil: {file_name}[/bold green]"
                )
                continue

            # ── LANGKAH 3: Gemini CLI healing ────────────────────────────────────
            if await RojoBuildAutoHealer.heal_with_gemini(agent, file_path, err):
                console_terminal_interface.print(
                    f"[bold green][RojoBuildHealer] ✅ Gemini heal berhasil: {file_name}[/bold green]"
                )
            else:
                console_terminal_interface.print(
                    f"[bold red][RojoBuildHealer] ❌ Gagal heal: {file_name}[/bold red]"
                )
                all_fixed = False

        return all_fixed


async def send_telegram_notification(message: str, important: bool = False, document_path: str = None) -> bool:
    """
    Mengirim notifikasi atau dokumen ke Telegram dengan sistem Retry
    dan Timeout 120 Detik (Mencegah Read timed out saat upload build.rbxl).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url_message = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    url_document = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"

    import aiohttp
    timeout = aiohttp.ClientTimeout(total=120)

    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if document_path and os.path.exists(document_path):
                    data = aiohttp.FormData()
                    data.add_field('chat_id', str(TELEGRAM_CHAT_ID))
                    data.add_field('caption', message)
                    data.add_field('document', open(document_path, 'rb'))

                    async with session.post(url_document, data=data) as response:
                        if response.status == 200:
                            return True
                        elif response.status == 502:
                            console_terminal_interface.print(f"[bold yellow][Deploy] Telegram 502 Bad Gateway. Retry {attempt+1}/3...[/bold yellow]")
                            await asyncio.sleep(5)
                else:
                    payload = {
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": message[:4096],
                        "parse_mode": "HTML"
                    }
                    async with session.post(url_message, json=payload) as response:
                        if response.status == 200:
                            return True
                        elif response.status == 429:
                            await asyncio.sleep(3)

        except asyncio.TimeoutError:
            console_terminal_interface.print(f"[bold red][Deploy] Telegram API Read timed out. Retry {attempt+1}/3...[/bold red]")
            await asyncio.sleep(5)
        except Exception as e:
            console_terminal_interface.print(f"[bold red][Deploy] Error Telegram HTTP: {e}[/bold red]")
            await asyncio.sleep(3)

    return False

async def send_telegram_document(file_path: str, caption: str = ""):
    global _last_telegram_send

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        console_terminal_interface.print("[dim yellow]Kredensial Telegram kosong, pengiriman dokumen dilewati.[/dim yellow]")
        return

    if not os.path.exists(file_path):
        console_terminal_interface.print(
            f"[bold red]Gagal mengirim ke Telegram: File {file_path} tidak ditemukan![/bold red]"
        )
        return

    async with _telegram_semaphore:
        now = time.time()
        elapsed = now - _last_telegram_send
        if elapsed < _min_interval_between_messages:
            await asyncio.sleep(_min_interval_between_messages - elapsed)

        _last_telegram_send = time.time()

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"

        def _send():
            try:
                with open(file_path, "rb") as f:
                    files = {"document": f}
                    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
                    res = requests.post(url, data=data, files=files, timeout=120)
                    if res.status_code == 200:
                        console_terminal_interface.print("[bold green]✅ File .rbxl sukses dievakuasi ke Telegram Anda![/bold green]")
                    else:
                        console_terminal_interface.print(f"[bold red]❌ Gagal kirim Telegram: {res.text}[/bold red]")
            except Exception as e:
                console_terminal_interface.print(f"[bold red]Exception Telegram Document: {e}[/bold red]")

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _send)


async def handle_roblox_telemetry(request):
    try:
        data = await request.json()
        await log_roblox_telemetry(
            data.get("server_id", "UNKNOWN"),
            data.get("event_type", "UNKNOWN"),
            data.get("event_data", {}),
        )
        return web.Response(text="TELEMETRY_LOGGED", status=200)
    except Exception as e:
        return web.Response(text=str(e), status=400)


async def start_telemetry_webhook():
    app = web.Application()
    app.router.add_post("/telemetry", handle_roblox_telemetry)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "0.0.0.0", VPS_WEBHOOK_PORT)
        await site.start()
        console_terminal_interface.print(
            f"[bold cyan][Webhook] Memantau Roblox di Port {VPS_WEBHOOK_PORT}...[/bold cyan]"
        )
    except OSError as e:
        console_terminal_interface.print(
            f"[bold yellow][Webhook] Port {VPS_WEBHOOK_PORT} tidak tersedia: {e}. Webhook dinonaktifkan.[/bold yellow]"
        )


class RobloxDeployer:
    @staticmethod
    def compile_rojo() -> tuple:
        """Returns (success: bool, stderr: str)"""
        console_terminal_interface.print("[bold yellow][Rojo] Mengompilasi Realitas ke .rbxl...[/bold yellow]")
        try:
            result = subprocess.run(
                ["rojo", "build", PROJECT_ROOT_DIRECTORY, "-o", COMPILED_GAME_FILE],
                capture_output=True,
                timeout=120,
            )
            stderr_str = result.stderr.decode(errors='ignore')
            if result.returncode != 0:
                console_terminal_interface.print(
                    f"[bold yellow][Rojo] Build gagal: {stderr_str[:300]}[/bold yellow]"
                )
            return result.returncode == 0, stderr_str
        except FileNotFoundError:
            console_terminal_interface.print("[bold yellow][Rojo] Tidak terinstall. Tahap build dilewati.[/bold yellow]")
            return False, "FileNotFoundError: rojo not installed"
        except subprocess.TimeoutExpired:
            console_terminal_interface.print("[bold yellow][Rojo] Build timeout.[/bold yellow]")
            return False, "TimeoutExpired"
        except Exception as e:
            console_terminal_interface.print(f"[bold yellow][Rojo] Error: {e}[/bold yellow]")
            return False, str(e)

    @staticmethod
    async def publish(evolution_level: int):
        if not os.path.exists(COMPILED_GAME_FILE):
            console_terminal_interface.print("[bold yellow][Deploy] File .rbxl tidak ditemukan. Publish dilewati.[/bold yellow]")
            return

        console_terminal_interface.print(
            f"[bold cyan][Deploy] Mengirimkan file kompilasi ke Telegram (Evolusi {evolution_level})...[/bold cyan]"
        )
        await send_telegram_document(
            COMPILED_GAME_FILE,
            f"🚀 [NEXUS APEX] File Final Evolusi {evolution_level} siap! (Akan di-publish ke Roblox Creator API)",
        )

        if not ROBLOX_OPEN_CLOUD_API_KEY:
            console_terminal_interface.print(
                "[bold yellow][Deploy] ROBLOX_OPEN_CLOUD_API_KEY tidak dikonfigurasi. Berhenti setelah evakuasi Telegram.[/bold yellow]"
            )
            return

        url = f"https://apis.roblox.com/universes/v1/{ROBLOX_UNIVERSE_ID}/places/{ROBLOX_PLACE_ID}/versions"
        headers = {
            "x-api-key": ROBLOX_OPEN_CLOUD_API_KEY,
            "Content-Type": "application/xml",
        }
        try:
            console_terminal_interface.print("[bold cyan][Deploy] Mengunggah ke Roblox Open Cloud API...[/bold cyan]")
            loop = asyncio.get_running_loop()

            with open(COMPILED_GAME_FILE, "rb") as f:
                file_data = f.read()

            def _do_publish():
                return requests.post(
                    url,
                    headers=headers,
                    data=file_data,
                    params={"versionType": "Published"},
                    timeout=120,
                )

            response = await loop.run_in_executor(None, _do_publish)

            if response.status_code == 200:
                version_number = response.json().get("versionNumber", "Unknown")
                console_terminal_interface.print(
                    f"[bold green]✅ Deployment Roblox Berhasil! (Versi {version_number})[/bold green]"
                )
                success_caption = (
                    f"✅ DEPLOYMENT BERHASIL! (Evolusi {evolution_level})\n"
                    f"🎮 Versi Roblox: {version_number}\n"
                    f"📦 File ini adalah versi final yang sudah aktif di server Roblox.\n"
                    f"🔗 Buka game kamu di Roblox untuk memverifikasi."
                )
                await send_telegram_notification(success_caption, important=True)
                await send_telegram_document(COMPILED_GAME_FILE, success_caption)
            else:
                # ── DEPLOYMENT GAGAL: Kirim file .rbxl ke Telegram untuk upload manual ──
                error_detail = response.text[:300] if response.text else "Tidak ada detail"
                fail_caption = (
                    f"❌ DEPLOYMENT ROBLOX GAGAL (Evolusi {evolution_level})\n"
                    f"Status: {response.status_code}\n"
                    f"Error: {error_detail}\n\n"
                    f"📥 File ini untuk UPLOAD MANUAL ke Roblox Studio:\n"
                    f"1. Download file .rbxl di atas\n"
                    f"2. Buka Roblox Studio → File → Open from File\n"
                    f"3. Publish manual via File → Publish to Roblox"
                )
                console_terminal_interface.print(f"[bold red]❌ [Deploy] Gagal! Status: {response.status_code}[/bold red]")
                await send_telegram_notification(fail_caption, important=True)
                # Kirim ulang file .rbxl dengan caption GAGAL yang jelas
                await send_telegram_document(
                    COMPILED_GAME_FILE,
                    fail_caption,
                )
        except Exception as e:
            # ── EXCEPTION saat upload: Kirim file ke Telegram ──
            exc_caption = (
                f"💥 EXCEPTION saat Deployment (Evolusi {evolution_level})\n"
                f"Error: {type(e).__name__}: {str(e)[:200]}\n\n"
                f"📥 Upload file ini secara MANUAL ke Roblox Studio."
            )
            console_terminal_interface.print(f"[bold red][Deploy] Exception: {e}[/bold red]")
            try:
                await send_telegram_notification(exc_caption, important=True)
                await send_telegram_document(COMPILED_GAME_FILE, exc_caption)
            except Exception:
                pass


def setup_rojo():
    dirs = [
        "src/ServerScriptService",
        "src/StarterPlayerScripts",
        "src/StarterCharacterScripts",
        "src/StarterGui",
        "src/ReplicatedStorage",
    ]
    for d in dirs:
        os.makedirs(os.path.join(PROJECT_ROOT_DIRECTORY, d), exist_ok=True)

    project_config = {
        "name": "ApexAbsolut",
        "tree": {
            "$className": "DataModel",
            "ServerScriptService": {"$path": "src/ServerScriptService"},
            "StarterPlayer": {
                "$className": "StarterPlayer",
                "StarterPlayerScripts": {"$path": "src/StarterPlayerScripts"},
                "StarterCharacterScripts": {"$path": "src/StarterCharacterScripts"},
            },
            "StarterGui": {"$path": "src/StarterGui"},
            "ReplicatedStorage": {"$path": "src/ReplicatedStorage"},
        },
    }
    config_path = os.path.join(PROJECT_ROOT_DIRECTORY, "default.project.json")
    with open(config_path, "w") as f:
        json.dump(project_config, f, indent=4)


async def dump_ssd():
    """Dump semua modul terverifikasi ke file di disk."""
    try:
        db = establish_database_connection()
        cur = db.cursor()
        cur.execute("SELECT filepath, code_content FROM verified_modules")
        rows = cur.fetchall()
        db.close()

        for row in rows:
            filepath = row[0]
            code_content = row[1]
            if not filepath or filepath == "memory":
                continue
            try:
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
                    await f.write(code_content)
            except Exception as e:
                console_terminal_interface.print(f"[bold yellow]Gagal dump {filepath}: {e}[/bold yellow]")
    except Exception as e:
        console_terminal_interface.print(f"[bold yellow]Gagal dump_ssd: {e}[/bold yellow]")


class DynamicTaskArchitect:
    """Arsitek AI Otonom yang membaca file selesai dan mencari gap di DevForum/Github untuk men-generate tugas baru."""

    @staticmethod
    async def analyze_and_plan_next_evolution(evolution_level: int, agent: dict) -> list:
        console_terminal_interface.print(
            f"\n[bold magenta]🔍 [Architect] Menganalisis File Ekosistem & Scraping RAG untuk Evolusi {evolution_level}...[/bold magenta]"
        )

        db = establish_database_connection()
        cur = db.cursor()
        cur.execute("SELECT module_name FROM verified_modules")
        rows = cur.fetchall()
        db.close()
        existing_modules = [r[0] for r in rows]

        devforum_data = await LuauKnowledgeScraper.search_devforum(
            "core systems needed for full roblox extraction game"
        )
        github_data = await LuauKnowledgeScraper.search_github_luau(
            "roblox game architecture framework complete"
        )
        # Tambahan: cari juga via Repository Search & Topics sebagai pelengkap
        if not github_data:
            github_data = await LuauKnowledgeScraper.search_github_repositories(
                "roblox extraction game framework"
            )
        topics_data = await LuauKnowledgeScraper.search_github_topics("roblox")

        sys_inst = (
            "Anda adalah Game Director & Arsitek Roblox Tingkat Militer. "
            "Tugas Anda: Analisis daftar modul yang sudah berhasil dibuat oleh tim programmer, "
            "baca data referensi (RAG) dari Github/DevForum tentang arsitektur game extraction end-to-end, "
            "lalu hasilkan JSON daftar tugas baru (fitur/sistem) yang BELUM ADA di ekosistem game ini."
        )

        schema = (
            "{\n"
            '  "new_tasks": [\n'
            '    {\n'
            '      "cat": "NAMA_KATEGORI_TUGAS_HURUF_BESAR",\n'
            '      "target_folder": "ServerScriptService",\n'
            '      "desc": "Instruksi spesifik dan detail tentang bentuk, warna (wajib neon/cerah), dan logika",\n'
            '      "req": ["DataStoreService"],\n'
            '      "forb": ["_G"]\n'
            '    }\n'
            '  ]\n'
            "}"
        )

        prompt_payload = (
            f"[DAFTAR MODUL YANG SUDAH SELESAI DIBUAT (JANGAN MENYURUH MEMBUAT INI LAGI)]:\n"
            f"{', '.join(existing_modules) if existing_modules else 'Belum ada modul.'}\n\n"
            f"[LIVE RAG KNOWLEDGE BASE (DEVFORUM & GITHUB)]:\n{devforum_data}\n{github_data}\n{topics_data}\n\n"
            f"BERDASARKAN DATA DI ATAS, rancang maksimal 5 sistem/tugas krusial yang HILANG atau BELUM ADA untuk melengkapi game extraction ini agar menjadi 100% End-to-End. "
            f"WAJIB KELUARKAN JSON MURNI SESUAI SCHEMA INI:\n{schema}"
        )

        env_vars = os.environ.copy()
        env_vars["GEMINI_API_KEY"] = agent["api_key"]
        env_vars["CI"] = "true"
        env_vars["TERM"] = "dumb"
        env_vars["NO_COLOR"] = "1"

        command = [
            GEMINI_CLI_PATH, "-m", "models/gemma-4-31b-it", "-y",
            "-p", "Baca stdin. Berikan output JSON murni tanpa markdown text.",
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env_vars,
            )

            full_input = f"[SYSTEM]:\n{sys_inst}\n\n[PROMPT]:\n{prompt_payload}"
            stdout_data, stderr_data = await asyncio.wait_for(
                process.communicate(input=full_input.encode("utf-8")),
                timeout=180.0,
            )

            raw_output = stdout_data.decode("utf-8", errors="ignore")

            start_idx = raw_output.find("{")
            end_idx = raw_output.rfind("}")
            if start_idx != -1 and end_idx != -1:
                clean_json = raw_output[start_idx:end_idx + 1]
                data = json.loads(clean_json)

                new_tasks = []
                valid_folders = [
                    "ServerScriptService", "StarterPlayerScripts",
                    "StarterCharacterScripts", "StarterGui", "ReplicatedStorage",
                ]
                for t in data.get("new_tasks", []):
                    cat = t.get("cat", f"SYS_{random.randint(100, 999)}")
                    target_folder = t.get("target_folder", "ServerScriptService")

                    if target_folder not in valid_folders:
                        target_folder = "ServerScriptService"

                    req_list = t.get("req", [])

                    if any(keyword in cat.upper() for keyword in ["WEAPON", "ARMOR", "ITEM", "GEAR", "TOOL", "FURNITURE"]) and "Recipe" not in req_list:
                        req_list.append("Recipe")

                    if any(keyword in cat.upper() for keyword in ["ARMOR", "HELMET"]):
                        for r in ["Durability", "ArmorTier", "MaterialType"]:
                            if r not in req_list:
                                req_list.append(r)

                    if any(keyword in cat.upper() for keyword in ["WEAPON", "ARMOR", "ITEM", "GEAR", "TOOL", "AMMUNITION", "BAIT"]):
                        for r in ["ItemCategory", "BasePrice", "ProximityPrompt"]:
                            if r not in req_list:
                                req_list.append(r)

                    if target_folder == "ServerScriptService":
                        ext = ".server.lua"
                    elif target_folder in ["StarterPlayerScripts", "StarterCharacterScripts", "StarterGui"]:
                        ext = ".client.lua"
                    elif target_folder == "ReplicatedStorage":
                        ext = ".lua"
                    else:
                        ext = ".server.lua"

                    new_tasks.append({
                        "name": f"{cat}_1",
                        "path": os.path.join(SOURCE_CODE_DIRECTORY, target_folder, cat, f"{cat}_1{ext}"),
                        "req": req_list,
                        "forb": t.get("forb", ["_G", "shared", "loadstring", "getfenv"]),
                        "desc": t.get("desc", f"Implementasi sistem {cat}"),
                    })
                console_terminal_interface.print(
                    f"[bold green]✅ Architect menemukan {len(new_tasks)} tugas baru yang harus dikerjakan![/bold green]"
                )
                return new_tasks

        except Exception as e:
            console_terminal_interface.print(f"[bold red]Architect Agent Error: {e}[/bold red]")

        # FIX: hapus return [] duplikat yang tidak pernah tercapai.
        # Sekarang ada satu titik return yang benar di sini.
        return []


def _build_task_queue():
    dynamic_tasks = [
        ("CORE_WORLD_SETTINGS", 1, "StarterPlayerScripts", "WAJIB membuat LocalScript yang mengunci kamera pemain ke First-Person (`Enum.CameraMode.LockFirstPerson`) selamanya tanpa bisa di-zoom out. Atur juga parameter gravitasi dan Lighting agar terasa seperti survival yang keras.", ["CameraMode", "LockFirstPerson"], []),
        ("DAY_NIGHT_CYCLE", 1, "ServerScriptService", "Rancang Sistem Siklus Siang, Sore, dan Malam yang dinamis. Waktu di dalam game WAJIB berputar. Lighting harus berubah drastis (gelap gulita di malam hari). Modul ini akan memengaruhi PerceptionRadius monster. WAJIB menyertakan estetika visual langit seperti sunset, halo matahari, dan aurora di bioma bersalju.", ["Lighting", "ClockTime"], []),
        ("WEATHER_DISASTER", 5, "ServerScriptService", "Rancang sistem cuaca ekstrem (Hujan Badai, Badai Pasir, Salju). WAJIB memengaruhi jarak pandang pemain dan memengaruhi atribut lingkungan. Contoh: Badai pasir di gurun pasir (mengurangi HP sedikit & mempersempit pandangan), Badai salju di gurun salju. Cuaca ekstrem WAJIB memengaruhi atribut/gameplay.", [], []),
        ("CORE_WORLD_GENERATION", 1, "ServerScriptService", "WAJIB membuat script Procedural Generation untuk membangun Baseplate dasar berukuran 2048x64x2048 dan meng-generate tanah/terrain. Gunakan warna PALING CERAH dan NEON. HUKUM PENEMPATAN AKURAT & BENTURAN: Semua tanah dan rintangan yang digenerate WAJIB diletakkan di permukaan menggunakan 'workspace:Raycast()', lalu diatur 'CanCollide = true' dan 'Anchored = true'.", ["Instance.new", "CanCollide", "Anchored", "RaycastParams"], []),
        ("BIOME_SYSTEM", 5, "ServerScriptService", "Rancang bioma lingkungan ekstrem (Banjir, Pasir, Hutan, Snow, Ocean). Bioma ini akan dibaca oleh sistem Monster sebagai 'Habitat'. HUKUM PENEMPATAN AKURAT & BENTURAN (DEVFORUM STANDARD): Saat meng-generate Pohon atau Batu, DILARANG mengandalkan kolisi bawaan MeshPart. Anda WAJIB menggunakan teknik 'Hitbox Separation'. Anda WAJIB menembakkan sinar 'workspace:Raycast()' ke arah bawah dengan 'RaycastParams' (mode Exclude pohon lain) untuk menemukan titik permukaan tanah sebelum meletakkan Hitbox. SETIAP BIOMA WAJIB BERUKURAN SEKITAR 1,300,000 x 960,000 studs (1.300 km x 960 km). Rancang ekosistem dengan ukuran raksasa ini.", ["Instance.new", "CanCollide", "Anchored", "RaycastParams", "HitboxSeparation"], []),
        ("RESOURCE_NODE_MANAGER", 1, "ServerScriptService", "Rancang Sistem Manajer Pohon dan Batu. HUKUM NODE: Semua Pohon dan Batu yang di-generate oleh BIOME_SYSTEM WAJIB ditambahkan 'IntValue' bernama 'Health' (misal: 100). Buat fungsi global yang mendengarkan pengurangan Health. Jika Health <= 0, hancurkan objek pohon/batu tersebut dan spawn wujud 3D 'RAW_MATERIAL_ITEM' (Kayu untuk Pohon, Besi Mentah/Batu untuk Rock) di posisi tersebut menggunakan teknik jatuh fisika ringan.", ["IntValue", "Health", "Instance.new"], []),
        ("ITEM_CATEGORY_DATABASE", 1, "ReplicatedStorage", "Rancang Database Kategori Item sentral. WAJIB membuat modul struktur data yang mendaftarkan Kategori resmi: 'Weapon', 'Ammunition', 'Armor', 'Medical', 'Material', 'Valuable', 'Bait', 'Tool'.", ["Weapon", "Ammunition", "Armor", "Bait", "Material", "Tool"], []),
        ("GATHERING_TOOLS", 2, "ServerScriptService", "Rancang ALAT PANEN: Kapak (Axe) dan Beliung (Pickaxe). HUKUM ALAT PANEN: Ini adalah 'Tool' yang bisa di-equip pemain. WAJIB menggunakan event '.Activated' dan menembakkan 'workspace:Raycast()' jarak dekat ke depan pemain. Kapak HANYA melukai objek ber-tag 'Tree'. Beliung HANYA melukai objek ber-tag 'Rock'. Kurangi nilai 'Health' (IntValue) dari objek tersebut saat dipukul. Set 'ItemCategory' = 'Tool'.", ["Tool", "Activated", "ItemCategory"], []),
        ("RAW_MATERIAL_ITEM", 100, "ServerScriptService", "Rancang RAW MATERIAL / BAHAN MENTAH (Sistem Resource Tiers Albion Online). HUKUM RAW MATERIAL: Item mentah WAJIB dibuat lebih dulu karena digunakan sebagai bahan baku item jadi. Contoh bahan mudah: Kayu (Wood), Batu (Stone), Kain (Cloth). Contoh menengah: Bijih Besi (Iron Ore), Kulit (Leather), Tulang (Bone). Contoh langka: Core Monster Biasa, Core Monster Epik, Core Monster Legendaris. CONTOH KODE: local Material = { ItemCategory = 'Material', BasePrice = 50, Rarity = 'Common', DropChance = 80 }; atau Rarity='Legendary', DropChance=1. Item ini HARAM memiliki atribut 'Recipe', 'Durability', atau 'ArmorTier'! Atur 'ItemCategory' = 'Material'. WAJIB buat wujud fisik 3D di tanah dengan ProximityPrompt.", ["ItemCategory", "BasePrice", "ProximityPrompt"], ["Recipe", "Durability", "ArmorTier", "Weapon"]),
        ("AMMUNITION_CALIBER", 30, "ReplicatedStorage", "Rancang modul Kaliber Peluru meniru 100% balistik Arena Breakout. HUKUM BALISTIK: Amunisi WAJIB mendefinisikan BaseDamage, PenetrationLevel (Tier 1-6), ArmorDamage, Velocity (untuk Bullet Drop Curve), dan BaseDrop (gravitasi spesifik peluru). CONTOH KODE: local Ammo = { Caliber='5.56x45mm', Name='M995', BaseDamage = 41, PenetrationLevel = 5, ArmorDamage = 27, Velocity = 1013, BaseDrop = 0.98, FleshDamageMultiplier = 1.0 }. Peluru Flesh/DumDum memiliki BaseDamage tinggi tapi PenetrationLevel 0. WAJIB punya wujud fisik 3D kotak amunisi dengan ProximityPrompt.", ["BaseDamage", "PenetrationLevel", "ItemCategory", "BasePrice", "ProximityPrompt", "Anchored"], ["Recipe", "Weapon"]),
        ("MODERN_ARMOR_HELMET", 25, "ServerScriptService", "Rancang BARANG JADI: Helm Taktis Militer & Rompi Anti-Peluru Modern (Meniru Arena Breakout). HUKUM ARMOR MODERN: WAJIB memiliki 'Recipe' yang logis (Rompi Tier 6 butuh bahan Ceramic/Titanium langka). CONTOH KODE: local Recipe = {CeramicPlate = 4, Kevlar = 5}; local Durability = 80; local ArmorTier = 5; local MaterialType = 'Ceramic'; local ErgoPenalty = -5. Material memengaruhi repairability. Set 'ItemCategory' = 'Armor'. HUKUM VISUAL EQUIP: Model 3D di tanah WAJIB dipasangkan 'ProximityPrompt'. Saat ditekan, Armor 3D WAJIB di-WeldConstraint ke UpperTorso karakter pemain!", ["Recipe", "Durability", "ArmorTier", "MaterialType", "ItemCategory", "BasePrice", "ProximityPrompt", "HitboxSeparation"], []),
        ("FANTASY_ARMOR_HELMET", 25, "ServerScriptService", "Rancang BARANG JADI: Jubah Penyihir & Zirah Ksatria Kuno (Fantasy Theme). HUKUM ARMOR FANTASY: Sistem armor ini terinspirasi Albion Online. WAJIB memiliki Tier (T1-T8), Quality (Masterpiece, dll). Armor kuat WAJIB pakai bahan mahal (Core Monster, Leather kualitas tinggi). Armor memiliki kekuatan pasif. CONTOH KODE: local Recipe = {Leather = 10, EpicMonsterCore = 1}; local Durability = 100; local ArmorTier = 5; local MaterialType = 'Leather'; local PassiveEffect = 'MaxHP +200'. Set 'ItemCategory' = 'Armor'. HUKUM VISUAL EQUIP: Wujud 3D di tanah dipasangkan 'ProximityPrompt'. WAJIB di-weld ke badan pemain saat dipungut.", ["Recipe", "Durability", "ArmorTier", "MaterialType", "ItemCategory", "BasePrice", "ProximityPrompt", "HitboxSeparation"], []),
        ("MODERN_WEAPON", 20, "ServerScriptService", "Rancang BARANG JADI: Senjata Api Modern (Assault Rifle, Sniper) meniru Arena Breakout. HUKUM MODERN WEAPON: HARAM memiliki variabel Damage langsung! Senjata ini WAJIB menembakkan peluru fisik (Raycast/FastCast) YANG DIPENGARUHI GRAVITASI (Bullet Drop) dan WAKTU TEMPUH (Travel Time) berdasarkan Velocity peluru. Senjata kuat pakai bahan rumit (Steel, GunParts). CONTOH KODE: local Recipe = {Steel = 10, WeaponParts = 3}; local CompatibleCaliber = '5.56x45mm'; local FireRate = 600; local Ergonomics = 65; local EffectiveRange = 100; local Recoil = {Vertical = 70, Horizontal = 60}. WAJIB punya 'ItemCategory' = 'Weapon'. HUKUM VISUAL EQUIP: Pasang ProximityPrompt. WAJIB di-WeldConstraint ke tangan pemain saat dipakai. TULISKAN `rbxassetid://<id>` BILA PERLU AGAR DIUNDUH AI SECARA OTONOM.", ["Recipe", "CompatibleCaliber", "ItemCategory", "BasePrice", "ProximityPrompt", "HitboxSeparation"], ["BaseDamage"]),
        ("FANTASY_WEAPON", 20, "ServerScriptService", "Rancang BARANG JADI: Senjata Sihir/Pedang Ksatria (Fantasy Theme). HUKUM FANTASY WEAPON: WAJIB memiliki Tier crafting (T1-T8) dan Quality (Masterpiece, dll) terinspirasi dari Albion Online. Senjata lemah menggunakan bahan mudah (Wood/Stone). Senjata kuat WAJIB menggunakan bahan langka (Core Monster Legendaris) dan MEMILIKI KEKUATAN SPESIAL (Lifesteal/Terbakar). CONTOH KODE: local Recipe = {Wood = 5, IronOre = 2, LegendaryMonsterCore = 1}; local MagicEffect = 'Lifesteal 15%'; local ItemCategory = 'Weapon'. HUKUM VISUAL EQUIP: Pasang ProximityPrompt. WAJIB di-WeldConstraint ke tangan karakter pemain.", ["Recipe", "ItemCategory", "BasePrice", "ProximityPrompt", "HitboxSeparation"], ["CompatibleCaliber"]),
        ("CORE_INVENTORY_SYSTEM", 1, "ServerScriptService", "Rancang Sistem Inventory Kustom khusus Extraction Game. DILARANG KERAS menggunakan Backpack bawaan Roblox. WAJIB membagi inventory menjadi 3 kompartemen struktur data di Server: 'MainBackpack' (Tas tempur, hilang 100% saat mati), 'SafeContainer' (Tas kecil aman saat mati), dan 'LobbyStorage' (Gudang Stash permanen di Lobby yang menampung barang beli/jual, TIDAK BISA dibawa ke arena tempur). HUKUM PERSISTENSI DATA MUTLAK: Barang di SafeContainer dan LobbyStorage TIDAK BOLEH HILANG saat pemain keluar game. WAJIB menggunakan event `Players.PlayerAdded` untuk me-load data dan `Players.PlayerRemoving` untuk me-save data ke DataStoreService dengan pcall().", ["DataStoreService", "pcall", "MainBackpack", "SafeContainer", "LobbyStorage", "PlayerRemoving", "PlayerAdded"], ["StarterGear"]),
        ("CORE_INBOX_SYSTEM", 1, "ServerScriptService", "Rancang Sistem Kotak Masuk (Inbox/Mailbox) mirip Arena Breakout. Bertindak sebagai penampung sementara dan aman untuk pemain. Data Inbox WAJIB disimpan di DataStoreService.", ["DataStoreService", "pcall", "Inbox"], []),
        ("CORE_INBOX_UI", 1, "StarterGui", "Rancang UI untuk Kotak Masuk (Inbox) dengan ikon Amplop.", ["RemoteFunction"], []),
        ("MONSTER", 50, "ServerScriptService", "Rancang monster/hewan unik. HUKUM EKOLOGI DUNIA NYATA: WAJIB mendefinisikan 'Diet', 'SocialBehavior', 'SpawnWeight', 'Habitat', 'LocomotionType' (Terrestrial, Aerial, Aquatic), dan 'DropTable' (Jika mati, harus men-spawn wujud fisik dari RAW_MATERIAL_ITEM yang terdaftar agar pemain bisa memungutnya). HUKUM RANTAI MAKANAN: Omnivora/Karnivora WAJIB memindai radius sekitarnya untuk mencari item fisik berlabel 'Bait' untuk dimakan. HUKUM MOTORIK: Gunakan PathfindingService. MUSUH HANYA MONSTER, DILARANG KERAS MENGGUNAKAN HUMANOID MANUSIA ATAU SENJATA API.", ["PathfindingService", "Humanoid", "Diet", "SocialBehavior", "SpawnWeight", "Habitat", "DropTable", "Stamina", "PerceptionRadius", "LocomotionType"], ["Motor6D", "Scavenger"]),
        ("LOBBY_SPACESHIP", 1, "ServerScriptService", "Rancang lobby di pesawat luar angkasa besar dengan domain investor. HUKUM FISIKA LOBBY: Lobby ini BUKAN Bioma! Bangun pesawat menggunakan blok Part biasa di langit/luar angkasa (Y = 10000). DILARANG KERAS menggunakan workspace:Raycast() ke tanah karena ini di angkasa. Namun lantai/dinding pesawat WAJIB CanCollide = true dan Anchored = true. KAPAL LUAR ANGKASA INI ADALAH ZONA AMAN TEMPAT 8 NPC TRADER MANUSIA BERADA.", ["Anchored", "CanCollide"], ["Terrain"]),
        ("FURNITURE", 50, "ServerScriptService", "Rancang furnitur lobby pesawat. Warna wajib putih cerah atau neon. HUKUM FISIKA: Furnitur diletakkan di dalam Lobby Pesawat (Y=10000), DILARANG Raycast ke tanah bumi. Furnitur WAJIB menggunakan 'HitboxSeparation', 'CanCollide = true' (di hitbox), dan 'Anchored = true'.", ["Anchored", "CanCollide", "HitboxSeparation"], ["Raycast"]),
        ("SMELTING_FURNACE", 1, "ServerScriptService", "Rancang Mesin Peleburan Logam (Furnace) di Lobby. HUKUM SMELTING: Mesin ini memiliki wujud fisik 3D dan 'ProximityPrompt' (ActionText='Lebur Besi'). Jika pemain membawa 'Besi Mentah' (Raw Iron) di inventory, mesin akan menghapusnya dari inventory, memutar animasi/partikel api selama beberapa detik menggunakan 'task.wait()', lalu men-spawn 'Iron Ingot' (Besi Matang) di depan mesin agar bisa dipungut pemain. FUNGSI BENGKEL: Pemain dapat membuat/menempa senjata dan item jadi menggunakan bahan mentah (Iron, Core Monster, dll), TETAPI WAJIB MEMBAYAR FEE (Biaya) ke NPC pemilik bengkel.", ["ProximityPrompt", "task.wait", "ParticleEmitter"], []),
        ("NPC_TRADER", 8, "ServerScriptService", "Rancang Skrip Server 8 NPC Trader terspesialisasi: 1. Blacksmith (Dekat Furnace, jual Besi/Armor), 2. Woodworker (Jual Kayu/Kapak), 3. Stonemason (Jual Batu/Beliung), 4. Gunsmith (Jual Senjata Api/Peluru), 5. Medic (Medical), 6. Chef (Daging/Makanan), 7. Scientist (Material langka), 8. Black Market (Valuable). HUKUM NPC HIDUP: NPC DILARANG menjadi patung statis! Mereka WAJIB dipasangkan alat kerja (Palu, Gergaji, dll) di tangan mereka menggunakan `WeldConstraint`. HARGA JUAL NPC = BasePrice * 2.0. HARGA BELI DARI PEMAIN = BasePrice * 0.4. NPC TRADER WAJIB BERWUJUD MANUSIA (HANYA ADA 8 MANUSIA DI GAME INI) DAN BERADA DI KAPAL LUAR ANGKASA.", ["Recipe", "ProximityPrompt", "BasePrice", "ItemCategory", "RemoteEvent", "WeldConstraint"], ["TakeDamage"]),
        ("NPC_SHOP_UI", 1, "StarterGui", "Rancang UI Katalog Belanja untuk NPC Trader.", ["RemoteEvent", "RemoteFunction"], []),
        ("FLEA_MARKET_BACKEND", 1, "ServerScriptService", "Rancang Backend Server Keamanan untuk Pasar Loak (Shopee pemain) menggunakan pcall.", ["RemoteFunction", "DataStoreService", "pcall", "Inbox"], []),
        ("PLAYER_FLEA_MARKET_UI", 1, "StarterGui", "Rancang UI Pasar Loak (Flea Market / Shopee antar pemain).", ["ItemCategory", "TextBox", "RemoteFunction"], []),
        ("HEALTH_AND_WOUND_SYSTEM", 1, "ServerScriptService", "Rancang Sistem Kesehatan mirip Arena Breakout. WAJIB membagi HP menjadi spesifik bagian tubuh: Head (35), Thorax (85), Stomach (70), LeftArm (60), RightArm (60), LeftLeg (65), RightLeg (65). Jika Head/Thorax mencapai 0, pemain langsung mati. Bagian lain yang 0 akan menyebabkan status 'Broken' (Patah Tulang, efek layar hitam/kabur & lambat berjalan). Tambahkan sistem status 'Bleeding' (Pendarahan) yang terus mengurangi HP.", ["Humanoid", "Head", "Thorax", "Stomach", "Bleeding", "Broken", "Pain"], []),
        ("MEDICAL_ITEM_SYSTEM", 10, "ServerScriptService", "Rancang Sistem Item Medis penyembuh (Bandage, Surgical Kit, Medkit, Painkiller, Potion/Food) meniru Arena Breakout & Albion Online. HUKUM MEDIS: 'Bandage' hanya menyembuhkan status 'Bleeding'. 'Surgical Kit' hanya menyembuhkan status 'Broken' pada anggota tubuh tertentu (misal kaki yang patah). 'Medkit' menambah HP murni pada bagian tubuh tertentu. 'Potion/Food' memberikan regenerasi HP pelan/Stamina (Albion style). CONTOH KODE: local Medical = { ItemCategory = 'Medical', HealTarget = 'Bleeding', UseTime = 3, Charges = 4 }. WAJIB menggunakan 'ProximityPrompt' untuk item fisik di tanah.", ["ItemCategory", "HealTarget", "UseTime", "Charges", "ProximityPrompt"], []),
        ("DYNAMIC_HUD_INTERFACE", 1, "StarterGui", "Rancang UI HUD (Heads-Up Display) dan Control Layout meniru 100% Arena Breakout Mobile. HUKUM HUD FPS: WAJIB berisi tombol aksi lengkap (Tembak Kiri/Kanan, Scope/ADS, Lompat, Jongkok, Tiarap, Reload, Buka Tas, Medis, Miring Kanan/Kiri). WAJIB ada indikator HP per-anggota tubuh (Head, Thorax, Limbs), Minimap, dan jumlah Amunisi. HUKUM KUSTOMISASI KONTROL: WAJIB ada menu 'Customize Layout' dimana pemain bisa menyeret (Drag), mengubah ukuran (Resize), dan mengatur Transparansi (Opacity) semua tombol tersebut dan menyimpannya. WAJIB sediakan preset tata letak (Thumb Setup 2 Jari, 3 Finger Claw, 4 Finger Claw, 5 Finger Claw).", ["ScreenGui", "TextButton", "ImageButton", "UIListLayout", "UIPadding"], []),
        ("CORE_MISSION_SYSTEM", 1, "ServerScriptService", "Rancang Sistem Misi Harian dan Event Mingguan (Quest System).", ["Inbox", "Mission"], []),
        ("CORE_MISSION_UI", 1, "StarterGui", "Rancang UI Daftar Misi mendengarkan RemoteEvent.", ["RemoteEvent"], []),
        ("CORE_MONETIZATION_SYSTEM", 1, "ServerScriptService", "Rancang sistem monetisasi sentral. HUKUM ANTI-P2W MUTLAK: `MarketplaceService.ProcessReceipt` HANYA BOLEH dideklarasikan di SATU skrip ini.", ["MarketplaceService", "ProcessReceipt", "DataStoreService", "pcall"], ["Sword", "Gun", "Armor", "Weapon"]),
        ("AUTONOMOUS_GAP_ANALYSIS", 1, "ServerScriptService", "Analisis ekosistem game saat ini. Secara otonom bangun sistem fundamental tambahan.", [], []),
        ("DAILY_LOG_SYSTEM", 1, "ServerScriptService", "Rancang sistem log harian.", [], []),
        ("AUDIO_SYSTEM", 1, "StarterPlayerScripts", "Rancang sistem audio client untuk BGM dan SFX.", [], []),
    ]

    task_queue = []
    for cat, amt, target_folder, desc, req, forb in dynamic_tasks:
        for i in range(1, amt + 1):
            if target_folder == "ServerScriptService":
                ext = ".server.lua"
            elif target_folder in ["StarterPlayerScripts", "StarterCharacterScripts", "StarterGui"]:
                ext = ".client.lua"
            elif target_folder == "ReplicatedStorage":
                ext = ".lua"
            else:
                ext = ".server.lua"

            task_queue.append({
                "name": f"{cat}_{i}",
                "path": os.path.join(SOURCE_CODE_DIRECTORY, target_folder, cat, f"{cat}_{i}{ext}"),
                "req": req,
                "forb": forb,
                "desc": desc,
            })
    return task_queue



# ══════════════════════════════════════════════════════════════════════════════
# ⚡ ANTIGRAVITY-STYLE PARALLEL EXECUTION SYSTEM
# Setiap task mendapat agent sendiri, berjalan bersamaan, tidak saling tunggu.
# File-lock mencegah dua agent menulis file yang sama secara bersamaan.
# ══════════════════════════════════════════════════════════════════════════════

_FILE_LOCKS: dict = {}
_FILE_LOCKS_MUTEX = asyncio.Lock()

async def _get_file_lock(path: str) -> asyncio.Lock:
    """Satu Lock per path file — mencegah tabrakan tulis antar agent."""
    async with _FILE_LOCKS_MUTEX:
        if path not in _FILE_LOCKS:
            _FILE_LOCKS[path] = asyncio.Lock()
        return _FILE_LOCKS[path]


_STUCK_LOOP_TEMPLATES: dict = {
    "DYNAMIC_HUD_INTERFACE": (
        "--!strict\n"
        "local Players = game:GetService(\"Players\")\n"
        "local LocalPlayer = Players.LocalPlayer or Players.PlayerAdded:Wait()\n"
        "local PlayerGui = LocalPlayer:WaitForChild(\"PlayerGui\")\n"
        "\n"
        "local ScreenGui = Instance.new(\"ScreenGui\")\n"
        "ScreenGui.Name = \"DynamicHUDInterface\"\n"
        "ScreenGui.Parent = PlayerGui\n"
        "\n"
        "local MainFrame = Instance.new(\"Frame\")\n"
        "MainFrame.Name = \"MainFrame\"\n"
        "MainFrame.Size = UDim2.new(0.2, 0, 0.1, 0) -- Contoh ukuran\n"
        "MainFrame.Position = UDim2.new(0.5, -MainFrame.Size.X.Scale/2, 0.1, 0) -- Contoh posisi\n"
        "MainFrame.BackgroundColor3 = Color3.fromRGB(50, 50, 50)\n"
        "MainFrame.BorderSizePixel = 0\n"
        "MainFrame.Parent = ScreenGui\n"
        "\n"
        "local TitleLabel = Instance.new(\"TextLabel\")\n"
        "TitleLabel.Name = \"TitleLabel\"\n"
        "TitleLabel.Size = UDim2.new(1, 0, 0.3, 0)\n"
        "TitleLabel.Position = UDim2.new(0, 0, 0, 0)\n"
        "TitleLabel.BackgroundColor3 = Color3.fromRGB(70, 70, 70)\n"
        "TitleLabel.TextColor3 = Color3.fromRGB(255, 255, 255)\n"
        "TitleLabel.Font = Enum.Font.SourceSansBold\n"
        "TitleLabel.TextSize = 20\n"
        "TitleLabel.Text = \"Dynamic HUD\"\n"
        "TitleLabel.Parent = MainFrame\n"
        "\n"
        "-- Fungsi untuk mengupdate HUD (contoh)\n"
        "local function updateHUD()\n"
        "    -- Logika update di sini\n"
        "end\n"
        "\n"
        "-- Contoh koneksi event (jika ada)\n"
        "-- game:GetService(\"RunService\").Heartbeat:Connect(updateHUD)\n"
        "\n"
        "return ScreenGui\n"
    ),
    "MESHPART_CREATURE": (
        "--!strict\n"
        "local Part = Instance.new(\"Part\")\n"
        "Part.Name = \"CreatureMeshPart\"\n"
        "Part.Size = Vector3.new(4, 4, 4) -- Contoh ukuran\n"
        "Part.Position = Vector3.new(0, 10, 0) -- Contoh posisi\n"
        "Part.Anchored = false\n"
        "Part.CanCollide = true\n"
        "Part.Transparency = 0.5\n"
        "Part.Color = Color3.fromRGB(255, 0, 255) -- Contoh warna (placeholder)\n"
        "Part.Material = Enum.Material.Plastic\n"
        "Part.Parent = game.Workspace\n"
        "\n"
        "local SpecialMesh = Instance.new(\"SpecialMesh\")\n"
        "SpecialMesh.MeshType = Enum.MeshType.Sphere -- Contoh bentuk mesh\n"
        "SpecialMesh.Scale = Vector3.new(1, 1, 1)\n"
        "SpecialMesh.Parent = Part\n"
        "\n"
        "-- Logika tambahan untuk creature (misalnya, AI, pergerakan)\n"
        "local function moveCreature()\n"
        "    Part.Position += Vector3.new(0.1, 0, 0)\n"
        "end\n"
        "\n"
        "-- game:GetService(\"RunService\").Heartbeat:Connect(moveCreature)\n"
        "\n"
        "return Part\n"
    ),
    "MODEL_PROP": (
        "--!strict\n"
        "local Model = Instance.new(\"Model\")\n"
        "Model.Name = \"PropModel\"\n"
        "Model.Parent = game.Workspace\n"
        "\n"
        "local PrimaryPart = Instance.new(\"Part\")\n"
        "PrimaryPart.Name = \"PrimaryPart\"\n"
        "PrimaryPart.Size = Vector3.new(5, 5, 5)\n"
        "PrimaryPart.Position = Vector3.new(0, 5, 0)\n"
        "PrimaryPart.Anchored = true\n"
        "PrimaryPart.CanCollide = true\n"
        "PrimaryPart.Color = Color3.fromRGB(100, 100, 100)\n"
        "PrimaryPart.Parent = Model\n"
        "Model.PrimaryPart = PrimaryPart\n"
        "\n"
        "local Script = Instance.new(\"Script\")\n"
        "Script.Name = \"PropLogic\"\n"
        "Script.Parent = Model\n"
        "Script.Source = [[\n"
        "--!strict\n"
        "local part = script.Parent.PrimaryPart\n"
        "print(\"PropModel siap!\")\n"
        "-- Tambahkan logika model di sini\n"
        "]]\n"
        "\n"
        "return Model\n"
    ),
    "WORLD_ENVIRONMENT": (
        "--!strict\n"
        "local Lighting = game:GetService(\"Lighting\")\n"
        "local Workspace = game:GetService(\"Workspace\")\n"
        "\n"
        "Lighting.Brightness = 1 -- Contoh pengaturan cahaya\n"
        "Lighting.OutdoorAmbient = Color3.fromRGB(100, 100, 100)\n"
        "Lighting.FogEnd = 1000\n"
        "Lighting.FogColor = Color3.fromRGB(150, 150, 150)\n"
        "\n"
        "local Terrain = Workspace:GetService(\"Terrain\")\n"
        "Terrain:FillBlock(CFrame.new(0, -10, 0), Vector3.new(1000, 20, 1000), Enum.Material.Grass)\n"
        "\n"
        "local SpawnLocation = Instance.new(\"SpawnLocation\")\n"
        "SpawnLocation.Name = \"InitialSpawn\"\n"
        "SpawnLocation.Position = Vector3.new(0, 5, 0)\n"
        "SpawnLocation.Parent = Workspace\n"
        "\n"
        "print(\"Lingkungan dunia diinisialisasi!\")\n"
        "\n"
        "return Lighting\n"
    ),

    "CORE_PERSISTENCE": (
        '--!strict\n'
        'local DataStoreService = game:GetService("DataStoreService")\n'
        'local Players = game:GetService("Players")\n'
        'local RunService = game:GetService("RunService")\n'
        '\n'
        'local PlayerDataStore = DataStoreService:GetDataStore("PlayerPersistence")\n'
        '\n'
        'local PlayerDataModule = {}\n'
        'PlayerDataModule.__index = PlayerDataModule\n'
        '\n'
        'local activeSessionData: {[number]: {[string]: any}} = {}\n'
        '\n'
        'function PlayerDataModule.LoadPlayerData(player: Player): {[string]: any}\n'
        '    local userId: number = player.UserId\n'
        '    local success: boolean, result: any = pcall(function()\n'
        '        return PlayerDataStore:GetAsync("Player_" .. tostring(userId))\n'
        '    end)\n'
        '    local data: {[string]: any} = if success and typeof(result) == "table" then result else {Coins = 0, Level = 1, Inventory = {}}\n'
        '    activeSessionData[userId] = data\n'
        '    return data\n'
        'end\n'
        '\n'
        'function PlayerDataModule.SavePlayerData(player: Player): boolean\n'
        '    local userId: number = player.UserId\n'
        '    local data: {[string]: any}? = activeSessionData[userId]\n'
        '    if not data then return false end\n'
        '    local success: boolean, err: any = pcall(function()\n'
        '        PlayerDataStore:SetAsync("Player_" .. tostring(userId), data)\n'
        '    end)\n'
        '    if not success then warn("[Persistence] Save gagal: " .. tostring(err)) end\n'
        '    return success\n'
        'end\n'
        '\n'
        'local playerAddedConnection: RBXScriptConnection = Players.PlayerAdded:Connect(function(player: Player)\n'
        '    PlayerDataModule.LoadPlayerData(player)\n'
        'end)\n'
        '\n'
        'local playerRemovingConnection: RBXScriptConnection = Players.PlayerRemoving:Connect(function(player: Player)\n'
        '    PlayerDataModule.SavePlayerData(player)\n'
        '    activeSessionData[player.UserId] = nil\n'
        'end)\n'
        '\n'
        'local heartbeatConnection: RBXScriptConnection = RunService.Heartbeat:Connect(function()\n'
        'end)\n'
        '\n'
        'return PlayerDataModule\n'
    ),
}


def _get_stuck_loop_template_override(task_name: str, forb_keys: list) -> str:
    _task_category = "_".join(task_name.split("_")[:-1]) if "_" in task_name else task_name
    template = _STUCK_LOOP_TEMPLATES.get(_task_category, "")
    if template:
        for fk in forb_keys:
            if fk in template:
                return ""
    return template


async def _run_task_parallel(
    task_num: int,
    total_tasks: int,
    task: dict,
    dedicated_agent: dict,
    synthesizer,
    generation_counter: int,
    evolution_level: int,
) -> tuple:
    """
    ⚡ PARALLEL WORKER — Satu task, satu agent dedicated, berjalan bebas.
    Tidak berbagi agent dengan task lain = tidak ada antrian.
    File-lock mencegah tabrakan jika dua task menulis path yang sama.
    Healer berjalan per-task secara independen.
    """
    task_name = task["name"]
    task_path = task["path"]

    # ── Resume check ──────────────────────────────────────────────────────
    if os.path.exists(task_path) and os.path.getsize(task_path) > 50:
        _db_resume = establish_database_connection()
        _cur_resume = _db_resume.cursor()
        _cur_resume.execute(
            "SELECT module_name FROM verified_modules WHERE module_name = ?",
            (task_name,)
        )
        _row_resume = _cur_resume.fetchone()
        _db_resume.close()

        if _row_resume:
            console_terminal_interface.print(
                f"[dim green]⏭️  [RESUME] '{task_name}' sudah selesai. Dilewati.[/dim green]"
            )
            return (True, task_name, "resumed")
        else:
            try:
                with open(task_path, "r", encoding="utf-8") as _f_r:
                    _existing_code = _f_r.read()
                if len(_existing_code.strip()) > 50:
                    await save_verified_module(task_name, task_path, _existing_code)
                    console_terminal_interface.print(
                        f"[dim cyan]🔄 [RESUME-SYNC] '{task_name}' disinkronkan ke DB.[/dim cyan]"
                    )
                    return (True, task_name, "synced")
            except Exception:
                pass

    # ── Eksekusi dengan dedicated agent + file-lock ───────────────────────
    # ATURAN: Task WAJIB selesai 100% — tidak ada batas retry, tidak ada skip.
    completed = False
    prev_err = ""
    prev_code = ""
    real_attempt_count = 0

    import hashlib as _hashlib_loop
    _error_repeat_tracker: dict = {}
    # Setelah 5x error identik → suntikkan template override atau ganti strategi prompt
    # Tapi TIDAK PERNAH skip — loop terus sampai completed = True
    _MAX_SAME_ERROR_RETRIES = 5

    file_lock = await _get_file_lock(task_path)

    # Loop tanpa batas — keluar HANYA jika completed = True atau agent di-stop (/stop)
    while not completed:

        # Cek pause dari Telegram (/stop)
        if not _roblox_agent_paused.is_set():
            console_terminal_interface.print(
                f"[bold yellow]  [{dedicated_agent['name']}] PAUSE — menunggu /continue ...[/bold yellow]"
            )
            import asyncio as _aio_local
            loop = _aio_local.get_running_loop()
            await loop.run_in_executor(None, _roblox_agent_paused.wait)
            console_terminal_interface.print(
                f"[bold green]  [{dedicated_agent['name']}] RESUME — melanjutkan {task_name}[/bold green]"
            )

        # Cek stop global
        if not NexusGlobalState.is_running:
            return (False, task_name, "STOPPED_BY_USER")

        console_terminal_interface.print(
            f"[bold cyan]  [{dedicated_agent['name']}] "
            f"Task {task_num}/{total_tasks}: {task_name} — "
            f"Percobaan {real_attempt_count + 1} (infinite retry)[/bold cyan]"
        )
        try:
            async with file_lock:
                completed, prev_err, prev_code = await synthesizer.synthesize_handoff(
                    dedicated_agent,
                    task_path,
                    task_name,
                    task["desc"],
                    task["req"],
                    task["forb"],
                    prev_err,
                    prev_code,
                )
        except Exception as exc:
            prev_err = f"EXCEPTION: {str(exc)}"
            completed = False

        if completed:
            await send_telegram_notification(
                f"✅ [{dedicated_agent['name']}] TASK SELESAI\n"
                f"📄 {task_name}\n"
                f"🔁 Evolusi {evolution_level} | Siklus {generation_counter}",
                important=False,
            )
            return (True, task_name, "done")

        # Rate limit → tunggu lebih lama
        if "RATE_LIMIT" in prev_err:
            console_terminal_interface.print(
                f"[bold yellow]  [{dedicated_agent['name']}] Rate limit → retry 30s...[/bold yellow]"
            )
            await asyncio.sleep(30)
            continue

        _err_hash = _hashlib_loop.md5(prev_err.strip().encode()).hexdigest()[:12]
        _error_repeat_tracker[_err_hash] = _error_repeat_tracker.get(_err_hash, 0) + 1

        # Error identik berulang → suntikkan template atau ganti strategi
        if _error_repeat_tracker[_err_hash] >= _MAX_SAME_ERROR_RETRIES:
            console_terminal_interface.print(
                f"[bold red]  [STUCK LOOP] Error identik {_MAX_SAME_ERROR_RETRIES}x untuk {task_name}. "
                f"Suntikkan template / ganti strategi...[/bold red]"
            )
            _override = _get_stuck_loop_template_override(task_name, task["forb"])
            if _override:
                prev_err = (
                    f"[STUCK LOOP OVERRIDE — ITERASI KE-{_error_repeat_tracker[_err_hash]}]\n"
                    f"Error sebelumnya SAMA PERSIS sudah {_MAX_SAME_ERROR_RETRIES}x. "
                    f"AI WAJIB menggunakan template kode berikut TANPA MODIFIKASI:\n"
                    f"{_override}\n"
                    f"DILARANG mengubah template di atas. Salin PERSIS ke output.\n"
                    f"Error asli: {prev_err[:300]}"
                )
                console_terminal_interface.print(
                    f"[bold magenta]  [TEMPLATE OVERRIDE] Template darurat disuntikkan untuk {task_name}[/bold magenta]"
                )
            else:
                # Tidak ada template → ganti strategi dengan prompt yang berbeda total
                prev_err = (
                    f"[GANTI STRATEGI — PERCOBAAN KE-{real_attempt_count + 1}]\n"
                    f"Pendekatan sebelumnya gagal {_MAX_SAME_ERROR_RETRIES}x dengan error yang sama.\n"
                    f"WAJIB gunakan pendekatan BERBEDA TOTAL — tulis ulang dari nol.\n"
                    f"Error asli: {prev_err[:200]}"
                )
                # Reset error tracker untuk strategi baru
                _error_repeat_tracker.clear()
                console_terminal_interface.print(
                    f"[bold yellow]  [STRATEGI BARU] Reset prompt, pendekatan berbeda untuk {task_name}[/bold yellow]"
                )

            await send_telegram_notification(
                f"🔄 [{dedicated_agent['name']}] Stuck loop terdeteksi → ganti strategi\n"
                f"📄 {task_name} | Percobaan ke-{real_attempt_count + 1}",
                important=True,
            )

        real_attempt_count += 1
        backoff_delay = min(real_attempt_count * 2, 30)
        await asyncio.sleep(backoff_delay)

    return (False, task_name, prev_err[:120])


async def nexus_startup_sequence():
    """
    Jalankan saat sistem pertama kali dinyalakan.
    Scan mendalam isi semua file Lua — bukan hanya nama.
    """
    console_terminal_interface.print("[bold green]Nexus AI v2.0 Startup Sequence...[/bold green]")

    # Scan mendalam + auto-fix
    validate_result = await scan_deep_validate(auto_fix=True)

    console_terminal_interface.print(
        f"[bold cyan]Startup Scan Selesai:[/bold cyan]\n"
        f"  Total: {validate_result['total']} file\n"
        f"  Valid: {validate_result['valid']} file\n"
        f"  Auto-fix: {validate_result['fixed']} file\n"
        f"  Perlu AI: {len(validate_result['need_ai'])} file"
    )

    if validate_result["need_ai"]:
        console_terminal_interface.print("[bold yellow]File yang perlu perbaikan AI:[/bold yellow]")
        for item in validate_result["need_ai"][:5]:
            console_terminal_interface.print(f"  - {item['file']}: {', '.join(item['issues'])}")

    # Notif ke Telegram
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            msg = (
                "Nexus AI Agent v2.0 Menyala!\n\n"
                "Startup Scan Selesai:\n"
                "Total: " + str(validate_result["total"]) + " file Lua\n"
                "Auto-fix: " + str(validate_result["fixed"]) + " file\n"
                "Perlu AI fix: " + str(len(validate_result["need_ai"])) + " file\n\n"
                + ("Kirim /autofix untuk perbaiki file yang butuh AI." if validate_result["need_ai"] else "Semua file valid! Agent siap.")
            )
            if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                await session.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
                    timeout=aiohttp.ClientTimeout(total=30),
                )
    except Exception as e:
        console_terminal_interface.print(f"[yellow]Notif Telegram gagal: {e}[/yellow]")

async def run_orchestrator():
    try:
        await nexus_startup_sequence()
        await initialize_system_ledger()
        setup_rojo()
        NativeLuauCompiler.ensure_compiler_exists()

        asyncio.create_task(start_telemetry_webhook())
        asyncio.create_task(start_telegram_polling())  # Polyglot Telegram Listener (Non-Blocking)

        healer = AutoHealerAgent()
        await healer.initialize_and_scan()
        synthesizer = OmniSynthesizerAgent(healer)

        evolution_level = 1
        generation_counter = 1

        agent_idx = random.randint(0, len(ACTIVE_AGENTS) - 1) if ACTIVE_AGENTS else 0

        while True:
            console_terminal_interface.print(
                Panel(f"[bold magenta]=== EVOLUSI LEVEL {evolution_level} - SIKLUS KE-{generation_counter} ===[/bold magenta]")
            )

            if evolution_level == 1:
                task_queue = _build_task_queue()
            else:
                current_agent_architect = ACTIVE_AGENTS[agent_idx % len(ACTIVE_AGENTS)]
                agent_idx += 1
                task_queue = await DynamicTaskArchitect.analyze_and_plan_next_evolution(
                    evolution_level, current_agent_architect
                )

                if not task_queue:
                    console_terminal_interface.print(
                        "[bold yellow]Architect menyimpulkan game sudah lengkap atau terjadi limit. Memuat ulang ekspansi kosmetik...[/bold yellow]"
                    )
                    task_queue = [
                        {
                            "name": f"AUTONOMOUS_EXPANSION_{evolution_level}",
                            "path": os.path.join(
                                SOURCE_CODE_DIRECTORY, "StarterPlayerScripts",
                                f"EXPANSION_{evolution_level}.client.lua",
                            ),
                            "req": [],
                            "forb": ["_G", "shared"],
                            "desc": "Berdasarkan ekosistem yang ada, ciptakan sistem kosmetik atau optimasi baru yang membuat game ini mencapai level AAA.",
                        }
                    ]

            total_tasks = len(task_queue)

            n_workers = len(ACTIVE_AGENTS)  # Maks concurrent = jumlah API key

            console_terminal_interface.print(
                Panel(
                    f"[bold cyan]⚡ WORKER POOL AKTIF\n"
                    f"{total_tasks} task dalam antrian — {n_workers} worker berjalan paralel\n"
                    f"Setiap worker ambil 1 task → kerjakan sampai LULUS → ambil berikutnya\n"
                    f"Evolusi {evolution_level} | Siklus {generation_counter}[/bold cyan]"
                )
            )

            # Notifikasi Telegram: evolusi dimulai
            await send_telegram_notification(
                f"🚀 EVOLUSI {evolution_level} DIMULAI\n"
                f"📋 {total_tasks} task dalam antrian\n"
                f"👥 {n_workers} worker paralel (1 per API key)\n"
                f"🔁 Siklus ke-{generation_counter}",
                important=True,
            )

            # ── Worker Pool: N worker, masing-masing ambil task dari antrian ──
            # Tidak ada asyncio.gather(*semua_task) — hanya N coroutine berjalan
            _evo_task_queue: asyncio.Queue = asyncio.Queue()
            for i, task in enumerate(task_queue):
                await _evo_task_queue.put((i, task))

            _parallel_results: list = []
            _results_lock = asyncio.Lock()

            async def _pool_worker(worker_id: int):
                """Satu worker = satu API key. Ambil task → selesaikan → ambil berikutnya."""
                dedicated_agent = ACTIVE_AGENTS[worker_id % len(ACTIVE_AGENTS)]
                while True:
                    try:
                        i, task = _evo_task_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return  # Antrian habis, worker selesai

                    result = await _run_task_parallel(
                        task_num=i + 1,
                        total_tasks=total_tasks,
                        task=task,
                        dedicated_agent=dedicated_agent,
                        synthesizer=synthesizer,
                        generation_counter=generation_counter,
                        evolution_level=evolution_level,
                    )
                    async with _results_lock:
                        _parallel_results.append(result)
                    _evo_task_queue.task_done()

            # Jalankan tepat N worker — bukan len(task_queue) worker!
            await asyncio.gather(*[_pool_worker(i) for i in range(n_workers)])

            # Hitung hasil
            tasks_done = sum(1 for r in _parallel_results if isinstance(r, tuple) and r[0])
            tasks_failed = total_tasks - tasks_done
            failed_tasks = [
                f"• {r[1]}: {r[2]}"
                for r in _parallel_results
                if isinstance(r, tuple) and not r[0]
            ]

            # Ringkasan evolusi ke Telegram
            _evo_summary = (
                f"📊 EVOLUSI {evolution_level} SELESAI\n"
                f"✅ Berhasil: {tasks_done}/{total_tasks}\n"
                f"❌ Gagal: {tasks_failed}/{total_tasks}\n"
                f"🔁 Siklus ke-{generation_counter}"
            )
            if failed_tasks:
                _evo_summary += "\n\n📋 Task gagal:\n" + "\n".join(failed_tasks[:5])
            await send_telegram_notification(_evo_summary, important=True)
            console_terminal_interface.print(
                f"\n[bold magenta]Siklus {generation_counter} Selesai. "
                f"Berhasil: {tasks_done}/{total_tasks}, Gagal: {tasks_failed}/{total_tasks}. "
                f"Sinkronisasi File...[/bold magenta]"
            )
            await dump_ssd()

            # ⚡ DEPLOY PER EVOLUSI — upload ke Roblox setiap evolusi selesai
            console_terminal_interface.print(
                f"[bold green]🚀 Evolusi {evolution_level} selesai! Deploy ke Roblox segera...[/bold green]"
            )

            # ══════════════════════════════════════════════════════════════
            # PRE-DEPLOYMENT VALIDATOR
            # Pastikan 100% file sudah ada dan valid sebelum upload ke Roblox.
            # Jika ada yang hilang → regenerasi dulu, deployment menunggu.
            # ══════════════════════════════════════════════════════════════
            _validator = PreDeploymentValidator()
            _deploy_agent = ACTIVE_AGENTS[agent_idx % len(ACTIVE_AGENTS)]
            _validation_task_queue = _build_task_queue()

            console_terminal_interface.print(
                "[bold yellow]🛡️  [PRE-DEPLOY] Memverifikasi kelengkapan semua file game...[/bold yellow]"
            )
            _deploy_safe = await _validator.validate_and_complete(
                task_queue=_validation_task_queue,
                synthesizer=synthesizer,
                agent=_deploy_agent,
                notify_fn=send_telegram_notification,
            )

            if not _deploy_safe:
                validator_fail_msg = (
                    f"🚫 DEPLOYMENT EVOLUSI {evolution_level} DIBATALKAN\n"
                    "Pre-Deployment Validator menemukan file yang tidak bisa di-generate.\n"
                    "Periksa log VPS: tail -f nohup.out\n\n"
                    "Kemungkinan penyebab:\n"
                    "• Rate limit API Gemini habis\n"
                    "• Task tertentu selalu gagal compiler check\n"
                    "• Disk VPS penuh"
                )
                console_terminal_interface.print(
                    "[bold red]🚫 DEPLOYMENT DIBATALKAN: File tidak 100% lengkap.[/bold red]"
                )
                await send_telegram_notification(validator_fail_msg, important=True)
                if os.path.exists(COMPILED_GAME_FILE):
                    await send_telegram_document(
                        COMPILED_GAME_FILE,
                        f"📦 File .rbxl Evolusi {evolution_level} (mungkin tidak lengkap)\n"
                        f"Periksa file sebelum upload manual!",
                    )
                # Tetap lanjut ke evolusi berikutnya meski deploy dibatalkan
                evolution_level += 1
                generation_counter += 1
                await asyncio.sleep(10)
                continue

            # ══════════════════════════════════════════════════════════════
            # Semua file valid → Proaktif fix → build Rojo + publish ke Roblox
            # ══════════════════════════════════════════════════════════════
            console_terminal_interface.print(
                "[bold cyan][ProactiveFix] Scanning semua file sebelum Rojo build...[/bold cyan]"
            )
            _proactive_fixes = RojoBuildAutoHealer.proactive_scan_and_fix()
            if _proactive_fixes > 0:
                console_terminal_interface.print(
                    f"[bold green][ProactiveFix] ✅ {_proactive_fixes} masalah tipe properti diperbaiki SEBELUM build.[/bold green]"
                )

            rojo_ok, rojo_stderr = RobloxDeployer.compile_rojo()
            if not rojo_ok:
                # Pipeline Auto-Heal Rojo: diagnosa → perbaiki file → retry build (maks 3x)
                _deploy_agent = ACTIVE_AGENTS[agent_idx % len(ACTIVE_AGENTS)]
                for _rojo_attempt in range(1, 4):
                    console_terminal_interface.print(
                        f"[bold cyan][RojoBuildHealer] Percobaan auto-heal {_rojo_attempt}/3...[/bold cyan]"
                    )
                    _healed = await RojoBuildAutoHealer.heal_loop(rojo_stderr, _deploy_agent)
                    if _healed:
                        rojo_ok, rojo_stderr = RobloxDeployer.compile_rojo()
                        if rojo_ok:
                            console_terminal_interface.print(
                                "[bold green][RojoBuildHealer] ✅ Rojo build BERHASIL setelah auto-heal![/bold green]"
                            )
                            break
                    if _rojo_attempt < 3:
                        console_terminal_interface.print(
                            f"[bold yellow][RojoBuildHealer] Percobaan {_rojo_attempt}/3 gagal. Mencoba lagi...[/bold yellow]"
                        )
            if not rojo_ok:
                rojo_fail_msg = (
                    f"🔨 ROJO BUILD GAGAL setelah 3x auto-heal (Evolusi {evolution_level})\n"
                    f"Error terakhir: {rojo_stderr[:300]}\n"
                    f"Periksa log VPS: tail -f nexus_healer.log"
                )
                console_terminal_interface.print(f"[bold red]{rojo_fail_msg}[/bold red]")
                await send_telegram_notification(rojo_fail_msg, important=True)
            else:
                await healer.initialize_and_scan()
                await RobloxDeployer.publish(evolution_level)

            # Lanjut ke evolusi berikutnya
            await send_telegram_notification(
                f"⏭️ Evolusi {evolution_level} selesai → Lanjut Evolusi {evolution_level + 1}...",
                important=False
            )
            evolution_level += 1
            generation_counter += 1
            await asyncio.sleep(10)
            continue

    except Exception as e:
        error_msg = f"FATAL ERROR di Orchestrator: {type(e).__name__}: {e}"
        console_terminal_interface.print(f"[bold red]{error_msg}[/bold red]")
        try:
            await send_telegram_notification(f"❌ {error_msg}")
        except Exception:
            pass
        raise


def _shutdown_handler(signum, frame):
    console_terminal_interface.print("[bold red]\nSistem dihentikan oleh pengguna (SIGINT/SIGTERM).[/bold red]")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    console_terminal_interface.print(
        Panel("[bold cyan]NEXUS TIER ABSOLUTE APEX - SELF-HEALING AUTONOMOUS AGENT INITIALIZING...[/bold cyan]")
    )
    try:
        asyncio.run(run_orchestrator())
    except SystemExit:
        pass
        
