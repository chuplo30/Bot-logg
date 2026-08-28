#!/usr/bin/env python3
"""WeAreDev VM runner v3 - Comprehensive execution tracing.

Returns nil for all API operations (like the original stubs) but
LOGS every access, call, and assignment for source reconstruction.

Output format:
  [STUBS_OK]  - Tracing environment ready
  [P]text       - Print output
  [T]entry      - Trace entry
  [DONE]        - Execution complete
"""
import sys
from lupa import LuaRuntime

TRACER_LUA = r'''
local _trace = {}
local _trace_n = 0
local _orig_print = print

local function safe_tostring(v)
    if type(v) == "string" then
        return "\"" .. v:gsub("\"", "\\\"") .. "\""
    end
    if type(v) == "nil" then return "nil" end
    if type(v) == "boolean" then return tostring(v) end
    if type(v) == "function" then return "function" end
    if type(v) == "table" then return "{}" end
    return tostring(v)
end

local function T(entry)
    _trace_n = _trace_n + 1
    _trace[_trace_n] = entry
    _orig_print("[T]" .. entry)
end

local function traced_print(...)
    local args = {...}
    local strs = {}
    for i, v in ipairs(args) do
        strs[i] = tostring(v)
    end
    local line = table.concat(strs, "\t")
    _orig_print("[P]" .. line)
    local arg_strs = {}
    for i, v in ipairs(args) do
        arg_strs[i] = safe_tostring(v)
    end
    T("print(" .. table.concat(arg_strs, ", ") .. ")")
end

-- Tracing proxy: returns nil but logs all access
local function make_tracer(name)
    local proxy = {}
    local mt = {
        __index = function(t, k)
            local kstr = type(k) == "string" and k or tostring(k)
            T(name .. "." .. kstr)
            return nil
        end,
        __newindex = function(t, k, v)
            local kstr = type(k) == "string" and k or tostring(k)
            local vstr = safe_tostring(v)
            T(name .. "." .. kstr .. " = " .. vstr)
        end,
        __call = function(t, ...)
            local args = {}
            for i, a in ipairs({...}) do
                args[i] = safe_tostring(a)
            end
            T(name .. "(" .. table.concat(args, ", ") .. ")")
            return nil
        end,
        __tostring = function(t) return name end,
        __concat = function(a, b) return nil end,
        __len = function(t) return 0 end,
        __add = function(a, b) return nil end,
        __sub = function(a, b) return nil end,
        __mul = function(a, b) return nil end,
        __div = function(a, b) return nil end,
        __mod = function(a, b) return nil end,
        __pow = function(a, b) return nil end,
        __eq = function(a, b) return false end,
        __lt = function(a, b) return false end,
        __le = function(a, b) return false end,
    }
    setmetatable(proxy, mt)
    return proxy
end

_G.print = traced_print
_G.warn = traced_print
_G.info = traced_print

if not _G.getfenv then _G.getfenv = function(l) return _G end end
if not _G.getgenv then _G.getgenv = function() return _G end end
if not _G.setfenv then _G.setfenv = function() end end
if not _G.unpack then _G.unpack = table.unpack end

local _orig_pcall = pcall
_G.pcall = function(f, ...)
    local results = {_orig_pcall(f, ...)}
    local ok = results[1]
    if not ok then
        local err = tostring(results[2])
        -- Skip noisy anti-tamper pow errors (expected in deobf env)
        if not err:find("pow", 1, true) then
            T("-- pcall error: " .. err)
        end
    end
    return table.unpack(results)
end

local _orig_xpcall = xpcall
_G.xpcall = function(f, handler, ...)
    local results = {_orig_xpcall(f, handler, ...)}
    local ok = results[1]
    if not ok then
        T("-- xpcall error: " .. tostring(results[2]))
    end
    return table.unpack(results)
end

local _orig_load = loadstring or load
if _orig_load then
    local _real_load = _orig_load
    _G.load = function(src, ...)
        if type(src) == "string" and #src > 5 then
            local first100 = src:sub(1, 100)
            if not first100:find("bit32", 1, true) and not first100:find("4294967296", 1, true) then
                T("-- loadstring called (" .. #src .. " chars)")
            end
        end
        return _real_load(src, ...)
    end
    _G.loadstring = _G.load
end

_G.newproxy = function(b)
    local t = {}
    if b then setmetatable(t, {__index = function() return nil end}) end
    return t
end

local api_names = {
    "game", "workspace", "Instance", "Enum",
    "Players", "ReplicatedStorage", "ReplicatedFirst",
    "ServerStorage", "ServerScriptService", "StarterGui",
    "StarterPlayer", "StarterPack", "StarterCharacterScripts",
    "Lighting", "Teams", "Chat", "Debris",
    "TweenService", "RunService", "UserInputService",
    "HttpService", "MarketplaceService", "CollectionService",
    "PathfindingService", "SoundService", "TextService",
    "GuiService", "UserSettings", "CoreGui", "CorePackages",
    "VirtualUser", "ContentProvider",
    "DataStoreService", "BadgeService",
    "UDim", "UDim2", "Color3", "Vector2", "Vector3",
    "CFrame", "Ray", "Region3", "TweenInfo",
    "Rect", "Font", "NumberSequence", "ColorSequence",
    "NumberRange", "RaycastParams", "PhysicalProperties",
    "task", "coroutine",
}

for _, api_name in ipairs(api_names) do
    _G[api_name] = make_tracer(api_name)
end

_orig_print("[STUBS_OK]")
'''

def main():
    if len(sys.argv) < 2:
        print("[EX]No input file")
        return

    with open(sys.argv[1], 'r', encoding='utf-8', errors='replace') as f:
        code = f.read()

    lua = LuaRuntime(unpack_returned_tuples=True)
    try:
        lua.execute(TRACER_LUA + '\n' + code)
        print('[DONE]')
    except Exception as e:
        err_str = str(e)
        if len(err_str) > 500:
            err_str = err_str[:500] + '...'
        print(f'[EX]{err_str}')

if __name__ == '__main__':
    main()