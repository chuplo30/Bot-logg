
import re
import sys
import os
import zlib
import base64
import time
import json
import math
import tempfile
import multiprocessing
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any


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
            print("    Install: pip install lupa")

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _setup(self):
        """Set up Lua environment with bit32 polyfill + Roblox stubs + unpack fix."""
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

-- v5: unpack polyfill for LuaJIT Lua 5.2+ compatibility
if not _G.unpack then _G.unpack = table.unpack end

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
        result = self.lua.execute(setup_lua)

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
    if src == nil then return nil, "nil" end
    load_count = load_count + 1
    if load_count > 1 and type(src) == "string" and #src > 10 then
        local first300 = src:sub(1, 300)
        local is_vm = first300:find("bit32", 1, true) or first300:find("4294967296", 1, true) or first300:find("getfenv", 1, true)
        if not is_vm then
            captured_loads[#captured_loads+1] = src
        end
    end
    local ok, r1, r2 = pcall(_orig_load, src, ...)
    if ok then return r1, r2 else return nil, r2 end
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
# Obfuscator Detector (v5: improved, more types)
# ============================================================

class ObfuscatorDetector:
    # v5: expanded signatures - ordered by specificity (most specific first)
    SIGNATURES = [
        ("IronBrew2", ["IronBrew-2.0"]),
        ("LuaObfuscator.com (Ferib)", ["LuaObfuscator.com", "Much Love, Ferib"]),
        ("AstroProtect", ["AstroProtect"]),
        ("WAN OBFUSCATE", ["WAN OBFUSCATE"]),
        ("WAN OBFUSCATOR", ["WAN OBFUSCATOR"]),
        ("MoonSec", ["MoonSec"]),
        ("Clyde Protection", ["Clyde"]),
        ("PSU", ["PSU", "Prometheus"]),
        ("Luraph", ["Luraph", "luraph"]),
        ("Oxy", ["Oxy"]),
        ("WeAreDev", ["wearedevs.net/obfuscator"]),
        ("Prometheus", ["PrometheusObfuscator"]),
    ]

    @classmethod
    def detect(cls, code: str) -> Optional[str]:
        for name, sigs in cls.SIGNATURES:
            for sig in sigs:
                if sig in code:
                    return name
        if cls._is_luaobfuscator_ferib(code):
            return "LuaObfuscator.com (Ferib)"
        # v5.2: structural WeAreDev detection (works WITHOUT header comment)
        if cls._is_wearedev_structural(code):
            return "WeAreDev"
        # v5: IronBrew (v1) needs "LOL!" but NOT in a Ferib context
        if "LOL!" in code and "IronBrew-2.0" not in code:
            if not cls._is_luaobfuscator_ferib(code):
                return "IronBrew"
        if "IronBrew-2.0" in code and "LOL!" in code:
            return "IronBrew2"
        if cls._has_vm_pattern(code):
            return "Unknown VM-based"
        if cls._is_base64_compressed(code):
            return "Base64+Compressed"
        return None

    @classmethod
    def _is_wearedev_structural(cls, code: str) -> bool:
        score = 0
        if re.match(r'\s*return\s*\(function\s*\(\.\.\.\)', code):
            score += 3
        decimal_esc_count = len(re.findall(r'\\\d{3}', code))
        if decimal_esc_count > 300:
            score += 2
        elif decimal_esc_count > 100:
            score += 1
        if re.search(r'for\s+\w+,\w+\s+in\s+ipairs\s*\(\{', code):
            if re.search(r'\w+\[\w+\],\w+\[\w+\],\w+\[\w+\],\w+\[\w+\]\s*=\s*\w+\[\w+\],\w+\[\w+\],\w+\[\w+\][+-]\d+,\w+\[\w+\][+-]\d+', code):
                score += 3
        digit_key_count = len(re.findall(r'\["\\0[4-5]\\d"\]', code))
        if digit_key_count >= 6:
            score += 2
        elif digit_key_count >= 3:
            score += 1
        if re.search(r'local function\s+\w+\(\w+\)\s*return\s+\w+\[\w+\s*[+-]', code):
            score += 2
        if '\\115\\116\\114\\105\\110\\103' in code:
            score += 2
        if re.search(r'end\)\(getfenv\s+and\s+getfenv\(\)\s*or\s*_ENV', code):
            if 'newproxy' in code and 'setmetatable' in code and 'getmetatable' in code:
                score += 2
        if 'string.char' in code and 'table.concat' in code and 'string.len' in code:
            if 'string.sub' in code and 'math.floor' in code:
                score += 1
        obf_arith_count = len(re.findall(r'\d+\+-\d+', code))
        if obf_arith_count > 200:
            score += 2
        elif obf_arith_count > 100:
            score += 1
        if re.search(r'while\s+\w+\s+do\s*$', code, re.MULTILINE):
            if_count = len(re.findall(r'if\s+\w+<', code))
            if if_count > 50:
                score += 2
            elif if_count > 20:
                score += 1
        big_nums = re.findall(r'\b\d{15,}\b', code)
        if len(big_nums) >= 5:
            score += 2
        elif len(big_nums) >= 2:
            score += 1
        has_start_return = bool(re.match(r'\s*return\s*\(function', code))
        has_end_getfenv = 'end)(getfenv' in code or 'end)(getfenv' in code[-200:]
        if has_start_return and has_end_getfenv:
            score += 2
        acc_matches = re.findall(r'local function\s+(\w+)\(\w+\)\s*return\s+\w+\[\w+\s*[+-]', code)
        if acc_matches:
            acc_name = acc_matches[0]
            acc_usage = len(re.findall(re.escape(acc_name) + r'\(', code))
            if acc_usage > 30:
                score += 2
            elif acc_usage > 15:
                score += 1
        return score >= 6

    @classmethod
    def _is_luaobfuscator_ferib(cls, code: str) -> bool:
        """Detect LuaObfuscator.com by Ferib via structural patterns.

        Key indicators:
        - local v0=tonumber;local v1=string.byte;local v2=string.char... (var aliasing)
        - math.ldexp usage
        - getfenv or function() pattern
        - string.gsub with '..' separator (byte string decoder)
        - v15/v16 style numbered variable names
        """
        # Pattern 1: classic Ferib var aliasing header
        if re.search(r'local\s+v\d+\s*=\s*tonumber\s*;\s*local\s+v\d+\s*=\s*string\.byte', code):
            return True
        # Pattern 2: math.ldexp + getfenv combo (rare in other obfuscators)
        has_ldexp = 'math.ldexp' in code or 'v8=math.ldexp' in code
        has_getfenv_fallback = 'getfenv or function()' in code
        if has_ldexp and has_getfenv_fallback:
            return True
        # Pattern 3: string.gsub with ".." separator pattern (byte-level decoder)
        if re.search(r'string\.gsub\s*\(.*?"\.\."', code) and 'math.ldexp' in code:
            return True
        return False

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
                    "Luraph", "PSU", "Prometheus", "Oxy",
                    "WeAreDev", "wearedevs", "LuaObfuscator.com",
                    "Much Love, Ferib"]:
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


class LuaObfuscatorFeribDeobfuscator:
    """LuaObfuscator.com by Ferib - VM execution + loadstring capture.

    Ferib's obfuscator compiles Lua source into a custom bytecode VM.
    The bytecode is stored as a hex-encoded string with RLE compression.
    A decoder function (v15) reads the bytecode, uses math.ldexp for bit extraction,
    and reassembles the original source via loadstring.

    Detection: "LuaObfuscator.com" banner, math.ldexp, v0=tonumber alias pattern.

    Strategy:
    1. Pre-process to fix Lua 5.5 for-loop const variable issue
    2. Execute with loadstring capture
    3. Fall back to subprocess tracer
    4. Fall back to structural analysis
    """

    @staticmethod
    def _fix_for_loop_const(code: str) -> str:
        """Fix Lua 5.5 for-loop const variable issue.

        In Lua 5.5, for-loop control variables are treated as const.
        If the loop body reassigns them, compilation fails.
        This pre-processing renames the loop variable and creates a mutable local.
        """
        matches = list(re.finditer(r'for\s+(v\d+)\s*=', code))
        if not matches:
            return code

        for m in reversed(matches):
            var = m.group(1)
            for_start = m.start()

            after_for = code[for_start:]
            do_idx = after_for.find(' do ')
            if do_idx == -1:
                do_idx = after_for.find('\tdo ')
                if do_idx == -1:
                    do_idx = after_for.find(' do\n')
                    if do_idx == -1:
                        continue

            loop_body_start = for_start + do_idx + 4
            body_region = code[loop_body_start:loop_body_start + 3000]

            # Check if var is reassigned inside the loop body
            reassigned = False
            for pat in [
                re.escape(var) + r'\s*=',
                r'[,=]\s*' + re.escape(var) + r'\s*[,=;)]',
            ]:
                if re.search(pat, body_region):
                    reassigned = True
                    break

            if not reassigned:
                continue

            temp_var = f'__{var}_it'
            old_for = f'for {var}='
            new_for = f'for {temp_var}='

            pos = code.find(old_for, for_start)
            if pos != for_start:
                continue

            code = code[:for_start] + new_for + code[for_start + len(old_for):]

            do_pos = code.find('do', for_start + len(new_for))
            if do_pos != -1:
                after_do = do_pos + 2
                inject = f' local {var}={temp_var};'
                code = code[:after_do] + inject + code[after_do:]

        return code

    @staticmethod
    def _extract_strings_static(code: str) -> List[str]:
        """Extract string constants from Ferib constant pool."""
        strings = []
        # Look for string literals in the code
        str_literals = re.findall(r'"([A-Za-z_][A-Za-z0-9_]{2,})"', code)
        api_names = {"print", "warn", "game", "Instance", "workspace", "wait",
                     "GetService", "FindFirstChild", "Clone", "Destroy",
                     "CFrame", "Vector3", "Color3", "UDim2", "TweenInfo",
                     "TweenService", "Players", "LocalPlayer", "Character",
                     "Humanoid", "Head", "Torso", "Position", "Size",
                     "HttpGet", "HttpPost", "setreadonly", "readfile", "writefile",
                     "getgenv", "setgenv", "getfenv", "setfenv", "loadstring",
                     "pcall", "xpcall", "require", "spawn", "delay", "wait",
                     "FireServer", "InvokeServer", "OnServerEvent", "OnClientEvent",
                     "Connect", "Wait", "ChildAdded", "ChildRemoved"}
        for s in str_literals:
            if s in api_names or (len(s) > 4 and s[0].islower()):
                strings.append(s)
        return list(set(strings))

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if not engine.available:
            return None

        # Pre-process: fix Lua 5.5 for-loop const variable issue
        code_fixed = LuaObfuscatorFeribDeobfuscator._fix_for_loop_const(code)
        if len(code_fixed) != len(code) and verbose:
            print(f"  [*] Applied for-loop const fix ({len(code_fixed) - len(code)} bytes added)")

        # Try direct execution with loadstring capture
        if verbose:
            print("  [*] Executing LuaObfuscator.com (Ferib) VM...")

        ok, source, prints = engine.execute_and_capture(code_fixed, timeout=30)

        # v5: filter out error messages that look like source
        is_error = (not ok) or (source and source.startswith('[string "'))
        # v8 fix: keep the REAL failure reason around instead of discarding
        # it. The old fallback text below used to always say the same
        # canned "Lua 5.1/5.3 vs 5.5" guess regardless of what actually
        # went wrong -- which could be totally unrelated (a real bug, a
        # timeout, a missing global, etc). Surfacing the actual Lua error
        # lets you tell the difference instead of guessing from a fixed string.
        real_error = source if is_error and source else None
        if source and len(source) > 10 and not is_error:
            vm_indicators = ["math.ldexp", "getfenv or function", "v15(", "v16,"]
            vm_score = sum(1 for v in vm_indicators if v in source[:500])

            if vm_score <= 1:
                if verbose:
                    print(f"  [+] Captured clean source: {len(source)} chars")
                return source, {"method": "VM execution (loadstring capture)", "source_len": len(source)}
            else:
                if verbose:
                    print(f"  [*] Captured source is still VM-wrapped (vm_score={vm_score}), trying recursive...")
                ok2, source2, prints2 = engine.execute_and_capture(source, timeout=30)
                if source2 and len(source2) > 10:
                    vm_score2 = sum(1 for v in vm_indicators if v in source2[:500])
                    if vm_score2 <= 1:
                        if verbose:
                            print(f"  [+] Recursively deobfuscated: {len(source2)} chars")
                        return source2, {"method": "recursive VM execution", "source_len": len(source2), "layers": 2}

        if ok and prints:
            recovered = SourceReconstructor.from_prints(prints)
            if verbose:
                print(f"  [*] Using print trace: {len(prints)} prints")
            return recovered, {"method": "VM execution (print trace)", "print_count": len(prints)}

        # Try subprocess tracer (also includes for-loop fix)
        if verbose:
            print("  [*] Direct execution failed, trying subprocess tracer...")
        source_sub = LuaObfuscatorFeribDeobfuscator._subprocess_trace(code_fixed, verbose)
        if source_sub and len(source_sub) > 10:
            return source_sub, {"method": "subprocess tracer", "source_len": len(source_sub)}

        # Fall back to static analysis
        if verbose:
            print("  [*] VM execution failed, falling back to structural analysis...")
        strings = LuaObfuscatorFeribDeobfuscator._extract_strings_static(code)
        pool_strings = LuaObfuscatorFeribDeobfuscator._decode_constant_pool(code)
        lines = ["-- LuaObfuscator.com (Ferib) - Structural Analysis"]
        if real_error:
            lines.append(f"-- Execution failed with a real Lua error (not a version guess):")
            lines.append(f"--   {real_error[:300]}")
        else:
            lines.append(f"-- Note: VM execution did not produce recoverable source (no Lua error was raised)")
        lines.append(f"")
        if pool_strings:
            lines.append(f"-- Decoded constant pool ({len(pool_strings)} entries):")
            for i, s in enumerate(pool_strings):
                lines.append(f"--   [{i}] = {repr(s)}")
            lines.append("")
        if strings:
            lines.append(f"-- Recovered {len(strings)} API/string references:")
            for i, s in enumerate(sorted(strings)):
                lines.append(f"--   [{i}] = {repr(s)}")
            lines.append("")
        lines.append("-- Use a Lua 5.1 or 5.3 environment for full source recovery")
        return "\n".join(lines), {"method": "structural analysis", "strings": len(strings), "pool_strings": len(pool_strings)}

    @staticmethod
    def _decode_constant_pool(code: str) -> List[str]:
        """Decode Ferib constant pool from the RLE-encoded bytecode.

        The v15 function receives a hex-encoded string with RLE compression.
        After decoding, the first part is a constant pool of strings used by the VM.
        """
        try:
            idx = code.find('v15("')
            if idx == -1:
                return []
            start = idx + 5
            pos = start
            while pos < len(code):
                if code[pos] == '\\' and pos + 1 < len(code):
                    pos += 2
                    continue
                if code[pos] == '"':
                    break
                pos += 1
            encoded = code[start:pos]

            stripped = encoded[4:]  # Skip "LOL!"
            result = bytearray()
            repeat_count = None
            i = 0
            while i + 1 < len(stripped):
                seg = stripped[i:i+2]
                i += 2
                if ord(seg[1]) == 81:  # 'Q'
                    repeat_count = int(seg[0], 16)
                else:
                    char_val = int(seg, 16)
                    if repeat_count is not None:
                        result.extend(bytes([char_val]) * repeat_count)
                        repeat_count = None
                    else:
                        result.append(char_val)

            decoded = bytes(result)
            offset = 4  # Skip 4-byte header
            strings = []
            while offset + 5 < len(decoded):
                entry_type = decoded[offset]
                str_len = decoded[offset + 1]
                if entry_type != 3 or str_len == 0 or str_len > 200:
                    break
                if offset + 5 + str_len > len(decoded):
                    break
                s = decoded[offset + 5:offset + 5 + str_len].decode('utf-8', errors='replace')
                strings.append(s)
                offset += 5 + str_len
            return strings
        except Exception:
            return []

    @staticmethod
    def _subprocess_trace(code: str, verbose: bool) -> Optional[str]:
        """Execute via subprocess with enhanced loadstring capture."""
        import subprocess

        tracer_lua = r"""
-- v5 Ferib tracer
local _orig_load = loadstring or load
local _orig_loadstring = _orig_load
local _captured_sources = {}
local _capture_count = 0
local _orig_print = print
local _prints = {}
local _print_n = 0

-- v5: unpack polyfill
if not unpack then unpack = table.unpack end

_G.print = function(...)
    local args = {...}
    local strs = {}
    for i, v in ipairs(args) do strs[i] = tostring(v) end
    local line = table.concat(strs, "\t")
    _print_n = _print_n + 1
    _prints[_print_n] = line
end

_G.load = function(src, ...)
    if src == nil then return nil, "nil" end
    if type(src) == "string" and #src > 10 then
        _capture_count = _capture_count + 1
        _captured_sources[_capture_count] = src
    end
    local ok, r1, r2 = pcall(_orig_load, src, ...)
    if ok then return r1, r2 else return nil, r2 end
end
_G.loadstring = _G.load

-- Roblox stubs
local function deep_stub()
    return setmetatable({},{
        __call=function(self,...) return nil end,
        __index=function(t,k) return deep_stub() end,
        __newindex=function(t,k,v) end,
    })
end
for _,g in ipairs({"game","workspace","Instance","Enum","Players","ReplicatedStorage","RunService","TweenService","HttpService","UDim2","Color3","Vector3","CFrame","task","Vector2","UserInputService","Lighting","Debris","StarterGui","StarterPlayer","StarterPack","Teams","Chat","CollectionService","PathfindingService","SoundService","TextService","GuiService","UserSettings","CoreGui","Rect","UDim","Font","NumberSequence","ColorSequence","NumberRange","TweenInfo","RaycastParams","Material","UGCValidationService","MarketplaceService","script","shared","_G","ServerStorage","ServerScriptService","ReplicatedFirst","DataStoreService","MessagingService","BadgeService","GamePassService","InsertService","AssetService","ContentProvider","ContextActionService","LocalizationService","PhysicsService","VoiceChatService","ProximityPromptService","SocialService","TeleportService","AnalyticsService","MemoryStoreService","TextChatService","VRService","GroupService","FriendService","GamepadService","Stats","LogService","ScriptContext","SelectionService","CustomAvatarService","AvatarEditorService","PolicyService","ProcessInstancePhysicsService","HapticService","PluginManager","ChangeHistoryService","TestService","NotificationService","ExperienceNotificationService","VirtualInputManager","VirtualUser","BrickColor","Region3","Ray","Random","PhysicalProperties","OverlapParams","RaycastResult","Axes","Faces","PathWaypoint","DockWidgetPluginGuiInfo","Camera","Terrain"}) do
    _G[g] = deep_stub()
end

-- Execute the obfuscated code
local code = ...
local fn, err = load(code)
if fn then
    pcall(fn)
end

-- Output results
if _capture_count > 0 then
    for i, src in pairs(_captured_sources) do
        -- Only output non-VM sources
        if not src:find("math.ldexp", 1, true) and not src:find("getfenv or function", 1, true) then
            print("[FERIB_SRC_START]")
            print(src)
            print("[FERIB_SRC_END]")
        end
    end
end

for i = 1, _print_n do
    print("[PRINT]" .. _prints[i])
end

print("[DONE]")
"""

        import base64 as b64lib
        tracer_b64 = b64lib.b64encode(tracer_lua.encode('utf-8')).decode('ascii')
        fix_func = (
            'def fix_for_const(code):\n'
            '    import re\n'
            '    matches=list(re.finditer(r"for\\s+(v\\d+)\\s*=",code))\n'
            '    if not matches:return code\n'
            '    for m in reversed(matches):\n'
            '        var=m.group(1);fs=m.start()\n'
            '        af=code[fs:]\n'
            '        di=af.find(" do ")\n'
            '        if di==-1:di=af.find("\\tdo ")\n'
            '        if di==-1:di=af.find(" do\\n")\n'
            '        if di==-1:continue\n'
            '        lbs=fs+di+4;br=code[lbs:lbs+3000]\n'
            '        ra=False\n'
            '        ev=re.escape(var)\n'
            '        for p in [ev+r"\\s*=",r"[,=]\\s*"+ev+r"\\s*[,=;)]"]:\n'
            '            if re.search(p,br):ra=True;break\n'
            '        if not ra:continue\n'
            '        tv="__"+var+"_it";of="for "+var+"=";nf="for "+tv+"="\n'
            '        if code.find(of,fs)!=fs:continue\n'
            '        code=code[:fs]+nf+code[fs+len(of):]\n'
            '        dp=code.find("do",fs+len(nf))\n'
            '        if dp!=-1:code=code[:dp+2]+" local "+var+"="+tv+";"+code[dp+2:]\n'
            '    return code\n'
        )
        runner_code = (
            'import sys,os,base64,re\n'
            'from lupa import LuaRuntime\n'
            + fix_func +
            'TRACER=base64.b64decode("' + tracer_b64 + '").decode("utf-8")\n'
            'if len(sys.argv)<2:print("[EX]No input");sys.exit(1)\n'
            'with open(sys.argv[1],"r",encoding="utf-8",errors="replace") as f:code=f.read()\n'
            'code=fix_for_const(code)\n'
            'lua=LuaRuntime(unpack_returned_tuples=True)\n'
            'try:lua.execute(TRACER+chr(10)+code)\n'
            'except Exception as e:print("[EX]"+str(e)[:500])\n'
        )

        runner_file = tempfile.mktemp(suffix='.py', prefix='ferib_runner_')
        obf_file = tempfile.mktemp(suffix='.lua', prefix='ferib_v5_')
        try:
            with open(runner_file, 'w') as f:
                f.write(runner_code)
            with open(obf_file, 'w') as f:
                f.write(code)

            result = subprocess.run(
                [sys.executable, runner_file, obf_file],
                capture_output=True, text=True, timeout=30
            )
        except subprocess.TimeoutExpired:
            if verbose:
                print("  [!] Subprocess timed out")
            return None
        except Exception as e:
            if verbose:
                print(f"  [!] Subprocess error: {e}")
            return None
        finally:
            for fp in (runner_file, obf_file):
                if os.path.exists(fp):
                    try:
                        os.unlink(fp)
                    except:
                        pass

        # Parse output
        stdout = result.stdout
        sources = []
        in_source = False
        source_lines = []

        for line in stdout.split('\n'):
            line_stripped = line.strip()
            if line_stripped == '[FERIB_SRC_START]':
                in_source = True
                source_lines = []
            elif line_stripped == '[FERIB_SRC_END]':
                in_source = False
                src = '\n'.join(source_lines)
                if len(src) > 10:
                    sources.append(src)
            elif in_source:
                source_lines.append(line)

        if sources:
            # Return the longest captured source (most likely the real one)
            return max(sources, key=len)
        return None





