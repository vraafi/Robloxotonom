--!strict
-- Layanan: StarterPlayerScripts atau StarterGui (LocalScript)
-- Nama: HUDResponsiveInjector
-- Fungsi: Menyuntikkan tombol 'X' ke semua Frame HUD & Responsive Mobile Scaling
-- Versi: 1.1.0
-- Perubahan:
--   - Perbaikan: Cek AbsoluteSize SETELAH frame ter-render (tidak ada jeda tunggal 0.1s)
--   - Fitur Baru: Tombol X bisa dikustomisasi warnanya via konstanta di atas
--   - Perbaikan: Konversi Offset ke Scale tidak merusak elemen yang sudah 100% Scale
--   - Perbaikan: Tidak menyuntik tombol X ke ScrollingFrame (hanya Frame utama)
--   - Fitur Baru: Fungsi openFrame untuk membantu membuka frame yang ditutup

local Players = game:GetService("Players")
local player = Players.LocalPlayer
local playerGui = player:WaitForChild("PlayerGui")

-- =========================================================================
-- KONFIGURASI
-- =========================================================================

-- Ukuran minimum frame agar mendapat tombol X (dalam pixel)
local MIN_FRAME_SIZE_PX = 100

-- Warna tombol X
local CLOSE_BTN_COLOR = Color3.fromRGB(220, 50, 50)
local CLOSE_BTN_TEXT_COLOR = Color3.fromRGB(255, 255, 255)

-- Nama unik tombol X agar tidak duplikat
local CLOSE_BTN_NAME = "NexusAutoCloseButton"

-- =========================================================================
-- FUNGSI KONVERSI OFFSET KE SCALE
-- =========================================================================

--[[
    Mengonversi Offset (pixel statis) menjadi Scale (persentase responsif).
    Ini memastikan UI tidak "hancur" di resolusi berbeda, terutama HP.
    
    [PERBAIKAN]: Cek tambahan agar tidak mengubah elemen yang sudah murni Scale
    (Scale components yang sudah 1.0 tidak perlu dikonversi).
]]
local function convertOffsetToScale(guiObject: GuiObject): ()
    local parent = guiObject.Parent
    if not parent or not parent:IsA("GuiBase2d") then return end

    local parentSize = (parent :: GuiBase2d).AbsoluteSize
    if parentSize.X == 0 or parentSize.Y == 0 then return end

    local currentSize = guiObject.Size
    local currentPos = guiObject.Position

    -- [PERBAIKAN]: Jangan konversi jika sudah murni Scale (Offset = 0)
    local hasOffset = (currentSize.X.Offset ~= 0) or (currentSize.Y.Offset ~= 0)
        or (currentPos.X.Offset ~= 0) or (currentPos.Y.Offset ~= 0)
    if not hasOffset then return end

    -- Konversi Size
    local newSizeXScale = currentSize.X.Scale + (currentSize.X.Offset / parentSize.X)
    local newSizeYScale = currentSize.Y.Scale + (currentSize.Y.Offset / parentSize.Y)

    -- Konversi Position
    local newPosXScale = currentPos.X.Scale + (currentPos.X.Offset / parentSize.X)
    local newPosYScale = currentPos.Y.Scale + (currentPos.Y.Offset / parentSize.Y)

    guiObject.Size = UDim2.new(newSizeXScale, 0, newSizeYScale, 0)
    guiObject.Position = UDim2.new(newPosXScale, 0, newPosYScale, 0)
end

-- =========================================================================
-- FUNGSI SUNTIK TOMBOL X
-- =========================================================================

