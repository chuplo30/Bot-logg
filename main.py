#!/usr/bin/env python3
"""
Lua Deobfuscation Toolkit v3.0 - Bot Discord + Flask Server
By Hunter Gay - Hunter Team Community

Gộp từ lua_deobf_toolkit_v3.py + wearedev_vm_runner.py
Chạy trên Render với Flask health check.
Lệnh .log → trả file log hoặc link download.
"""

import re
import sys
import os
import zlib
import base64
import time
import json
import math
import uuid
import tempfile
import threading
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify, send_file, abort

app = Flask(__name__)

# ============================================================
# Config
# ============================================================
LOG_DIR = os.environ.get("LOG_DIR", "/tmp/deobf_logs")
os.makedirs(LOG_DIR, exist_ok=True)
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
TZ = timezone(timedelta(hours=7))  # UTC+7

# ============================================================
# Utility
# ============================================================

def eval_arith(expr: str) -> Optional[int]:
    """Evaluate obfuscated arithmetic like -418876+418904."""
    expr = expr.strip().replace('-(-', '+(')
    try:
        return int(eval(expr))
    except:
        return None


def decode_decimal_escapes(s: str) -> str:
    r"""Convert \ddd decimal escapes in a Lua string to actual characters."""
    result = []
    i = 0
    while i < len(s):
        if s[i] == '\\' and i + 1 < len(s) and s[i+1].isdigit():
            num_str = ''
            j = i + 1
            while j < len(s) and j < i + 4 and s[j].isdigit():
                num_str += s[j]
                j += 1
            if num_str:
                result.append(chr(int(num_str)))
                i = j
                continue
        result.append(s[i])
        i += 1
    return ''.join(result)


# ============================================================
# WeAreDev VM Tracer (inline từ wearedev_vm_runner.py)
# ============================================================

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


def run_vm_tracer(code: str, timeout: float = 15) -> Tuple[List[str], List[str], List[str]]:
    """Chạy VM tracer inline (không cần subprocess). Returns (prints, trace, errors)."""
    from lupa import LuaRuntime
    lua = LuaRuntime(unpack_returned_tuples=True)
    prints, trace, errors = [], [], []

    try:
        def capture_print(*args):
            line = '\t'.join(str(a) for a in args)
            if line.startswith('[P]'):
                prints.append(line[3:])
            elif line.startswith('[T]'):
                trace.append(line[3:])
            elif line.startswith('[EX]'):
                errors.append(line[4:])

        lua.globals()['print'] = capture_print
        lua.execute(TRACER_LUA + '\n' + code)
        prints.append('[DONE]')
    except Exception as e:
        err_str = str(e)
        if len(err_str) > 500:
            err_str = err_str[:500] + '...'
        errors.append(err_str)

    return prints, trace, errors


# ============================================================
# Lua Execution Engine (lupa-based)
# ============================================================

