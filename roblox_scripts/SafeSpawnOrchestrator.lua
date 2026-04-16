--!strict
-- Layanan: ServerScriptService
-- Nama: SafeSpawnOrchestrator
-- Fungsi: Mencegah pemain jatuh ke void saat map belum selesai di-render (Master Spawner)
-- Versi: 1.1.0
-- Perubahan:
--   - Perbaikan: Respawn otomatis jika pemain jatuh ke bawah Y < -50
--   - Fitur Baru: Cooldown anti-teleport spam (0.5 detik min antar teleportasi)
--   - Perbaikan: Karakter mati tidak memicu ForceField baru
--   - Fitur Baru: Logging ke output untuk debugging mudah

local Players = game:GetService("Players")
local Workspace = game:GetService("Workspace")
local RunService = game:GetService("RunService")

-- =========================================================================
-- KONFIGURASI
-- =========================================================================

-- Titik aman absolut jika map utama gagal ditemukan (darurat)
local EMERGENCY_SPAWN_CFRAME = CFrame.new(0, 100, 0)

-- Threshold Y: jika pemain jatuh di bawah ini, segera teleport ulang
local VOID_THRESHOLD_Y = -50

-- Cooldown minimum antara dua teleportasi (detik)
local TELEPORT_COOLDOWN = 0.5

-- Durasi ForceField perlindungan saat spawn (detik)
local FORCE_FIELD_DURATION = 5

-- 5 Koordinat horizontal yang berdekatan di atas platform pesawat
-- [PERBAIKAN]: Koordinat disesuaikan dengan posisi SpaceshipSpawnFloor (Y = 1000 + 5/2 + 3 = 1005.5)
local SPAWN_COORDINATES = {
    CFrame.new(4980, 1005, 5000),
    CFrame.new(4990, 1005, 5000),
    CFrame.new(5000, 1005, 5000),
    CFrame.new(5010, 1005, 5000),
    CFrame.new(5020, 1005, 5000)
}

-- =========================================================================
-- FUNGSI INTERNAL
-- =========================================================================

-- Waktu teleportasi terakhir per pemain (untuk cooldown)
local lastTeleportTime: {[Player]: number} = {}

local function log(msg: string): ()
    -- Hanya print di server
    if RunService:IsServer() then
        print("[SafeSpawnOrchestrator]", msg)
    end
end

