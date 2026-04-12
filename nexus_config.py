import os
import sys
from rich.console import Console
from dotenv import load_dotenv

_env_paths = [
    os.path.join(os.path.dirname(__file__), ".env.nexus"),
    os.path.join(os.path.dirname(__file__), ".env"),
    ".env.nexus",
    ".env"
]
for _env_path in _env_paths:
    if os.path.exists(_env_path):
        load_dotenv(_env_path)
        break

console_terminal_interface = Console()

AGENT_WORKER_POOL = [
    {"name": "Agent_Alpha",   "api_key": os.getenv("GEMINI_KEY_01", "") or os.getenv("GEMINI_KEY_1", "")},
    {"name": "Agent_Beta",    "api_key": os.getenv("GEMINI_KEY_02", "") or os.getenv("GEMINI_KEY_2", "")},
    {"name": "Agent_Gamma",   "api_key": os.getenv("GEMINI_KEY_03", "") or os.getenv("GEMINI_KEY_3", "")},
    {"name": "Agent_Delta",   "api_key": os.getenv("GEMINI_KEY_04", "") or os.getenv("GEMINI_KEY_4", "")},
    {"name": "Agent_Epsilon", "api_key": os.getenv("GEMINI_KEY_05", "") or os.getenv("GEMINI_KEY_5", "")},
    {"name": "Agent_Zeta",    "api_key": os.getenv("GEMINI_KEY_06", "") or os.getenv("GEMINI_KEY_6", "")},
    {"name": "Agent_Eta",     "api_key": os.getenv("GEMINI_KEY_07", "") or os.getenv("GEMINI_KEY_7", "")},
    {"name": "Agent_Theta",   "api_key": os.getenv("GEMINI_KEY_08", "") or os.getenv("GEMINI_KEY_8", "")},
    {"name": "Agent_Iota",    "api_key": os.getenv("GEMINI_KEY_09", "") or os.getenv("GEMINI_KEY_9", "")},
    {"name": "Agent_Kappa",   "api_key": os.getenv("GEMINI_KEY_10", "")},
]

ACTIVE_AGENTS = []
for agent in AGENT_WORKER_POOL:
    if agent["api_key"] is not None and agent["api_key"].strip() != "":
        ACTIVE_AGENTS.append(agent)

if not ACTIVE_AGENTS:
    console_terminal_interface.print("[bold red]FATAL ERROR: Tidak ada API Key aktif yang ditemukan. Sistem dihentikan.[/bold red]")
    sys.exit(1)

console_terminal_interface.print(f"[bold green]✅ {len(ACTIVE_AGENTS)} Agent aktif terdeteksi.[/bold green]")

ROBLOX_UNIVERSE_ID = os.getenv("ROBLOX_UNIVERSE_ID", "0")
ROBLOX_PLACE_ID = os.getenv("ROBLOX_PLACE_ID", "0")
ROBLOX_OPEN_CLOUD_API_KEY = os.getenv("ROBLOX_OPEN_CLOUD_API_KEY", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

VPS_WEBHOOK_PORT = int(os.getenv("VPS_WEBHOOK_PORT", "8080"))
LIVE_JIT_MESSAGING_TOPIC = "ABSOLUTE_APEX_JIT"

_default_root = os.path.join(os.path.expanduser("~"), "FantasyExtraction_Roblox_TrueApex")
PROJECT_ROOT_DIRECTORY = os.getenv("PROJECT_ROOT_DIRECTORY", _default_root)
SOURCE_CODE_DIRECTORY = os.path.join(PROJECT_ROOT_DIRECTORY, "src")
DATABASE_PATH = os.path.join(PROJECT_ROOT_DIRECTORY, "true_apex_matrix.sqlite")
COMPILED_GAME_FILE = os.path.join(PROJECT_ROOT_DIRECTORY, "build.rbxl")
LUAU_ANALYZE_BINARY_PATH = os.path.join(PROJECT_ROOT_DIRECTORY, "luau-analyze")
LUNE_BINARY_PATH = os.path.join(PROJECT_ROOT_DIRECTORY, "lune")
LUAURC_PATH = os.path.join(PROJECT_ROOT_DIRECTORY, ".luaurc")
TEMP_IO_DIRECTORY = os.path.join(PROJECT_ROOT_DIRECTORY, "temp_io_matrix")

os.makedirs(PROJECT_ROOT_DIRECTORY, exist_ok=True)
os.makedirs(SOURCE_CODE_DIRECTORY, exist_ok=True)
os.makedirs(TEMP_IO_DIRECTORY, exist_ok=True)

_gemini_cli_candidates = [
    "/home/runner/.local/bin/gemini",
    os.path.expanduser("~/.local/bin/gemini"),
    "/home/ubuntu/.local/share/pnpm/gemini",
    os.path.expanduser("~/.local/share/pnpm/gemini"),
    "/usr/local/bin/gemini",
    "/usr/bin/gemini",
]

def _find_gemini_cli() -> str:
    import shutil as _shutil
    for candidate in _gemini_cli_candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = _shutil.which("gemini")
    if found:
        return found
    return "gemini"

GEMINI_CLI_PATH = _find_gemini_cli()
console_terminal_interface.print(f"[dim cyan]Gemini CLI Path: {GEMINI_CLI_PATH}[/dim cyan]")

ROBLOX_MCP_URL = os.getenv("ROBLOX_MCP_URL", "")
if ROBLOX_MCP_URL:
    console_terminal_interface.print(f"[bold green]🔌 MCP Server Terhubung: {ROBLOX_MCP_URL}[/bold green]")
else:
    console_terminal_interface.print(f"[dim yellow]⚠️ ROBLOX_MCP_URL kosong. Mode otonom buta.[/dim yellow]")


class APIKeyRotator:
    _current_index = 0
    _pool_size = len(ACTIVE_AGENTS)
    _total_daily_requests = 0
    _max_daily_requests = 11000

    @classmethod
    def get_current_key(cls) -> str:
        current_agent = ACTIVE_AGENTS[cls._current_index % cls._pool_size]
        return current_agent["api_key"]

    @classmethod
    def rotate_key(cls) -> str:
        cls._current_index += 1
        current_agent = ACTIVE_AGENTS[cls._current_index % cls._pool_size]
        console_terminal_interface.print(f"[dim cyan][API Rotator] Mengalihkan beban ke {current_agent['name']}...[/dim cyan]")
        return current_agent["api_key"]

    @classmethod
    def get_agent_by_index(cls, idx: int) -> dict:
        return ACTIVE_AGENTS[idx % cls._pool_size]

    @classmethod
    def track_request(cls) -> bool:
        cls._total_daily_requests += 1
        if cls._total_daily_requests >= cls._max_daily_requests:
            console_terminal_interface.print("[bold red]CIRCUIT BREAKER AKTIF: Limit harian tercapai.[/bold red]")
            return False
        return True