class LuaEngine:
    """Lua VM execution engine using lupa (LuaJIT/Lua 5.5)."""

    _instance = None

    def __init__(self):
        try:
            from lupa import LuaRuntime
            self.lua = LuaRuntime(unpack_returned_tuples=True)
            self._setup()
            self.available = True
        except ImportError:
            self.available = False
            print("[!] lupa not installed. VM execution disabled.")

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _setup(self):
        """Set up Lua environment with bit32 polyfill + Roblox stubs."""
        setup_lua = r"""
local bit32={}
_G.bit32=bit32
local function U(x) x=x or 0; if x<0 then x=x+4294967296 end; return x%4294967296 end
bit32.bxor=function(a,b) a,b=U(a),U(b);local r,p=0,1;for i=0,31 do local ba,bb=a%2,b%2;if ba~=bb then r=r+p end;a=(a-ba)/2;b=(b-bb)/2;p=p*2 end;return r end
bit32.band=function(a,b) a,b=U(a),U(b);local r,p=0,1;for i=0,31 do local ba,bb=a%2,b%2;if ba==1 and bb==1 then r=r+p end;a=(a-ba)/2;b=(b-bb)/2;p=p*2 end;return r end
bit32.bor=function(a,b) a,b=U(a),U(b);local r,p=0,1;for i=0,31 do local ba,bb=a%2,b%2;if ba==1 or bb==1 then r=r+p end;a=(a-ba)/2;b=(b-bb)/2;p=p*2 end;return r end
bit32.bnot=function(a) return 4294967295-U(a) end
bit32.lshift=function(a,n) a=U(a);n=n or 0;if n<0 then return bit32.rshift(a,-n) end;if n>=32 then return 0 end;return (a*(2^n))%4294967296 end
bit32.rshift=function(a,n) a=U(a);n=n or 0;if n<0 then return bit32.lshift(a,-n) end;if n>=32 then return 0 end;return math.floor(a/(2^n)) end
bit32.arshift=function(a,n) a=U(a);if a>=2147483648 then a=a-4294967296 end;n=n or 0;if n>=32 then n=31 end;return math.floor(a/(2^n)) end
bit32.btest=function(a,b) return bit32.band(a,b)~=0 end
bit32.lrotate=function(a,n) a=U(a);n=n%32;if n<0 then n=n+32 end;return bit32.bor(bit32.lshift(a,n),bit32.rshift(a,32-n)) end
bit32.rrotate=function(a,n) a=U(a);n=n%32;if n<0 then n=n+32 end;return bit32.bor(bit32.rshift(a,n),bit32.lshift(a,32-n)) end
bit32.extract=function(a,f,w) w=w or 1;return bit32.band(bit32.rshift(a,f),2^w-1) end
bit32.replace=function(a,v,f,w) w=w or 1;local m=bit32.lshift(2^w-1,f);return bit32.bor(bit32.band(a,bit32.bnot(m)),bit32.lshift(bit32.band(v,2^w-1),f)) end

if not _G.getfenv then _G.getfenv=function(l) return _G end end
if not _G.getgenv then _G.getgenv=function() return _G end end
if not _G.setfenv then _G.setfenv=function() end end

local function deep_stub()
    return setmetatable({},{
        __call=function(self,...) return nil end,
        __index=function(t,k) return deep_stub() end,
        __newindex=function(t,k,v) end,
    })
end

for _,g in ipairs({"task","game","Instance","TweenService","UDim2","Color3","Vector3","Vector2","CFrame","Enum","workspace","HttpService","Players","ReplicatedStorage","RunService","UserInputService","Lighting","Debris","StarterGui","StarterPlayer","StarterPack","Teams","Chat","CollectionService","PathfindingService","SoundService","TextService","GuiService","UserSettings","CoreGui","Rect","UDim","Font","NumberSequence","ColorSequence","NumberRange","TweenInfo","RaycastParams","Material","UGCValidationService","MarketplaceService"}) do
    _G[g] = deep_stub()
end

print("_SETUP_OK")
"""
        self.lua.execute(setup_lua)

    def execute_and_capture(self, code: str, timeout: float = 20) -> Tuple[bool, str, List[str]]:
        """
        Execute Lua code and capture print output + loadstring calls.
        Returns (success, source_or_error, print_lines)
        """
        if not self.available:
            return False, "lupa not available", []

        runner_lua = r"""
local code = ...

local _orig_print = print
local _orig_load = load
local _print_output = {}
local captured_loads = {}
local load_count = 0

_G.print = function(...)
    local args = {...}
    local strs = {}
    for i, v in ipairs(args) do strs[i] = tostring(v) end
    local line = table.concat(strs, "\t")
    _print_output[#_print_output+1] = line
end
_G.warn = _G.print
_G.info = _G.print

_G.load = function(src, ...)
    load_count = load_count + 1
    if load_count > 1 and type(src) == "string" and #src > 10 then
        local first300 = src:sub(1, 300)
        local is_vm = first300:find("bit32", 1, true) or first300:find("4294967296", 1, true) or first300:find("getfenv", 1, true)
        if not is_vm then
            captured_loads[#captured_loads+1] = src
        end
    end
    return _orig_load(src, ...)
end
if not _G.loadstring then
    _G.loadstring = _G.load
end

local fn, err = load(code)
if not fn then
    return {status="compile_error", error=tostring(err), prints={}}
end

local ok, result = pcall(fn)

if #captured_loads > 0 then
    local loads = {}
    for i, c in ipairs(captured_loads) do loads[i] = c end
    return {status="captured", loads=loads, prints=_print_output}
end

return {status=ok and "ok" or "runtime_error", error=ok and nil or tostring(result), result_type=type(result), prints=_print_output}
"""

        start = time.time()
        try:
            result = self.lua.execute(runner_lua, code)
            elapsed = time.time() - start

            def lua2py(obj):
                if hasattr(obj, 'keys'):
                    d = {str(k): lua2py(obj[k]) for k in obj.keys()}
                    int_keys = [k for k in obj.keys() if isinstance(k, int)]
                    if int_keys and max(int_keys) == len(int_keys) and min(int_keys) == 1:
                        return [lua2py(obj[i]) for i in range(1, len(int_keys)+1)]
                    return d
                elif hasattr(obj, 'values'):
                    return [lua2py(v) for v in obj.values()]
                return obj

            result = lua2py(result) if hasattr(result, 'keys') else result

            if isinstance(result, dict):
                status = result.get("status", "unknown")
                prints = result.get("prints", [])
                loads = result.get("loads", [])
                if not isinstance(prints, list): prints = []
                if not isinstance(loads, list): loads = []

                if status == "captured" and loads:
                    return True, loads[0], prints
                if status == "ok":
                    return True, None, prints
                return False, result.get("error", "unknown error"), prints
            else:
                return True, None, []

        except Exception as e:
            elapsed = time.time() - start
            err_str = str(e)
            if elapsed >= timeout - 1:
                return False, "Execution timed out", []
            return False, err_str, []

    def execute_simple(self, code: str, timeout: float = 15) -> Tuple[bool, List[str]]:
        """Execute and only capture print output."""
        ok, source, prints = self.execute_and_capture(code, timeout)
        return ok, prints


# ============================================================
# Obfuscator Detector
# ============================================================

class ObfuscatorDetector:
    SIGNATURES = [
        ("IronBrew2", ["LOL!", "IronBrew-2.0"]),
        ("IronBrew",  ["LOL!"]),
        ("AstroProtect", ["AstroProtect"]),
        ("WAN OBFUSCATE", ["WAN OBFUSCATE"]),
        ("WAN OBFUSCATOR", ["WAN OBFUSCATOR"]),
        ("MoonSec", ["MoonSec"]),
        ("Clyde Protection", ["Clyde"]),
        ("PSU", ["PSU", "Prometheus"]),
        ("Luraph", ["Luraph", "luraph"]),
        ("Oxy", ["Oxy"]),
        ("WeAreDev", ["wearedevs.net/obfuscator"]),
    ]

    @classmethod
    def detect(cls, code: str) -> Optional[str]:
        for name, sigs in cls.SIGNATURES:
            for sig in sigs:
                if sig in code:
                    return name
        if cls._has_vm_pattern(code):
            return "Unknown VM-based"
        if cls._is_base64_compressed(code):
            return "Base64+Compressed"
        return None

    @classmethod
    def _has_vm_pattern(cls, code: str) -> bool:
        indicators = [
            r"bit32\s*\.\s*bxor", r"4294967296",
            r"getfenv|getgenv",
            r"setmetatable.*__index",
            r"while true do.*elseif.*==",
        ]
        return sum(1 for p in indicators if re.search(p, code)) >= 3

    @classmethod
    def _is_base64_compressed(cls, code: str) -> bool:
        b64_strings = re.findall(r'[A-Za-z0-9+/]{100,}={0,2}', code)
        return any(len(s) > 500 for s in b64_strings)


