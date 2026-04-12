#!/bin/bash

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
python3 nexus_main.py

cleanup
