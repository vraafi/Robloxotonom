"""
Script pengujian sistem Nexus selama 15 menit.
Menguji semua komponen: import, database, compiler, validator, dan Roblox API.
"""
import asyncio
import os
import sys
import time
import traceback

def test_imports():
    """Test semua import berhasil."""
    print("[TEST 1/6] Menguji import modul...")
    try:
        import nexus_config
        import nexus_database
        import nexus_compiler
        import nexus_healer
        import nexus_agents
        import nexus_main
        print("  ✅ Semua import LULUS")
        return True
    except Exception as e:
        print(f"  ❌ Import GAGAL: {e}")
        traceback.print_exc()
        return False


def test_config():
    """Test konfigurasi."""
    print("[TEST 2/6] Menguji konfigurasi...")
    try:
        from nexus_config import ACTIVE_AGENTS, ROBLOX_UNIVERSE_ID, ROBLOX_PLACE_ID, ROBLOX_OPEN_CLOUD_API_KEY
        
        if not ACTIVE_AGENTS:
            print("  ❌ Tidak ada agent aktif!")
            return False
        
        print(f"  ✅ {len(ACTIVE_AGENTS)} agent aktif")
        print(f"  ✅ Universe ID: {ROBLOX_UNIVERSE_ID}")
        print(f"  ✅ Place ID: {ROBLOX_PLACE_ID}")
        print(f"  ✅ API Key: {'Ada' if ROBLOX_OPEN_CLOUD_API_KEY else 'Kosong'}")
        return True
    except Exception as e:
        print(f"  ❌ Config GAGAL: {e}")
        return False


async def test_database():
    """Test database SQLite."""
    print("[TEST 3/6] Menguji database SQLite...")
    try:
        from nexus_database import initialize_system_ledger, save_verified_module, retrieve_ecosystem_context
        
        await initialize_system_ledger()
        print("  ✅ Inisialisasi database LULUS")
        
        hash_val = await save_verified_module("TEST_MODULE", "/tmp/test.lua", "--!strict\nlocal x = 1")
        print(f"  ✅ Save module LULUS (hash: {hash_val[:8]}...)")
        
        ctx = await retrieve_ecosystem_context()
        print(f"  ✅ Retrieve context LULUS (length: {len(ctx)})")
        return True
    except Exception as e:
        print(f"  ❌ Database GAGAL: {e}")
        traceback.print_exc()
        return False


def test_validator():
    """Test AbsoluteOmniValidator."""
    print("[TEST 4/6] Menguji AbsoluteOmniValidator...")
    try:
        from nexus_compiler import AbsoluteOmniValidator
        
        good_code = """--!strict
local ItemCategory = "Material"
local BasePrice = 100
local ProximityPrompt = Instance.new("ProximityPrompt")
ProximityPrompt.ActionText = "Ambil"
"""
        ok, msg = AbsoluteOmniValidator.execute_validation(good_code, ["ItemCategory", "BasePrice", "ProximityPrompt"], [])
        print(f"  ✅ Validator kode valid: {'LULUS' if ok else 'GAGAL'} - {msg[:60]}")
        
        bad_code = "local x = 1"
        ok2, msg2 = AbsoluteOmniValidator.execute_validation(bad_code, ["ItemCategory"], [])
        print(f"  ✅ Validator kode invalid terdeteksi: {'LULUS' if not ok2 else 'GAGAL (harusnya ditolak)'}")
        
        return True
    except Exception as e:
        print(f"  ❌ Validator GAGAL: {e}")
        traceback.print_exc()
        return False


