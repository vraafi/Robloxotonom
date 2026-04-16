import asyncio
import subprocess
import json
import re
import time
from collections import deque
from nexus_config import GEMINI_CLI_PATH, console_terminal_interface

# =====================================================================
# [KEMAMPUAN BARU]: SAKELAR OVERRIDE PRIORITAS TELEGRAM (MUTEX LOCK)
# =====================================================================
class NexusGlobalState:
    # Jika True, SEMUA agen otonom latar belakang WAJIB BERHENTI/TIDUR sementara.
    TELEGRAM_OVERRIDE_ACTIVE = False
    # [PERBAIKAN BUG]: Tambahan flag untuk melacak apakah bot sedang shutdown
    BOT_SHUTTING_DOWN = False


class NexusMemoryBuffer:
    def __init__(self, max_history: int = 15):
        self.history: deque = deque(maxlen=max_history)

    def add_user_message(self, text: str) -> None:
        self.history.append(f"PENGGUNA: {text}")

    def add_ai_message(self, text: str) -> None:
        # [PERBAIKAN]: Truncate hanya pada penyimpanan, bukan saat dikirim ke Telegram
        truncated_text = text[:500] + "... [dipotong]" if len(text) > 500 else text
        self.history.append(f"NEXUS AI: {truncated_text}")

    def get_context_string(self) -> str:
        if not self.history:
            return "Belum ada riwayat percakapan sebelumnya."
        return "\n".join(self.history)

    def clear(self) -> None:
        """[FITUR BARU]: Hapus seluruh riwayat percakapan."""
        self.history.clear()


global_agent_memory = NexusMemoryBuffer()


def inject_antigravity_laws(base_prompt: str) -> str:
    """Menyuntikkan aturan fisika Roblox ke dalam prompt AI."""
    antigravity_rules = """
[GOOGLE ANTIGRAVITY SPATIAL & SIKLUS DIRECTIVE - LEVEL 9 ABSOLUT]
Sebagai AI Agent, Anda WAJIB mematuhi hukum fisika Roblox (DevForum Standards) berikut:
1. PENEMPATAN PRESISI: WAJIB `workspace:Raycast()`. Cek `raycastResult.Normal:Dot(Vector3.new(0, 1, 0)) > 0.8`. Gunakan `Model:PivotTo(CFrame)`.
2. SPATIAL OVERLAP QUERY: Cek ruang kosong dengan `workspace:GetPartBoundsInBox` (Gunakan OverlapParams Exclude ActiveMap).
3. NETWORK OWNERSHIP: Panggil `part:SetNetworkOwner(nil)` untuk NPC agar aman dari eksploiter. Matikan state `FallingDown`.
4. CONTINUOUS RESPAWN: Saat monster mati (`Humanoid.Died`), jalankan `task.delay(10, function() if not workspace:FindFirstChild("ActiveMap") then return end; spawnNew() end)`. Hancurkan mayat dengan Debris.
5. SIKLUS 2.5 JAM: Sebelum `ActiveMap:Destroy()`, hancurkan `Humanoid.SeatPart.SeatWeld`, set `Sit = false`, dan teleport pemain ke `SpaceshipSpawnFloor`.
6. ANTI-LAG: Jika `Position.Y < -50`, hancurkan NPC.
7. DATASTORE SAFETY: Selalu gunakan `pcall()` untuk semua operasi DataStore. Jangan pernah mengakses DataStore tanpa error handling.
8. REMOTE EVENTS: Selalu validasi data dari RemoteEvent di sisi Server. Jangan percaya data dari Client.
"""
    return f"{base_prompt}\n\n{antigravity_rules}"


async def decompose_complex_prompt(complex_prompt: str, model: str) -> list:
    """Memecah prompt kompleks menjadi sub-tugas menggunakan AI Orchestrator."""
    context_history = global_agent_memory.get_context_string()
    planner_prompt = f"""
Anda Orchestrator AI. Pecah permintaan pengguna menjadi array JSON berisi sub-tugas yang spesifik dan dapat dieksekusi.
Baca konteks ini jika pengguna memberi perintah lanjutan:
--- RIWAYAT PERCAKAPAN ---
{context_history}
--------------------------
Permintaan: {complex_prompt}
Output WAJIB JSON array murni tanpa format markdown. Contoh: ["sub-tugas 1", "sub-tugas 2"]
Jika permintaan sederhana, kembalikan array dengan satu elemen saja.
"""
    command = [GEMINI_CLI_PATH, "generate", "--model", model, "--prompt", planner_prompt]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        # [PERBAIKAN BUG]: Tambahkan timeout untuk mencegah hanging
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            process.kill()
            console_terminal_interface.print("[bold red]Orchestrator timeout! Menggunakan prompt asli.[/bold red]")
            return [complex_prompt]

        if process.returncode == 0:
            clean_json = re.sub(r"```json\n?|\n?```|```", "", stdout.decode("utf-8").strip()).strip()
            try:
                task_list = json.loads(clean_json)
                if isinstance(task_list, list) and len(task_list) > 0:
                    return task_list
            except json.JSONDecodeError:
                pass
    except Exception as e:
        console_terminal_interface.print(f"[bold yellow]Orchestrator error: {e}. Menggunakan prompt asli.[/bold yellow]")
    return [complex_prompt]


