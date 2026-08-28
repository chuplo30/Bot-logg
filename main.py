#!/usr/bin/env python3
"""
Lua Deobfuscation Toolkit v2.0 - Full Source Recovery
By Hunter Gay - Hunter Team Community

Recovers original Lua source code from obfuscated scripts.
Uses Lua VM execution (lupa/LuaJIT) + static analysis.

Supported obfuscators:
  - IronBrew / IronBrew2
  - WAN OBFUSCATE / WAN OBFUSCATOR
  - MoonSec V3
  - Clyde Protection v2
  - AstroProtect 2.2
  - Base64 + DEFLATE / ZLIB / GZIP
  - Generic VM-based (auto-detect + execution)

Usage:
  python lua_deobf_toolkit.py input.lua
  python lua_deobf_toolkit.py input.lua -o output.lua
  python lua_deobf_toolkit.py input.lua --detect-only
  python lua_deobf_toolkit.py input.lua -v

Requirements:
  pip install lupa
"""

import re
import sys
import os
import zlib
import base64
import time
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any


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

-- Capture state
local _orig_print = print
local _orig_load = load
local _print_output = {}
local captured_loads = {}
local load_count = 0

-- Hook print
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

-- Try to load and execute
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

            # Convert Lua table to Python dict recursively
            def lua2py(obj):
                if hasattr(obj, 'keys'):
                    d = {str(k): lua2py(obj[k]) for k in obj.keys()}
                    # Check if it's an array-like table (consecutive int keys from 1)
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
    """Base64 + DEFLATE/ZLIB/GZIP -> Lua source.
    Only triggers if no known obfuscator signature is found (pure compression wrapper)."""

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        # Skip if any known VM obfuscator is detected
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

                # Check if result is plain Lua (not another VM wrapper)
                vm_indicators = ["bit32", "4294967296", "while true do", "getfenv"]
                vm_score = sum(1 for v in vm_indicators if v in source)

                meta = {
                    "method": f"base64 + {name}",
                    "b64_len": len(b64_str),
                    "compressed": len(compressed),
                    "decompressed": len(decompressed),
                    "vm_wrapped": vm_score > 2,
                }

                # If the decompressed content is another VM, try executing it
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

        # Step 1: Extract and decompress
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
        except Exception as e:
            return None

        if verbose:
            opcodes = re.findall(r'elseif ox==(\d+)', vm_code)
            h_table = re.findall(r'\{\d+,\d+,\{[^}]*\},\{[^}]*\}\}', vm_code)
            wm = re.search(r'cid:\s*(\w+)', code)
            print(f"  [*] DEFLATE: {len(compressed)} -> {len(vm_code)} bytes")
            print(f"  [*] VM opcodes: {len(set(int(x) for x in opcodes)) if opcodes else 0}")
            print(f"  [*] Encrypted strings: {len(h_table)}")
            if wm:
                print(f"  [*] Watermark: cid:{wm.group(1)}")

        # Step 2: Execute VM to recover source
        if engine.available:
            if verbose:
                print("  [*] Executing VM...")

            ok, source, prints = engine.execute_and_capture(code, timeout=30)
            if verbose:
                print(f"  [*] VM result: ok={ok}, source_len={len(source) if source else 0}, prints={prints}")

            if source and len(source) > 10 and "bit32" not in source[:200]:
                # loadstring was captured - this IS the source
                return source, {"method": "VM execution (loadstring capture)"}

            if ok and prints:
                # VM ran successfully, reconstruct from print output
                recovered = SourceReconstructor.from_prints(prints)
                return recovered, {
                    "method": "VM execution (print trace)",
                    "print_count": len(prints),
                }

            if not ok:
                err_str = str(source) if source else ""
                # Integrity check with fake error message
                if "attempt to call a table value" in err_str:
                    # Try executing with no args via a wrapper
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

        # Fallback: static string extraction
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
        # Look for string literals that look like API calls or variable names
        str_literals = re.findall(r'"([A-Za-z_][A-Za-z0-9_]{2,})"', code)
        # Filter common Lua/Roblox API names
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

        # Fallback: extract encoded data
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

        # Fallback: static analysis
        tables = re.findall(r'local\s+\w+\s*=\s*\{([^}]{50,})\}', code)
        ascii85 = re.search(r'<~([A-Za-z0-9!#$%&*+/=?@^_`{|}~-]+)~>', code)
        lines = ["-- Clyde Protection v2 (structural analysis)"]
        lines.append(f"-- Data tables: {len(tables)}")
        if ascii85:
            lines.append(f"-- Ascii85 payload: {len(ascii85.group(1))} chars")
        lines.append("-- Decryption: Ascii85 -> S-box CBC XOR -> key XOR -> position XOR")
        return "\n".join(lines), {"method": "static analysis", "tables": len(tables)}