# ============================================================
# Deobfuscation Engines
# ============================================================

class Base64CompressDeobfuscator:
    """Base64 + DEFLATE/ZLIB/GZIP -> Lua source."""

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        for sig in ["AstroProtect", "WAN OBFUSCATE", "WAN OBFUSCATOR",
                    "MoonSec", "Clyde", "IronBrew", "LOL!",
                    "Luraph", "PSU", "Prometheus", "Oxy"]:
            if sig in code:
                return None

        b64_match = re.search(r'[A-Za-z0-9+/]{100,}={0,2}', code)
        if not b64_match:
            return None

        b64_str = b64_match.group(0)
        pad = 4 - len(b64_str) % 4
        if pad < 4:
            b64_str += "=" * pad

        try:
            compressed = base64.b64decode(b64_str)
        except Exception:
            return None

        for wbits, name in [(-15, "raw DEFLATE"), (15, "zlib"), (31, "gzip"), (47, "auto-gzip")]:
            try:
                decompressed = zlib.decompress(compressed, wbits)
                source = decompressed.decode("utf-8", errors="replace")
                vm_indicators = ["bit32", "4294967296", "while true do", "getfenv"]
                vm_score = sum(1 for v in vm_indicators if v in source)

                meta = {
                    "method": f"base64 + {name}",
                    "b64_len": len(b64_str),
                    "compressed": len(compressed),
                    "decompressed": len(decompressed),
                    "vm_wrapped": vm_score > 2,
                }

                if vm_score > 2 and engine.available:
                    if verbose:
                        print(f"  [*] Decompressed content is VM-wrapped, executing...")
                    ok, src, prints = engine.execute_and_capture(source, timeout=30)
                    if src and len(src) > 5 and "bit32" not in src[:100]:
                        meta["method"] += " + VM execution"
                        return src, meta
                    if ok and prints:
                        meta["prints"] = prints
                        from_recon = SourceReconstructor.from_prints(prints)
                        return from_recon, meta

                return source, meta
            except Exception:
                continue

        return None


class AstroProtectDeobfuscator:
    """AstroProtect 2.2: base64 -> DEFLATE -> Lua VM -> execute."""

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if "AstroProtect" not in code:
            return None

        b64_match = re.search(r'\w+="([A-Za-z0-9+/=]{200,})"', code)
        if not b64_match:
            return None

        b64_str = b64_match.group(1)
        pad = 4 - len(b64_str) % 4
        if pad < 4:
            b64_str += "=" * pad

        try:
            compressed = base64.b64decode(b64_str)
            vm_code = zlib.decompress(compressed, -15).decode("utf-8", errors="replace")
        except Exception:
            return None

        if verbose:
            opcodes = re.findall(r'elseif ox==(\d+)', vm_code)
            h_table = re.findall(r'\{\d+,\d+,\{[^}]*\},\{[^}]*\}\}', vm_code)
            print(f"  [*] DEFLATE: {len(compressed)} -> {len(vm_code)} bytes")
            print(f"  [*] VM opcodes: {len(set(int(x) for x in opcodes)) if opcodes else 0}")
            print(f"  [*] Encrypted strings: {len(h_table)}")

        if engine.available:
            if verbose:
                print("  [*] Executing VM...")

            ok, source, prints = engine.execute_and_capture(code, timeout=30)
            if verbose:
                print(f"  [*] VM result: ok={ok}, source_len={len(source) if source else 0}, prints={prints}")

            if source and len(source) > 10 and "bit32" not in source[:200]:
                return source, {"method": "VM execution (loadstring capture)"}

            if ok and prints:
                recovered = SourceReconstructor.from_prints(prints)
                return recovered, {
                    "method": "VM execution (print trace)",
                    "print_count": len(prints),
                }

            if not ok:
                err_str = str(source) if source else ""
                if "attempt to call a table value" in err_str:
                    if verbose:
                        print("  [*] Trying direct VM execution...")
                    wrapper = r'''
local _print_output = {}
local _orig_print = print
_G.print = function(...)
    local args = {...}
    local strs = {}
    for i, v in ipairs(args) do strs[i] = tostring(v) end
    _print_output[#_print_output+1] = table.concat(strs, "\t")
end
_G.warn = _G.print

local code = ...
local fn = load(code)
if fn then
    local ok = pcall(fn)
    if ok then
        return {status="ok", prints=_print_output}
    else
        return {status="error", prints=_print_output}
    end
end
return {status="load_error", prints={}}
'''
                    try:
                        result = engine.lua.execute(wrapper, vm_code)
                        if isinstance(result, dict):
                            pr = result.get("prints", [])
                            if pr:
                                recovered = SourceReconstructor.from_prints(pr)
                                return recovered, {"method": "direct VM execution", "print_count": len(pr)}
                    except Exception:
                        pass

                if verbose:
                    print(f"  [!] VM error: {err_str[:100]}")

        return None, {"method": "static analysis only", "vm_size": len(vm_code)}