# ============================================================
# WeAreDev Bytecode Disassembler (v5.5 NEW)
# ============================================================

class WeAreDevDisassembler:
    """v5.5: Disassemble WeAreDev VM bytecode into human-readable opcode listing.
    
    Parses the binary search tree dispatch, extracts opcode handlers,
    classifies operations, and shows decoded strings involved."""

    @staticmethod
    def disassemble(code: str, verbose: bool = False) -> str:
        code = re.sub(r'^--\[\[.*?\]\]\s*', '', code)
        lines = []
        
        # Extract P-table
        ptable_result = WeAreDevDisassembler._extract_ptable_fast(code)
        if ptable_result is None:
            return "-- Failed to extract P-table structure"
        P_decoded, accessor_name, m_offset = ptable_result
        string_map = WeAreDevDeobfuscator._build_string_map(
            code, P_decoded, m_offset, accessor_name)
        
        # Find VM function
        vm_result = WeAreDevDisassembler._find_vm_function(code)
        if vm_result is None:
            return "-- Could not locate VM interpreter function"
        vm_start, vm_end = vm_result
        vm_body = code[vm_start:vm_end]
        
        # Pre-process
        simplified = WeAreDevDisassembler._simplify_vm(vm_body, accessor_name, string_map)
        
        # Parse opcodes from binary search tree
        opcodes = WeAreDevDisassembler._parse_binary_tree(simplified)
        
        # Build output
        lines.append(f"-- WeAreDev VM Disassembly (v5.5)")
        lines.append(f"-- P-table: {len(P_decoded)} entries, {m_offset} offset")
        lines.append(f"-- Accessor: {accessor_name}()")
        lines.append(f"-- VM function: {len(vm_body)} chars")
        lines.append(f"-- Detected opcodes: {len(opcodes)}")
        lines.append(f"-- Binary search depth: {WeAreDevDisassembler._tree_depth(simplified)}")
        lines.append("")
        
        # Classify opcodes
        push_ops = [op for op in opcodes if op['type'] == 'push_string']
        char_ops = [op for op in opcodes if op['type'] == 'push_char']
        arith_ops = [op for op in opcodes if op['type'] == 'arithmetic']
        ctrl_ops = [op for op in opcodes if op['type'] == 'control_flow']
        string_ops = [op for op in opcodes if op['type'] == 'string_op']
        table_ops = [op for op in opcodes if op['type'] == 'table_op']
        
        lines.append(f"-- Opcode breakdown:")
        lines.append(f"--   push_string: {len(push_ops)}")
        lines.append(f"--   push_char:   {len(char_ops)}")
        lines.append(f"--   arithmetic:   {len(arith_ops)}")
        lines.append(f"--   control_flow:{len(ctrl_ops)}")
        lines.append(f"--   string_op:   {len(string_ops)}")
        lines.append(f"--   table_op:    {len(table_ops)}")
        lines.append("")
        
        # List opcodes
        lines.append("-- === OPCODE LIST ===")
        for i, op in enumerate(opcodes):
            lo, hi = op['range']
            lines.append(f"-- [{i:3d}] IP [{lo:>12}, {hi:>12})")
            lines.append(f"--       Type: {op['type']}")
            if op['next_ip'] is not None:
                lines.append(f"--       Next IP: {op['next_ip']}")
            for detail in op['details'][:3]:
                lines.append(f"--       {detail}")
            lines.append("")
        
        # Decoded string constants
        meaningful = {k: v for k, v in P_decoded.items()
                     if v and len(v.strip()) > 0
                     and not re.match(r'^[A-Za-z0-9+/=]{6,}$', v)}
        if meaningful:
            lines.append("-- === DECODED STRING CONSTANTS ===")
            for idx in sorted(meaningful.keys()):
                s = meaningful[idx]
                if len(s) < 100 and all(32 <= ord(c) < 127 for c in s):
                    lines.append(f'--   [{idx:3d}] = {repr(s)}')
            lines.append("")
        
        return '\n'.join(lines)

    @staticmethod
    def _extract_ptable_fast(code: str):
        """Quick P-table extraction for disassembly."""
        p_match = re.search(r'local (\w+)=\{', code)
        if not p_match:
            return None
        p_start = p_match.end()
        depth, pos = 1, p_start
        while pos < len(code) and depth > 0:
            if code[pos] == '{': depth += 1
            elif code[pos] == '}': depth -= 1
            pos += 1
        p_raw_text = code[p_start:pos - 1]
        p_entries = []
        scan = 0
        while scan < len(p_raw_text):
            q1 = p_raw_text.find('"', scan)
            if q1 == -1: break
            q2 = p_raw_text.find('"', q1 + 1)
            if q2 == -1: break
            raw = p_raw_text[q1 + 1:q2]
            p_entries.append(decode_decimal_escapes(raw))
            scan = q2 + 1
        if not p_entries:
            return None
        b64_map = WeAreDevDeobfuscator._extract_b64_table(code)
        if not b64_map:
            return None
        P_decoded = {}
        for i, entry in enumerate(p_entries, 1):
            if entry and len(entry) > 0:
                P_decoded[i] = WeAreDevDeobfuscator._b64_decode(entry, b64_map)
            else:
                P_decoded[i] = ''
        swaps = WeAreDevDeobfuscator._extract_swap_loop(code)
        if swaps:
            WeAreDevDeobfuscator._apply_swaps(P_decoded, swaps)
        m_offset, acc_name = WeAreDevDeobfuscator._extract_m_offset(code)
        return P_decoded, acc_name, m_offset

    @staticmethod
    def _find_vm_function(code: str):
        """Find VM function using the return(W(...))(q(m))end pattern."""
        wm = re.search(r'while\s+(\w+)\s+do\s*if\s+\1<', code)
        if not wm:
            return None
        while_pos = wm.start()
        func_start = code.rfind('function(', max(0, while_pos - 5000), while_pos)
        if func_start == -1:
            return None
        # Find end: 'return q(m)end,function(' or '))end,function('
        for pat in [r'\)\(q\(m\)\)end\)\(function\(',
                   r'q\(m\)end,function\(',
                   r'q\(m\)end\),function\(']:
            m = re.search(pat, code[while_pos:])
            if m:
                if pat.startswith('\\)'):
                    func_end = while_pos + m.end() - len(',function(')
                else:
                    func_end = while_pos + m.end() - len(',function(')
                return func_start, func_end
        return None

    @staticmethod
    def _simplify_vm(vm_body: str, accessor_name: str, string_map: dict) -> str:
        """Simplify VM code for analysis."""
        code = WeAreDevDeobfuscator._simplify_arith_in_code(vm_body)
        # Replace accessor calls with decoded strings
        if string_map:
            acc = re.escape(accessor_name)
            def replace_acc(m):
                val = eval_arith(m.group(1))
                if val is not None and val in string_map:
                    s = string_map[val]
                    if len(s) < 80 and all(32 <= ord(c) < 127 for c in s):
                        return repr(s)
                return m.group(0)
            code = re.sub(acc + r'\((-?\d+(?:[+-]\(?-?\d+(?:\([^)]+\))?\)?|[+-]-?\d+)*)\)',
                        replace_acc, code)
        return code

    @staticmethod
    def _tree_depth(code: str) -> int:
        """Measure the maximum nesting depth of the binary search tree."""
        max_depth = 0
        depth = 0
        i = 0
        while i < len(code):
            for kw in ['if ', 'while ', 'for ']:
                if code[i:i+len(kw)] == kw:
                    before_ok = (i == 0 or not code[i-1].isalnum())
                    if before_ok:
                        depth += 1
                        max_depth = max(max_depth, depth)
            if code[i:i+3] == 'end':
                before = code[i-1] if i > 0 else ''
                if not before.isalnum():
                    depth = max(0, depth - 1)
            i += 1
        return max_depth

    @staticmethod
    def _parse_binary_tree(simplified: str) -> list:
        """Parse the binary search tree to extract opcode handlers."""
        opcodes = []
        # Extract all comparison thresholds
        thresholds = []
        for m in re.finditer(r'if\s+B<(\d+)', simplified):
            thresholds.append(int(m.group(1)))
        thresholds.sort()
        
        # Build ranges and find handler code for each
        prev = 0
        for t in thresholds:
            # Find code between 'if B < prev' and 'if B < t'
            range_code = WeAreDevDisassembler._extract_handler(simplified, prev, t, thresholds)
            op_type, details, next_ip = WeAreDevDisassembler._classify_handler(range_code)
            opcodes.append({
                'range': (prev, t),
                'type': op_type,
                'details': details,
                'next_ip': next_ip,
            })
            prev = t
        # Last range
        range_code = WeAreDevDisassembler._extract_handler(simplified, prev, None, thresholds)
        op_type, details, next_ip = WeAreDevDisassembler._classify_handler(range_code)
        opcodes.append({
            'range': (prev, 'inf'),
            'type': op_type,
            'details': details,
            'next_ip': next_ip,
        })
        return opcodes

    @staticmethod
    def _extract_handler(code: str, lo, hi, all_thresholds: list) -> str:
        """Extract the handler code for a given IP range."""
        # Find the 'if B < lo' pattern
        if lo == 0:
            # First range - look for 'if B < hi then HANDLER'
            m = re.search(r'if B<' + str(hi) + r'then', code)
            if m:
                start = m.end()
                # Find the 'else' or 'elseif' that ends this handler
                depth = 1
                i = start
                while i < len(code):
                    if code[i:i+7] == 'elseif':
                        depth -= 1
                        if depth == 0:
                            return code[start:i]
                    elif code[i:i+4] == 'else' and code[i:i+7] != 'elseif':
                        depth -= 1
                        if depth == 0:
                            return code[start:i]
                    elif code[i:i+3] == 'end':
                        depth -= 1
                        if depth == 0:
                            return code[start:i]
                    for kw in ['if ', 'while ', 'for ']:
                        if code[i:i+len(kw)] == kw:
                            before = code[i-1] if i > 0 else ''
                            if not before.isalnum():
                                depth += 1
                            break
                    i += 1
                return code[start:start+200]
        elif hi is not None:
            # Middle range: look for 'if B < lo then ... elseif B < hi then HANDLER'
            m = re.search(r'elseif B<' + str(hi) + r'then', code)
            if not m:
                m = re.search(r'else if B<' + str(hi) + r'then', code)
            if m:
                start = m.end()
                # Find next 'else'/'elseif'/'end'
                depth = 1
                i = start
                while i < len(code):
                    if code[i:i+7] == 'elseif' or code[i:i+9] == 'else if B<':
                        depth -= 1
                        if depth == 0:
                            return code[start:i]
                    elif code[i:i+4] == 'else' and code[i:i+7] != 'elseif':
                        depth -= 1
                        if depth == 0:
                            return code[start:i]
                    elif code[i:i+3] == 'end':
                        depth -= 1
                        if depth == 0:
                            return code[start:i]
                    for kw in ['if ', 'while ', 'for ']:
                        if code[i:i+len(kw)] == kw:
                            before = code[i-1] if i > 0 else ''
                            if not before.isalnum():
                                depth += 1
                            break
                    i += 1
                return code[start:start+200]
        return ''

    @staticmethod
    def _classify_handler(code: str) -> tuple:
        """Classify a handler's operation type."""
        if not code:
            return ('unknown', [], None)
        
        details = []
        op_type = 'control_flow'
        next_ip = None
        
        # Find next IP (B=NUMBER at end of handler)
        for m in re.finditer(r'B=(-?\d+)', code):
            val = eval_arith(m.group(1))
            if val is not None and abs(val) > 1000:
                next_ip = val
        
        # Check for table.insert / push to result
        if 'm={' in code or 'h(m,' in code or '.append(' in code:
            op_type = 'push_string'
            # Extract string literals
            for sm in re.finditer(r'"([^"\n]{2,})"', code):
                s = sm.group(1)
                if not re.match(r'^[A-Za-z0-9+/=]{6,}$', s):
                    if all(32 <= ord(c) < 127 for c in s):
                        details.append(f'string: {repr(s)[:60]}')
        
        # Check for chr/string.char
        if 'chr(' in code:
            op_type = 'push_char'
            for m in re.finditer(r'chr\((\d+)\)', code):
                try:
                    c = chr(int(m.group(1)))
                    if 32 <= ord(c) < 127:
                        details.append(f'char: {repr(c)}')
                except:
                    pass
        
        # Check for string operations
        if any(op in code for op in ['string.sub', 'string.len', 'string.byte']):
            if op_type == 'control_flow':
                op_type = 'string_op'
        
        # Check for arithmetic
        arith_count = len(re.findall(r'[+\-*/%^]', code))
        if arith_count > 5 and op_type == 'control_flow':
            op_type = 'arithmetic'
        
        # Check for table operations
        if '#' in code or 'len(' in code:
            if op_type == 'control_flow':
                op_type = 'table_op'
        
        return (op_type, details[:5], next_ip)


