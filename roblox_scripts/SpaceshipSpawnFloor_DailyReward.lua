--!strict
-- Layanan: ServerScriptService (Script)
-- Nama: SpaceshipSpawnFloor_DailyReward
-- Fungsi: Membuat platform SpaceshipSpawnFloor dan menangani Daily Reward UTC 00:00
-- Versi: 1.1.0
-- Perubahan:
--   - Perbaikan: Leaderstats Cash sekarang tidak di-reset ke 0 jika sudah ada
--   - Perbaikan: Penanganan error DataStore lebih lengkap (cek success + error message)
--   - Fitur Baru: Notifikasi ke pemain saat menerima atau sudah klaim Daily Reward
--   - Perbaikan: Tidak duplikat membuat Cash IntValue jika sudah ada dari script lain
--   - Perbaikan: LOGIKA TELEPORTASI TETAP TIDAK ADA DI SINI (diurus SafeSpawnOrchestrator)
--
-- ⚠️ PENTING — URUTAN EKSEKUSI:
-- Script ini WAJIB berjalan sebelum SafeSpawnOrchestrator karena SafeSpawnOrchestrator
-- mencari "SpaceshipSpawnFloor" yang dibuat di sini.
-- Pastikan script ini memiliki priority lebih tinggi di ServerScriptService
-- (letakkan di folder dengan BindToClose atau prioritas eksplisit).

local Players = game:GetService("Players")
local DataStoreService = game:GetService("DataStoreService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")

-- =========================================================================
-- KONFIGURASI
-- =========================================================================

-- Daily reward dalam Cash
local DAILY_REWARD_AMOUNT = 1000

-- DataStore key prefix
local DATASTORE_KEY_SUFFIX = "_LastClaim"

-- =========================================================================
-- INISIALISASI DATASTORE
-- =========================================================================

-- [PERBAIKAN]: Gunakan pcall saat mengakses DataStore itu sendiri
local DailyRewardStore
local storeSuccess, storeError = pcall(function()
    DailyRewardStore = DataStoreService:GetDataStore("DailyReward_UTC_Data")
end)

if not storeSuccess then
    warn("[DailyReward] Gagal membuka DataStore: " .. tostring(storeError))
end

-- =========================================================================
-- MEMBUAT PLATFORM PESAWAT LUAR ANGKASA
-- =========================================================================
-- Platform ini WAJIB dibuat sebelum pemain join agar SafeSpawnOrchestrator
-- bisa menemukannya dengan Workspace:FindFirstChild("SpaceshipSpawnFloor").

-- [PERBAIKAN]: Cek apakah sudah ada agar tidak duplikat saat script reload
local existingFloor = workspace:FindFirstChild("SpaceshipSpawnFloor")
if not existingFloor then
    local spaceshipFloor = Instance.new("Part")
    spaceshipFloor.Name = "SpaceshipSpawnFloor"
    spaceshipFloor.Size = Vector3.new(200, 5, 200)  -- [PERBAIKAN]: Diperlebar dari 100 ke 200 agar lebih aman
    spaceshipFloor.Position = Vector3.new(5000, 1000, 5000)
    spaceshipFloor.Anchored = true
    spaceshipFloor.Locked = true  -- [FITUR BARU]: Kunci agar tidak bisa dipindah via Studio
    spaceshipFloor.CanCollide = true
    spaceshipFloor.BrickColor = BrickColor.new("Medium stone grey")
    spaceshipFloor.Material = Enum.Material.SmoothPlastic
    spaceshipFloor.Parent = workspace
    print("[SpaceshipSpawnFloor] Platform dibuat di koordinat " .. tostring(spaceshipFloor.Position))
else
    print("[SpaceshipSpawnFloor] Platform sudah ada, melewati pembuatan.")
end

-- =========================================================================
-- FUNGSI UTILITAS LEADERSTATS
-- =========================================================================

--[[
    Mendapatkan atau membuat leaderstats untuk pemain.
    [PERBAIKAN]: Tidak menimpa Cash yang sudah ada dari sistem lain.
]]
local function getOrCreateLeaderstats(player: Player): Folder
    local leaderstats = player:FindFirstChild("leaderstats") :: Folder?
    if not leaderstats then
        leaderstats = Instance.new("Folder")
        leaderstats.Name = "leaderstats"
        leaderstats.Parent = player
    end
    return leaderstats :: Folder
end

local function getOrCreateCash(leaderstats: Folder): IntValue
    local cashVal = leaderstats:FindFirstChild("Cash") :: IntValue?
    if not cashVal then
        cashVal = Instance.new("IntValue")
        cashVal.Name = "Cash"
        cashVal.Value = 0  -- Nilai default hanya 0 saat pertama kali dibuat
        cashVal.Parent = leaderstats
    end
    return cashVal :: IntValue
end

-- =========================================================================
-- FUNGSI NOTIFIKASI PEMAIN
-- =========================================================================