class IronBrewDeobfuscator:
    """IronBrew / IronBrew2: RLE bytecode -> XOR strings -> execute."""

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if "LOL!" not in code:
            return None

        if engine.available:
            if verbose:
                print("  [*] Executing IronBrew2 VM...")
            ok, source, prints = engine.execute_and_capture(code, timeout=20)
            if source and len(source) > 5 and "LOL!" not in source[:50]:
                return source, {"method": "VM execution (loadstring capture)"}
            if ok and prints:
                recovered = SourceReconstructor.from_prints(prints)
                return recovered, {"method": "VM execution (print trace)", "print_count": len(prints)}

        strings = IronBrewDeobfuscator._extract_strings(code)
        lines = ["-- IronBrew2 Deobfuscated (string extraction)"]
        lines.append(f"-- Recovered {len(strings)} strings:")
        for i, s in enumerate(strings):
            lines.append(f"--   [{i}] = {repr(s)}")
        lines.append("")
        lines.append("-- Full deobfuscation requires VM execution (lupa)")
        return "\n".join(lines), {"method": "string extraction", "strings": len(strings)}

    @staticmethod
    def _extract_strings(code: str) -> List[str]:
        strings = []
        str_literals = re.findall(r'"([A-Za-z_][A-Za-z0-9_]{2,})"', code)
        api_names = {"print", "warn", "game", "Instance", "workspace", "wait",
                     "GetService", "FindFirstChild", "Clone", "Destroy",
                     "CFrame", "Vector3", "Color3", "UDim2", "TweenInfo",
                     "TweenService", "Players", "LocalPlayer", "Character",
                     "Humanoid", "Head", "Torso", "Position", "Size"}
        for s in str_literals:
            if s in api_names or (len(s) > 4 and s[0].islower()):
                strings.append(s)
        return list(set(strings))


class WANDeobfuscator:
    """WAN OBFUSCATE / WAN OBFUSCATOR: byte table + XOR + VM."""

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if "WAN OBFUSCATE" not in code and "WAN OBFUSCATOR" not in code:
            return None

        if engine.available:
            if verbose:
                print("  [*] Executing WAN VM...")
            ok, source, prints = engine.execute_and_capture(code, timeout=20)
            if source and len(source) > 5 and "WAN" not in source[:50]:
                return source, {"method": "VM execution (loadstring capture)"}
            if ok and prints:
                recovered = SourceReconstructor.from_prints(prints)
                return recovered, {"method": "VM execution (print trace)", "print_count": len(prints)}

        return None, {"method": "requires VM execution"}


class MoonSecDeobfuscator:
    """MoonSec V3: serialized Lua bytecode."""

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if "MoonSec" not in code and "moonsec" not in code.lower():
            return None

        if engine.available:
            if verbose:
                print("  [*] Executing MoonSec V3...")
            ok, source, prints = engine.execute_and_capture(code, timeout=20)
            if source and len(source) > 5 and "MoonSec" not in source[:50]:
                return source, {"method": "VM execution (loadstring capture)"}
            if ok and prints:
                recovered = SourceReconstructor.from_prints(prints)
                return recovered, {"method": "VM execution (print trace)", "print_count": len(prints)}

        b64_match = re.search(r'"([A-Za-z0-9+/=]{100,})"', code)
        lines = ["-- MoonSec V3 (structural analysis)"]
        if b64_match:
            lines.append(f"-- Encoded bytecode: {len(b64_match.group(1))} chars")
        entries = re.findall(r'\{(\d+),\s*\d+,\s*\{', code)
        if entries:
            lines.append(f"-- Helper entries: {len(entries)}")
        lines.append("-- Use lupa to execute VM and recover source.")
        return "\n".join(lines), {"method": "static analysis"}


class ClydeDeobfuscator:
    """Clyde Protection v2: Ascii85 + S-box XOR chain."""

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if "Clyde" not in code:
            return None

        if engine.available:
            if verbose:
                print("  [*] Executing Clyde Protection v2...")
            ok, source, prints = engine.execute_and_capture(code, timeout=20)
            if source and len(source) > 5 and "Clyde" not in source[:50]:
                return source, {"method": "VM execution (loadstring capture)"}
            if ok and prints:
                recovered = SourceReconstructor.from_prints(prints)
                return recovered, {"method": "VM execution (print trace)", "print_count": len(prints)}

        tables = re.findall(r'local\s+\w+\s*=\s*\{([^}]{50,})\}', code)
        ascii85 = re.search(r'<~([A-Za-z0-9!#$%&*+/=?@^_`{|}~-]+)~>', code)
        lines = ["-- Clyde Protection v2 (structural analysis)"]
        lines.append(f"-- Data tables: {len(tables)}")
        if ascii85:
            lines.append(f"-- Ascii85 payload: {len(ascii85.group(1))} chars")
        lines.append("-- Decryption: Ascii85 -> S-box CBC XOR -> key XOR -> position XOR")
        return "\n".join(lines), {"method": "static analysis", "tables": len(tables)}


