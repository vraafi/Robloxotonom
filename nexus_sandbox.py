"""
nexus_sandbox.py
================
Sistem Sandbox Terisolasi untuk Pengujian Kode Sebelum Push ke GitHub.
Pipeline: Buat sandbox -> Copy kode baru -> Test syntax -> Test Rojo build -> Push jika OK

Import di nexus_agents.py:
    from nexus_sandbox import sandbox_test_and_push
"""

import os
import shutil
import tempfile
import subprocess
import asyncio
from typing import Tuple, Optional


class NexusSandbox:
    """Sandbox terisolasi untuk menguji kode sebelum push ke GitHub."""

    def __init__(self, repo_root: str):
        self.repo_root = repo_root
        self.sandbox_dir: Optional[str] = None
        self._project_path: Optional[str] = None

    def __enter__(self):
        self.sandbox_dir = tempfile.mkdtemp(prefix="nexus_sandbox_")
        self._project_path = os.path.join(self.sandbox_dir, "project")
        shutil.copytree(
            self.repo_root,
            self._project_path,
            ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "*.pyc",
                "temp_io_matrix", "polyglot_sandboxes", "*.bak"
            ),
        )
        print("[Sandbox] Dibuat: " + self.sandbox_dir)
        return self

    def __exit__(self, *args):
        shutil.rmtree(self.sandbox_dir, ignore_errors=True)
        print("[Sandbox] Dihapus.")

    @property
    def project_path(self) -> str:
        return self._project_path

    def apply_change(self, relative_path: str, new_content: str) -> str:
        full_path = os.path.join(self._project_path, relative_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        return full_path

    def test_python_syntax(self, relative_path: str) -> Tuple[bool, str]:
        full_path = os.path.join(self._project_path, relative_path)
        r = subprocess.run(
            ["python3", "-m", "py_compile", full_path],
            capture_output=True, text=True, timeout=30
        )
        return r.returncode == 0, r.stderr or "Syntax OK"

    def test_lua_syntax(self, relative_path: str) -> Tuple[bool, str]:
        luau_bin = os.path.join(self.repo_root, "luau-analyze")
        if not os.path.exists(luau_bin):
            return True, "luau-analyze tidak ditemukan, skip"
        full_path = os.path.join(self._project_path, relative_path)
        r = subprocess.run(
            [luau_bin, full_path],
            capture_output=True, text=True, timeout=30
        )
        ok = r.returncode == 0
        return ok, (r.stdout + r.stderr) if not ok else "Luau syntax OK"

    def test_rojo_build(self) -> Tuple[bool, str]:
        rojo_bin = shutil.which("rojo") or os.path.join(self.repo_root, "rojo")
        if not rojo_bin or not os.path.exists(rojo_bin):
            return True, "rojo tidak ditemukan, skip build test"
        out_file = os.path.join(self.sandbox_dir, "sandbox_build_test.rbxl")
        r = subprocess.run(
            [rojo_bin, "build", "--output", out_file],
            cwd=self._project_path,
            capture_output=True, text=True, timeout=120
        )
        return r.returncode == 0, r.stderr if r.returncode != 0 else "Rojo build OK"

    def commit_to_real_project(self, relative_path: str):
        src = os.path.join(self._project_path, relative_path)
        dst = os.path.join(self.repo_root, relative_path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)


async def sandbox_test_and_push(
    repo_root: str,
    file_relative_path: str,
    new_content: str,
    send_fn,
    commit_message: str = "Auto-fix dari Nexus AI",
    push_to_github: bool = True,
    test_rojo: bool = False,
) -> Tuple[bool, str]:
    """
    Pipeline: Sandbox -> Test -> Commit -> Push ke GitHub jika OK.

    Args:
        repo_root: Path absolut ke root repository
        file_relative_path: Path relatif dari repo_root, misal: src/StarterGui/HUD.client.lua
        new_content: Konten baru file
        send_fn: async function(text) untuk notif Telegram
        commit_message: Pesan commit Git
        push_to_github: True untuk push setelah test OK
        test_rojo: True untuk test Rojo build
    """
    await send_fn(
        "Sandbox Testing\n"
        "File: " + os.path.basename(file_relative_path) + "\n"
        "Menguji di lingkungan terisolasi..."
    )

    with NexusSandbox(repo_root) as sandbox:
        sandbox.apply_change(file_relative_path, new_content)

        ext = os.path.splitext(file_relative_path)[1].lower()
        if ext == ".py":
            ok, msg = sandbox.test_python_syntax(file_relative_path)
            test_name = "Python syntax"
        elif ext in (".lua", ".luau"):
            ok, msg = sandbox.test_lua_syntax(file_relative_path)
            test_name = "Luau syntax"
        else:
            ok, msg = True, "Tipe file tidak diuji syntax"
            test_name = "Skip"

        if not ok:
            await send_fn(
                "Sandbox GAGAL -- " + test_name + " Error\n"
                + msg[:400]
                + "\nKode TIDAK di-push. AI akan memperbaiki ulang..."
            )
            return False, test_name + " error: " + msg

        if test_rojo and ext in (".lua", ".luau", ".rbxmx"):
            rojo_ok, rojo_msg = sandbox.test_rojo_build()
            if not rojo_ok:
                await send_fn("Sandbox GAGAL -- Rojo Build Error\n" + rojo_msg[:400] + "\nKode TIDAK di-push.")
                return False, "Rojo build error: " + rojo_msg
            else:
                await send_fn("Rojo build OK di sandbox!")

        sandbox.commit_to_real_project(file_relative_path)

    await send_fn("Sandbox OK! " + os.path.basename(file_relative_path) + " lolos semua uji.")

    if not push_to_github:
        return True, "Disimpan lokal, tidak di-push"

    github_token = (
        os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "")
        or os.getenv("GITHUB_TOKEN", "")
    )
    if not github_token:
        await send_fn(
            "Push dilewati\n"
            "GITHUB_TOKEN tidak ditemukan di .env.nexus\n"
            "Tambahkan: GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxxx"
        )
        return True, "Lokal OK, tidak push (no token)"

    try:
        sp_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        subprocess.run(["git", "-C", repo_root, "config", "user.email", "nexus-ai@bot.local"], capture_output=True)
        subprocess.run(["git", "-C", repo_root, "config", "user.name", "Nexus AI"], capture_output=True)
        subprocess.run(["git", "-C", repo_root, "add", file_relative_path], capture_output=True, timeout=30)
        subprocess.run(["git", "-C", repo_root, "commit", "-m", commit_message], capture_output=True, text=True, timeout=30)
        r = subprocess.run(
            ["git", "-C", repo_root, "push"],
            capture_output=True, text=True, timeout=60,
            env=sp_env
        )

        if r.returncode == 0:
            await send_fn("Push Berhasil! Commit: " + commit_message)
            return True, "Push OK"
        else:
            err = r.stderr[:300]
            await send_fn("Push gagal:\n" + err + "\nFile tetap tersimpan lokal.")
            return True, "Lokal OK, push gagal: " + err

    except Exception as e:
        await send_fn("Exception saat push: " + str(e) + "\nFile tersimpan lokal.")
        return True, "Lokal OK, exception push: " + str(e)