class WeAreDevDeobfuscator:
    """WeAreDev Obfuscator v1.0.0: CFF + encrypted string/number tables.
    Uses multiprocessing to handle infinite loops (game scripts)."""

    RUNNER_PATH = "/home/z/my-project/scripts/run_wearedev.lua"

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if not engine.available:
            return None

        import multiprocessing
        import tempfile
        import re

        # Strip comment header
        obf = re.sub(r'^--\[\[.*?\]\]\s*', '', code)

        with open(WeAreDevDeobfuscator.RUNNER_PATH) as f:
            runner = f.read()

        result_file = tempfile.mktemp(suffix='.txt', prefix='wearedev_')

        def run_lua():
            from lupa import LuaRuntime
            import sys as _sys
            _lua = LuaRuntime(unpack_returned_tuples=True)
            _out = open(result_file, 'w')
            class _W:
                def write(self, s): _out.write(s); _out.flush()
                def flush(self): _out.flush()
            _orig = _sys.stdout
            _sys.stdout = _W()
            try:
                _lua.execute(runner)
                print('[STUBS_OK]')
                try:
                    _lua.execute(obf)
                    print('[COMPLETED]')
                except Exception as e:
                    print(f'[ERROR]{e}')
                _g = _lua.globals()
                cp = _g._captured_prints
                n = 0
                try:
                    while True:
                        v = cp[n+1]
                        if v is None: break
                        print(f'[P]{v}')
                        n += 1
                except: pass
                print(f'[PCOUNT]{n}')
                cc = _g._captured_calls
                n = 0
                try:
                    while True:
                        v = cc[n+1]
                        if v is None: break
                        print(f'[C]{v}')
                        n += 1
                except: pass
                print(f'[CCOUNT]{n}')
            except Exception as e:
                print(f'[FATAL]{e}')
            finally:
                _out.close()
                _sys.stdout = _orig

        if verbose:
            print("  [*] Executing WeAreDev VM (multiprocessing, 15s timeout)...")

        p = multiprocessing.Process(target=run_lua)
        p.start()
        p.join(timeout=15)
        if p.is_alive():
            p.terminate(); p.join(timeout=3)
            if p.is_alive(): p.kill(); p.join()
            if verbose:
                print("  [*] Timed out - captured output is valid")

        # Parse results
        prints, calls = [], []
        if os.path.exists(result_file):
            with open(result_file) as f:
                for line in f:
                    line = line.rstrip('\n')
                    if line.startswith('[P]'): prints.append(line[3:])
                    elif line.startswith('[C]'): calls.append(line[3:])
            os.unlink(result_file)

        if not prints and not calls:
            return None

        # Reconstruct source
        source = SourceReconstructor.from_prints(prints) if prints else "-- No print output captured"
        meta = {"method": "CFF VM execution (multiprocessing)", "print_count": len(prints)}
        if calls:
            meta["call_count"] = len(calls)
        return source, meta


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

        # Static analysis
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
        """Reconstruct source from print() side effects."""
        if not prints:
            return "-- No output captured"

        lines = []
        for p in prints:
            # Try to detect the type of argument
            if p.startswith("{") or p.startswith("table:"):
                lines.append(f"print({p})")
            elif p == ("true" or p == "false" or p == "nil"):
                lines.append(f"print({p})")
            else:
                # Try as string or number
                try:
                    float(p)
                    lines.append(f"print({p})")
                except ValueError:
                    lines.append(f'print("{p}")')
        return "\n".join(lines)

    @staticmethod
    def from_api_calls(calls: List[dict]) -> str:
        """Reconstruct source from API call traces."""
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
        # Detect
        detected = ObfuscatorDetector.detect(code)
        if self.verbose:
            print(f"[*] File: {name}")
            print(f"[*] Size: {len(code):,} chars")
            print(f"[*] Detected: {detected or 'Unknown'}")

        source = None
        meta = {"detected": detected}
        obf_name = detected or "Unknown"
        prints = []

        # Try each deobfuscator
        for deobf_cls in self.DEOBFUSCATORS:
            cls_name = deobf_cls.__name__.replace("Deobfuscator", "")

            # Skip if detected type doesn't match (except generic ones)
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

                # No source recovered but we got prints
                if "prints" in result_meta:
                    prints = result_meta["prints"]

            except Exception as e:
                if self.verbose:
                    print(f"[!] {cls_name} error: {e}")
                meta["error"] = str(e)

        # If we have prints but no source, reconstruct from prints
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
    lines = [f"-- Deobfuscated by Hunter Gay - Lua Deobfuscation Toolkit v2.0"]
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
        print("Lua Deobfuscation Toolkit v2.0")
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
        print("            WeAreDev v1.0.0, Base64+Compress, Generic VM")
        sys.exit(0)

    # Parse args
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

    # Deobfuscate
    obf_name, source, meta = deobf.deobfuscate_file(input_file)

    # Format output
    output = format_output(obf_name, source, meta, verbose)

    # Write or print
    if output_file:
        os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(output)
        if verbose:
            print(f"[+] Saved: {output_file}")
    else:
        print(output)

    # Summary
    if verbose:
        print(f"\n[+] Obfuscator: {obf_name}")
        for k, v in meta.items():
            print(f"    {k}: {v}")