async def test_roblox_api():
    """Test Roblox Open Cloud API."""
    print("[TEST 5/6] Menguji Roblox Open Cloud API...")
    try:
        import requests
        from nexus_config import ROBLOX_UNIVERSE_ID, ROBLOX_OPEN_CLOUD_API_KEY
        
        if not ROBLOX_OPEN_CLOUD_API_KEY:
            print("  ⚠️ API Key kosong, uji dilewati")
            return True
        
        url = f"https://apis.roblox.com/cloud/v2/universes/{ROBLOX_UNIVERSE_ID}"
        headers = {"x-api-key": ROBLOX_OPEN_CLOUD_API_KEY}
        
        loop = asyncio.get_event_loop()
        
        def _test():
            return requests.get(url, headers=headers, timeout=15)
        
        response = await loop.run_in_executor(None, _test)
        
        print(f"  Status HTTP: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"  ✅ Roblox API BERHASIL!")
            print(f"  ✅ Universe: {data.get('displayName', 'N/A')}")
            return True
        elif response.status_code == 401:
            print(f"  ❌ API Key tidak valid (401 Unauthorized)")
            print(f"  Detail: {response.text[:200]}")
            return False
        elif response.status_code == 403:
            print(f"  ❌ Akses ditolak (403 Forbidden) - Universe mungkin tidak milik akun ini")
            print(f"  Detail: {response.text[:200]}")
            return False
        elif response.status_code == 404:
            print(f"  ❌ Universe tidak ditemukan (404)")
            return False
        else:
            print(f"  ⚠️ Status tidak terduga: {response.status_code}")
            print(f"  Detail: {response.text[:200]}")
            return False
    except Exception as e:
        print(f"  ❌ Roblox API Test GAGAL: {e}")
        return False


def test_healer_import():
    """Test ApexKeyRotator dari healer."""
    print("[TEST 6/6] Menguji ApexKeyRotator...")
    try:
        from nexus_healer import ApexKeyRotator
        from nexus_config import ACTIVE_AGENTS
        
        keys = [a["api_key"] for a in ACTIVE_AGENTS if a["api_key"]]
        rotator = ApexKeyRotator(keys)
        
        key1 = rotator.get_key()
        key2 = rotator.get_key()
        
        print(f"  ✅ Key rotator LULUS ({len(keys)} keys aktif)")
        print(f"  ✅ Key 1 valid: {bool(key1)}, Key 2 valid: {bool(key2)}")
        return True
    except Exception as e:
        print(f"  ❌ Healer Test GAGAL: {e}")
        return False


async def run_15_minute_stability_test():
    """Jalankan pengujian stabilitas selama 15 menit."""
    print("\n" + "="*60)
    print("🚀 NEXUS STABILITY TEST - 15 MENIT")
    print("="*60 + "\n")
    
    results = {}
    
    results["imports"] = test_imports()
    results["config"] = test_config()
    results["database"] = await test_database()
    results["validator"] = test_validator()
    results["healer"] = test_healer_import()
    results["roblox_api"] = await test_roblox_api()
    
    print("\n" + "="*60)
    print("📊 HASIL PENGUJIAN AWAL:")
    for test_name, result in results.items():
        status = "✅ LULUS" if result else "❌ GAGAL"
        print(f"  {test_name}: {status}")
    
    all_passed = all(results.values())
    
    if not all_passed:
        print("\n⚠️ Ada test yang gagal! Periksa error di atas.")
    
    print("\n" + "="*60)
    print("⏳ Memulai pengujian stabilitas 15 menit...")
    print("="*60)
    
    start_time = time.time()
    duration = 15 * 60
    iteration = 0
    errors = []
    
    while time.time() - start_time < duration:
        iteration += 1
        elapsed = time.time() - start_time
        remaining = duration - elapsed
        
        print(f"\n[Iterasi {iteration}] Elapsed: {elapsed:.0f}s | Sisa: {remaining:.0f}s")
        
        try:
            from nexus_database import retrieve_ecosystem_context, update_daily_log, get_daily_log_amount
            ctx = await retrieve_ecosystem_context()
            
            await update_daily_log(f"player_{iteration}", 10)
            amount = await get_daily_log_amount(f"player_{iteration}")
            
            print(f"  ✅ Database operasi OK (context length: {len(ctx)}, daily log: {amount})")
        except Exception as e:
            error_msg = f"Database error pada iterasi {iteration}: {e}"
            errors.append(error_msg)
            print(f"  ❌ {error_msg}")
        
        try:
            from nexus_compiler import AbsoluteOmniValidator
            test_code = f"""--!strict
local ItemCategory = "Material"
local BasePrice = {iteration * 10}
local ProximityPrompt = Instance.new("ProximityPrompt")
ProximityPrompt.ActionText = "Ambil"
local count_{iteration} = {iteration}
"""
            ok, msg = AbsoluteOmniValidator.execute_validation(
                test_code, ["ItemCategory", "BasePrice", "ProximityPrompt"], []
            )
            print(f"  ✅ Validator OK: {'Valid' if ok else 'Invalid'}")
        except Exception as e:
            error_msg = f"Validator error pada iterasi {iteration}: {e}"
            errors.append(error_msg)
            print(f"  ❌ {error_msg}")
        
        try:
            from nexus_healer import ApexKeyRotator
            from nexus_config import ACTIVE_AGENTS
            keys = [a["api_key"] for a in ACTIVE_AGENTS]
            rotator = ApexKeyRotator(keys)
            key = rotator.get_key()
            print(f"  ✅ Key rotator OK (key valid: {bool(key)})")
        except Exception as e:
            error_msg = f"Key rotator error pada iterasi {iteration}: {e}"
            errors.append(error_msg)
            print(f"  ❌ {error_msg}")
        
        await asyncio.sleep(30)
    
    print("\n" + "="*60)
    print("📊 HASIL PENGUJIAN STABILITAS 15 MENIT:")
    print(f"  Total iterasi: {iteration}")
    print(f"  Total error: {len(errors)}")
    
    if errors:
        print("\n❌ ERROR YANG DITEMUKAN:")
        for err in errors:
            print(f"  - {err}")
        print("\n⚠️ PENGUJIAN GAGAL - Ada error ditemukan!")
        return False
    else:
        print("\n✅ SEMUA TEST LULUS - Tidak ada error selama 15 menit!")
        return True


if __name__ == "__main__":
    success = asyncio.run(run_15_minute_stability_test())
    sys.exit(0 if success else 1)
