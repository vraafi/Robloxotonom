# PANDUAN MUTLAK SETUP VPS LINUX UNTUK NEXUS AI

Sistem otonom ini mengandalkan **Gemma 4 31B IT** sebagai model utama (1.500 RPD per API Key) dengan masa penalaran tinggi (Batas Waktu 30 Menit per File). Lingkungan Linux harus dirakit dengan presisi. Ikuti langkah-langkah di bawah ini secara berurutan di terminal SSH/Putty VPS Ubuntu/Debian Anda.

---

## Tahap 1: Pembaruan Sistem Inti

Jalankan perintah ini untuk memastikan VPS Anda tidak memiliki konflik pustaka (library) lama.

```bash
sudo apt update && sudo apt upgrade -y
```

---

## Tahap 2: Instalasi Komponen Dasar (Python, Node.js, & Git)

Sistem ini membutuhkan Python untuk AI dan Node.js untuk kompilator Rojo.

**Fakta Mutlak:** `unzip` wajib diinstal agar `nexus_compiler.py` bisa mengekstrak biner `luau-analyze` dan `lune`.

```bash
# 1. Install Python dan alat pendukung (termasuk unzip dan tmux untuk jalan 24/7)
sudo apt install python3 python3-pip python3-venv curl wget git screen tmux unzip -y

# 2. Install Node.js Versi 20 (Wajib untuk Rojo)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install nodejs -y

# 3. Verifikasi instalasi
node --version    # Harus menampilkan v20.x.x
python3 --version # Harus menampilkan Python 3.x.x
```

---

## Tahap 3: Instalasi Kompilator Roblox (Rojo)

Rojo akan merakit ratusan file `.lua` yang ditulis AI menjadi satu file `.rbxl` yang bisa dibaca Roblox Studio.

**Rojo tidak tersedia di npm.** Instalasinya dilakukan langsung dari binary GitHub Releases (sudah diuji dan berfungsi):

```bash
# Download binary Rojo v7.6.1 untuk Linux x86_64 (VPS pada umumnya)
wget https://github.com/rojo-rbx/rojo/releases/download/v7.6.1/rojo-7.6.1-linux-x86_64.zip

# Ekstrak binary
unzip rojo-7.6.1-linux-x86_64.zip

# Beri izin eksekusi dan pindahkan ke PATH global
chmod +x rojo
sudo mv rojo /usr/local/bin/

# Hapus file zip
rm rojo-7.6.1-linux-x86_64.zip
```

Verifikasi instalasi:
```bash
rojo --version
# Harus menampilkan: Rojo 7.6.1
```

> Jika VPS Anda menggunakan arsitektur ARM (jarang), ganti URL download dengan:
> `https://github.com/rojo-rbx/rojo/releases/download/v7.6.1/rojo-7.6.1-linux-aarch64.zip`

---

## Tahap 4: Instalasi Mesin AI (Gemini CLI)

Sistem ini memanggil gemini-cli langsung dari Google untuk mengeksekusi model Gemma 4 31B IT secara non-interaktif (headless mode).

```bash
npm install -g @google/gemini-cli
```

Verifikasi instalasi:
```bash
gemini --version
# Harus menampilkan: 0.37.1 (atau lebih baru)
```

---

## Tahap 5: Instalasi Modul Python

Install semua library Python yang dibutuhkan oleh seluruh file sistem Nexus.

```bash
# Install library asinkron dan UI Terminal
pip3 install aiohttp aiofiles rich requests python-dotenv

# (Opsional) Install Aider Chat untuk bedah kode tambahan (digunakan AutoHealerAgent)
pip3 install aider-chat
```

---

## Tahap 6: Instalasi Kompiler Multi-Bahasa (WAJIB untuk Nexus Polyglot /polyglot)

Fitur `/polyglot` di Telegram membutuhkan kompiler untuk setiap bahasa yang didukung.
**Install semuanya agar semua perintah /polyglot bisa berjalan tanpa BINARY NOT FOUND error.**