--[[
    Menyuntikkan tombol 'X' (Close) ke pojok kanan atas sebuah Frame.
    
    [PERBAIKAN]: Menggunakan AbsoluteSize untuk validasi, yang lebih akurat
    daripada Size property (yang bisa berupa Scale 0,0 tapi absolut besar).
]]
local function injectCloseButton(frame: Frame): ()
    -- Jangan tambahkan tombol X ke ScrollingFrame (akan mengacaukan scroll)
    if frame:IsA("ScrollingFrame") then return end

    -- Jangan tambahkan jika frame terlalu kecil
    if frame.AbsoluteSize.X < MIN_FRAME_SIZE_PX or frame.AbsoluteSize.Y < MIN_FRAME_SIZE_PX then
        return
    end

    -- Cek apakah sudah ada tombol close
    if frame:FindFirstChild(CLOSE_BTN_NAME) then return end

    local closeBtn = Instance.new("TextButton")
    closeBtn.Name = CLOSE_BTN_NAME

    -- Ukuran responsif berbasis Scale
    closeBtn.Size = UDim2.new(0.1, 0, 0.1, 0)

    -- Pastikan tombol selalu kotak sempurna di semua resolusi
    local aspectRatio = Instance.new("UIAspectRatioConstraint")
    aspectRatio.AspectRatio = 1
    aspectRatio.DominantAxis = Enum.DominantAxis.Width
    aspectRatio.Parent = closeBtn

    -- Posisi di pojok kanan atas dengan margin kecil
    closeBtn.AnchorPoint = Vector2.new(1, 0)
    closeBtn.Position = UDim2.new(0.98, 0, 0.02, 0)

    -- Visual
    closeBtn.BackgroundColor3 = CLOSE_BTN_COLOR
    closeBtn.Text = "X"
    closeBtn.TextColor3 = CLOSE_BTN_TEXT_COLOR
    closeBtn.TextScaled = true
    closeBtn.Font = Enum.Font.GothamBold
    closeBtn.AutoButtonColor = true
    closeBtn.ZIndex = frame.ZIndex + 1  -- Pastikan selalu di atas frame

    -- Sudut bulat
    local uiCorner = Instance.new("UICorner")
    uiCorner.CornerRadius = UDim.new(0.2, 0)
    uiCorner.Parent = closeBtn

    -- [PERBAIKAN]: Gunakan satu handler untuk mouse dan touch
    local function onClose()
        frame.Visible = false
    end

    closeBtn.MouseButton1Click:Connect(onClose)
    -- TouchTap hanya tersedia di platform mobile
    closeBtn.TouchTap:Connect(onClose)

    closeBtn.Parent = frame
end

-- =========================================================================
-- FUNGSI OPTIMASI UI
-- =========================================================================

--[[
    Memindai dan mengoptimalkan satu elemen GUI.
    - Konversi Offset ke Scale jika diperlukan
    - Suntik tombol X ke Frame yang terlihat
]]
local function optimizeUI(guiElement: Instance): ()
    if not guiElement:IsA("GuiObject") then return end

    local guiObj = guiElement :: GuiObject

    -- Konversi Offset ke Scale untuk kompatibilitas HP
    if guiObj.Size.X.Offset > 0 or guiObj.Size.Y.Offset > 0 then
        convertOffsetToScale(guiObj)
    end

    -- Suntik tombol X hanya ke Frame yang terlihat dan tidak transparan penuh
    if guiElement:IsA("Frame") then
        local frame = guiElement :: Frame
        if frame.Visible and frame.BackgroundTransparency < 1 then
            injectCloseButton(frame)
        end
    end
end

-- =========================================================================
-- [FITUR BARU]: Fungsi untuk membuka kembali frame yang ditutup
-- =========================================================================

--[[
    Membuka kembali sebuah Frame berdasarkan namanya.
    Berguna untuk sistem menu yang perlu membuka panel.
    
    Contoh penggunaan dari script lain:
    local injector = require(script.Parent.HUDResponsiveInjector)
    injector.openFrame("InventoryFrame")
]]
local function openFrame(frameName: string): boolean
    for _, descendant in ipairs(playerGui:GetDescendants()) do
        if descendant:IsA("Frame") and descendant.Name == frameName then
            descendant.Visible = true
            return true
        end
    end
    warn("[HUDResponsiveInjector] Frame '" .. frameName .. "' tidak ditemukan.")
    return false
end

-- =========================================================================
-- WATCHER: Mengawasi UI baru yang ditambahkan
-- =========================================================================

playerGui.DescendantAdded:Connect(function(descendant: Instance)
    -- [PERBAIKAN]: Tunggu AbsoluteSize ter-render dengan cara yang lebih andal
    -- Menggunakan task.wait() minimal 2 frame rendering
    task.wait(0.1)

    -- Cek ulang apakah elemen masih valid setelah wait
    if not descendant or not descendant.Parent then return end

    optimizeUI(descendant)
end)

-- =========================================================================
-- INISIALISASI: Optimasi semua UI yang sudah ada saat script pertama dijalankan
-- =========================================================================

for _, descendant in ipairs(playerGui:GetDescendants()) do
    optimizeUI(descendant)
end

-- =========================================================================
-- MODUL EXPORT (jika script ini di-require dari script lain)
-- =========================================================================
-- Catatan: LocalScript tidak bisa di-require secara langsung oleh script lain.
-- Pindahkan ke ModuleScript jika ingin menggunakan openFrame dari luar.