class WeAreDevDeobfuscator:
    """WeAreDev Obfuscator v1.0.0 - Full decompiler with execution tracing.

    Architecture:
    - Phase 1: Decode P-table string constants via Lua injection
    - Phase 2: Run VM with tracing proxies (captures ALL operations)
    - Phase 3: Reconstruct Lua source from execution trace
    - Phase 4: Generate comprehensive output
    """

    M_OFFSET = 472584 - 466871  # 5713

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if not engine.available:
            return None

        obf = re.sub(r'^--\[\[.*?\]\]\s*', '', code)

        # Phase 1: Decode P-table
        if verbose:
            print("  [*] Phase 1: Decoding P-table string constants...")
        P_decoded = WeAreDevDeobfuscator._decode_p_table(obf, engine)
        if not P_decoded:
            if verbose:
                print("  [!] Failed to decode P-table")
            return None

        string_map = WeAreDevDeobfuscator._build_string_map(obf, P_decoded)
        real_strings = {k: v for k, v in string_map.items()
                        if v and not re.match(r'^[A-Za-z0-9]{8,20}$', v)}

        if verbose:
            print(f"  [*] P-table: {len(P_decoded)} entries, {len(real_strings)} meaningful strings")

        # Phase 2: Execute VM with tracing (inline, không subprocess)
        if verbose:
            print("  [*] Phase 2: Executing VM with full tracing (15s timeout)...")

        prints, trace, errors = run_vm_tracer(obf)

        if verbose:
            print(f"  [*] Captured: {len(prints)} prints, {len(trace)} trace entries, {len(errors)} errors")

        # Phase 3: Reconstruct source from trace
        reconstructed = WeAreDevDeobfuscator._reconstruct_source(trace, prints)

        # Phase 4: Generate output
        source = WeAreDevDeobfuscator._generate_output(
            obf, P_decoded, string_map, prints, trace, errors, reconstructed, verbose)

        meta = {
            "method": "P-table decode + traced VM execution + source reconstruction",
            "p_entries": len(P_decoded),
            "strings_decoded": len(real_strings),
            "print_count": len(prints),
            "trace_entries": len(trace),
            "reconstructed_lines": len(reconstructed.split('\n')) if reconstructed else 0,
        }

        return source, meta

    @staticmethod
    def _decode_p_table(obf: str, engine: LuaEngine) -> Optional[Dict[int, str]]:
        """Decode P-table by injecting print before CFF return."""
        inject_marker = 'return(function(P,l,g,d,Q,z,H,O,c,j,x,f,R,U,k,a,v,K,C,E)'
        inject_pos = obf.find(inject_marker)
        if inject_pos == -1:
            return None

        inject = 'do \n  for i=1,#P do \n    if type(P[i])=="string" and #P[i]>0 then \n      local hex="" \n      for ci=1,#P[i] do hex=hex..string.format("%02x",P[i]:byte(ci)) end \n      print("PDEC|"..i.."|"..hex) \n    else \n      print("PDEC|"..i.."|") \n    end \n  end \n  print("PDEC_DONE") \n  return nil \nend \n'

        modified = obf[:inject_pos] + inject + obf[inject_pos:]

        captured = []
        engine.lua.globals()['print'] = lambda *args: captured.append(' '.join(str(a) for a in args))

        try:
            engine.lua.execute(modified)
        except:
            pass

        engine._setup()

        P_hex = {}
        for line in captured:
            if line.startswith('PDEC|'):
                parts = line.split('|')
                idx = int(parts[1])
                P_hex[idx] = parts[2] if len(parts) > 2 else ''

        P_decoded = {}
        for idx, h in P_hex.items():
            if h:
                try:
                    raw = bytes.fromhex(h)
                    P_decoded[idx] = raw.decode('utf-8')
                except:
                    P_decoded[idx] = f'[hex:{h}]'
            else:
                P_decoded[idx] = ''

        return P_decoded if P_decoded else None

    @staticmethod
    def _build_string_map(obf: str, P_decoded: Dict[int, str]) -> Dict[int, str]:
        """Build M(value) -> decoded string mapping."""
        string_map = {}
        m_pattern = r'M\((-?\d+\+-?\d+)\)'
        for m in re.finditer(m_pattern, obf):
            val = eval_arith(m.group(1))
            if val is not None:
                idx = val - WeAreDevDeobfuscator.M_OFFSET
                if idx in P_decoded:
                    string_map[val] = P_decoded[idx]
        return string_map

    @staticmethod
    def _reconstruct_source(trace: List[str], prints: List[str]) -> str:
        """Convert trace entries to reconstructed Lua source."""
        if not trace:
            return ''

        comments = []
        code_entries = []
        for entry in trace:
            if entry.startswith('--'):
                comments.append(entry)
            else:
                code_entries.append(entry)

        filtered = []
        for i, entry in enumerate(code_entries):
            is_prefix = any(
                j != i and (other.startswith(entry + '.') or other.startswith(entry + '('))
                for j, other in enumerate(code_entries)
            )
            if not is_prefix:
                filtered.append(entry)

        seen = set()
        unique = []
        for entry in filtered:
            if entry not in seen:
                seen.add(entry)
                unique.append(entry)

        lines = []

        for c in comments:
            if 'pow' not in c and 'Tamper' not in c.lower():
                lines.append(c)

        has_print_in_trace = any(e.startswith('print(') for e in unique)
        for entry in unique:
            lines.append(entry)

        if not has_print_in_trace:
            for p in prints:
                try:
                    float(p)
                    stmt = f'print({p})'
                except ValueError:
                    stmt = f'print("{p}")'
                lines.append(stmt)

        return '\n'.join(lines)

    @staticmethod
    def _generate_output(obf: str, P_decoded: Dict[int, str],
                         string_map: Dict[int, str],
                         prints: List[str], trace: List[str],
                         errors: List[str], reconstructed: str,
                         verbose: bool) -> str:
        """Generate the final deobfuscated output."""
        lines = []
        lines.append('-- Deobfuscated by Hunter Gay - Lua Deobfuscation Toolkit v3.0')
        lines.append('-- WeAreDev Obfuscator v1.0.0')
        lines.append('')

        has_reconstructed = reconstructed and len(reconstructed.strip()) > 0
        if has_reconstructed:
            lines.append('-- ============================================')
            lines.append('-- RECONSTRUCTED SOURCE CODE')
            lines.append('-- ============================================')
            lines.append(reconstructed)
            lines.append('')

        if prints:
            lines.append('-- ============================================')
            lines.append('-- SCRIPT OUTPUT')
            lines.append('-- ============================================')
            for p in prints:
                lines.append(f'-- output: {p}')
            lines.append('')

        non_tamper = [e for e in errors if 'pow' not in e.lower() and 'Tamper' not in e]
        tamper_count = len(errors) - len(non_tamper)

        if non_tamper:
            lines.append('-- ============================================')
            lines.append('-- RUNTIME ERRORS')
            lines.append('-- ============================================')
            for e in non_tamper[:10]:
                lines.append(f'--   {e[:200]}')
            lines.append('')

        if tamper_count > 0 and verbose:
            lines.append(f'-- Note: {tamper_count} anti-tamper check(s) failed (expected)')
            lines.append('')

        lines.append('-- ============================================')
        lines.append('-- DECODED STRING CONSTANTS (P-table)')
        lines.append('-- ============================================')

        meaningful = {}
        for idx in sorted(P_decoded.keys()):
            s = P_decoded[idx]
            if not s or not s.strip():
                continue
            if re.match(r'^[A-Za-z0-9]{8,20}$', s):
                continue
            meaningful[idx] = s

        if meaningful:
            for idx, s in sorted(meaningful.items()):
                lines.append(f'--   [{idx}] = {repr(s)}')
            lines.append('')

        lines.append('-- ============================================')
        lines.append('-- M() FUNCTION REFERENCE MAP')
        lines.append(f'-- M(x) = P[x - {WeAreDevDeobfuscator.M_OFFSET}]')
        lines.append('-- ============================================')

        if string_map:
            for val in sorted(string_map.keys()):
                s = string_map[val]
                if s and not re.match(r'^[A-Za-z0-9]{8,20}$', s):
                    lines.append(f'--   M({val}) = {repr(s)}')
            lines.append('')

        lines.append('-- ============================================')
        lines.append('-- SCRIPT BEHAVIOR ANALYSIS')
        lines.append('-- ============================================')

        all_str = ' '.join(str(v) for v in meaningful.values())
        behaviors = []

        checks = [
            ('print' in all_str, 'Outputs text via print()'),
            ('pcall' in all_str, 'Uses pcall for error handling'),
            ('setmetatable' in all_str and '__index' in all_str, 'OOP-style tables with metatables'),
            ('__metatable' in all_str, 'Protects metatable access (anti-inspect)'),
            ('gsub' in all_str or 'gmatch' in all_str, 'Pattern matching (gsub/gmatch)'),
            ('random' in all_str, 'Uses math.random'),
            ('byte' in all_str or 'char' in all_str, 'String byte-level processing'),
            ('concat' in all_str, 'Uses table.concat'),
            ('tonumber' in all_str, 'String to number conversion'),
            ('floor' in all_str, 'Uses math.floor'),
            ('error' in all_str and 'Tamper' in all_str, 'Anti-tamper protection'),
            (len(trace) > 5, f'Makes {len(trace)}+ API/operations calls'),
        ]

        for cond, desc in checks:
            if cond:
                behaviors.append(desc)

        if behaviors:
            for b in behaviors:
                lines.append(f'--   [+] {b}')
        else:
            lines.append('--   (no specific behavior identified)')
        lines.append('')

        lines.append('-- ============================================')
        lines.append('-- SIMPLIFIED CFF (M() calls resolved to strings)')
        lines.append('-- ============================================')

        simplified = obf

        def replace_m(m):
            expr = m.group(1)
            val = eval_arith(expr)
            if val is not None and val in string_map:
                s = string_map[val]
                if s and not re.match(r'^[A-Za-z0-9]{8,20}$', s):
                    return repr(s)
            return m.group(0)

        simplified = re.sub(r'M\((-?\d+\+-?\d+)\)', replace_m, simplified)
        simplified = re.sub(r'=(\s*)(-?\d+\+-?\d+)', lambda m: '=' + m.group(1) + str(eval_arith(m.group(2)) or m.group(2)), simplified)

        if len(simplified) > 2000:
            lines.append(f'-- (showing first 2000 of {len(simplified)} chars)')
            lines.append(simplified[:2000])
            lines.append('-- ... (truncated)')
        else:
            lines.append(simplified)

        lines.append('')

        return '\n'.join(lines)


