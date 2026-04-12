import os
import json
import sqlite3
import hashlib
import datetime
import asyncio
import shutil
from typing import List

from nexus_config import (
    DATABASE_PATH,
    PROJECT_ROOT_DIRECTORY,
    TEMP_IO_DIRECTORY,
    console_terminal_interface
)

DATABASE_MUTEX = asyncio.Lock()


def establish_database_connection() -> sqlite3.Connection:
    database_connection = sqlite3.connect(DATABASE_PATH, isolation_level=None, check_same_thread=False)
    database_connection.execute("PRAGMA journal_mode = WAL")
    database_connection.execute("PRAGMA synchronous = NORMAL")
    database_connection.execute("PRAGMA busy_timeout = 5000")
    return database_connection


async def initialize_system_ledger():
    if os.path.exists(TEMP_IO_DIRECTORY):
        shutil.rmtree(TEMP_IO_DIRECTORY, ignore_errors=True)

    os.makedirs(TEMP_IO_DIRECTORY, exist_ok=True)
    os.makedirs(PROJECT_ROOT_DIRECTORY, exist_ok=True)

    async with DATABASE_MUTEX:
        def _init_db():
            conn = establish_database_connection()
            cursor = conn.cursor()

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS verified_modules (
                    module_name TEXT PRIMARY KEY,
                    filepath TEXT,
                    code_content TEXT,
                    cryptographic_sha256_hash TEXT
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS live_telemetry_logs (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id TEXT,
                    event_type TEXT,
                    event_data_json TEXT,
                    is_analyzed BOOLEAN DEFAULT 0,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS daily_logs (
                    player_id TEXT NOT NULL,
                    log_date TEXT NOT NULL,
                    amount_logged INTEGER DEFAULT 0,
                    PRIMARY KEY (player_id, log_date)
                )
            ''')

            conn.commit()
            conn.close()

        await asyncio.to_thread(_init_db)
        console_terminal_interface.print("[bold green]Database Ledger diinisialisasi dengan WAL Mode.[/bold green]")


async def save_verified_module(module_name: str, filepath: str, code_content: str) -> str:
    cryptographic_hash = hashlib.sha256(code_content.encode("utf-8")).hexdigest()

    async with DATABASE_MUTEX:
        def _save():
            conn = establish_database_connection()
            cursor = conn.cursor()

            cursor.execute('''
                INSERT OR REPLACE INTO verified_modules
                (module_name, filepath, code_content, cryptographic_sha256_hash)
                VALUES (?, ?, ?, ?)
            ''', (module_name, filepath, code_content, cryptographic_hash))

            conn.commit()
            conn.close()

        await asyncio.to_thread(_save)

    return cryptographic_hash


async def retrieve_ecosystem_context() -> str:
    async with DATABASE_MUTEX:
        def _retrieve():
            try:
                conn = establish_database_connection()
                cursor = conn.cursor()

                cursor.execute("SELECT module_name, code_content FROM verified_modules LIMIT 15")
                rows = cursor.fetchall()

                conn.close()

                context_string = ""
                for row in rows:
                    module_name = row[0]
                    code_snippet = row[1][:400]
                    context_string += f"--- {module_name} ---\n{code_snippet}...\n\n"

                return context_string
            except Exception:
                return ""

        return await asyncio.to_thread(_retrieve)


async def log_roblox_telemetry(server_id: str, event_type: str, event_data: dict):
    async with DATABASE_MUTEX:
        def _log():
            conn = establish_database_connection()
            cursor = conn.cursor()

            cursor.execute('''
                INSERT INTO live_telemetry_logs
                (server_id, event_type, event_data_json)
                VALUES (?, ?, ?)
            ''', (server_id, event_type, json.dumps(event_data)))

            conn.commit()
            conn.close()

        await asyncio.to_thread(_log)


async def get_unanalyzed_telemetry() -> List[dict]:
    async with DATABASE_MUTEX:
        def _get():
            conn = establish_database_connection()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT log_id, server_id, event_type, event_data_json
                FROM live_telemetry_logs
                WHERE is_analyzed = 0 LIMIT 10
            ''')
            rows = cursor.fetchall()

            logs_list = []
            for row in rows:
                log_id = row[0]
                server_id = row[1]
                event_type = row[2]
                event_data = json.loads(row[3])

                logs_list.append({
                    "log_id": log_id,
                    "server_id": server_id,
                    "event_type": event_type,
                    "event_data": event_data
                })

                cursor.execute("UPDATE live_telemetry_logs SET is_analyzed = 1 WHERE log_id = ?", (log_id,))

            conn.commit()
            conn.close()

            return logs_list

        return await asyncio.to_thread(_get)


async def update_daily_log(player_id: str, amount: int):
    today = datetime.date.today().isoformat()
    async with DATABASE_MUTEX:
        def _update():
            conn = establish_database_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO daily_logs (player_id, log_date, amount_logged)
                VALUES (?, ?, ?)
                ON CONFLICT(player_id, log_date) DO UPDATE SET
                amount_logged = amount_logged + excluded.amount_logged
                ''',
                (player_id, today, amount)
            )
            conn.commit()
            conn.close()
        await asyncio.to_thread(_update)


async def get_daily_log_amount(player_id: str) -> int:
    today = datetime.date.today().isoformat()
    async with DATABASE_MUTEX:
        def _get():
            conn = establish_database_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT amount_logged FROM daily_logs WHERE player_id = ? AND log_date = ?",
                (player_id, today)
            )
            result = cursor.fetchone()
            conn.close()
            return result[0] if result else 0
        return await asyncio.to_thread(_get)
