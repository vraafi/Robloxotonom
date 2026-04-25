TELEMETRY_SCRIPT_LUA = """--!strict
-- Nexus Telemetry Script
-- Injected by Nexus AI to monitor runtime errors and player behavior.

local LogService = game:GetService("LogService")
local ScriptContext = game:GetService("ScriptContext")
local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")
local RunService = game:GetService("RunService")

local SERVER_ID = game.JobId
if SERVER_ID == "" then SERVER_ID = "STUDIO_TESTING" end
local WEBHOOK_URL = "http://127.0.0.1:8080/telemetry"

local function SendTelemetry(eventType: string, eventData: any)
    local payload = {
        server_id = SERVER_ID,
        event_type = eventType,
        event_data = eventData
    }
    local success, encoded = pcall(function()
        return HttpService:JSONEncode(payload)
    end)
    if not success then return end

    pcall(function()
        HttpService:PostAsync(WEBHOOK_URL, encoded, Enum.HttpContentType.ApplicationJson, false)
    end)
end

-- Catch all unhandled Lua errors
ScriptContext.Error:Connect(function(message, trace, script)
    local scriptName = "Unknown"
    if script then scriptName = script:GetFullName() end

    SendTelemetry("RUNTIME_ERROR", {
        message = message,
        trace = trace,
        script_name = scriptName
    })
end)

-- Monitor output logs for warnings or errors
LogService.MessageOut:Connect(function(message, messageType)
    if messageType == Enum.MessageType.MessageError or messageType == Enum.MessageType.MessageWarning then
        SendTelemetry("LOG_OUTPUT", {
            type = (messageType == Enum.MessageType.MessageError and "ERROR" or "WARNING"),
            message = message
        })
    end
end)

-- Monitor player death and falling into the void
Players.PlayerAdded:Connect(function(player)
    player.CharacterAdded:Connect(function(character)
        local humanoid = character:WaitForChild("Humanoid")

        humanoid.Died:Connect(function()
            local rootPart = character:FindFirstChild("HumanoidRootPart")
            local deathPosition = "Unknown"
            local isVoidFall = false

            if rootPart and rootPart:IsA("BasePart") then
                deathPosition = tostring(rootPart.Position)
                if rootPart.Position.Y < -50 then
                    isVoidFall = true
                end
            end

            SendTelemetry("PLAYER_DEATH", {
                player_id = player.UserId,
                is_void_fall = isVoidFall,
                death_position = deathPosition
            })
        end)
    end)
end)

print("Nexus Telemetry Active")
"""
