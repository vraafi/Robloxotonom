# Nexus - Roblox Autonomous AI Game Generator

Sistem AI otonom untuk menghasilkan kode Luau game Roblox secara otomatis menggunakan Google Gemini AI.

## Fitur Utama

- **10 AI Agents** berjalan paralel dengan round-robin API key rotation
- **AbsoluteOmniValidator** - validasi kode Luau tingkat militer
- **ApexKeyRotator** - rotasi API key otomatis untuk menghindari rate limit
- **SQLite Database** dengan WAL mode untuk performa tinggi
- **Roblox Open Cloud API** integration untuk deploy otomatis

## Arsitektur

- nexus_config.py - Konfigurasi agents, API keys, constants
- nexus_database.py - SQLite database ledger sistem
- nexus_compiler.py - Validator kode Luau dan code generation
- nexus_healer.py - Key rotator dan self-healing system
- nexus_agents.py - 10 AI agent definitions
- nexus_main.py - Entry point utama
- nexus_test.py - Test suite komprehensif
- start.sh - Script runner

## Status

Sistem telah diuji 15 menit tanpa error.