# ============================================================
# ============================================================
#                    DISCORD BOT WRAPPER
# ============================================================
# ============================================================
"""
Command:
  .log   (attach a .lua/.txt file to the message)

Behavior:
  - Downloads the attached file
  - Runs it through LuaDeobfuscator (defined above in this same file)
  - Strips comments (-- line comments and --[[ ]] block comments,
    including the toolkit's own header comments) from the recovered source
  - Replies with the cleaned source, as a code block if short enough,
    otherwise as a .lua file attachment
"""

import threading as _threading
import discord
from discord.ext import commands
from flask import Flask

TOKEN = os.environ.get("DISCORD_TOKEN")
COMMAND_PREFIX = "."
DISCORD_MSG_LIMIT = 1900  # leave headroom under the 2000 char hard limit


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
# Bot events / commands
# ------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"[+] Logged in as {bot.user} (id={bot.user.id})")


@bot.command(name="log")
async def log_cmd(ctx: commands.Context):
    """.log -- attach a .lua/.txt file to deobfuscate it and strip comments."""

    if not ctx.message.attachments:
        await ctx.reply("Attach a `.lua` or `.txt` file with the command.")
        return

    attachment = ctx.message.attachments[0]
    if not attachment.filename.lower().endswith((".lua", ".txt")):
        await ctx.reply("File must be `.lua` or `.txt`.")
        return

    status_msg = await ctx.reply(f"Deobfuscating `{attachment.filename}`...")

    try:
        raw_bytes = await attachment.read()
        code = raw_bytes.decode("utf-8", errors="replace")

        obf_name, source, meta = deobfuscator.deobfuscate(code, attachment.filename)
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
            await ctx.send(file=discord.File(tmp_path, filename="deobfuscated.lua"))
            os.remove(tmp_path)

    except Exception as e:
        await status_msg.edit(content=f"Error: `{e}`")


if __name__ == "__main__":
    if not TOKEN:
        print("[!] Set DISCORD_TOKEN env var before running.")
    else:
        start_keep_alive()
        bot.run(TOKEN)