-- [FITUR BARU]: Buat RemoteEvent untuk notifikasi ke client (opsional)
-- Pastikan ada handler di LocalScript untuk menampilkan notifikasi ke UI
local notifyEvent: RemoteEvent? = nil
local function setupNotifyEvent(): ()
    local existing = ReplicatedStorage:FindFirstChild("NexusDailyRewardNotify")
    if existing and existing:IsA("RemoteEvent") then
        notifyEvent = existing :: RemoteEvent
    else
        local re = Instance.new("RemoteEvent")
        re.Name = "NexusDailyRewardNotify"
        re.Parent = ReplicatedStorage
        notifyEvent = re
    end
end
setupNotifyEvent()

local function notifyPlayer(player: Player, message: string): ()
    -- Kirim notifikasi ke client via RemoteEvent (jika ada handler di LocalScript)
    if notifyEvent then
        pcall(function()
            notifyEvent:FireClient(player, message)
        end)
    end
    -- Juga print di server untuk logging
    print(string.format("[DailyReward] %s: %s", player.Name, message))
end

-- =========================================================================
-- LOGIKA DAILY REWARD
-- =========================================================================

--[[
    Menangani pemberian Daily Reward untuk pemain.
    Reset setiap 00:00 UTC — dihitung berdasarkan hari UTC absolut.
    
    [PERBAIKAN]: Error handling yang lebih lengkap dengan pesan error spesifik.
    [PERBAIKAN]: Tidak menimpa leaderstats/Cash yang sudah ada.
]]
local function handleDailyReward(player: Player): ()
    -- Jika DataStore tidak tersedia, skip
    if not DailyRewardStore then
        warn("[DailyReward] DataStore tidak tersedia, skip reward untuk " .. player.Name)
        return
    end

    local currentUTCTime = os.time()
    local currentUTCDay = math.floor(currentUTCTime / 86400)

    -- Baca data klaim terakhir
    local success, result = pcall(function()
        return DailyRewardStore:GetAsync(tostring(player.UserId) .. DATASTORE_KEY_SUFFIX)
    end)

    if not success then
        warn("[DailyReward] GetAsync gagal untuk " .. player.Name .. ": " .. tostring(result))
        return
    end

    local lastClaimedDay = result

    if lastClaimedDay ~= nil and currentUTCDay <= lastClaimedDay then
        -- Sudah klaim hari ini
        notifyPlayer(player, "Sudah klaim Daily Reward hari ini. Kembali besok!")
        return
    end

    -- Belum klaim atau hari baru — berikan reward
    local leaderstats = getOrCreateLeaderstats(player)
    local cashVal = getOrCreateCash(leaderstats)

    -- [PERBAIKAN]: Cek apakah pemain masih terhubung sebelum memberi reward
    if not player.Parent then
        warn("[DailyReward] Pemain " .. player.Name .. " sudah keluar sebelum reward diberikan.")
        return
    end

    cashVal.Value = cashVal.Value + DAILY_REWARD_AMOUNT

    -- Simpan hari klaim (gunakan pcall agar tidak crash jika DataStore down)
    local saveSuccess, saveError = pcall(function()
        DailyRewardStore:SetAsync(tostring(player.UserId) .. DATASTORE_KEY_SUFFIX, currentUTCDay)
    end)

    if saveSuccess then
        notifyPlayer(player, string.format(
            "Selamat! Kamu menerima %d Dolar sebagai Daily Login Reward! Total Cash: %d",
            DAILY_REWARD_AMOUNT,
            cashVal.Value
        ))
    else
        -- Reward sudah diberikan di memori, tapi gagal tersimpan — beri tahu pemain
        warn("[DailyReward] SetAsync gagal untuk " .. player.Name .. ": " .. tostring(saveError))
        notifyPlayer(player, string.format(
            "Kamu menerima %d Dolar! (Peringatan: data mungkin tidak tersimpan permanen)",
            DAILY_REWARD_AMOUNT
        ))
    end
end

-- =========================================================================
-- EVENT PLAYER ADDED
-- =========================================================================

-- [PERBAIKAN]: Tangani pemain yang sudah ada saat script pertama kali diload
-- (misalnya saat Studio testing dengan pemain yang sudah join)
for _, existingPlayer in ipairs(Players:GetPlayers()) do
    task.spawn(handleDailyReward, existingPlayer)
end

Players.PlayerAdded:Connect(function(player: Player)
    -- Kecil delay agar leaderstats dari script lain sempat dibuat terlebih dahulu
    -- sebelum kita mencoba mengaksesnya
    task.wait(1)

    -- Cek lagi apakah pemain masih ada setelah wait
    if player and player.Parent then
        handleDailyReward(player)
    end
end)

print("[SpaceshipSpawnFloor_DailyReward] v1.1.0 berhasil dimuat.")