class WeAreDevDeobfuscator:
    """WeAreDev v1.0.0 decompiler - v5.5 with CFF block extraction, enhanced tracer,
    arithmetic simplification, deep body mining, smart variable naming,
    bytecode disassembler, and improved VM analysis."""

    M_OFFSET = 472584 - 466871

    # v5.3: Known API method names used by WeAreDev VM
    VM_API_NAMES = frozenset({
        'GetService', 'WaitForChild', 'FindFirstChild', 'FindFirstChildOfClass',
        'Connect', 'Disconnect', 'Fire', 'FireServer', 'InvokeServer',
        'OnServerEvent', 'OnClientEvent', 'IsA', 'Clone', 'Destroy',
        'HttpGet', 'HttpPost', 'Wait', 'GetPropertyChangedSignal',
        'GetChildren', 'GetDescendants', 'GetAttribute', 'SetAttribute',
        'LoadCharacter', 'MoveTo', 'WalkTo', 'Play', 'Stop',
        'GetDataStore', 'GetAsync', 'SetAsync', 'GetOrderedDataStore',
        'ComputeAsync', 'GetWaypoints', 'CreatePath',
    })

    VM_UTILITY_NAMES = frozenset({
        'gsub', 'sub', 'find', 'match', 'format', 'rep', 'len', 'byte',
        'char', 'lower', 'upper', 'reverse', 'gmatch', 'concat',
        'tonumber', 'tostring', 'type', 'pairs', 'ipairs', 'unpack',
        'pcall', 'xpcall', 'error', 'warn', 'assert', 'select',
        'rawget', 'rawset', 'setmetatable', 'getmetatable',
        'math', 'string', 'table', 'coroutine', 'bit32',
    })

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if not engine.available:
            return None
        import subprocess
        obf = re.sub(r'^--\[\[.*?\]\]\s*', '', code)
        m_offset, accessor_name = WeAreDevDeobfuscator._extract_m_offset(obf)
        if verbose:
            print(f"  [*] {accessor_name}() offset: {m_offset}")

        if verbose:
            print("  [*] Phase 1: P-table decode...")
        static_result = WeAreDevDeobfuscator._static_decode_p_table(obf, verbose)
        if static_result:
            P_decoded, accessor_name, m_offset = static_result
        else:
            if verbose:
                print("  [*] Static decode failed, trying injection...")
            P_decoded = WeAreDevDeobfuscator._decode_p_table(obf, engine)
        if not P_decoded:
            return None

        string_map = WeAreDevDeobfuscator._build_string_map(obf, P_decoded, m_offset, accessor_name)
        real_strings = {k: v for k, v in string_map.items()
                        if v and not re.match(r'^[A-Za-z0-9]{8,20}$', v)}
        if verbose:
            print(f"  [*] P-table: {len(P_decoded)} entries, {len(real_strings)} meaningful")

        if verbose:
            print("  [*] Phase 2: VM trace (30s)...")
        prints, trace, errors = WeAreDevDeobfuscator._execute_vm_traced(obf)
        if verbose:
            print(f"  [*] Trace: {len(trace)} entries, {len(prints)} prints")

        if verbose:
            print("  [*] Phase 3: CFF resolution (balanced parens)...")
        resolved_cff = WeAreDevDeobfuscator._resolve_cff_strings_v2(obf, string_map, accessor_name)
        acc_esc = re.escape(accessor_name)
        oc = len(re.findall(acc_esc + r'\(', obf))
        nc = len(re.findall(acc_esc + r'\(', resolved_cff))
        if verbose:
            print(f"  [*] Resolved {oc - nc}/{oc} accessor calls")

        # v5.3: Phase 3.5 - Post-CFF: decode escapes + simplify arithmetic
        if verbose:
            print("  [*] Phase 3.5: Post-CFF decode + simplify...")
        decoded_cff = WeAreDevDeobfuscator._post_process_cff(resolved_cff)

        if verbose:
            print("  [*] Phase 4: Deep analysis...")
        body_code = WeAreDevDeobfuscator._deep_mine_body(decoded_cff, string_map)
        cff_code = WeAreDevDeobfuscator._mine_cff_code(decoded_cff, string_map)
        structure_code = WeAreDevDeobfuscator._extract_code_structure(decoded_cff, string_map)
        # v5.4: Extract complete code blocks (loop bodies, function bodies, if blocks)
        cff_blocks = WeAreDevDeobfuscator._extract_cff_blocks(decoded_cff, string_map)
        # v5.4: Phase 4.5 - Opcode analysis
        opcode_strings = WeAreDevDeobfuscator._mine_opcode_strings(decoded_cff)
        if verbose:
            print(f"  [*] Body:{len(body_code)} CFF:{len(cff_code)} Struct:{len(structure_code)} Opcode:{len(opcode_strings)} Blocks:{len(cff_blocks)}")

        if verbose:
            print("  [*] Phase 5: Reconstruct + smart rename...")
        reconstructed = WeAreDevDeobfuscator._reconstruct_source(trace, prints, string_map, resolved_cff)
        reconstructed = WeAreDevDeobfuscator._smart_rename(reconstructed)

        source = WeAreDevDeobfuscator._generate_clean_output(
            reconstructed, trace, prints, errors, P_decoded, string_map, verbose,
            m_offset, accessor_name,
            # v6 fix: the "bonus mining" sections (cff_code, structure_code,
            # body_code, opcode_strings, cff_blocks) are regex-mined directly
            # from the still-raw, not-fully-decoded VM/CFF dispatch code --
            # they frequently pick up the obfuscator's OWN interpreter
            # internals (base64 decode loops, jump-table chains, bit
            # arithmetic) and dump it as if it were recovered user logic.
            # Since strip_lua_comments() in the bot removes the "-- === X ==="
            # section headers that would normally separate this from the
            # reliable trace-based reconstruction, it ends up looking like
            # unexplained junk glued onto clean output. Disabled by default;
            # the raw values are still in `meta` below for debugging.
            None, None, None,
            opcode_strings=None, cff_blocks=None)

        meta = {
            "method": "P-table + VM trace + CFF blocks + enhanced tracer + opcode analysis + smart naming (v5.5)",
            "p_entries": len(P_decoded), "strings_decoded": len(real_strings),
            "print_count": len(prints), "trace_entries": len(trace),
            "cff_code_patterns": len(cff_code),
            "structure_patterns": len(structure_code) if structure_code else 0,
            "body_code_patterns": len(body_code),
            "opcode_strings": len(opcode_strings),
            "cff_blocks": len(cff_blocks),
            "reconstructed_lines": len(reconstructed.split(chr(10))) if reconstructed else 0,
        }
        return source, meta

    # ============================================================
    # Phase 1: P-table decode
    # ============================================================

    @staticmethod
    def _extract_b64_table(obf: str):
        for tbl_match in re.finditer(r'local (\w+)=\{', obf):
            body_start = tbl_match.end()
            depth, pos = 1, body_start
            while pos < len(obf) and depth > 0:
                if obf[pos] == '{': depth += 1
                elif obf[pos] == '}': depth -= 1
                pos += 1
            body = obf[body_start:pos - 1]
            entries = re.split(r'[;,]', body)
            b64_map = {}
            for entry in entries:
                entry = entry.strip()
                if not entry or '=' not in entry:
                    continue
                key_part, val_part = entry.split('=', 1)
                key_part, val_part = key_part.strip(), val_part.strip()
                val = eval_arith(val_part)
                if val is None:
                    continue
                if key_part.startswith('[') and key_part.endswith(']'):
                    inner = key_part[1:-1]
                    if len(inner) >= 2 and inner[0] == chr(34) and inner[-1] == chr(34):
                        kbody = inner[1:-1]
                        if len(kbody) >= 2 and kbody[0] == chr(92):
                            try:
                                key = chr(int(kbody[1:]))
                            except:
                                continue
                        else:
                            key = kbody
                    else:
                        key = inner
                elif len(key_part) == 1:
                    key = key_part
                else:
                    continue
                b64_map[key] = val
            if len(b64_map) >= 50:
                return b64_map
        return None

    @staticmethod
    def _b64_decode(encoded: str, b64_map: dict) -> str:
        if not encoded:
            return ''
        out, j, H = [], 0, 0
        for ch in encoded:
            v = b64_map.get(ch)
            if v is not None:
                j = j + v * (64 ** (3 - H))
                H += 1
                if H == 4:
                    H = 0
                    out.append(chr((j >> 16) & 0xFF))
                    out.append(chr((j >> 8) & 0xFF))
                    out.append(chr(j & 0xFF))
                    j = 0
            elif ch == '=':
                out.append(chr((j >> 16) & 0xFF))
                pos = encoded.index(ch)
                if pos < len(encoded) - 1 and encoded[pos + 1] == '=':
                    pass
                else:
                    out.append(chr((j >> 8) & 0xFF))
                break
        return ''.join(out)

    @staticmethod
    def _extract_swap_loop(obf: str):
        m = re.search(r'for\s+\w+,\w+\s+in\s+ipairs\(\{(.*?)\}\)', obf, re.DOTALL)
        if not m:
            return None
        swaps = []
        for pair in re.finditer(r'\{([^}]+)\}', m.group(1)):
            nums = pair.group(1).split(',')
            if len(nums) >= 2:
                a, b = eval_arith(nums[0].strip()), eval_arith(nums[1].strip())
                if a is not None and b is not None:
                    swaps.append((a, b))
        return swaps if swaps else None

    @staticmethod
    def _apply_swaps(p_table: dict, swaps: list):
        for a, b in swaps:
            keys = sorted(k for k in p_table if a <= k <= b)
            reversed_values = [p_table[k] for k in reversed(keys)]
            for i, k in enumerate(keys):
                p_table[k] = reversed_values[i]

    @staticmethod
    def _static_decode_p_table(obf: str, verbose: bool = False):
        p_match = re.search(r'local\s+(\w+)=\{', obf)
        if not p_match:
            return None
        p_start = p_match.end()
        depth, pos = 1, p_start
        while pos < len(obf) and depth > 0:
            if obf[pos] == '{': depth += 1
            elif obf[pos] == '}': depth -= 1
            pos += 1
        p_end = pos - 1
        p_raw_text = obf[p_start:p_end]
        # v5.4: Use _extract_m_offset for correct accessor name (fixes wrong name after P-table)
        _m_off, acc_from_offset = WeAreDevDeobfuscator._extract_m_offset(obf)
        if re.search(re.escape(acc_from_offset) + r'\(', obf):
            accessor_name = acc_from_offset
            m_offset = _m_off
        else:
            acc_match = re.search(r'local\s+function\s+(\w+)\(', obf[p_end:p_end+200])
            accessor_name = acc_match.group(1) if acc_match else 'M'
        p_entries, scan = [], 0
        while scan < len(p_raw_text):
            q1 = p_raw_text.find(chr(34), scan)
            if q1 == -1: break
            q2 = p_raw_text.find(chr(34), q1 + 1)
            if q2 == -1: break
            raw = p_raw_text[q1 + 1:q2]
            p_entries.append(decode_decimal_escapes(raw))
            scan = q2 + 1
        if not p_entries:
            return None
        b64_map = WeAreDevDeobfuscator._extract_b64_table(obf)
        if not b64_map:
            return None
        if verbose:
            print(f'  [*] P-table: {len(p_entries)} raw, b64 alphabet: {len(b64_map)} chars')
        p_decoded = {}
        for i, entry in enumerate(p_entries, 1):
            if entry and len(entry) > 0:
                p_decoded[i] = WeAreDevDeobfuscator._b64_decode(entry, b64_map)
            else:
                p_decoded[i] = ''
        swaps = WeAreDevDeobfuscator._extract_swap_loop(obf)
        if swaps:
            WeAreDevDeobfuscator._apply_swaps(p_decoded, swaps)
            if verbose:
                print(f'  [*] Applied {len(swaps)} swap operations')
        m_offset, _ = WeAreDevDeobfuscator._extract_m_offset(obf)
        return p_decoded, accessor_name, m_offset

    @staticmethod
    def _decode_p_table(obf: str, engine: LuaEngine) -> Optional[Dict[int, str]]:
        inject_match = re.search(r'return\(function\([a-zA-Z,]+\)', obf)
        if not inject_match:
            return None
        inject_pos = inject_match.start()
        param_str = obf[inject_match.start()+16:inject_match.end()-1]
        p_var = param_str.split(',')[0].strip() if param_str else 'P'
        inject = ('do \n  for i=1,#' + p_var + ' do \n'
            '    if type(' + p_var + '[i])=="string" and #' + p_var + '[i]>0 then \n'
            '      local hex="" \n'
            '      for ci=1,#' + p_var + '[i] do hex=hex..string.format("%02x",' + p_var + '[i]:byte(ci)) end \n'
            '      print("PDEC|"..i.."|"..hex) \n'
            '    else \n'
            '      print("PDEC|"..i.."|") \n'
            '    end \n'
            '  end \n'
            '  print("PDEC_DONE") \n'
            '  return nil \n'
            'end \n')
        modified = obf[:inject_pos] + inject + obf[inject_pos:]
        load_guard = ('local _wad_real_load = loadstring or load\n'
            'if _wad_real_load then\n'
            '    load = function(src, ...)\n'
            '        if src == nil then return nil, "nil" end\n'
            '        local ok, r1, r2 = pcall(_wad_real_load, src, ...)\n'
            '        if ok then return r1, r2 else return nil, r2 end\n'
            '    end\n'
            '    loadstring = load\n'
            'end\n')
        modified = load_guard + modified
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
                P_hex[int(parts[1])] = parts[2] if len(parts) > 2 else ''
        P_decoded = {}
        for idx, h in P_hex.items():
            if h:
                try:
                    P_decoded[idx] = bytes.fromhex(h).decode('utf-8')
                except:
                    P_decoded[idx] = f'[hex:{h}]'
            else:
                P_decoded[idx] = ''
        return P_decoded if P_decoded else None

    @staticmethod
    def _extract_m_offset(obf: str) -> Tuple[int, str]:
        m = re.search(r'local function (\w+)\(\w+\)return \w+\[\w+([+-])\(?([^)]+?)\)?\]end', obf)
        if not m:
            m = re.search(r'local function (\w+)\(\w+\)return \w+\[\w+([+-])([^\]]+)\]end', obf)
        if m:
            func_name, sign, expr = m.group(1), m.group(2), m.group(3)
            val = eval_arith(expr)
            if val is not None:
                offset = val if sign == '-' else -val
                return offset, func_name
        return WeAreDevDeobfuscator.M_OFFSET, 'M'

    @staticmethod
    def _build_string_map(obf: str, P_decoded: Dict[int, str], m_offset: int, accessor_name: str = 'M') -> Dict[int, str]:
        string_map = {}
        # v5.4: Broadened regex to handle parenthesized negatives like P(-548-(-35897))
        for m in re.finditer(accessor_name + r'\((-?\d+(?:[+-]\(?-?\d+\)?|[+-]-?\d+)*)\)', obf):
            expr = m.group(1).replace('((', '(').replace('))', ')')
            val = eval_arith(expr)
            if val is not None:
                idx = val - m_offset
                if idx in P_decoded:
                    string_map[val] = P_decoded[idx]
        return string_map

    # ============================================================
    # Phase 3: CFF resolution v5.2 (BALANCED PAREN MATCHING)
    # ============================================================

    @staticmethod
    def _resolve_cff_strings_v2(obf: str, string_map: dict, accessor_name: str) -> str:
        """v5.2: Resolve accessor calls using balanced paren matching.
        Fixes calls like c(-466069-(-524710)) that have unclosed inner parens."""
        if not string_map:
            return obf
        acc = accessor_name
        acc_len = len(acc)
        BS = chr(92)
        DQ = chr(34)
        result = []
        i = 0
        n = len(obf)
        while i < n:
            if obf[i] == acc[0] and i + acc_len < n and obf[i:i+acc_len] == acc and obf[i+acc_len] == '(':
                depth, j = 0, i + acc_len
                found = False
                while j < n:
                    if obf[j] == '(': depth += 1
                    elif obf[j] == ')':
                        depth -= 1
                        if depth == 0:
                            found = True
                            break
                    j += 1
                if found:
                    expr = obf[i+acc_len+1:j]
                    val = eval_arith(expr)
                    if val is not None and val in string_map:
                        s = string_map[val]
                        if s:
                            escaped = s.replace(BS, BS+BS).replace(DQ, BS+DQ)
                            result.append(DQ + escaped + DQ)
                            i = j + 1
                            continue
                result.append(obf[i:j+1] if found else obf[i])
                i = (j + 1) if found else (i + 1)
            elif obf[i] == DQ:
                j = i + 1
                while j < n and obf[j] != DQ:
                    if obf[j] == BS and j + 1 < n: j += 1
                    j += 1
                result.append(obf[i:j+1])
                i = j + 1
            else:
                result.append(obf[i])
                i += 1
        return ''.join(result)

    @staticmethod
    def _resolve_cff_strings(obf: str, string_map: dict, accessor_name: str) -> str:
        return WeAreDevDeobfuscator._resolve_cff_strings_v2(obf, string_map, accessor_name)

    # ============================================================
    # Phase 3.5: Post-CFF decode + arithmetic simplification (v5.3 NEW)
    # ============================================================

    @staticmethod
    def _simplify_arith_expr(expr: str) -> str:
        """Simplify a single arithmetic expression like -903041-(-903042) -> 1."""
        expr = expr.strip()
        val = eval_arith(expr)
        if val is not None:
            return str(val)
        return expr

    @staticmethod
    def _simplify_arith_in_code(code: str) -> str:
        """Simplify arithmetic expressions throughout code: -903041-(-903042) -> 1."""
        # Pattern: number -(-number) (single paren)
        code = re.sub(r'-?\d+-\(-\d+\)',
                       lambda m: WeAreDevDeobfuscator._simplify_arith_expr(m.group(0)), code)
        # Pattern: number -((-number)) (double parens)
        code = re.sub(r'-?\d+-\(\(-\d+\)\)',
                       lambda m: WeAreDevDeobfuscator._simplify_arith_expr(m.group(0)), code)
        # Pattern: number +-number
        code = re.sub(r'(-?\d+)[+]-\d+',
                       lambda m: WeAreDevDeobfuscator._simplify_arith_expr(m.group(0)), code)
        # Pattern: number +(-number)
        code = re.sub(r'(-?\d+)\+\(-\d+\)',
                       lambda m: WeAreDevDeobfuscator._simplify_arith_expr(m.group(0)), code)
        # Pattern: -number+number (obfuscated constants)
        def neg_plus(m):
            val = eval_arith(m.group(0))
            if val is not None and abs(val) < 100000:
                return str(val)
            return m.group(0)
        code = re.sub(r'-\d+\+\d+', neg_plus, code)
        # Pattern: simple number - number (careful not to match inside identifiers)
        def safe_simplify(m):
            expr = m.group(0)
            start = m.start()
            if start > 0 and code[start-1].isalpha():
                return expr
            return WeAreDevDeobfuscator._simplify_arith_expr(expr)
        code = re.sub(r'(?<![a-zA-Z_.])-?\d+-\d+', safe_simplify, code)
        return code

    @staticmethod
    def _decode_string_literals(code: str) -> str:
        r"""Decode \ddd decimal escapes inside all quoted string literals."""
        result = []
        i = 0
        n = len(code)
        while i < n:
            if code[i] == '"':
                j = i + 1
                while j < n and code[j] != '"':
                    if code[j] == '\\' and j + 1 < n:
                        j += 2
                    else:
                        j += 1
                if j < n:
                    raw_str = code[i+1:j]
                    decoded = decode_decimal_escapes(raw_str)
                    escaped = decoded.replace('\\', '\\\\').replace('"', '\\"')
                    result.append('"' + escaped + '"')
                    i = j + 1
                    continue
            result.append(code[i])
            i += 1
        return ''.join(result)

    @staticmethod
    def _post_process_cff(resolved_cff: str) -> str:
        """v5.3: Decode decimal escapes + simplify arithmetic in CFF output.
        This makes subsequent pattern mining much more effective."""
        # Step 1: Decode decimal escape sequences in string literals
        decoded = WeAreDevDeobfuscator._decode_string_literals(resolved_cff)
        # Step 2: Simplify arithmetic expressions
        simplified = WeAreDevDeobfuscator._simplify_arith_in_code(decoded)
        return simplified

    # ============================================================
    # Phase 4.5: Opcode string analysis (v5.3 NEW)
    # ============================================================

    @staticmethod
    def _mine_opcode_strings(decoded_cff: str) -> List[str]:
        """v5.3: Extract API method names, messages, and meaningful strings
        from the VM's opcode dispatch branches."""
        if not decoded_cff:
            return []
        lines, seen = [], set()
        def add(line):
            if line and line not in seen:
                seen.add(line)
                lines.append(line)

        API = WeAreDevDeobfuscator.VM_API_NAMES
        UTILITY = WeAreDevDeobfuscator.VM_UTILITY_NAMES

        # Find all meaningful string literals
        for m in re.finditer(r'"([^"]{2,})"', decoded_cff):
            s = m.group(1)
            # Must be printable ASCII
            if not all(32 <= ord(c) < 127 for c in s):
                continue
            # Skip base64-looking strings and random alphanumeric
            if re.match(r'^[A-Za-z0-9+/=]{6,}$', s):
                continue
            if re.match(r'^[A-Za-z][a-z0-9]{2,}[A-Z][a-z0-9]*$', s):
                continue

            # API method names
            if s in API:
                add(f'-- VM uses API: {s}')
                continue

            # Utility names
            if s in UTILITY:
                add(f'-- VM uses: {s}')
                continue

            # Roblox API names not in our set
            if s in ('Instance', 'game', 'workspace', 'Enum', 'task', 'coroutine',
                     'Color3', 'Vector3', 'Vector2', 'UDim2', 'UDim', 'CFrame',
                     'TweenInfo', 'Rect', 'Font', 'NumberSequence', 'ColorSequence',
                     'NumberRange', 'RaycastParams', 'PhysicalProperties',
                     'Players', 'ReplicatedStorage', 'RunService', 'UserInputService',
                     'TweenService', 'Lighting', 'StarterGui', 'HttpService',
                     'DataStoreService', 'MarketplaceService', 'CollectionService',
                     'PathfindingService', 'SoundService', 'TextService',
                     'GuiService', 'CoreGui', 'VirtualUser', 'ContentProvider'):
                add(f'-- VM references: {s}')
                continue

            # String constants that look like messages, property values, identifiers
            if len(s) >= 4 and len(s) <= 120:
                # Property names
                if re.match(r'^[A-Z][a-zA-Z0-9]*$', s) and s[0].isupper():
                    if s not in ('true', 'false', 'nil', 'then', 'else', 'end', 'do',
                                 'local', 'function', 'return', 'if', 'while', 'for',
                                 'in', 'not', 'and', 'or', 'repeat', 'until', 'break'):
                        # Could be a property name, Enum member, or class name
                        if s in ('ScreenGui', 'Frame', 'TextLabel', 'TextButton',
                                 'UICorner', 'UIPadding', 'UIStroke', 'UIListLayout',
                                 'UIGridLayout', 'ImageLabel', 'ImageButton',
                                 'ScrollingFrame', 'ViewportFrame', 'CanvasGroup',
                                 'BillboardGui', 'SurfaceGui', 'Folder',
                                 'RemoteEvent', 'RemoteFunction', 'BindableEvent',
                                 'BindableFunction', 'ObjectValue', 'StringValue',
                                 'BoolValue', 'IntValue', 'NumberValue',
                                 'Humanoid', 'Part', 'Model', 'Workspace',
                                 'Camera', 'LocalScript', 'Script', 'ModuleScript'):
                            add(f'-- VM creates/references class: {s}')
                        elif any(x in s for x in ['Color', 'Size', 'Position', 'Text',
                                                    'Font', 'Visible', 'Enabled', 'Name',
                                                    'Parent', 'Value', 'Transparency',
                                                    'Anchor', 'Border', 'Layout', 'ZIndex',
                                                    'Background', 'Offset', 'Scale']):
                            add(f'-- VM sets property: {s}')
                        elif '.' in s and s.split('.')[0] in ('Font', 'Enum', 'TweenInfo'):
                            add(f'-- VM uses: {s}')
                        else:
                            add(f'-- VM identifier: {s}')
                    continue

                # Messages / display text (contains spaces or special chars)
                if any(c in s for c in [' ', '!', '?', '.', ':', '/', '\\', '%']) and not s.startswith('end'):
                    # Skip code-like strings (VM internal operations)
                    if re.match(r'^[a-z]=', s) or re.match(r'^[a-z][A-Z]', s):
                        continue
                    # Skip strings that look like VM bytecode fragments
                    if '=' in s and any(kw in s for kw in ['q[o]', 'q[S]', 'p[r[', 'q=p[r',
                                                          'q<', 'q and', 'q or', 'q=U',
                                                          'end else', 'end end', 'end if']):
                        continue
                    # Skip strings with too many VM-like patterns (generic variable names)
                    vm_var_count = sum(1 for c in s if c == '=' )
                    short_assign = len(re.findall(r'[a-wyz][\[=]', s))
                    if vm_var_count >= 3 or short_assign >= 4:
                        continue
                    if 'Tamper' in s or 'error' in s.lower() or 'warn' in s.lower():
                        add(f'-- Anti-tamper check: "{s}"')
                    elif any(kw in s.lower() for kw in ['http', '://', 'www.', '.com', '.io', '.gg']):
                        add(f'-- URL detected: "{s}"')
                    elif re.match(r'^%[sdifgoxq]', s) or '%' in s:
                        add(f'string.format("{s}", ...)')
                    elif len(s) >= 8 and s.count(' ') >= 1:
                        alpha_count = sum(1 for c in s if c.isalpha())
                        if alpha_count >= len(s) * 0.6:
                            add(f'-- String constant: "{s}"')

        # Also extract any remaining c() calls that we can evaluate
        for m in re.finditer(r'c\(([^)]+)\)', decoded_cff):
            expr = m.group(1).strip()
            val = eval_arith(expr)
            if val is not None and abs(val) < 10000000:
                # These are unresolved accessor calls - note them
                pass

        return lines

    # ============================================================
    # Phase 4: Deep body mining (v5.2 NEW)
    # ============================================================

    @staticmethod
    def _deep_mine_body(resolved_cff: str, string_map: dict = None) -> List[str]:
        """v5.2: Mine the fully-resolved body for display text, messages, URLs."""
        if not resolved_cff:
            return []
        code_lines, seen = [], set()
        def add(line):
            if line and line not in seen:
                seen.add(line)
                code_lines.append(line)
        for m in re.finditer(r'\.([A-Za-z_]\w*)\s*=\s*"([^"]{2,})"', resolved_cff):
            prop, val = m.group(1), m.group(2)
            if prop in ('Name', 'Text', 'Value', 'Tag') and len(val) < 100:
                if not re.match(r'^[A-Za-z0-9]{8,}$', val):
                    add(f'.{prop} = "{val}"')
        for m in re.finditer(r'string\.format\("([^"]{5,})"', resolved_cff):
            fmt = m.group(1)
            if '%' in fmt and len(fmt) < 300:
                add(f'string.format("{fmt}", ...)')
        for m in re.finditer(r'require\("([^"]+)"\)', resolved_cff):
            add(f'require("{m.group(1)}")')
        for m in re.finditer(r'(?:error|warn)\("([^"]{5,})"', resolved_cff):
            msg = m.group(1)
            if len(msg) > 5 and not re.match(r'^[A-Za-z0-9]{8,}$', msg):
                add(f'{m.group(0)}')
        for m in re.finditer(r'print\("([^"]{3,})"\)', resolved_cff):
            msg = m.group(1)
            if len(msg) > 3 and not re.match(r'^[A-Za-z0-9]{8,}$', msg):
                add(f'print("{msg}")')
        for m in re.finditer(r'(?:HttpGet|HttpPost)\("(https?://[^"]+)"\)', resolved_cff):
            add(f':HttpGet("{m.group(1)}")')
        for m in re.finditer(r'"([^"]{5,})"', resolved_cff):
            s = m.group(1)
            if ' ' in s and 3 < len(s) < 100:
                if any(kw in s.lower() for kw in ['status', 'result', 'counter', 'enable',
                                                      'error', 'warn', 'loaded', 'success', 'fail']):
                    add(f'-- Display text: "{s}"')
        return code_lines

    # ============================================================
    # Phase 2: VM trace
    # ============================================================

    _TRACER_LUA = 'local _trace = {}\nlocal _trace_n = 0\nlocal _orig_print = print\n\n-- v5: unpack polyfill for LuaJIT Lua 5.2+ compatibility\nif not _G.unpack then _G.unpack = table.unpack end\n\nlocal function safe_tostring(v)\n    if type(v) == "string" then\n        return string.format("%q", v)\n    end\n    if type(v) == "nil" then return "nil" end\n    if type(v) == "boolean" then return tostring(v) end\n    if type(v) == "function" then return "function" end\n    if type(v) == "table" then return "{}" end\n    return tostring(v)\nend\n\n-- v6 fix: used ONLY for values being ASSIGNED (obj.Prop = value), never\n-- for call arguments. Unlike safe_tostring, this shows the readable chain\n-- path for our tracer proxies (e.g. "game.GetService(Players).LocalPlayer")\n-- instead of collapsing every unresolved object into a bare "{}" -- which\n-- downstream reconstruction used to turn into a misleading "nil". Kept\n-- separate from safe_tostring so existing call-argument patterns (which\n-- expect a plain "{}" placeholder for the self/receiver argument) keep\n-- working unchanged.\nlocal function safe_tostring_value(v)\n    if type(v) == "table" then\n        local mt = getmetatable(v)\n        if mt and mt.__tostring then\n            return tostring(v)\n        end\n        return "{}"\n    end\n    return safe_tostring(v)\nend\n\nlocal function T(entry)\n    _trace_n = _trace_n + 1\n    _trace[_trace_n] = entry\n    _orig_print("[T]" .. entry)\nend\n\nlocal function traced_print(...)\n    local args = {...}\n    local strs = {}\n    for i, v in ipairs(args) do\n        strs[i] = tostring(v)\n    end\n    local line = table.concat(strs, "\\t")\n    _orig_print("[P]" .. line)\n    local arg_strs = {}\n    for i, v in ipairs(args) do\n        arg_strs[i] = safe_tostring(v)\n    end\n    T("print(" .. table.concat(arg_strs, ", ") .. ")")\nend\n\nlocal _cb_depth = 0\nlocal MAX_CB_DEPTH = 3\n\n-- forward declaration so helpers defined before make_chain_tracer\'s real\n-- body can still close over the correct (soon-to-be-assigned) local\nlocal make_chain_tracer\n\n-- v8: methods that return a COLLECTION of children in real Roblox\n-- (GetChildren, GetDescendants, ...). Previously these returned an opaque\n-- proxy that pairs()/ipairs() can\'t iterate, so any script whose real\n-- logic lives INSIDE such a loop (e.g. "for _,v in pairs(workspace:GetDescendants())")\n-- traced to nothing at all. Returning a real Lua table with a couple of\n-- fake-but-tracer-backed entries lets the loop body actually execute at\n-- least once, revealing what\'s inside.\nlocal ENUMERABLE_METHODS = {\n    GetChildren = true, GetDescendants = true, GetPlayers = true,\n    GetTouchingParts = true, GetConnectedParts = true,\n}\nlocal function is_enumerable_call(path)\n    for name in pairs(ENUMERABLE_METHODS) do\n        if path:sub(-#name - 1) == "." .. name then return true end\n    end\n    return false\nend\n\n-- v8: methods named "Is..." (IsA, IsDescendantOf, IsAncestorOf, ...) are\n-- boolean-returning in the real Roblox API. Returning a generic proxy here\n-- is truthy in Lua either way, but `not proxy` is always false -- which\n-- silently breaks extremely common patterns like\n-- "if v:IsA(x) and not v:IsDescendantOf(y) then". Returning a real `true`\n-- lets that boolean logic behave as intended so more branches get entered.\nlocal function is_boolean_call(path)\n    local method = path:match("%.([%w_]+)$")\n    return method ~= nil and method:sub(1, 2) == "Is"\nend\nlocal function make_fake_children(parent_path, count)\n    local list = {}\n    for i = 1, (count or 2) do\n        list[i] = make_chain_tracer(parent_path .. "[" .. i .. "]")\n    end\n    return list\nend\n\n-- v8: pick plausible dummy arguments for a callback based on the event\n-- name in its chain path, instead of always using the same generic tuple.\n-- A closer-to-real argument shape means less of the callback body bails\n-- out early on a type mismatch (e.g. "if input.KeyCode == ... then").\nlocal function get_dummy_args(path)\n    if path:find("Heartbeat", 1, true) or path:find("Stepped", 1, true)\n        or path:find("RenderStepped", 1, true) then\n        return {0.016}\n    elseif path:find("InputBegan", 1, true) or path:find("InputEnded", 1, true)\n        or path:find("InputChanged", 1, true) then\n        return {make_chain_tracer(path .. ":input"), false}\n    elseif path:find("Touched", 1, true) or path:find("TouchEnded", 1, true) then\n        return {make_chain_tracer(path .. ":part")}\n    elseif path:find("CharacterAdded", 1, true) or path:find("PlayerAdded", 1, true)\n        or path:find("PlayerRemoving", 1, true) then\n        return {make_chain_tracer(path .. ":char")}\n    else\n        return {make_chain_tracer(path .. ":cb_arg"), false, 0.016, 1}\n    end\nend\n\nfunction make_chain_tracer(name)\n    local proxy = {}\n    local full_path = name\n    local mt = {\n        __index = function(t, k)\n            local kstr = type(k) == "string" and k or tostring(k)\n            T(full_path .. "." .. kstr)\n            local new_path = full_path .. "." .. kstr\n            return make_chain_tracer(new_path)\n        end,\n        __newindex = function(t, k, v)\n            local kstr = type(k) == "string" and k or tostring(k)\n            local vstr = safe_tostring_value(v)\n            T(full_path .. "." .. kstr .. " = " .. vstr)\n        end,\n        __call = function(t, ...)\n            local raw_args = {...}\n            local args = {}\n            for i, a in ipairs(raw_args) do\n                args[i] = safe_tostring(a)\n            end\n            T(full_path .. "(" .. table.concat(args, ", ") .. ")")\n\n            -- v6 fix: keep the last STRING-typed argument as part of the\n            -- returned object\'s identity. Without this, EVERY call like\n            -- game:GetService("RunService"), game:GetService("TweenService"),\n            -- obj:WaitForChild("Name") etc. collapsed into the exact same\n            -- ambiguous path "foo()" -- so later code couldn\'t tell which\n            -- service/child a chain actually came from, and downstream\n            -- reconstruction had to guess (often guessing wrong, e.g. every\n            -- unresolved chain getting attributed to whichever service was\n            -- seen last in the script).\n            local discriminator = nil\n            for i = #raw_args, 1, -1 do\n                if type(raw_args[i]) == "string" then\n                    discriminator = raw_args[i]\n                    break\n                end\n            end\n\n            -- v6 fix: auto-invoke function arguments (event handler\n            -- callbacks). Roblox events (Heartbeat, InputBegan,\n            -- MouseButton1Click, CharacterAdded, ...) never fire on their\n            -- own during a static/offline VM run, so without this the\n            -- body of every :Connect(function() ... end) was completely\n            -- invisible to the tracer -- which is where most of a script\'s\n            -- real logic usually lives. We call it once with plausible\n            -- dummy arguments so its body actually executes and gets traced.\n            if _cb_depth < MAX_CB_DEPTH then\n                for i = 1, #raw_args do\n                    if type(raw_args[i]) == "function" then\n                        _cb_depth = _cb_depth + 1\n                        local dummy_args = get_dummy_args(full_path)\n                        local ok, err = pcall(raw_args[i], table.unpack(dummy_args))\n                        _cb_depth = _cb_depth - 1\n                        if not ok then\n                            T("-- callback error (" .. full_path .. "): " .. tostring(err))\n                        end\n                    end\n                end\n            end\n\n            -- v8: if this call is one of the known "returns a collection"\n            -- methods (GetChildren/GetDescendants/...), hand back a real,\n            -- iterable Lua table instead of another opaque chain proxy.\n            if is_enumerable_call(full_path) then\n                return make_fake_children(full_path, 2)\n            end\n            if is_boolean_call(full_path) then\n                return true\n            end\n\n            if discriminator then\n                return make_chain_tracer(full_path .. "(" .. discriminator .. ")")\n            end\n            return make_chain_tracer(full_path .. "()")\n        end,\n        __tostring = function(t) return full_path end,\n        __concat = function(a, b) return "" end,\n        __len = function(t) return 0 end,\n        __add = function(a, b) return 0 end,\n        __sub = function(a, b) return 0 end,\n        __mul = function(a, b) return 0 end,\n        __div = function(a, b) return 0 end,\n        __mod = function(a, b) return 0 end,\n        __pow = function(a, b) return 0 end,\n        __eq = function(a, b) return false end,\n        -- v8: bias toward entering branches rather than skipping them.\n        -- Comparing a proxy (unresolved value) against a real number/other\n        -- value is inherently a coin flip -- but for RECOVERY purposes,\n        -- missing real logic (false negative) is worse than tracing a\n        -- branch that wouldn\'t truly have run (false positive). Numeric\n        -- threshold checks like "if dims[3] >= 20 and dims[3] <= limit"\n        -- previously always evaluated false here, silently skipping\n        -- everything inside.\n        __lt = function(a, b) return true end,\n        __le = function(a, b) return true end,\n    }\n    setmetatable(proxy, mt)\n    return proxy\nend\nlocal make_tracer = make_chain_tracer\n\n_G.print = traced_print\n_G.warn = traced_print\n_G.info = traced_print\n\nif not _G.getfenv then _G.getfenv = function(l) return _G end end\nif not _G.getgenv then _G.getgenv = function() return _G end end\nif not _G.setfenv then _G.setfenv = function() end end\nif not _G.unpack then _G.unpack = table.unpack end\n\nlocal _orig_pcall = pcall\n_G.pcall = function(f, ...)\n    local results = {_orig_pcall(f, ...)}\n    local ok = results[1]\n    if not ok then\n        local err = tostring(results[2])\n        if not err:find("pow", 1, true) then\n            T("-- pcall error: " .. err)\n        end\n    end\n    return table.unpack(results)\nend\n\nlocal _orig_xpcall = xpcall\n_G.xpcall = function(f, handler, ...)\n    local results = {_orig_xpcall(f, handler, ...)}\n    local ok = results[1]\n    if not ok then\n        T("-- xpcall error: " .. tostring(results[2]))\n    end\n    return table.unpack(results)\nend\n\nlocal _orig_load = loadstring or load\nif _orig_load then\n    local _real_load = _orig_load\n    _G.load = function(src, ...)\n        if src == nil then return nil, "cannot load nil" end\n        if type(src) ~= "string" and type(src) ~= "function" then\n            local ok, r1, r2 = pcall(_real_load, src, ...)\n            if ok then return r1, r2 else return nil, r2 end\n        end\n        if type(src) == "string" and #src > 5 then\n            local first100 = src:sub(1, 100)\n            if not first100:find("bit32", 1, true) and not first100:find("4294967296", 1, true) then\n                T("-- loadstring called (" .. #src .. " chars)")\n            end\n        end\n        local ok, r1, r2 = pcall(_real_load, src, ...)\n        if ok then return r1, r2 else return nil, r2 end\n    end\n    _G.loadstring = _G.load\n    if debug then\n        if debug.getupvalue then\n            debug.getupvalue = function(...) return nil end\n        end\n        if debug.setupvalue then\n            debug.setupvalue = function(...) return nil end\n        end\n    end\nend\n\n_G.newproxy = function(b)\n    local t = {}\n    if b then setmetatable(t, {__index = function() return nil end}) end\n    return t\nend\n\nlocal api_names = {\n    "game", "workspace", "Instance", "Enum",\n    "Players", "ReplicatedStorage", "ReplicatedFirst",\n    "ServerStorage", "ServerScriptService", "StarterGui",\n    "StarterPlayer", "StarterPack", "StarterCharacterScripts",\n    "Lighting", "Teams", "Chat", "Debris",\n    "TweenService", "RunService", "UserInputService",\n    "HttpService", "MarketplaceService", "CollectionService",\n    "PathfindingService", "SoundService", "TextService",\n    "GuiService", "UserSettings", "CoreGui", "CorePackages",\n    "VirtualUser", "ContentProvider",\n    "DataStoreService", "BadgeService",\n    "UDim", "UDim2", "Color3", "Vector2", "Vector3",\n    "CFrame", "Ray", "Region3", "TweenInfo",\n    "Rect", "Font", "NumberSequence", "ColorSequence",\n    "NumberRange", "RaycastParams", "PhysicalProperties",\n    "task", "coroutine",\n}\n\nfor _, api_name in ipairs(api_names) do\n    _G[api_name] = make_tracer(api_name)\nend\n\n_orig_print("[STUBS_OK]")\n'

    @staticmethod
    def _get_tracer_lua() -> str:
        return WeAreDevDeobfuscator._TRACER_LUA

    @staticmethod
    def _execute_vm_traced(obf: str) -> Tuple[List[str], List[str], List[str]]:
        """Execute VM via subprocess with tracing. v5.2: 30s timeout."""
        import subprocess
        tracer_lua = WeAreDevDeobfuscator._get_tracer_lua()
        import base64
        tracer_b64 = base64.b64encode(tracer_lua.encode('utf-8')).decode('ascii')
        runner_code = ('import sys,os,base64\n'
            'from lupa import LuaRuntime\n'
            'TRACER_LUA=base64.b64decode("' + tracer_b64 + '").decode("utf-8")\n'
            'if len(sys.argv)<2:\n'
            '    print("[EX]No input file");sys.exit(1)\n'
            'with open(sys.argv[1],"r",encoding="utf-8",errors="replace") as f:code=f.read()\n'
            'lua=LuaRuntime(unpack_returned_tuples=True)\n'
            'try:lua.execute(TRACER_LUA+chr(10)+code);print("[DONE]")\n'
            'except Exception as e:print("[EX]"+str(e)[:500])\n')
        runner_file = tempfile.mktemp(suffix='.py', prefix='wad_runner_')
        obf_file = tempfile.mktemp(suffix='.lua', prefix='wearedev_v5_')
        try:
            with open(runner_file, 'w') as f: f.write(runner_code)
            with open(obf_file, 'w') as f: f.write(obf)
            result = subprocess.run(
                [sys.executable, runner_file, obf_file],
                capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            result = subprocess.CompletedProcess([], 1, stdout='', stderr='timeout')
        except Exception:
            result = subprocess.CompletedProcess([], 1, stdout='', stderr='error')
        finally:
            for fp in (runner_file, obf_file):
                if os.path.exists(fp):
                    try: os.unlink(fp)
                    except: pass
        prints, trace, errors = [], [], []
        for line in result.stdout.split('\n'):
            line = line.strip()
            if not line: continue
            if line.startswith('[P]'): prints.append(line[3:])
            elif line.startswith('[T]'): trace.append(line[3:])
            elif line.startswith('[EX]'): errors.append(line[4:])
        return prints, trace, errors

    # ============================================================
    # Phase 5: Source reconstruction (v5.2 UPGRADED)
    # ============================================================

    COLON_METHODS = frozenset({
        'GetService', 'WaitForChild', 'FindFirstChild', 'FindFirstChildOfClass',
        'FindFirstChildWhichIsA', 'IsA', 'Clone', 'Destroy', 'Connect',
        'Disconnect', 'InvokeServer', 'FireServer', 'Fire', 'OnServerEvent',
        'OnClientEvent', 'HttpGet', 'HttpPost', 'Wait', 'GetPropertyChangedSignal',
    })

    @staticmethod
    def _clean_chain(chain: str, service_names: set, last_service_var: str = None) -> str:
        result = chain
        # v6 fix: game.GetService(ServiceName) now carries the REAL service
        # name directly (see tracer __call fix) -- handle this explicit,
        # unambiguous form first. No guessing needed anymore.
        m_explicit = re.match(r'^game\.GetService\((\w+)\)\.?(.*)$', result)
        if m_explicit:
            svc, rest = m_explicit.group(1), m_explicit.group(2)
            result = f'{svc}.{rest}' if rest else svc
        # v5.3: Remove game.GetService() prefix first (legacy/ambiguous form,
        # kept as a fallback for any trace entries that still produce it)
        while 'game.GetService()' in result:
            parts = result.split('game.GetService()', 1)
            rest = parts[1] if len(parts) > 1 else ''
            if rest.startswith('.'):
                rest = rest[1:]
            # Check if the next segment is a service name
            first_seg = rest.split('.')[0] if rest else ''
            for svc in service_names:
                if first_seg == svc or rest.startswith(svc + '.'):
                    rest = rest[len(svc):]
                    if rest.startswith('.'):
                        rest = rest[1:]
                    break
            result = rest if rest else result
            break  # Only strip once
        # v5.3: Clean service-name prefixes (e.g., "PlayersLocalPlayer" -> "LocalPlayer")
        for svc in sorted(service_names, key=len, reverse=True):
            if result.startswith(svc):
                rest = result[len(svc):]
                if not rest or rest[0] == '.':
                    result = rest[1:] if rest.startswith('.') else rest
                    break
        # v5.3: Convert dot method calls to colon where appropriate
        COLON_METHODS = WeAreDevDeobfuscator.COLON_METHODS
        for method in COLON_METHODS:
            pattern = '.' + method + '('
            replacement = ':' + method + '('
            result = result.replace(pattern, replacement)
        return result

    @staticmethod
    def _reconstruct_source(trace: List[str], prints: List[str],
                            string_map: Dict[int, str] = None,
                            resolved_cff: str = '') -> str:
        if not trace and not prints:
            return ''
        COLON_METHODS = WeAreDevDeobfuscator.COLON_METHODS
        non_prefix = []
        for i, entry in enumerate(trace):
            if entry.startswith('--'):
                non_prefix.append(entry)
                continue
            is_pref = any(
                j != i and (other.startswith(entry + '.') or other.startswith(entry + '('))
                for j, other in enumerate(trace)
            )
            if not is_pref:
                non_prefix.append(entry)
        lines = []
        inst_counter = 0
        current_inst = None
        pending_value = None
        pending_value_type = None
        service_names = set()
        last_service_var = None
        inst_properties = {}
        connect_stack = []

        for entry in non_prefix:
            stripped = entry.strip()
            if not stripped or (stripped.endswith('.') and '(' not in stripped):
                continue
            if stripped.startswith('--'):
                if 'pow' not in stripped and 'Tamper' not in stripped.lower() and 'pcall' not in stripped.lower():
                    lines.append(stripped)
                continue
            if stripped.startswith('print('):
                continue
            m = re.match(r'game\.GetService\(\{\},\s*"(\w+)"\)', stripped)
            if m:
                svc = m.group(1)
                service_names.add(svc)
                last_service_var = svc
                lines.append(f'local {svc} = game:GetService("{svc}")')
                pending_value = pending_value_type = None
                continue
            m = re.match(r'Instance\.new\("([^"]+)"\)', stripped)
            if m:
                if current_inst and inst_properties.get(current_inst):
                    for prop, val in inst_properties[current_inst]:
                        if val == '{}' and pending_value: val = pending_value
                        if val == 'nil' and prop in ('BackgroundColor3', 'TextColor3') and pending_value: val = pending_value
                        lines.append(f'{current_inst}.{prop} = {val}')
                    inst_properties[current_inst] = []
                inst_counter += 1
                current_inst = f'inst{inst_counter}'
                lines.append(f'local {current_inst} = Instance.new("{m.group(1)}")')
                inst_properties[current_inst] = []
                pending_value = pending_value_type = None
                continue
            m = re.match(r'Instance\.new\(\)\.([\w.]+)\s*=\s*(.+)', stripped)
            if m and current_inst:
                prop, val = m.group(1), m.group(2).strip()
                if val == '{}': val = pending_value if pending_value else 'nil'
                inst_properties.setdefault(current_inst, []).append((prop, val))
                if pending_value and '=' in stripped:
                    pending_value = pending_value_type = None
                continue
            m = re.match(r'(UDim2|Color3|UDim|Vector3|Vector2|CFrame|TweenInfo|Rect|NumberSequence|ColorSequence|NumberRange|RaycastParams|PhysicalProperties)\.(\w+)\((.+)', stripped)
            if m:
                pending_value = stripped
                pending_value_type = m.group(1)
                continue
            m = re.match(r'Enum\.(\w+\.\w+)', stripped)
            if m:
                pending_value = stripped
                pending_value_type = 'Enum'
                continue
            # v5.3: Connect with optional args (handle both .Connect and :Connect)
            # Also handle Instance.new() connect (add comment about missing target)
            m = re.match(r'Instance\.new\(\)\.([\w.]+):Connect\(\{\},\s*function\)', stripped)
            if m:
                event_name = m.group(1)
                # Close any open connect
                if connect_stack:
                    lines.append('    -- [event handler body requires Roblox environment]')
                    lines.append('end)')
                    connect_stack.pop()
                lines.append(f'-- [{event_name} event handler registered]')
                lines.append(f'-- [handler body not executed during trace]')
                pending_value = pending_value_type = None
                continue
            m = re.match(r'(.+?)[.:]Connect\(\{\},\s*function\(([^)]*)\)\)', stripped)
            if not m:
                m = re.match(r'(.+?)[.:]Connect\(\{\},\s*function\)', stripped)
            if m:
                chain = m.group(1)
                args = m.group(2).strip() if len(m.groups()) > 1 and m.group(2) else ''
                chain = WeAreDevDeobfuscator._clean_chain(chain, service_names, last_service_var)
                if connect_stack:
                    lines.append('    -- [event handler body requires Roblox environment]')
                    lines.append('end)')
                    connect_stack.pop()
                if args:
                    lines.append(f'{chain}:Connect(function({args}))')
                else:
                    lines.append(f'{chain}:Connect(function()')
                connect_stack.append(chain)
                pending_value = pending_value_type = None
                continue
            # v6: game.GetService(ServiceName).X.Y -- explicit, unambiguous
            # form (see tracer fix). Handle before the legacy empty-parens form.
            m = re.match(r'game\.GetService\((\w+)\)\.([\w.]+)$', stripped)
            if m:
                svc, chain = m.group(1), m.group(2)
                chain = WeAreDevDeobfuscator._clean_chain(f'game.GetService({svc}).{chain}', service_names, last_service_var)
                first_part = chain.split('.')[0] if chain else ''
                if first_part and first_part not in ('LocalPlayer', 'Character', 'Humanoid', 'Workspace') and first_part != svc:
                    lines.append(f'local {first_part} = game:GetService("{first_part}")')
                pending_value = pending_value_type = None
                continue
            # v5.3: Handle game.GetService().X.Y chains (from remaining trace entries)
            m = re.match(r'game\.GetService\(\)\.([\w.]+)$', stripped)
            if m:
                chain = m.group(1)
                chain = WeAreDevDeobfuscator._clean_chain(chain, service_names, last_service_var)
                first_part = chain.split('.')[0] if chain else ''
                # v5.3: Don't create false service references for non-service names
                if first_part and first_part not in ('LocalPlayer', 'Character', 'Humanoid', 'Workspace'):
                    lines.append(f'local {first_part} = game:GetService("{first_part}")')
                pending_value = pending_value_type = None
                continue
            # v6: game.GetService(ServiceName).X.Y(args) -- explicit method call form
            m = re.match(r'game\.GetService\((\w+)\)\.([\w.]+)\.([\w]+)\(\{\},\s*(.+)\)', stripped)
            if m:
                svc, chain, method, args = m.group(1), m.group(2), m.group(3), m.group(4).strip()
                chain = WeAreDevDeobfuscator._clean_chain(f'game.GetService({svc}).{chain}', service_names, last_service_var)
                colon = ':' if method in COLON_METHODS else '.'
                lines.append(f'{chain}{colon}{method}({args})')
                pending_value = pending_value_type = None
                continue
            m = re.match(r'game\.GetService\(\)\.([\w.]+)\.([\w]+)\(\{\},\s*(.+)\)', stripped)
            if m:
                chain, method, args = m.group(1), m.group(2), m.group(3).strip()
                chain = WeAreDevDeobfuscator._clean_chain(chain, service_names, last_service_var)
                colon = ':' if method in COLON_METHODS else '.'
                lines.append(f'{chain}{colon}{method}({args})')
                pending_value = pending_value_type = None
                continue
            # v6: game.GetService(ServiceName).X.Y = val -- explicit property assignment form
            m = re.match(r'game\.GetService\((\w+)\)\.([\w.]+)\.([\w]+)\s*=\s*(.+)', stripped)
            if m:
                svc, obj_chain, prop, val = m.group(1), m.group(2), m.group(3), m.group(4).strip()
                obj_chain = WeAreDevDeobfuscator._clean_chain(f'game.GetService({svc}).{obj_chain}', service_names, last_service_var)
                if val == '{}': val = pending_value if pending_value else 'nil'
                lines.append(f'{obj_chain}.{prop} = {val}')
                if pending_value:
                    pending_value = pending_value_type = None
                continue
            m = re.match(r'game\.GetService\(\)\.([\w.]+)\.([\w]+)\s*=\s*(.+)', stripped)
            if m:
                obj_chain, prop, val = m.group(1), m.group(2), m.group(3).strip()
                obj_chain = WeAreDevDeobfuscator._clean_chain(obj_chain, service_names, last_service_var)
                if val == '{}': val = pending_value if pending_value else 'nil'
                lines.append(f'{obj_chain}.{prop} = {val}')
                if pending_value:
                    pending_value = pending_value_type = None
                continue
            m = re.match(r'game\.HttpGet\(\{\},\s*(.+)\)', stripped)
            if m:
                lines.append(f'game:HttpGet({m.group(1).strip()})')
                pending_value = pending_value_type = None
                continue
            # v5.2: Generic property assignment (with service name prefix cleanup)
            m = re.match(r'([\w.]+)\.([\w]+)\s*=\s*(.+)', stripped)
            if m:
                obj, prop, val = m.group(1), m.group(2), m.group(3).strip()
                # Clean service prefixes: UserInputServiceLocalPlayer -> LocalPlayer
                for svc in service_names:
                    if obj.startswith(svc) and len(obj) > len(svc):
                        obj = obj[len(svc):]
                        if obj.startswith('.'):
                            obj = obj[1:]
                        break
                if val == '{}': val = pending_value if pending_value else 'nil'
                lines.append(f'{obj}.{prop} = {val}')
                if pending_value and '=' in stripped:
                    pending_value = pending_value_type = None
                continue
            # v5.3: Clean service prefixes and game.GetService() from remaining entries
            cleaned = stripped
            cleaned = cleaned.replace('({}, ', '(').replace(', {})', ')')
            cleaned = cleaned.replace('{}', '').strip()
            # v6: game.GetService(ServiceName) -- explicit, deterministic form.
            # Handle this FIRST so we never fall through to the ambiguous
            # last-service-seen guess below for entries that already tell us
            # exactly which service they belong to.
            m_explicit_svc = re.match(r'^game\.GetService\((\w+)\)\.(.*)$', cleaned)
            if m_explicit_svc:
                cleaned = f'{m_explicit_svc.group(1)}.{m_explicit_svc.group(2)}'
            # v5.3: Remove game.GetService() prefix (legacy/ambiguous form)
            if cleaned.startswith('game.GetService().'):
                rest = cleaned[len('game.GetService().'):]
                for svc in service_names:
                    if rest.startswith(svc + '.'):
                        rest = rest[len(svc)+1:]
                        break
                cleaned = rest
            # Clean service-name prefixes from trace entries (legacy heuristic,
            # only still needed for any entry that didn't already carry an
            # explicit service name via the v6 fix above)
            for svc in service_names:
                if cleaned.startswith(svc) and len(cleaned) > len(svc) and cleaned[len(svc)] in ('.', ''):
                    if cleaned.startswith(svc + 'LocalPlayer'):
                        cleaned = 'LocalPlayer' + cleaned[len(svc)+10:]
                    elif cleaned.startswith(svc + 'Heartbeat'):
                        cleaned = 'RunService.Heartbeat' + cleaned[len(svc)+9:]
                    else:
                        cleaned = cleaned[len(svc):]
                        if cleaned.startswith('.'): cleaned = cleaned[1:]
                    break
            if cleaned and cleaned != '{}':
                if current_inst and '.Parent = ' in cleaned:
                    lines.append(cleaned)
                elif cleaned.endswith(')') and not cleaned.startswith('--'):
                    lines.append(cleaned)
                elif '=' in cleaned and not cleaned.startswith('local '):
                    lines.append(cleaned)
                elif not any(c in cleaned for c in ['{}', 'function', 'end']):
                    lines.append(cleaned)
        if current_inst and inst_properties.get(current_inst):
            for prop, val in inst_properties[current_inst]:
                if val == '{}' and pending_value: val = pending_value
                if val == 'nil' and prop in ('BackgroundColor3', 'TextColor3') and pending_value: val = pending_value
                lines.append(f'{current_inst}.{prop} = {val}')
        while connect_stack:
            lines.append('    -- [event handler body requires Roblox environment]')
            lines.append('end)')
            connect_stack.pop()
        has_print = any(l.strip().startswith('print(') for l in lines)
        if not has_print:
            for p in prints:
                try: float(p); lines.append(f'print({p})')
                except ValueError: lines.append(f'print("{p}")')
        return '\n'.join(lines)

    @staticmethod
    def _smart_rename(reconstructed: str) -> str:
        """v5.2: Rename instN to their .Name values.
        inst1.Name = "Main" -> all inst1 -> Main"""
        if not reconstructed:
            return reconstructed
        lines = reconstructed.split('\n')
        rename_map = {}
        for line in lines:
            m = re.match(r'(inst\d+)\.Name\s*=\s*["\']([^"\']+)["\']', line)
            if m and re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', m.group(2)):
                rename_map[m.group(1)] = m.group(2)
        if not rename_map:
            return reconstructed
        result = []
        for line in lines:
            new_line = line
            for old, new in rename_map.items():
                new_line = re.sub(r'\b' + re.escape(old) + r'\b', new, new_line)
            result.append(new_line)
        return '\n'.join(result)

    @staticmethod
    def _extract_code_structure(resolved_cff: str, string_map: Dict[int, str]) -> List[str]:
        if not resolved_cff:
            return []
        lines, seen = [], set()
        def add(line):
            if line and line not in seen:
                seen.add(line)
                lines.append(line)
        sa = WeAreDevDeobfuscator._simplify_arith_in_code
        for m in re.finditer(r'local function (\w+)', resolved_cff):
            add(f'local function {m.group(1)}(...)\n    -- [body requires Roblox environment]\nend')
        for m in re.finditer(r'(?<!local )function (\w+)', resolved_cff):
            add(f'function {m.group(1)}(...)\n    -- [body requires Roblox environment]\nend')
        for m in re.finditer(r'local (\w+)\s*=\s*require', resolved_cff):
            add(f'local {m.group(1)} = require(...)')
        # v5.3: Simplified for loops
        for m in re.finditer(r'for (\w+)\s*=\s*(.+?)\s*,\s*(.+?)\s+do', resolved_cff):
            var, start, limit = m.group(1), sa(m.group(2).strip()), sa(m.group(3).strip())
            add(f'for {var} = {start}, {limit} do')
        for m in re.finditer(r'for (\w+)(?:,\s*\w+)?\s+in\s+(pairs|ipairs)\((.+?)\)\s+do', resolved_cff):
            add(f'for {m.group(1)} in {m.group(2)}({m.group(3)}) do')
        # v5.3: Simplified while loops
        for m in re.finditer(r'while (.+?)\s+do', resolved_cff):
            cond = sa(m.group(1).strip())
            if len(cond) < 100 and 'true' not in cond:
                add(f'while {cond} do')
        # v5.3: Simplified if conditions (limit to avoid VM dispatch spam)
        if_count = 0
        for m in re.finditer(r'if (.+?)\s+then', resolved_cff):
            cond = sa(m.group(1).strip())
            if len(cond) < 100:
                # Skip trivial single-variable VM dispatch conditions
                if re.match(r'^[a-z]\s*[<>=!]+\s*\d+$', cond):
                    continue
                add(f'if {cond} then')
                if_count += 1
                if if_count >= 20:
                    break
        for m in re.finditer(r'return (.+)', resolved_cff):
            val = sa(m.group(1).strip())
            if len(val) < 100:
                add(f'return {val}')
        for m in re.finditer(r'(\w+)\.(\w+)\s*=\s*("[^"]{2,}")', resolved_cff):
            obj, prop, val = m.group(1), m.group(2), m.group(3)
            if not re.match(r'^[A-Za-z0-9]{8,}$', val.strip('"')):
                add(f'{obj}.{prop} = {val}')
        for m in re.finditer(r'local (\w+)\s*=\s*(\{)', resolved_cff):
            add(f'local {m.group(1)} = {{ ... }}')
        return lines

    @staticmethod
    def _mine_cff_code(resolved_cff: str, string_map: Dict[int, str] = None) -> List[str]:
        if not resolved_cff:
            return []
        code_lines, seen = [], set()
        def add(line):
            if line and line not in seen:
                seen.add(line)
                code_lines.append(line)
        if ':GetService("' in resolved_cff:
            for m in re.finditer(r':GetService\("([^"]+)"\)', resolved_cff):
                add(f'game:GetService("{m.group(1)}")')
        for m in re.finditer(r'Instance[.]new\("([^"]+)"\)', resolved_cff):
            add(f'Instance.new("{m.group(1)}")')
        for m in re.finditer(r':WaitForChild\("([^"]+)"\)', resolved_cff):
            add(f':WaitForChild("{m.group(1)}")')
        for m in re.finditer(r':FindFirstChild\("([^"]+)"\)', resolved_cff):
            add(f':FindFirstChild("{m.group(1)}")')
        for m in re.finditer(r':FindFirstChildOfClass\("([^"]+)"\)', resolved_cff):
            add(f':FindFirstChildOfClass("{m.group(1)}")')
        for m in re.finditer(r'(?:HttpGet|HttpPost)\("(https?://[^"]+)"\)', resolved_cff):
            add(f':HttpGet("{m.group(1)}")')
        for m in re.finditer(r':SetAttribute\("([^"]+)"', resolved_cff): add(f':SetAttribute("{m.group(1)}", ...)')
        for m in re.finditer(r':GetAttribute\("([^"]+)"\)', resolved_cff): add(f':GetAttribute("{m.group(1)}")')
        for m in re.finditer(r'\.(OnServerEvent|OnClientEvent)\("([^"]+)"\)', resolved_cff): add(f'.{m.group(1)}("{m.group(2)}")')
        for m in re.finditer(r'\.(InvokeServer|FireServer)\("([^"]+)"\)', resolved_cff): add(f':{m.group(1)}("{m.group(2)}")')
        for m in re.finditer(r'require\("([^"]+)"\)', resolved_cff): add(f'require("{m.group(1)}")')
        for m in re.finditer(r'Enum\.([A-Z]\w+\.[A-Z]\w+)', resolved_cff): add(f'Enum.{m.group(1)}')
        for m in re.finditer(r'\.(?:[Tt]ext|[Nn]ame)\s*=\s*"([^"]{2,})"', resolved_cff):
            prop, val = m.group(0).split('=', 1)
            val = val.strip()
            if not re.match(r'^"[A-Za-z0-9]{8,}"$', val):
                add(m.group(0))
        for m in re.finditer(r'string\.format\("([^"]{3,})"', resolved_cff):
            fmt = m.group(1)
            if '%' in fmt and len(fmt) < 200: add(f'string.format("{fmt}", ...)')
        for m in re.finditer(r'(?:error|warn|assert)\("([^"]{5,})"', resolved_cff):
            msg = m.group(1)
            if len(msg) > 10 and not re.match(r'^[A-Za-z0-9]{8,}$', msg): add(m.group(0))
        for m in re.finditer(r'print\("([^"]{5,})"\)', resolved_cff):
            msg = m.group(1)
            if len(msg) > 5 and not re.match(r'^[A-Za-z0-9]{8,}$', msg): add(f'print("{msg}")')
        if resolved_cff.count('TweenService') > 0 and ':Create(' in resolved_cff: add('TweenService:Create(...)')
        for m in re.finditer(r'"(\d{8,12})"', resolved_cff): add(f'-- Asset ID: {m.group(1)}')
        for pat in [':GetChildren()', ':GetDescendants()', ':IsA(', ':Clone()', ':Destroy()', ':Wait()', ':Play()', ':Stop()', ':LoadCharacter()', ':MoveTo(', ':WalkTo(', ':CreatePath()', ':ComputeAsync(', ':GetWaypoints()']:
            if pat in resolved_cff: add(pat)
        for m in re.finditer(r':IsA\("([^"]+)"\)', resolved_cff): add(f':IsA("{m.group(1)}")')
        for pat, label in [('.Parent =', '.Parent = ...'), ('.Visible =', None), ('.Enabled =', None),
                          ('.Value =', '.Value = ...'), ('.Position =', '.Position = ...'),
                          ('.Size =', '.Size = ...'), ('.BackgroundColor3 =', '.BackgroundColor3 = ...'),
                          ('.TextColor3 =', '.TextColor3 = ...'), ('.Font =', '.Font = ...'),
                          ('.TextSize =', '.TextSize = ...'), ('.Transparency =', '.Transparency = ...'),
                          ('.AnchorPoint =', '.AnchorPoint = ...'), ('.BackgroundTransparency =', '.BackgroundTransparency = ...'),
                          ('.BorderSizePixel =', '.BorderSizePixel = ...'), ('.ZIndex =', '.ZIndex = ...'),
                          ('.LayoutOrder =', '.LayoutOrder = ...')]:
            if pat in resolved_cff:
                if label: add(label)
                else:
                    for vm in re.finditer(re.escape(pat) + r'(true|false)', resolved_cff):
                        add(f'{pat}{vm.group(1)}')
        for pat in ['CFrame.new(', 'Vector3.new(', 'Vector2.new(', 'UDim2.new(', 'Color3.fromRGB(', 'TweenInfo.new(', 'math.random(', 'task.wait(', 'task.spawn(', 'task.delay(', 'coroutine.wrap(', 'pcall(', 'xpcall(']:
            if pat in resolved_cff: add(pat + '...)')
        if '.Changed:' in resolved_cff: add('.Changed:Connect(...)')
        for m in re.finditer(r'GetPropertyChangedSignal\("([^"]+)"\)', resolved_cff): add(f':GetPropertyChangedSignal("{m.group(1)}")')
        if 'CharacterAdded:' in resolved_cff: add('.CharacterAdded:Connect(function(character)')
        if 'InputBegan:' in resolved_cff: add('.InputBegan:Connect(function(input, gameProcessed)')
        if 'InputEnded:' in resolved_cff: add('.InputEnded:Connect(function(input, gameProcessed)')
        if 'Heartbeat:' in resolved_cff: add('.Heartbeat:Connect(function(dt)')
        if 'DataStoreService' in resolved_cff: add('game:GetService("DataStoreService")')
        for m in re.finditer(r':GetDataStore\("([^"]+)"\)', resolved_cff): add(f':GetDataStore("{m.group(1)}")')
        if ':GetAsync(' in resolved_cff: add(':GetAsync(...)')
        if ':SetAsync(' in resolved_cff: add(':SetAsync(...)')
        if ':GetOrderedDataStore(' in resolved_cff: add(':GetOrderedDataStore(...)')
        if 'PathfindingService' in resolved_cff: add('game:GetService("PathfindingService")')
        if 'UserInputService' in resolved_cff: add('game:GetService("UserInputService")')
        for m in re.finditer(r'rbxassetid://(\d+)', resolved_cff): add(f'-- rbxassetid://{m.group(1)}')
        for pat in ['UIGradient', 'UIPadding', 'UICorner', 'UIStroke', 'UISizeConstraint', 'UIListLayout', 'UIGridLayout', 'UIPageLayout', 'UITableLayout']:
            if pat in resolved_cff: add(pat)
        return code_lines

    @staticmethod
    def _extract_code_from_cff(resolved_cff: str) -> List[str]:
        return WeAreDevDeobfuscator._mine_cff_code(resolved_cff)

    # ============================================================
    # Phase 4.6: CFF Block Extraction (v5.4 NEW)
    # ============================================================

    @staticmethod
    def _find_block_end(code: str, start: int) -> int:
        """Find matching 'end' for a for/while/if/function block starting at start."""
        depth = 1
        i = start
        n = len(code)
        in_str = False
        while i < n:
            c = code[i]
            if in_str:
                if c == '\\' and i + 1 < n:
                    i += 2
                    continue
                if c == '"':
                    in_str = False
                i += 1
                continue
            if c == '"':
                in_str = True
                i += 1
                continue
            if c == '-' and i + 1 < n and code[i+1] == '-':
                while i < n and code[i] != '\n':
                    i += 1
                continue
            # Count block openers/closers
            if code[i:i+3] == 'end':
                after = code[i+3:i+4] if i+3 < n else ''
                if not after.isalnum() and after != '_':
                    depth -= 1
                    if depth == 0:
                        return i
            # Check for nested blocks (for, while, if, function, do, repeat)
            for kw in ('function', 'for', 'while', 'repeat'):
                if code[i:i+len(kw)] == kw:
                    before_ok = (i == 0 or not code[i-1].isalnum()) and (code[i-1:i] != '.')
                    after_ok = (i+len(kw) >= n or not code[i+len(kw)].isalnum()) and (code[i+len(kw):i+len(kw)+1] != ':')
                    if before_ok and after_ok:
                        if kw == 'function':
                            depth += 1
                        elif kw in ('for', 'while'):
                            # Check for 'do' keyword
                            do_pos = code.find('do', i)
                            if do_pos > 0 and do_pos - i < 80:
                                depth += 1
                        elif kw == 'repeat':
                            depth += 1
                        break
            if code[i:i+2] == 'do':
                before_ok = (i == 0 or not code[i-1].isalnum()) and (code[i-1:i] != '.')
                after_ok = (i+2 >= n or not code[i+2].isalnum())
                if before_ok and after_ok:
                    depth += 1
            i += 1
        return -1

    @staticmethod
    def _is_vm_internal(block: str) -> bool:
        """Filter out VM-internal blocks (swap, b64 decode, dispatch)."""
        # Skip swap loops (P-table reordering)
        if 'ipairs({{' in block and '}},{' in block:
            return True
        if re.search(r'ipairs\(\{\{\d+', block):
            return True
        # Skip 4-element swap assignments
        if re.search(r'\w+\[\w+\]\s*,\s*\w+\[\w+\]\s*,\s*\w+\[\w+\]\s*,\s*\w+\[\w+\]', block):
            return True
        # Skip b64 decode loop
        if 'string.char' in block and ('64)^(' in block or 'string.sub' in block):
            return True
        # Skip giant if/elseif dispatch chains
        if block.count('elseif') > 3:
            return True
        # Skip blocks that are pure VM register operations (single-letter vars, obfuscated arithmetic)
        # VM register blocks typically have patterns like: g=y L=1 q=L j=q<L
        vm_reg_count = 0
        for line in block.split('\n'):
            line = line.strip()
            # Lines like: single_letter=single_letter or single_letter=single_letter+number
            if re.match(r'^[a-z]\s*[=<>!]', line) or re.match(r'^[a-z]\s*$', line):
                vm_reg_count += 1
        non_vm_lines = sum(1 for line in block.split('\n')
                            if line.strip() and not re.match(r'^[a-z]\s*[=<>!]', line.strip())
                            and line.strip() not in ('end', 'do', 'then'))
        if vm_reg_count > 4 and non_vm_lines < 3:
            return True
        # Skip blocks with binary garbage strings
        binary_strings = 0
        for m in re.finditer(r'"([^"]{2,})"', block):
            s = m.group(1)
            if any(ord(c) > 127 for c in s):
                binary_strings += 1
        if binary_strings >= 2:
            return True
        # Skip blocks where most content is obfuscated arithmetic (large numbers +/- large numbers)
        arith_count = len(re.findall(r'\d{5,}[+-]\d{5,}', block))
        total_content = len(block.replace(' ', '').replace('\n', ''))
        if total_content > 0 and arith_count > 5 and arith_count / (total_content / 50) > 0.3:
            return True
        return False

    @staticmethod
    def _extract_cff_blocks(decoded_cff: str, string_map: Dict[int, str] = None) -> List[str]:
        """v5.4: Extract COMPLETE code blocks from CFF-resolved code.
        Returns full loop bodies, function bodies, and if/then blocks."""
        if not decoded_cff:
            return []
        blocks = []
        seen = set()
        sa = WeAreDevDeobfuscator._simplify_arith_in_code

        def add(block):
            block = block.strip()
            if not block or len(block) < 15:
                return
            key = block[:100]
            if key in seen:
                return
            seen.add(key)
            if not WeAreDevDeobfuscator._is_vm_internal(block):
                blocks.append(block)

        # Numeric for loops
        for m in re.finditer(r'for\s+(\w+)\s*=\s*(.+?)\s*,\s*(.+?)(?:\s*,\s*(.+?))?\s+do', decoded_cff):
            var, s_e, l_e, st_e = m.group(1), m.group(2).strip(), m.group(3).strip(), m.group(4)
            header = 'for %s = %s, %s' % (var, sa(s_e), sa(l_e))
            if st_e:
                header += ', %s' % sa(st_e.strip())
            header += ' do'
            end_p = WeAreDevDeobfuscator._find_block_end(decoded_cff, m.end())
            if end_p > 0:
                body = decoded_cff[m.end():end_p+3].strip()
                if len(body) < 600:
                    add(header + '\n    ' + '\n    '.join(body.split('\n')) + '\nend')

        # for...in loops
        for m in re.finditer(r'for\s+(\w+)(?:,\s*\w+)?\s+in\s+(pairs|ipairs|next)\((.+?)\)\s+do', decoded_cff):
            var, it_fn, it_arg = m.group(1), m.group(2), sa(m.group(3).strip())
            header = 'for %s in %s(%s) do' % (var, it_fn, it_arg)
            end_p = WeAreDevDeobfuscator._find_block_end(decoded_cff, m.end())
            if end_p > 0:
                body = decoded_cff[m.end():end_p+3].strip()
                if len(body) < 600:
                    add(header + '\n    ' + '\n    '.join(body.split('\n')) + '\nend')

        # while loops
        w_count = 0
        for m in re.finditer(r'while\s+(.+?)\s+do', decoded_cff):
            cond = sa(m.group(1).strip())
            if len(cond) > 120:
                continue
            header = 'while %s do' % cond
            end_p = WeAreDevDeobfuscator._find_block_end(decoded_cff, m.end())
            if end_p > 0:
                body = decoded_cff[m.end():end_p+3].strip()
                if 10 < len(body) < 600:
                    add(header + '\n    ' + '\n    '.join(body.split('\n')) + '\nend')
                    w_count += 1
                    if w_count >= 20:
                        break

        # Function definitions
        for m in re.finditer(r'(local\s+)?function\s+(\w+)\s*\(([^)]*)\)', decoded_cff):
            prefix = m.group(1) or ''
            fname = m.group(2)
            params = m.group(3)
            if len(fname) == 1 and fname.islower():
                continue
            header = '%sfunction %s(%s)' % (prefix, fname, params)
            end_p = WeAreDevDeobfuscator._find_block_end(decoded_cff, m.end())
            if end_p > 0:
                body = decoded_cff[m.end():end_p+3].strip()
                if 20 < len(body) < 2000:
                    body_s = sa(body)
                    add(header + '\n    ' + '\n    '.join(body_s.split('\n')) + '\nend')

        # if/then blocks (limit to avoid dispatch spam)
        if_n = 0
        for m in re.finditer(r'if\s+(.+?)\s+then', decoded_cff):
            cond = sa(m.group(1).strip())
            if len(cond) > 100:
                continue
            if re.match(r'^[a-z]\s*[<>=!]+\s*\d+$', cond):
                continue
            header = 'if %s then' % cond
            end_p = WeAreDevDeobfuscator._find_block_end(decoded_cff, m.end())
            if end_p > 0:
                body = decoded_cff[m.end():end_p+3].strip()
                if 10 < len(body) < 400:
                    add(header + '\n    ' + '\n    '.join(body.split('\n')) + '\nend')
                    if_n += 1
                    if if_n >= 30:
                        break

        # repeat...until
        for m in re.finditer(r'repeat\b', decoded_cff):
            until_p = decoded_cff.find('until', m.end())
            if 0 < until_p - m.end() < 500:
                body = decoded_cff[m.end():until_p].strip()
                until_cond = decoded_cff[until_p+5:until_p+100].split('\n')[0].strip()
                if until_cond:
                    until_cond = sa(until_cond)
                    add('repeat\n    %s\nuntil %s' % (body, until_cond))

        return blocks

    # ============================================================
    # Phase 6: Output generation (v5.4 UPGRADED)
    # ============================================================

    @staticmethod
    def _generate_clean_output(reconstructed: str, trace: List[str], prints: List[str],
                               errors: List[str], P_decoded: Dict[int, str],
                               string_map: Dict[int, str], verbose: bool,
                               m_offset: int = 5713, accessor_name: str = 'M',
                               cff_code: List[str] = None, structure_code: List[str] = None,
                               body_code: List[str] = None,
                               opcode_strings: List[str] = None,
                               cff_blocks: List[str] = None) -> str:
        lines = []
        meaningful = {}
        for idx in sorted(P_decoded.keys()):
            s = P_decoded[idx]
            if not s or not s.strip(): continue
            if re.match(r'^[A-Za-z0-9]{8,20}$', s): continue
            meaningful[idx] = s
        has_recon = reconstructed and len(reconstructed.strip()) > 0
        has_any = (has_recon or (cff_code and len(cff_code) > 0) or
                   (body_code and len(body_code) > 0) or
                   (opcode_strings and len(opcode_strings) > 0) or
                   (cff_blocks and len(cff_blocks) > 0))
        if has_any:
            lines.append('-- [[ Deobfuscated by Lua Deobfuscator Bot v5.5 ]]')
            lines.append('-- Method: P-table + VM trace + CFF blocks + enhanced tracer + opcode analysis + disassembler')
            lines.append(f'-- P-table: {len(P_decoded)} entries, {len(meaningful)} meaningful strings')
            lines.append('')
        if has_recon:
            lines.append('-- === RECONSTRUCTED SOURCE ===')
            lines.append(reconstructed)
            lines.append('')
        # v5.3: Opcode analysis section (most informative for complex scripts)
        if opcode_strings:
            existing = set()
            if has_recon:
                for rl in reconstructed.split('\n'):
                    existing.add(rl.strip())
                    existing.add(rl.strip().lstrip('-- '))
            unique = [os for os in opcode_strings if os.strip() not in existing]
            if unique:
                lines.append('-- === VM OPCODE ANALYSIS ===')
                lines.append('-- [Strings and API methods used by the VM internally]')
                for os in unique: lines.append(os)
                lines.append('')
        if body_code:
            existing = set()
            if has_recon:
                for rl in reconstructed.split('\n'): existing.add(rl.strip())
            if opcode_strings:
                for os in opcode_strings: existing.add(os.strip().lstrip('-- '))
            unique = [bl for bl in body_code if bl.strip() not in existing]
            if unique:
                lines.append('-- === ADDITIONAL PATTERNS (deep body mining) ===')
                for bl in unique: lines.append(bl)
                lines.append('')
        if cff_code:
            existing = set()
            if has_recon:
                for rl in reconstructed.split('\n'): existing.add(rl.strip().lstrip('local ').lstrip('-- '))
            if opcode_strings:
                for os in opcode_strings: existing.add(os.strip().lstrip('-- '))
            unique = []
            for cl in cff_code:
                cs = cl.strip().lstrip('local ').lstrip('-- ')
                if cs not in existing and cs not in (':sub(...)', ':find(...)', ':match(...)', ':gsub(...)'):
                    if cl.startswith('-- Asset ID:'): unique.append(cl); continue
                    unique.append(cl)
            if unique:
                lines.append('-- === API CALLS & PATTERNS ===')
                for cl in unique: lines.append(cl)
                lines.append('')
        if structure_code:
            existing = set()
            if has_recon:
                for rl in reconstructed.split('\n'): existing.add(rl.strip())
            if cff_code:
                for cl in cff_code: existing.add(cl.strip())
            if body_code:
                for bl in body_code: existing.add(bl.strip())
            if opcode_strings:
                for os in opcode_strings: existing.add(os.strip().lstrip('-- '))
            unique = [sl for sl in structure_code if sl.strip() not in existing]
            if unique:
                lines.append('-- === CODE STRUCTURE ===')
                for sl in unique: lines.append(sl)
                lines.append('')
        # v5.4: Complete code blocks (loop bodies, function bodies, if/then blocks)
        if cff_blocks:
            existing = set()
            if has_recon:
                for rl in reconstructed.split('\n'):
                    existing.add(rl.strip())
                    existing.add(rl.strip().lstrip('-- '))
            if cff_code:
                for cl in cff_code: existing.add(cl.strip())
            if structure_code:
                for sl in structure_code: existing.add(sl.strip())
            if body_code:
                for bl in body_code: existing.add(bl.strip())
            if opcode_strings:
                for os in opcode_strings: existing.add(os.strip().lstrip('-- '))
            unique = []
            for blk in cff_blocks:
                blk_s = blk.strip()
                first_line = blk_s.split('\n')[0].strip()
                if first_line not in existing and blk_s not in existing:
                    unique.append(blk)
            if unique:
                lines.append('-- === EXTRACTED CODE BLOCKS (loops, functions, conditions) ===')
                for blk in unique: lines.append(blk)
                lines.append('')
        if not has_recon and not cff_code and not body_code and not opcode_strings and not cff_blocks:
            if prints:
                lines.append('-- === PRINT OUTPUT ===')
                for p in prints:
                    try: float(p); lines.append(f'print({p})')
                    except ValueError: lines.append(f'print("{p}")')
                lines.append('')
            else:
                lines.append('-- Source reconstruction incomplete.')
                lines.append('-- The script uses a stack-based VM; full decompilation requires VM simulation.')
                lines.append('')
        if meaningful:
            lines.append('-- === DECODED STRING CONSTANTS ===')
            for idx, s in sorted(meaningful.items()):
                lines.append(f'--   [{idx}] = {repr(s)}')
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

    # v5: added LuaObfuscatorFeribDeobfuscator, reordered for priority
    DEOBFUSCATORS = [
        AstroProtectDeobfuscator,
        IronBrewDeobfuscator,
        WANDeobfuscator,
        MoonSecDeobfuscator,
        ClydeDeobfuscator,
        LuaObfuscatorFeribDeobfuscator,  # v5: new
        WeAreDevDeobfuscator,
        Base64CompressDeobfuscator,
        GenericVMDeobfuscator,
    ]

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.engine = LuaEngine.get()

    def deobfuscate_file(self, filepath: str) -> Tuple[str, str, dict]:
        """Deobfuscate file -> (obfuscator_name, source, metadata)"""
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()
        return self.deobfuscate(code, filepath)

    def deobfuscate(self, code: str, name: str = "input") -> Tuple[str, str, dict]:
        """
        Deobfuscate Lua code.
        Returns (obfuscator_name, recovered_source, metadata)
        """
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

            # v5: better matching logic for detected obfuscator
            if detected:
                # Allow GenericVM and Base64Compress to always run
                if cls_name not in ("GenericVM", "Base64Compress"):
                    # Check if the class name is related to the detected type
                    detected_lower = detected.lower()
                    cls_lower = cls_name.lower()

                    # Direct name match
                    if cls_lower in detected_lower or detected_lower in cls_lower:
                        pass  # This is the right deobfuscator, proceed
                    # Special case mappings
                    elif detected_lower == "luaobfuscator.com (ferib)" and cls_lower == "luaobfuscatorferib":
                        pass  # Match
                    elif detected_lower.startswith("ironbrew") and cls_lower.startswith("ironbrew"):
                        pass  # Match
                    elif detected_lower.startswith("wan") and cls_lower == "wan":
                        pass  # Match
                    else:
                        continue  # Skip this deobfuscator

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

    def detect_only(self, filepath: str) -> str:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()
        return ObfuscatorDetector.detect(code) or "Unknown/Clear text"

import threading as _threading
import discord
from discord.ext import commands
from flask import Flask
import aiohttp

TOKEN = os.environ.get("DISCORD_TOKEN")
COMMAND_PREFIX = "."
DISCORD_MSG_LIMIT = 1900
MAX_FETCH_BYTES = 5 * 1024 * 1024

keep_alive_app = Flask(__name__)

@keep_alive_app.route("/")
def _health():
    return "Bot is running."

def _run_keep_alive():
    port = int(os.environ.get("PORT", 10000))
    keep_alive_app.run(host="0.0.0.0", port=port)

def start_keep_alive():
    _threading.Thread(target=_run_keep_alive, daemon=True).start()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

deobfuscator = LuaDeobfuscator(verbose=False)


def strip_lua_comments(source: str) -> str:
    source = re.sub(r"--\[(=*)\[.*?\]\1\]", "", source, flags=re.DOTALL)
    cleaned_lines = []
    for line in source.split("\n"):
        idx = line.find("--")
        if idx != -1:
            before = line[:idx]
            if before.count('"') % 2 == 0 and before.count("'") % 2 == 0:
                line = before.rstrip()
        cleaned_lines.append(line)
    result = "\n".join(cleaned_lines)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


async def _fetch_source(ctx: commands.Context, link: Optional[str]):
    if ctx.message.attachments:
        attachment = ctx.message.attachments[0]
        if not attachment.filename.lower().endswith((".lua", ".txt")):
            raise ValueError("File must be `.lua` or `.txt`.")
        raw = await attachment.read()
        return attachment.filename, raw.decode("utf-8", errors="replace")

    if link:
        if not (link.startswith("http://") or link.startswith("https://")):
            raise ValueError("That doesn't look like a valid link.")
        async with aiohttp.ClientSession() as session:
            async with session.get(link, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    raise ValueError(f"Link returned HTTP {resp.status}.")
                data = await resp.content.read(MAX_FETCH_BYTES + 1)
                if len(data) > MAX_FETCH_BYTES:
                    raise ValueError("File from link is too large (over 5 MB).")
        filename = link.rsplit("/", 1)[-1] or "link.lua"
        return filename, data.decode("utf-8", errors="replace")

    raise ValueError("Attach a `.lua`/`.txt` file, or give a link: `.l <link>")


@bot.event
async def on_ready():
    print(f"[+] Logged in as {bot.user} (id={bot.user.id})")


@bot.command(name="l")
async def l_cmd(ctx: commands.Context, link: Optional[str] = None):
    try:
        filename, code = await _fetch_source(ctx, link)
    except ValueError as e:
        await ctx.reply(str(e))
        return

    status_msg = await ctx.reply(f"Deobfuscating `{filename}`...")

    try:
        obf_name, source, meta = deobfuscator.deobfuscate(code, filename)
        cleaned = strip_lua_comments(source)

        header = f"Obfuscator detected: **{obf_name}**\n"

        if not cleaned.strip():
            reason = source.strip() or "No source could be recovered."
            await status_msg.edit(
                content=(
                    f"{header}Nothing left after stripping comments -- "
                    f"the deobfuscator itself didn't recover real source, "
                    f"it only returned notes:\n```\n{reason}\n```"
                    f"{' (lupa not installed -- install it for VM execution)' if not deobfuscator.engine.available else ''}"
                )
            )
            return

        if len(cleaned) <= DISCORD_MSG_LIMIT:
            await status_msg.edit(content=f"{header}```lua\n{cleaned}\n```")
        else:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".lua", delete=False, encoding="utf-8"
            ) as f:
                f.write(cleaned)
                tmp_path = f.name

            await status_msg.edit(content=header)
            try:
                await ctx.send(file=discord.File(tmp_path, filename="deobfuscated.lua"))
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    except Exception as e:
        await status_msg.edit(content=f"Error: `{e}`")


@bot.command(name="d")
async def d_cmd(ctx: commands.Context, link: Optional[str] = None):
    """Disassemble WeAreDev VM bytecode."""
    try:
        filename, code = await _fetch_source(ctx, link)
    except ValueError as e:
        await ctx.reply(str(e))
        return

    detected = ObfuscatorDetector.detect(code)
    if detected != "WeAreDev":
        await ctx.reply(f"Disassembly is only available for WeAreDev obfuscated scripts. Detected: **{detected or 'Unknown'}**")
        return

    status_msg = await ctx.reply(f"Disassembling `{filename}`...")
    try:
        disasm = WeAreDevDisassembler.disassemble(code, verbose=True)
        cleaned = strip_lua_comments(disasm)
        if len(cleaned) <= DISCORD_MSG_LIMIT:
            await status_msg.edit(content=f"**WeAreDev VM Disassembly** (`{filename}`)\n```\n{cleaned}\n```")
        else:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".lua", delete=False, encoding="utf-8"
            ) as f:
                f.write(cleaned)
                tmp_path = f.name
            await status_msg.edit(content=f"**WeAreDev VM Disassembly** (`{filename}`)")
            try:
                await ctx.send(file=discord.File(tmp_path, filename="disassembly.lua"))
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    except Exception as e:
        await status_msg.edit(content=f"Disassembly error: `{e}`")


