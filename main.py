

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
    - Phase 1: Static base64 P-table decode (custom alphabet)
    - Phase 2: Run VM with smart callable chain tracing proxies
    - Phase 3: CFF string resolution (accessor calls -> string literals)
    - Phase 4: Reconstruct Lua source from execution trace
    - Phase 5: Generate comprehensive output

    v4 Improvement: Static b64 decode, P-table rotation, full CFF resolution,
    smart callable stubs that prevent cascade crashes.
    """

    M_OFFSET = 472584 - 466871  # 5713 — fallback default

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if not engine.available:
            return None

        import subprocess
        obf = re.sub(r'^--\[\[.*?\]\]\s*', '', code)

        # Extract M() offset dynamically from the obfuscated code
        m_offset, accessor_name = WeAreDevDeobfuscator._extract_m_offset(obf)
        if verbose:
            print(f"  [*] Extracted {accessor_name}() offset: {m_offset}")

        # Phase 1: Decode P-table (v4: static Python decode)
        if verbose:
            print("  [*] Phase 1: Decoding P-table string constants (v4 static)...")
        static_result = WeAreDevDeobfuscator._static_decode_p_table(obf, verbose)
        if static_result:
            P_decoded, accessor_name, m_offset = static_result
        else:
            if verbose:
                print("  [*] Static decode failed, falling back to v3 injection...")
            P_decoded = WeAreDevDeobfuscator._decode_p_table(obf, engine)
        if not P_decoded:
            if verbose:
                print("  [!] Failed to decode P-table")
            return None

        string_map = WeAreDevDeobfuscator._build_string_map(obf, P_decoded, m_offset, accessor_name)
        real_strings = {k: v for k, v in string_map.items()
                        if v and not re.match(r'^[A-Za-z0-9]{8,20}$', v)}

        if verbose:
            print(f"  [*] P-table: {len(P_decoded)} entries, {len(real_strings)} meaningful strings ({accessor_name}() offset={m_offset})")

        # Phase 2: Execute VM with tracing
        if verbose:
            print("  [*] Phase 2: Executing VM with full tracing (15s timeout)...")

        prints, trace, errors = WeAreDevDeobfuscator._execute_vm_traced(obf)

        if verbose:
            print(f"  [*] Captured: {len(prints)} prints, {len(trace)} trace entries, {len(errors)} errors")

        # Phase 3: Resolve strings in CFF (v4)
        if verbose:
            print("  [*] Phase 3: Resolving string constants in CFF...")
        resolved_cff = WeAreDevDeobfuscator._resolve_cff_strings(obf, string_map, accessor_name)
        acc_escaped = re.escape(accessor_name)
        orig_count = len(re.findall(acc_escaped + r'\(', obf))
        new_count = len(re.findall(acc_escaped + r'\(', resolved_cff))
        if verbose:
            print(f'  [*] Resolved {orig_count - new_count} accessor calls to string literals')

        # Phase 4: Reconstruct source from trace
        reconstructed = WeAreDevDeobfuscator._reconstruct_source(trace, prints)

        # Phase 4: Generate output
        source = WeAreDevDeobfuscator._generate_output(
            obf, P_decoded, string_map, prints, trace, errors, reconstructed, verbose, m_offset, accessor_name, resolved_cff)

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
    def _extract_b64_table(obf: str):
        """Extract the custom base64 alphabet table B from WeAreDev code.

        Returns dict mapping char -> 6-bit index.
        """
        m = re.search(r'local\s+B=\{(.*?)\}', obf, re.DOTALL)
        if not m:
            return None
        body = m.group(1)
        entries = re.split(r'[;,]', body)
        b64_map = {}
        for entry in entries:
            entry = entry.strip()
            if not entry or '=' not in entry:
                continue
            key_part, val_part = entry.split('=', 1)
            key_part = key_part.strip()
            val_part = val_part.strip()
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
        return b64_map if b64_map else None

    @staticmethod
    def _b64_decode(encoded: str, b64_map: dict) -> str:
        """Decode a string using the custom WeAreDev base64 alphabet."""
        if not encoded:
            return ''
        out = []
        j = 0
        H = 0
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
        """Extract P-table swap operations from the ipairs loop."""
        m = re.search(r'for\s+\w+,\w+\s+in\s+ipairs\(\{(.*?)\}\)', obf, re.DOTALL)
        if not m:
            return None
        body = m.group(1)
        swaps = []
        for pair in re.finditer(r'\{([^}]+)\}', body):
            nums = pair.group(1).split(',')
            if len(nums) >= 2:
                a = eval_arith(nums[0].strip())
                b = eval_arith(nums[1].strip())
                if a is not None and b is not None:
                    swaps.append((a, b))
        return swaps if swaps else None

    @staticmethod
    def _apply_swaps(p_table: dict, swaps: list):
        """Apply swap operations to P-table (individual element swaps, matching WeAreDev VM behavior)."""
        for a, b in swaps:
            if a in p_table and b in p_table:
                p_table[a], p_table[b] = p_table[b], p_table[a]

    @staticmethod
    def _static_decode_p_table(obf: str, verbose: bool = False):
        """Phase 1 v4: Fully decode P-table using static analysis."""
        p_match = re.search(r'local\s+(\w+)=\{', obf)
        if not p_match:
            return None
        p_start = p_match.end()
        depth = 1
        pos = p_start
        while pos < len(obf) and depth > 0:
            if obf[pos] == '{':
                depth += 1
            elif obf[pos] == '}':
                depth -= 1
            pos += 1
        p_end = pos - 1
        p_raw_text = obf[p_start:p_end]

        acc_match = re.search(r'local\s+function\s+(\w+)\(', obf[p_end:p_end+200])
        accessor_name = acc_match.group(1) if acc_match else 'M'

        p_entries = []
        scan = 0
        while scan < len(p_raw_text):
            q1 = p_raw_text.find(chr(34), scan)
            if q1 == -1:
                break
            q2 = p_raw_text.find(chr(34), q1 + 1)
            if q2 == -1:
                break
            raw = p_raw_text[q1 + 1:q2]
            decoded = decode_decimal_escapes(raw)
            p_entries.append(decoded)
            scan = q2 + 1

        if not p_entries:
            return None
        if verbose:
            print(f'  [*] P-table: {len(p_entries)} raw entries')

        b64_map = WeAreDevDeobfuscator._extract_b64_table(obf)
        if not b64_map:
            if verbose:
                print('  [!] Could not extract base64 table')
            return None
        if verbose:
            print(f'  [*] Base64 alphabet: {len(b64_map)} chars')

        p_decoded = {}
        for i, entry in enumerate(p_entries, 1):
            if entry and len(entry) > 0:
                decoded_str = WeAreDevDeobfuscator._b64_decode(entry, b64_map)
                p_decoded[i] = decoded_str
            else:
                p_decoded[i] = ''

        if verbose:
            meaningful = sum(1 for v in p_decoded.values()
                            if v and not re.match(r'^[A-Za-z0-9]{8,20}$', v))
            print(f'  [*] After b64 decode: {len(p_decoded)} entries, {meaningful} meaningful strings')

        swaps = WeAreDevDeobfuscator._extract_swap_loop(obf)
        if swaps:
            WeAreDevDeobfuscator._apply_swaps(p_decoded, swaps)
            if verbose:
                print(f'  [*] Applied {len(swaps)} swap operations')

        m_offset, _ = WeAreDevDeobfuscator._extract_m_offset(obf)
        if verbose:
            print(f'  [*] {accessor_name}() offset: {m_offset}')

        return p_decoded, accessor_name, m_offset

    @staticmethod
    def _resolve_cff_strings(obf: str, string_map: dict, accessor_name: str) -> str:
        """Replace all accessor(value) calls with their decoded string values."""
        if not string_map:
            return obf
        acc_escaped = re.escape(accessor_name)
        acc_pattern = acc_escaped + r'\(([^)]+)\)'

        def replace_accessor(m):
            expr = m.group(1)
            val = eval_arith(expr)
            if val is not None and val in string_map:
                s = string_map[val]
                if s:
                    BS = chr(92)
                    escaped = s.replace(BS, BS + BS).replace(chr(34), BS + chr(34))
                    return chr(34) + escaped + chr(34)
            return m.group(0)

        return re.sub(acc_pattern, replace_accessor, obf)

    @staticmethod
    def _decode_p_table(obf: str, engine: LuaEngine) -> Optional[Dict[int, str]]:
        """Decode P-table by injecting print before CFF return."""
        inject_match = re.search(r'return\(function\([a-zA-Z,]+\)', obf)
        if not inject_match:
            return None
        inject_pos = inject_match.start()
        inject_end = inject_match.end()
        # Get the first parameter name (P-table variable)
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

        # Prepend a load hook to prevent nil-crash during P-table decode
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
    def _extract_m_offset(obf: str) -> Tuple[int, str]:
        """Extract the M() accessor offset and function name dynamically.

        WeAreDev code has a pattern like:
          local function N(N) return y[N-(-35298+69912)] end
        or:
          local function C(C) return r[C+(830462+-799744)] end
        Returns (offset, accessor_function_name).
        """
        m = re.search(r'local function (\w+)\(\w+\)return \w+\[\w+([+-])\(?([^)]+?)\)?\]end', obf)
        if not m:
            m = re.search(r'local function (\w+)\(\w+\)return \w+\[\w+([+-])(\d+)\]end', obf)
        if m:
            func_name = m.group(1)
            sign = m.group(2)
            expr = m.group(3)
            val = eval_arith(expr)
            if val is not None:
                offset = val if sign == '-' else -val
                return offset, func_name
        return WeAreDevDeobfuscator.M_OFFSET, 'M'

    @staticmethod
    def _build_string_map(obf: str, P_decoded: Dict[int, str], m_offset: int, accessor_name: str = 'M') -> Dict[int, str]:
        """Build accessor(value) -> decoded string mapping."""
        string_map = {}
        m_pattern = accessor_name + r'\((-?\d+[+-]?-?\d+)\)'
        for m in re.finditer(m_pattern, obf):
            val = eval_arith(m.group(1))
            if val is not None:
                idx = val - m_offset
                if idx in P_decoded:
                    string_map[val] = P_decoded[idx]
        return string_map

    # Embedded tracer Lua environment (self-contained - no external files needed)
    _TRACER_LUA = 'local _trace = {}\nlocal _trace_n = 0\nlocal _orig_print = print\n\nlocal function safe_tostring(v)\n    if type(v) == "string" then\n        return string.format("%q", v)\n    end\n    if type(v) == "nil" then return "nil" end\n    if type(v) == "boolean" then return tostring(v) end\n    if type(v) == "function" then return "function" end\n    if type(v) == "table" then return "{}" end\n    return tostring(v)\nend\n\nlocal function T(entry)\n    _trace_n = _trace_n + 1\n    _trace[_trace_n] = entry\n    _orig_print("[T]" .. entry)\nend\n\nlocal function traced_print(...)\n    local args = {...}\n    local strs = {}\n    for i, v in ipairs(args) do\n        strs[i] = tostring(v)\n    end\n    local line = table.concat(strs, "\\t")\n    _orig_print("[P]" .. line)\n    local arg_strs = {}\n    for i, v in ipairs(args) do\n        arg_strs[i] = safe_tostring(v)\n    end\n    T("print(" .. table.concat(arg_strs, ", ") .. ")")\nend\n\nlocal function make_chain_tracer(name)\n    local proxy = {}\n    local full_path = name\n    local mt = {\n        __index = function(t, k)\n            local kstr = type(k) == "string" and k or tostring(k)\n            T(full_path .. "." .. kstr)\n            local new_path = full_path .. "." .. kstr\n            return make_chain_tracer(new_path)\n        end,\n        __newindex = function(t, k, v)\n            local kstr = type(k) == "string" and k or tostring(k)\n            local vstr = safe_tostring(v)\n            T(full_path .. "." .. kstr .. " = " .. vstr)\n        end,\n        __call = function(t, ...)\n            local args = {}\n            for i, a in ipairs({...}) do\n                args[i] = safe_tostring(a)\n            end\n            T(full_path .. "(" .. table.concat(args, ", ") .. ")")\n            return make_chain_tracer(full_path .. "()")\n        end,\n        __tostring = function(t) return full_path end,\n        __concat = function(a, b) return "" end,\n        __len = function(t) return 0 end,\n        __add = function(a, b) return 0 end,\n        __sub = function(a, b) return 0 end,\n        __mul = function(a, b) return 0 end,\n        __div = function(a, b) return 0 end,\n        __mod = function(a, b) return 0 end,\n        __pow = function(a, b) return 0 end,\n        __eq = function(a, b) return false end,\n        __lt = function(a, b) return false end,\n        __le = function(a, b) return false end,\n    }\n    setmetatable(proxy, mt)\n    return proxy\nend\nlocal make_tracer = make_chain_tracer\n\n_G.print = traced_print\n_G.warn = traced_print\n_G.info = traced_print\n\nif not _G.getfenv then _G.getfenv = function(l) return _G end end\nif not _G.getgenv then _G.getgenv = function() return _G end end\nif not _G.setfenv then _G.setfenv = function() end end\nif not _G.unpack then _G.unpack = table.unpack end\n\nlocal _orig_pcall = pcall\n_G.pcall = function(f, ...)\n    local results = {_orig_pcall(f, ...)}\n    local ok = results[1]\n    if not ok then\n        local err = tostring(results[2])\n        if not err:find("pow", 1, true) then\n            T("-- pcall error: " .. err)\n        end\n    end\n    return table.unpack(results)\nend\n\nlocal _orig_xpcall = xpcall\n_G.xpcall = function(f, handler, ...)\n    local results = {_orig_xpcall(f, handler, ...)}\n    local ok = results[1]\n    if not ok then\n        T("-- xpcall error: " .. tostring(results[2]))\n    end\n    return table.unpack(results)\nend\n\nlocal _orig_load = loadstring or load\nif _orig_load then\n    local _real_load = _orig_load\n    _G.load = function(src, ...)\n        if src == nil then return nil, "cannot load nil" end\n        if type(src) ~= "string" and type(src) ~= "function" then\n            local ok, r1, r2 = pcall(_real_load, src, ...)\n            if ok then return r1, r2 else return nil, r2 end\n        end\n        if type(src) == "string" and #src > 5 then\n            local first100 = src:sub(1, 100)\n            if not first100:find("bit32", 1, true) and not first100:find("4294967296", 1, true) then\n                T("-- loadstring called (" .. #src .. " chars)")\n            end\n        end\n        local ok, r1, r2 = pcall(_real_load, src, ...)\n        if ok then return r1, r2 else return nil, r2 end\n    end\n    _G.loadstring = _G.load\n    -- Prevent WeAreDev VM from accessing real load via debug library\n    if debug then\n        local _orig_debug_getinfo = debug.getinfo\n        local _orig_debug_getupvalue = debug.getupvalue\n        if _orig_debug_getupvalue then\n            debug.getupvalue = function(...) return nil end\n        end\n        if debug.setupvalue then\n            debug.setupvalue = function(...) return nil end\n        end\n    end\nend\n\n_G.newproxy = function(b)\n    local t = {}\n    if b then setmetatable(t, {__index = function() return nil end}) end\n    return t\nend\n\nlocal api_names = {\n    "game", "workspace", "Instance", "Enum",\n    "Players", "ReplicatedStorage", "ReplicatedFirst",\n    "ServerStorage", "ServerScriptService", "StarterGui",\n    "StarterPlayer", "StarterPack", "StarterCharacterScripts",\n    "Lighting", "Teams", "Chat", "Debris",\n    "TweenService", "RunService", "UserInputService",\n    "HttpService", "MarketplaceService", "CollectionService",\n    "PathfindingService", "SoundService", "TextService",\n    "GuiService", "UserSettings", "CoreGui", "CorePackages",\n    "VirtualUser", "ContentProvider",\n    "DataStoreService", "BadgeService",\n    "UDim", "UDim2", "Color3", "Vector2", "Vector3",\n    "CFrame", "Ray", "Region3", "TweenInfo",\n    "Rect", "Font", "NumberSequence", "ColorSequence",\n    "NumberRange", "RaycastParams", "PhysicalProperties",\n    "task", "coroutine",\n}\n\nfor _, api_name in ipairs(api_names) do\n    _G[api_name] = make_tracer(api_name)\nend\n\n_orig_print("[STUBS_OK]")\n'

    @staticmethod
    def _get_tracer_lua() -> str:
        """Return the embedded tracer Lua environment."""
        return WeAreDevDeobfuscator._TRACER_LUA


    @staticmethod
    def _execute_vm_traced(obf: str) -> Tuple[List[str], List[str], List[str]]:
        """Execute VM via subprocess with tracing. Returns (prints, trace, errors).

        Writes a temporary runner script that embeds the tracer Lua environment,
        so the toolkit is self-contained (no external wearedev_vm_runner.py needed).
        """
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
        obf_file = tempfile.mktemp(suffix='.lua', prefix='wearedev_v3_')
        try:
            with open(runner_file, 'w') as f:
                f.write(runner_code)
            with open(obf_file, 'w') as f:
                f.write(obf)

            result = subprocess.run(
                [sys.executable, runner_file, obf_file],
                capture_output=True, text=True, timeout=15
            )
        except subprocess.TimeoutExpired:
            result = subprocess.CompletedProcess([], 1, stdout='', stderr='timeout')
        except Exception:
            result = subprocess.CompletedProcess([], 1, stdout='', stderr='error')
        finally:
            for fp in (runner_file, obf_file):
                if os.path.exists(fp):
                    os.unlink(fp)

        prints, trace, errors = [], [], []
        for line in result.stdout.split('\n'):
            line = line.strip()
            if not line:
                continue
            if line.startswith('[P]'):
                prints.append(line[3:])
            elif line.startswith('[T]'):
                trace.append(line[3:])
            elif line.startswith('[EX]'):
                errors.append(line[4:])

        return prints, trace, errors

    @staticmethod
    def _reconstruct_source(trace: List[str], prints: List[str]) -> str:
        """Convert trace entries to reconstructed Lua source.

        Strategy:
        - Remove entries that are prefixes of other entries (intermediate indexing)
        - Keep leaf entries (final calls, assignments, standalone operations)
        - Deduplicate
        - Separate comments from code
        """
        if not trace:
            return ''

        # Separate comments and code
        comments = []
        code_entries = []
        for entry in trace:
            if entry.startswith('--'):
                comments.append(entry)
            else:
                code_entries.append(entry)

        # Remove prefix entries (intermediate indexing kept for final operations)
        filtered = []
        for i, entry in enumerate(code_entries):
            is_prefix = any(
                j != i and (other.startswith(entry + '.') or other.startswith(entry + '('))
                for j, other in enumerate(code_entries)
            )
            if not is_prefix:
                filtered.append(entry)

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for entry in filtered:
            if entry not in seen:
                seen.add(entry)
                unique.append(entry)

        # Build output
        lines = []

        # Comments (skip noisy anti-tamper pow errors)
        for c in comments:
            if 'pow' not in c and 'Tamper' not in c.lower():
                lines.append(c)

        # Code (skip prints already captured by trace)
        has_print_in_trace = any(e.startswith('print(') for e in unique)
        for entry in unique:
            lines.append(entry)

        # Add prints only if trace didn't capture them
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
                         verbose: bool, m_offset: int = 5713,
                         accessor_name: str = 'M',
                         resolved_cff: str = '') -> str:
        """Generate the final deobfuscated output."""
        lines = []
        lines.append('-- Deobfuscated by Hunter Gay - Lua Deobfuscation Toolkit v4.0')
        lines.append('-- WeAreDev Obfuscator v1.0.0')
        lines.append('')

        # Section 1: Reconstructed source (MOST IMPORTANT - put it first)
        has_reconstructed = reconstructed and len(reconstructed.strip()) > 0
        if has_reconstructed:
            lines.append('-- ============================================')
            lines.append('-- RECONSTRUCTED SOURCE CODE')
            lines.append('-- ============================================')
            lines.append(reconstructed)
            lines.append('')

        # Section 2: Execution output
        if prints:
            lines.append('-- ============================================')
            lines.append('-- SCRIPT OUTPUT')
            lines.append('-- ============================================')
            for p in prints:
                lines.append(f'-- output: {p}')
            lines.append('')

        # Section 3: Errors
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

        # Section 4: Decoded string constants
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

        # Section 5: M() reference map
        lines.append('-- ============================================')
        lines.append(f'-- {accessor_name}() FUNCTION REFERENCE MAP')
        lines.append(f'-- {accessor_name}(x) = P[x - {m_offset}]')
        lines.append('-- ============================================')

        if string_map:
            for val in sorted(string_map.keys()):
                s = string_map[val]
                if s and not re.match(r'^[A-Za-z0-9]{8,20}$', s):
                    lines.append(f'--   {accessor_name}({val}) = {repr(s)}')
            lines.append('')

        # Section 6: Behavioral analysis
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

        # Resolved CFF (v4)
        lines.append('-- ============================================')
        lines.append('-- RESOLVED CFF (v4: accessor calls replaced with string literals)')
        lines.append('-- ============================================')
        if resolved_cff:
            max_chars = 5000
            if len(resolved_cff) > max_chars:
                lines.append(f'-- (showing first {max_chars} of {len(resolved_cff)} chars)')
                lines.append(resolved_cff[:max_chars] + chr(10) + '-- ... (truncated)')
            else:
                lines.append(resolved_cff)
        else:
            lines.append('-- (v4 CFF resolution produced no output; v3 fallback not available in static mode)')

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

    def detect_only(self, filepath: str) -> str:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()
        return ObfuscatorDetector.detect(code) or "Unknown/Clear text"


# ============================================================
# CLI
# ============================================================

def format_output(obf_name: str, source: str, meta: dict, verbose: bool) -> str:
    """Format the final output."""
    lines = [f"-- Deobfuscated by Hunter Gay - Lua Deobfuscation Toolkit v4.0"]
    lines.append(f"-- Obfuscator: {obf_name}")
    for k, v in meta.items():
        if k not in ("error", "prints"):
            lines.append(f"-- {k}: {v}")
    lines.append("")
    lines.append(source)
    return "\n".join(lines)


def main():
    args = sys.argv[1:]

    if not args or "-h" in args or "--help" in args:
        print("Lua Deobfuscation Toolkit v4.0")
        print("By Hunter Gay - Hunter Team Community\n")
        print(f"Usage: python {sys.argv[0]} <input.lua> [options]")
        print("")
        print("Options:")
        print("  -o <file>     Output file (default: stdout)")
        print("  --detect-only  Only detect obfuscator type")
        print("  -v, --verbose  Verbose output")
        print("")
        print("Supported: IronBrew2, WAN OBFUSCATE, MoonSec V3,")
        print("            Clyde Protection v2, AstroProtect 2.2,")
        print("            WeAreDev v1.0.0 (FULL), Base64+Compress,")
        print("            Generic VM")
        sys.exit(0)

    input_file = None
    output_file = None
    detect_only = False
    verbose = False

    i = 0
    while i < len(args):
        if args[i] == "-o" and i + 1 < len(args):
            output_file = args[i + 1]
            i += 2
            continue
        if args[i].startswith("--detect"):
            detect_only = True
            i += 1
            continue
        if args[i] in ("-v", "--verbose"):
            verbose = True
            i += 1
            continue
        if args[i].startswith("-"):
            i += 1
            continue
        if input_file is None:
            input_file = args[i]
        i += 1

    if not input_file:
        print("Error: No input file specified")
        sys.exit(1)

    if not os.path.exists(input_file):
        print(f"Error: File not found: {input_file}")
        sys.exit(1)

    deobf = LuaDeobfuscator(verbose=verbose)

    if detect_only:
        detected = deobf.detect_only(input_file)
        print(f"Detected: {detected}")
        return

    obf_name, source, meta = deobf.deobfuscate_file(input_file)

    output = format_output(obf_name, source, meta, verbose)

    if output_file:
        os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(output)
        if verbose:
            print(f"[+] Saved: {output_file}")
    else:
        print(output)

    if verbose:
        print(f"\n[+] Obfuscator: {obf_name}")
        for k, v in meta.items():
            print(f"    {k}: {v}")


# ============================================================
#                    DISCORD BOT WRAPPER
# ============================================================
# Commands:
#   .l <link>          -- deobfuscate a Lua file from a URL
#   .l  (with file)    -- deobfuscate the attachment
#   .help              -- show usage

import threading as _threading
import discord
from discord.ext import commands
from flask import Flask
import aiohttp

TOKEN = os.environ.get("DISCORD_TOKEN")
COMMAND_PREFIX = "."
DISCORD_MSG_LIMIT = 1900  # leave headroom under the 2000 char hard limit
MAX_FETCH_BYTES = 5 * 1024 * 1024  # 5 MB safety cap for link downloads


# ------------------------------------------------------------
# Keep-alive web server
# ------------------------------------------------------------
# Render (and most host-a-web-service platforms) expect the process to
# bind a port and answer HTTP requests, or it marks the service as down
# and restarts/kills it. The Discord bot itself doesn't need HTTP for
# anything -- this Flask app exists purely to keep Render happy.

keep_alive_app = Flask(__name__)


@keep_alive_app.route("/")
def _health():
    return "Bot is running."


def _run_keep_alive():
    port = int(os.environ.get("PORT", 8080))
    keep_alive_app.run(host="0.0.0.0", port=port)


def start_keep_alive():
    _threading.Thread(target=_run_keep_alive, daemon=True).start()


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

# Deobfuscator is stateful (loads a Lua VM once), reuse a single instance
deobfuscator = LuaDeobfuscator(verbose=False)


# ------------------------------------------------------------
# Comment stripping
# ------------------------------------------------------------

def strip_lua_comments(source: str) -> str:
    """
    Best-effort removal of Lua comments from source text.

    Handles:
      - Block comments: --[[ ... ]], --[=[ ... ]=], etc.
      - Line comments: -- until end of line

    Caveat: this is a plain-text pass, not a real Lua tokenizer, so a
    literal "--" inside a string literal could in rare cases get cut.
    Good enough for cleaning toolkit output / typical scripts.
    """
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


# ------------------------------------------------------------
# Fetch helpers
# ------------------------------------------------------------

async def _fetch_source(ctx: commands.Context, link: Optional[str]):
    """
    Returns (filename, code_text) or raises ValueError with a user-facing
    message on failure.
    """
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

    raise ValueError("Attach a `.lua`/`.txt` file, or give a link: `.l <link>`")


# ------------------------------------------------------------
# Bot events / commands
# ------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"[+] Logged in as {bot.user} (id={bot.user.id})")


@bot.command(name="l")
async def l_cmd(ctx: commands.Context, link: Optional[str] = None):
    """.l <link> or .l with a file attached -- deobfuscate and strip comments."""

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


bot.remove_command('help')

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    """.help -- show usage."""
    embed = discord.Embed(
        title="Lua Deobfuscator Bot",
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
        name="Supported obfuscators",
        value="WeAreDevs, IronBrew, MoonSec, AstroProtect, WAN, Clyde, generic VM-based/CFF obfuscators.",
        inline=False,
    )
    embed.set_footer(text="Comments are stripped from the recovered source automatically.")
    await ctx.reply(embed=embed)


if __name__ == "__main__":
    if not TOKEN:
        print("[!] Set DISCORD_TOKEN env var before running.")
    else:
        start_keep_alive()
        bot.run(TOKEN)
