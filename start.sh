#!/bin/bash

# Source Rust (rustup) cargo environment if exists
if [ -f "$HOME/.cargo/env" ]; then
    source "$HOME/.cargo/env"
fi

# ========================================================
# ROBLOX MCP NETWORK TUNNEL SETUP
# Set your ngrok or local tunnel URL below to connect the AI
# to your local Roblox Studio for JSON-RPC MCP commands.
# ========================================================
export ROBLOX_MCP_URL="${ROBLOX_MCP_URL:-https://placeholder-tunnel.ngrok.app}"

cleanup() {
    trap - SIGINT SIGTERM SIGHUP EXIT
    echo -e "\n[Sistem] Mematikan seluruh proses Nexus..."
    kill $HEALER_PID 2>/dev/null
    exit 0
}

trap cleanup SIGINT SIGTERM SIGHUP EXIT

echo "[Sistem] Menjalankan Nexus Healing Agent di background..."
python3 nexus_healer.py > nexus_healer.log 2>&1 &
HEALER_PID=$!

sleep 2

echo "[Sistem] Menjalankan Nexus Main Orchestrator di foreground..."
echo "[Sistem] Telegram Bot sudah terintegrasi dalam nexus_main.py"
python3 nexus_main.py

cleanup
