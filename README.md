# Nexus Roblox Autonomous Agent

Bot Telegram + AI Agent untuk mengembangkan kode Roblox secara otomatis.

## Struktur File

```
nexus_project/
├── python_bot/
│   ├── nexus_agents.py         # Orchestrator AI + Memory Buffer
│   └── nexus_telegram_bot.py   # Bot Telegram interaktif
└── roblox_scripts/
    ├── SafeSpawnOrchestrator.lua           # Spawn pemain yang aman (Server)
    ├── SpaceshipSpawnFloor_DailyReward.lua # Platform spawn + Daily Reward (Server)
    └── HUDResponsiveInjector.lua           # Tombol X HUD + Mobile Scaling (Local)
```

## Changelog v1.1.0

### nexus_agents.py
- Perbaikan: Timeout per-attempt pada Gemini CLI (mencegah hanging selamanya)
- Perbaikan: Backoff eksponensial antara retry (2s, 4s, 8s)
- Perbaikan: Validasi task list dari decomposer (cek tipe data)
- Perbaikan: Flag BOT_SHUTTING_DOWN untuk graceful shutdown
- Fitur Baru: `NexusMemoryBuffer.clear()` untuk menghapus riwayat
- Fitur Baru: `execute_single_prompt()` untuk query sederhana tanpa dekomposisi
- Fitur Baru: `get_memory_summary()` untuk melihat status memori
- Fitur Baru: Deteksi keyword DataStore → otomatis tambahkan instruksi pcall

### nexus_telegram_bot.py
- Perbaikan: Penanganan file dengan encoding fallback (UTF-8 → Latin-1 → CP1252)
- Perbaikan: Chunking pesan lebih cerdas (tidak memotong di tengah code block)
- Perbaikan: Validasi chat_id lebih awal untuk hemat resource
- Perbaikan: Penanganan error BadRequest, RetryAfter, TimedOut
- Fitur Baru: Perintah `/help` — daftar semua perintah
- Fitur Baru: Perintah `/clear` — hapus riwayat percakapan
- Fitur Baru: Perintah `/status` — lihat status dan riwayat percakapan
- Fitur Baru: Rate limiting (3 pesan per 10 detik) untuk mencegah spam

### SafeSpawnOrchestrator.lua (Roblox Server Script)
- Perbaikan: Anti-Void Monitor — teleport ulang jika pemain jatuh ke Y < -50
- Perbaikan: Cooldown anti-spam teleportasi (0.5 detik minimum)
- Perbaikan: Tidak teleport karakter yang sudah mati
- Perbaikan: Bersihkan data pemain saat PlayerRemoving
- Fitur Baru: Logging ke output untuk debugging

### SpaceshipSpawnFloor_DailyReward.lua (Roblox Server Script)
- Perbaikan: Tidak menimpa Cash yang sudah ada dari sistem lain
- Perbaikan: Error handling DataStore lebih lengkap (cek pesan error)
- Perbaikan: Tidak duplikat membuat platform jika sudah ada
- Perbaikan: Cek pemain masih online sebelum memberi reward
- Perbaikan: Platform diperlebar dari 100x100 ke 200x200 agar lebih aman
- Fitur Baru: Notifikasi ke pemain via RemoteEvent (NexusDailyRewardNotify)
- Fitur Baru: Handle pemain yang sudah join saat script pertama diload

### HUDResponsiveInjector.lua (Roblox LocalScript)
- Perbaikan: Tidak mengubah elemen yang sudah murni Scale
- Perbaikan: Tidak menyuntik tombol X ke ScrollingFrame
- Perbaikan: ZIndex tombol X lebih tinggi dari parent frame
- Fitur Baru: Konfigurasi warna tombol X via konstanta di atas file
- Fitur Baru: Fungsi `openFrame(frameName)` untuk membuka frame dari script lain

## Logika yang Dipertahankan vs Dihapus

### DIPERTAHANKAN:
1. **Pemisahan tanggung jawab**: Teleportasi HANYA di SafeSpawnOrchestrator, Daily Reward HANYA di SpaceshipSpawnFloor_DailyReward. Ini desain yang benar dan tidak ada konflik.
2. **NexusGlobalState.TELEGRAM_OVERRIDE_ACTIVE**: Mutex yang mencegah agen latar belakang bertabrakan dengan permintaan Telegram.
3. **MODEL_FALLBACK_SEQUENCE**: Urutan fallback dari model terkuat ke terlemah.
4. **inject_antigravity_laws()**: Injeksi aturan fisika Roblox ke semua prompt AI.
5. **ForceField spawn protection**: Perlindungan saat spawn agar tidak mati karena glitch.

### LOGIKA YANG DIPERBAIKI (bukan dihapus):
1. **SpaceshipSpawnFloor ukuran 100→200**: Diperlebar agar 5 koordinat spawn tidak dekat tepi.
2. **Daily Reward leaderstats**: Tidak lagi reset Cash ke 0 jika sudah ada dari script lain.
3. **File reading encoding**: Fallback encoding untuk file yang bukan UTF-8 murni.

## Urutan Eksekusi di Roblox Studio

Di ServerScriptService, urutan yang BENAR:
1. `SpaceshipSpawnFloor_DailyReward` — harus jalan lebih dulu (membuat platform)
2. `SafeSpawnOrchestrator` — mencari platform yang dibuat di atas

Di StarterPlayerScripts / StarterGui:
- `HUDResponsiveInjector` — LocalScript, jalan di client setelah login

## Konfigurasi Python Bot

Pastikan `nexus_config.py` memiliki:
```python
TELEGRAM_BOT_TOKEN = "..."    # Token dari @BotFather
TELEGRAM_CHAT_ID = "..."      # Chat ID yang diizinkan
GEMINI_CLI_PATH = "gemini"    # Path ke Gemini CLI
```