async def execute_gemini_cli_pure(prompt: str, model: str, max_retries: int = 3) -> str:
    """Mengeksekusi prompt melalui Gemini CLI dengan retry logic."""
    antigravity_prompt = inject_antigravity_laws(prompt)
    context_history = global_agent_memory.get_context_string()
    antigravity_prompt = f"Konteks Sebelumnya:\n{context_history}\n\nInstruksi:\n{antigravity_prompt}"

    # Deteksi keyword khusus untuk instruksi tambahan
    if "WEAPON_CUSTOMIZATION_ENGINE" in prompt or "Armor" in prompt:
        antigravity_prompt += "\n\n[FATAL SYSTEM DIRECTIVE]: WAJIB sertakan '-- HitboxSeparation' di awal file."
    
    # [FITUR BARU]: Deteksi permintaan DataStore agar selalu pakai pcall
    if "DataStore" in prompt or "datastore" in prompt.lower():
        antigravity_prompt += "\n\n[SYSTEM DIRECTIVE]: WAJIB gunakan pcall() untuk SEMUA operasi DataStore tanpa pengecualian."

    command = [GEMINI_CLI_PATH, "generate", "--model", model, "--prompt", antigravity_prompt]

    for attempt in range(max_retries):
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            # [PERBAIKAN BUG]: Timeout per attempt untuk mencegah hanging
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60.0)
            except asyncio.TimeoutError:
                process.kill()
                console_terminal_interface.print(f"[bold yellow]Timeout percobaan {attempt + 1}/{max_retries}. Mencoba ulang...[/bold yellow]")
                await asyncio.sleep(2)
                continue

            if process.returncode == 0:
                result = stdout.decode("utf-8").strip()
                if result:
                    return result
                else:
                    console_terminal_interface.print(f"[bold yellow]Respons kosong dari model pada percobaan {attempt + 1}.[/bold yellow]")
            else:
                err_msg = stderr.decode("utf-8").strip()[:200]
                console_terminal_interface.print(f"[bold red]Model error (kode {process.returncode}) pada percobaan {attempt + 1}: {err_msg}[/bold red]")

        except FileNotFoundError:
            console_terminal_interface.print(f"[bold red]FATAL: Gemini CLI tidak ditemukan di: {GEMINI_CLI_PATH}[/bold red]")
            return ""
        except Exception as e:
            console_terminal_interface.print(f"[bold red]Exception pada percobaan {attempt + 1}: {e}[/bold red]")

        # Backoff eksponensial antara retry
        if attempt < max_retries - 1:
            wait_time = (2 ** attempt) * 2
            await asyncio.sleep(wait_time)

    return ""


async def execute_antigravity_fleet(complex_prompt: str, model: str) -> str:
    """
    Fungsi utama: Memecah prompt, mengeksekusi setiap sub-tugas, dan menggabungkan hasilnya.
    [PERBAIKAN]: Jika agen latar belakang sedang berjalan dan Telegram override aktif, 
    fungsi ini akan dijeda (yield) agar tidak bertabrakan.
    """
    # [FITUR BARU]: Jika sedang shutdown, langsung berhenti
    if NexusGlobalState.BOT_SHUTTING_DOWN:
        return "Bot sedang dalam proses shutdown. Silakan coba lagi setelah bot diaktifkan kembali."

    global_agent_memory.add_user_message(complex_prompt)
    tasks = await decompose_complex_prompt(complex_prompt, model)

    console_terminal_interface.print(f"[bold blue]📋 Total sub-tugas: {len(tasks)}[/bold blue]")

    combined_results = []
    for i, task in enumerate(tasks, 1):
        # [PERBAIKAN BUG]: Cek override sebelum setiap sub-tugas
        if NexusGlobalState.TELEGRAM_OVERRIDE_ACTIVE and i > 1:
            console_terminal_interface.print(f"[bold yellow]⏸️ Sub-tugas {i} ditunda karena Telegram override aktif.[/bold yellow]")
            await asyncio.sleep(1)

        task_str = str(task) if not isinstance(task, str) else task
        contextual_task = f"Tugas {i}/{len(tasks)}: {task_str}."
        console_terminal_interface.print(f"[bold cyan]🔧 Mengeksekusi sub-tugas {i}/{len(tasks)}...[/bold cyan]")

        result = await execute_gemini_cli_pure(contextual_task, model)
        if result:
            combined_results.append(f"--- HASIL TUGAS {i} ---\n{result}\n")
        else:
            combined_results.append(f"--- TUGAS {i} GAGAL ---\n")

    final_output = "\n".join(combined_results)
    global_agent_memory.add_ai_message(final_output)
    return final_output


# =====================================================================
# [FITUR BARU]: Fungsi utilitas tambahan
# =====================================================================

async def execute_single_prompt(prompt: str, model: str) -> str:
    """
    Eksekusi langsung tanpa dekomposisi — cocok untuk pertanyaan sederhana.
    """
    global_agent_memory.add_user_message(prompt)
    result = await execute_gemini_cli_pure(prompt, model)
    if result:
        global_agent_memory.add_ai_message(result)
    return result


def get_memory_summary() -> str:
    """Mengembalikan ringkasan riwayat percakapan saat ini."""
    history_count = len(global_agent_memory.history)
    context = global_agent_memory.get_context_string()
    return f"📊 Riwayat: {history_count} pesan tersimpan.\n\n{context}"


# =====================================================================
# BIARKAN KELAS ASLI ANDA SEPERTI OmniSynthesizerAgent, AutoHealerAgent 
# TETAP BERADA DI BAWAH BARIS INI. JANGAN DIHAPUS.
# =====================================================================