bot.remove_command('help')

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    embed = discord.Embed(
        title="Lua Deobfuscator Bot v5.5",
        description="Commands:",
        color=0x5865F2,
    )
    embed.add_field(
        name=".l (attach a file)",
        value="Attach a `.lua` or `.txt` file to the message and run `.l` to deobfuscate it.",
        inline=False,
    )
    embed.add_field(
        name=".l <link>",
        value="`.l https://example.com/script.lua` -- fetches and deobfuscates a script from a direct link.",
        inline=False,
    )
    embed.add_field(
        name=".d (disassemble)",
        value="Attach a `.lua` or `.txt` file and run `.d` to get a bytecode disassembly of the WeAreDev VM. Shows opcodes, decoded strings, and VM structure.",
        inline=False,
    )
    embed.add_field(
        name="Supported obfuscators",
        value="WeAreDev (headerless detect + bytecode disassembler), IronBrew2, WAN OBFUSCATE, MoonSec V3, Clyde, AstroProtect, LuaObfuscator.com (Ferib), PSU, Luraph, Base64+Compress, Generic VM-based.",
        inline=False,
    )
    embed.set_footer(text="Comments are stripped from the recovered source automatically.")
    await ctx.reply(embed=embed)


if __name__ == "__main__":
    # v9 fix: always bind the keep-alive port first, regardless of whether
    # TOKEN is present. Previously start_keep_alive() only ran inside the
    # `else` branch, so a missing/misread TOKEN env var caused the process
    # to print a warning and exit immediately -- no port ever got bound,
    # and Render's port scanner times out with "No open ports detected"
    # (which looks like a network/deploy issue, but the real cause is the
    # missing token being swallowed silently).
    start_keep_alive()
    if not TOKEN:
        print("[!] DISCORD_TOKEN (or DISCORD_BOT_TOKEN) env var is not set or empty. "
              "Bot will not connect to Discord, but the keep-alive port is up so "
              "Render won't kill the service -- fix the env var and redeploy.")
    else:
        bot.run(TOKEN)