```bash
# ---- C & C++ ----
sudo apt install build-essential g++ gcc -y

# ---- Go (Golang) ----
sudo apt install golang-go -y

# ---- Java (OpenJDK 21) ----
sudo apt install openjdk-21-jdk -y

# ---- Lua 5.4 ----
sudo apt install lua5.4 -y

# ---- Rust (rustc via rustup - cara resmi) ----
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"

# ---- JavaScript ----
# Node.js sudah terinstall di Tahap 2, tidak perlu install tambahan

# Verifikasi semua kompiler
echo '=== Cek Kompiler ===' && g++ --version | head -1 && go version && java --version 2>&1 | head -1 && lua5.4 -v && rustc --version && node --version
```

> **Catatan Rust:** Setelah install via rustup, jalankan `source "$HOME/.cargo/env"` atau logout-login ulang SSH agar PATH `~/.cargo/bin` aktif secara permanen.

---

## Tahap 7: Persiapan File Proyek

```bash
# Clone atau upload file proyek ke VPS Anda
# Kemudian masuk ke direktori proyek
cd nexus_project/
```

---

## Tahap 8: Persiapan File Lingkungan (.env.nexus)

File `.env.nexus` sudah disediakan di dalam folder proyek. Edit sesuai kebutuhan:

```bash
nano .env.nexus
```

Isi file `.env.nexus`:
```env
# Masukkan hingga 10 API Key Google AI Studio Anda (model Gemma 4 31B, 1.500 RPD per key)
GEMINI_KEY_01="AIzaSy_KUNCI_ANDA_DISINI"
GEMINI_KEY_02="AIzaSy_KUNCI_KEDUA_JIKA_ADA"
# ... hingga GEMINI_KEY_10

# Kredensial untuk upload otomatis ke game Anda
ROBLOX_UNIVERSE_ID="12345678"
ROBLOX_PLACE_ID="87654321"
ROBLOX_OPEN_CLOUD_API_KEY="kunci_open_cloud_anda"

# Token Bot untuk mengirim file .rbxl & menerima perintah /polyglot via Telegram
TELEGRAM_BOT_TOKEN="token_bot_anda"
TELEGRAM_CHAT_ID="@username_atau_id_chat_anda"

# GitHub Personal Access Token (untuk RAG Knowledge Scraping tanpa rate-limit)
# Buat di: https://github.com/settings/tokens -> New token -> scope: public_repo
GITHUB_PERSONAL_ACCESS_TOKEN="ghp_token_github_anda"

# (Opsional) URL ngrok untuk Roblox Studio MCP (jika pakai live playtest di Studio)
# ROBLOX_MCP_URL="https://xxxx.ngrok-free.app"
```

Tekan `CTRL+X`, lalu `Y`, lalu `Enter` untuk menyimpan.

---

## Tahap 9: Jalankan dengan Tmux (Mode 24/7)

Karena AI diberikan waktu berpikir hingga 30 Menit per tugas, **wajib menggunakan tmux** agar proses tidak terbunuh saat koneksi internet terputus.

```bash
# Buat sesi tmux baru yang kebal disconnect
tmux new -s nexus_ai

# Masuk ke direktori proyek
cd nexus_project/

# Berikan izin eksekusi pada script start
chmod +x start.sh

# Jalankan mesin utama
bash start.sh
```

**Untuk keluar dari tmux tanpa mematikan AI:**
Tekan `CTRL+B`, lepaskan, lalu tekan `D`.

**Untuk kembali ke sesi yang berjalan:**
```bash
tmux attach -t nexus_ai
```

---

## Catatan Penting

- **Model Utama:** Gemma 4 31B IT (`models/gemma-4-31b-it`) -- 1.500 RPD per API Key
- **Fallback 1:** Gemma 4 26B A4B IT (`models/gemma-4-26b-a4b-it`)
- **Fallback 2:** Gemini 3.1 Flash Lite Preview (`models/gemini-3.1-flash-lite-preview`) -- 500 RPD
- **Fallback Terakhir:** Gemini 2.0 Flash (`models/gemini-2.0-flash`)
- **JANGAN** menggunakan Gemini 2.5 Flash atau Gemini 2.5 Pro (RPD sangat rendah, langsung kena rate limit!)
- Sistem secara otomatis melakukan rotasi antar 10 API Key untuk menghindari rate limit
- Saat rate limit terdeteksi, sistem otomatis menunggu 60 detik sebelum mencoba lagi