local function getSafeSpawnLocation(): CFrame
    -- 1. Prioritas Pertama: Platform Pesawat Luar Angkasa (Pilih acak dari 5 koordinat)
    local spaceshipFloor = Workspace:FindFirstChild("SpaceshipSpawnFloor")
    if spaceshipFloor and spaceshipFloor:IsA("BasePart") then
        local randomIndex = math.random(1, #SPAWN_COORDINATES)
        return SPAWN_COORDINATES[randomIndex]
    end

    -- 2. Prioritas Kedua: Baseplate Universal (Lantai Dasar)
    local universalBaseplate = Workspace:FindFirstChild("NexusUniversalBaseplate")
    if universalBaseplate and universalBaseplate:IsA("BasePart") then
        local height = universalBaseplate.Size.Y
        return universalBaseplate.CFrame * CFrame.new(0, (height / 2) + 3, 0)
    end

    -- 3. Prioritas Ketiga: Udara Kosong (Darurat)
    log("⚠️ Tidak ada spawn platform ditemukan! Menggunakan koordinat darurat.")
    return EMERGENCY_SPAWN_CFRAME
end

local function teleportCharacterSafe(character: Model, player: Player): ()
    -- [FITUR BARU]: Cek cooldown sebelum teleport
    local now = tick()
    local lastTime = lastTeleportTime[player] or 0
    if (now - lastTime) < TELEPORT_COOLDOWN then
        return
    end
    lastTeleportTime[player] = now

    local humanoidRootPart = character:FindFirstChild("HumanoidRootPart") :: BasePart?
    local humanoid = character:FindFirstChildOfClass("Humanoid") :: Humanoid?

    if not humanoidRootPart or not humanoid then
        log("⚠️ HumanoidRootPart atau Humanoid tidak ditemukan pada karakter " .. player.Name)
        return
    end

    -- Jangan teleport karakter yang sudah mati
    if humanoid.Health <= 0 then
        return
    end

    -- Hentikan momentum jatuh
    humanoidRootPart.AssemblyLinearVelocity = Vector3.new(0, 0, 0)
    humanoidRootPart.AssemblyAngularVelocity = Vector3.new(0, 0, 0)

    local safeCFrame = getSafeSpawnLocation()
    character:PivotTo(safeCFrame)
    log("✅ Teleportasi " .. player.Name .. " ke " .. tostring(safeCFrame.Position))
end

-- =========================================================================
-- FUNGSI SPAWN UTAMA
-- =========================================================================

local function onCharacterAdded(character: Model, player: Player): ()
    local humanoidRootPart = character:WaitForChild("HumanoidRootPart", 10) :: BasePart?
    local humanoid = character:WaitForChild("Humanoid", 10) :: Humanoid?

    if not humanoidRootPart or not humanoid then
        log("❌ Gagal menunggu komponen karakter " .. player.Name)
        return
    end

    -- [PERBAIKAN ENGINE ROBLOX]: Jeda absolut menunggu engine selesai merakit karakter
    -- Tanpa jeda ini, Roblox engine akan menimpa CFrame kita dengan SpawnLocation default.
    task.wait(0.5)

    -- Berikan pelindung agar tidak mati jika terkena glitch awal
    local forceField = Instance.new("ForceField")
    forceField.Visible = false
    forceField.Parent = character

    -- Teleportasi ke lokasi aman
    teleportCharacterSafe(character, player)

    -- Hapus pelindung setelah durasi yang ditentukan
    task.delay(FORCE_FIELD_DURATION, function()
        if forceField and forceField.Parent then
            forceField:Destroy()
        end
    end)

    -- =========================================================================
    -- [FITUR BARU]: Anti-Void Monitor
    -- Pantau posisi Y pemain dan teleport ulang jika jatuh ke void
    -- =========================================================================
    local voidMonitorConnection: RBXScriptConnection? = nil
    voidMonitorConnection = RunService.Heartbeat:Connect(function()
        -- Hentikan monitor jika karakter sudah tidak valid
        if not character or not character.Parent then
            if voidMonitorConnection then
                voidMonitorConnection:Disconnect()
                voidMonitorConnection = nil
            end
            return
        end

        -- Hentikan monitor jika pemain sudah keluar
        if not player or not player.Parent then
            if voidMonitorConnection then
                voidMonitorConnection:Disconnect()
                voidMonitorConnection = nil
            end
            return
        end

        -- Cek posisi Y
        local hrp = character:FindFirstChild("HumanoidRootPart") :: BasePart?
        if hrp and hrp.Position.Y < VOID_THRESHOLD_Y then
            log("⚠️ " .. player.Name .. " jatuh ke void (Y=" .. tostring(math.floor(hrp.Position.Y)) .. "). Teleport ulang!")
            teleportCharacterSafe(character, player)
        end
    end)

    -- Bersihkan koneksi saat karakter mati atau dihapus
    humanoid.Died:Connect(function()
        if voidMonitorConnection then
            voidMonitorConnection:Disconnect()
            voidMonitorConnection = nil
        end
        lastTeleportTime[player] = nil
        log(player.Name .. " karakter mati. Monitor void dihentikan.")
    end)
end

-- =========================================================================
-- EVENT PLAYER ADDED
-- =========================================================================

Players.PlayerAdded:Connect(function(player: Player)
    -- Tangani karakter yang sudah ada (misalnya saat script reload)
    if player.Character then
        onCharacterAdded(player.Character, player)
    end

    player.CharacterAdded:Connect(function(character: Model)
        onCharacterAdded(character, player)
    end)
end)

-- Bersihkan data pemain saat keluar
Players.PlayerRemoving:Connect(function(player: Player)
    lastTeleportTime[player] = nil
end)

log("✅ SafeSpawnOrchestrator v1.1.0 berhasil dimuat.")