class GenericVMDeobfuscator:
    """Generic VM-based: try execution and capture output."""

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if engine.available:
            if verbose:
                print("  [*] Attempting VM execution...")
            ok, source, prints = engine.execute_and_capture(code, timeout=20)
            if source and len(source) > 5:
                return source, {"method": "VM execution (loadstring capture)"}
            if ok and prints:
                recovered = SourceReconstructor.from_prints(prints)
                return recovered, {"method": "VM execution (print trace)", "print_count": len(prints)}

        while_loops = code.count("while true do")
        cff = len(re.findall(r'=\s*\d+\s*\+\s*\w+', code))
        lines = ["-- Generic VM Analysis"]
        lines.append(f"-- while loops: {while_loops}, CFF patterns: {cff}")
        lines.append("-- Requires VM execution for full deobfuscation.")
        return "\n".join(lines), {"method": "static analysis"}


# ============================================================
# Source Reconstructor
# ============================================================

class SourceReconstructor:
    """Reconstruct original Lua source from execution traces."""

    @staticmethod
    def from_prints(prints: List[str]) -> str:
        if not prints:
            return "-- No output captured"

        lines = []
        for p in prints:
            try:
                float(p)
                lines.append(f"print({p})")
            except ValueError:
                lines.append(f'print("{p}")')
        return "\n".join(lines)

    @staticmethod
    def from_api_calls(calls: List[dict]) -> str:
        lines = []
        for call in calls:
            lines.append(call.get("raw", "-- unknown call"))
        return "\n".join(lines)


# ============================================================
# Main Deobfuscation Pipeline
# ============================================================

class LuaDeobfuscator:
    """Multi-pass Lua deobfuscation engine."""

    DEOBFUSCATORS = [
        AstroProtectDeobfuscator,
        IronBrewDeobfuscator,
        WANDeobfuscator,
        MoonSecDeobfuscator,
        ClydeDeobfuscator,
        WeAreDevDeobfuscator,
        Base64CompressDeobfuscator,
        GenericVMDeobfuscator,
    ]

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.engine = LuaEngine.get()

    def deobfuscate_code(self, code: str, name: str = "input") -> Tuple[str, str, dict]:
        """Deobfuscate Lua code string. Returns (obfuscator_name, source, metadata)"""
        detected = ObfuscatorDetector.detect(code)
        if self.verbose:
            print(f"[*] File: {name}")
            print(f"[*] Size: {len(code):,} chars")
            print(f"[*] Detected: {detected or 'Unknown'}")

        source = None
        meta = {"detected": detected}
        obf_name = detected or "Unknown"
        prints = []

        for deobf_cls in self.DEOBFUSCATORS:
            cls_name = deobf_cls.__name__.replace("Deobfuscator", "")

            if detected and cls_name not in ("GenericVM", "Base64Compress"):
                if cls_name.lower() not in detected.lower():
                    continue

            if self.verbose:
                print(f"[*] Trying {cls_name}...")

            try:
                result = deobf_cls.deobfuscate(code, self.engine, self.verbose)
                if result is None:
                    continue

                recovered, result_meta = result
                meta.update(result_meta)

                if recovered and len(recovered) > 5:
                    source = recovered
                    obf_name = detected or cls_name
                    break

                if "prints" in result_meta:
                    prints = result_meta["prints"]

            except Exception as e:
                if self.verbose:
                    print(f"[!] {cls_name} error: {e}")
                meta["error"] = str(e)

        if not source and prints:
            source = SourceReconstructor.from_prints(prints)
            meta["reconstructed_from"] = "print traces"

        if not source:
            source = f"-- Deobfuscation incomplete\n-- Obfuscator: {obf_name}\n-- The script uses VM-based obfuscation.\n-- Full source recovery requires manual VM analysis."

        return obf_name, source, meta

    def deobfuscate_file(self, filepath: str) -> Tuple[str, str, dict]:
        """Deobfuscate file -> (obfuscator_name, source, metadata)"""
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()
        return self.deobfuscate_code(code, filepath)

    def detect_only(self, filepath: str) -> str:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()
        return ObfuscatorDetector.detect(code) or "Unknown/Clear text"


def format_output(obf_name: str, source: str, meta: dict, verbose: bool) -> str:
    """Format the final output."""
    lines = [f"-- Deobfuscated by Hunter Gay - Lua Deobfuscation Toolkit v3.0"]
    lines.append(f"-- Obfuscator: {obf_name}")
    for k, v in meta.items():
        if k not in ("error", "prints"):
            lines.append(f"-- {k}: {v}")
    lines.append("")
    lines.append(source)
    return "\n".join(lines)


# ============================================================
# Logging System cho lệnh .log
# ============================================================

def write_log(user_id: str, username: str, action: str, result: str, meta: dict = None) -> str:
    """Ghi log vào file và trả về file path."""
    log_id = uuid.uuid4().hex[:8]
    timestamp = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    log_filename = f"log_{timestamp.replace(':', '-').replace(' ', '_')}_{log_id}.txt"
    log_path = os.path.join(LOG_DIR, log_filename)

    lines = [
        f"=== Lua Deobfuscation Log ===",
        f"Time: {timestamp} (UTC+7)",
        f"User: {username} ({user_id})",
        f"Action: {action}",
        f"",
    ]

    if meta:
        lines.append("--- Metadata ---")
        for k, v in meta.items():
            if k not in ("error", "prints"):
                lines.append(f"  {k}: {v}")
        lines.append("")

    lines.append("--- Result ---")
    lines.append(result)

    content = "\n".join(lines)
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(content)

    return log_path


def list_logs(limit: int = 20) -> List[dict]:
    """Liệt kê các file log gần nhất."""
    logs = []
    if not os.path.exists(LOG_DIR):
        return logs
    for f in sorted(os.listdir(LOG_DIR), reverse=True)[:limit]:
        fp = os.path.join(LOG_DIR, f)
        if os.path.isfile(fp):
            stat = os.stat(fp)
            logs.append({
                "filename": f,
                "size": stat.st_size,
                "created": datetime.fromtimestamp(stat.st_ctime, TZ).strftime("%Y-%m-%d %H:%M:%S"),
            })
    return logs


# ============================================================
# Flask Routes
# ============================================================

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({
        "status": "online",
        "service": "Lua Deobfuscation Toolkit v3.0",
        "author": "Hunter Gay - Hunter Team Community",
    })


@app.route("/deobf", methods=["POST"])
def api_deobfuscate():
    """API endpoint: POST /deobf với raw Lua code trong body."""
    data = request.get_json(force=True, silent=True) or {}
    code = data.get("code", "") or request.get_data(as_text=True)
    verbose = data.get("verbose", False)
    user_id = data.get("user_id", "api")
    username = data.get("username", "api_user")

    if not code or len(code.strip()) < 10:
        return jsonify({"error": "No Lua code provided (min 10 chars)"}), 400

    deobf = LuaDeobfuscator(verbose=verbose)
    obf_name, source, meta = deobf.deobfuscate_code(code)
    output = format_output(obf_name, source, meta, verbose)

    # Ghi log
    log_path = write_log(user_id, username, "api_deobf", output, meta)

    return jsonify({
        "obfuscator": obf_name,
        "metadata": {k: v for k, v in meta.items() if k != "error"},
        "source": source,
        "log_file": log_path,
    })


@app.route("/deobf/upload", methods=["POST"])
def api_deobf_upload():
    """API endpoint: POST /deobf/upload với file .lua upload."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    code = f.read().decode("utf-8", errors="replace")
    verbose = request.form.get("verbose", "false").lower() == "true"
    user_id = request.form.get("user_id", "api")
    username = request.form.get("username", "api_user")

    deobf = LuaDeobfuscator(verbose=verbose)
    obf_name, source, meta = deobf.deobfuscate_code(code, f.filename)
    output = format_output(obf_name, source, meta, verbose)

    log_path = write_log(user_id, username, f"upload:{f.filename}", output, meta)

    return jsonify({
        "obfuscator": obf_name,
        "filename": f.filename,
        "metadata": {k: v for k, v in meta.items() if k != "error"},
        "source": source,
        "log_file": log_path,
    })


@app.route("/log", methods=["GET"])
def api_list_logs():
    """Lệnh .log - Liệt kê log hoặc trả file/log cụ thể."""
    log_id = request.args.get("id", "")
    download = request.args.get("download", "").lower() == "true"

    if log_id:
        # Tìm log theo ID (prefix match)
        for f in os.listdir(LOG_DIR):
            if log_id in f:
                fp = os.path.join(LOG_DIR, f)
                if download:
                    return send_file(fp, as_attachment=True, download_name=f)
                with open(fp, "r", encoding="utf-8") as fh:
                    content = fh.read()
                return jsonify({"filename": f, "content": content})
        return jsonify({"error": f"Log '{log_id}' not found"}), 404

    # Liệt kê tất cả logs
    logs = list_logs()
    return jsonify({"logs": logs, "total": len(logs)})


@app.route("/log/latest", methods=["GET"])
def api_latest_log():
    """Lấy log mới nhất."""
    logs = list_logs(limit=1)
    if not logs:
        return jsonify({"error": "No logs found"}), 404

    fp = os.path.join(LOG_DIR, logs[0]["filename"])
    download = request.args.get("download", "").lower() == "true"
    if download:
        return send_file(fp, as_attachment=True, download_name=logs[0]["filename"])
    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()
    return jsonify({"filename": logs[0]["filename"], "content": content})


@app.route("/log/<filename>", methods=["GET"])
def api_get_log(filename: str):
    """Download log theo filename."""
    fp = os.path.join(LOG_DIR, filename)
    if not os.path.exists(fp):
        return jsonify({"error": "Log not found"}), 404
    return send_file(fp, as_attachment=True, download_name=filename)


# ============================================================
# Discord Bot (chạy trong thread riêng nếu có token)
# ============================================================

def run_discord_bot():
    """Chạy Discord bot trong thread riêng."""
    if not BOT_TOKEN:
        print("[Bot] No DISCORD_TOKEN set, skipping bot startup.")
        return

    try:
        import discord
    except ImportError:
        print("[Bot] discord.py not installed. Install: pip install discord.py")
        return

    intents = discord.Intents.default()
    intents.message_content = True
    bot = discord.Client(intents=intents)

    @bot.event
    async def on_ready():
        print(f"[Bot] Logged in as {bot.user}")

    @bot.event
    async def on_message(message):
        if message.author.bot:
            return

        content = message.content.strip()

        # Lệnh .log
        if content == ".log":
            logs = list_logs(limit=5)
            if not logs:
                await message.reply("Chưa có log nào.")
                return

            lines = ["**Recent Logs:**"]
            for log in logs:
                lines.append(f"- `{log['filename']}` ({log['size']} bytes) - {log['created']}")
            lines.append("")
            lines.append(f"Dùng `.log <filename>` để xem, `.log download <filename>` để tải file.")
            await message.reply("\n".join(lines))
            return

        # .log download <filename>
        if content.startswith(".log download "):
            filename = content[14:].strip()
            fp = os.path.join(LOG_DIR, filename)
            if not os.path.exists(fp):
                await message.reply(f"Không tìm thấy log: `{filename}`")
                return
            await message.reply(file=discord.File(fp, filename=filename))
            return

        # .log <filename> hoặc .log <id>
        if content.startswith(".log "):
            query = content[5:].strip()
            found = None
            for f in os.listdir(LOG_DIR):
                if query in f:
                    found = os.path.join(LOG_DIR, f)
                    break

            if not found:
                await message.reply(f"Không tìm thấy log: `{query}`")
                return

            with open(found, "r", encoding="utf-8") as fh:
                log_content = fh.read()

            if len(log_content) > 2000:
                # Gửi file nếu quá dài
                await message.reply(file=discord.File(found, filename=os.path.basename(found)))
            else:
                await message.reply(f"```\n{log_content}\n```")
            return

    bot.run(DISCORD_TOKEN)


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))

    # Chạy Discord bot trong thread riêng
    bot_thread = threading.Thread(target=run_discord_bot, daemon=True)
    bot_thread.start()

    # Chạy Flask
    print(f"[Server] Starting on port {port}...")
    app.run(host="0.0.0.0", port=port)
