# main.py
#!/usr/bin/env python3
"""
Lua Deobfuscation Toolkit v3.0 - Web API + Discord Bot
By Hunter Gay - Hunter Team Community
"""

import os
import re
import sys
import json
import base64
import zlib
import tempfile
import subprocess
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any
from flask import Flask, request, jsonify, render_template_string
import requests
import logging

# Discord.py imports
try:
    import discord
    from discord.ext import commands
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False
    print("[!] discord.py not installed. Discord bot disabled.")

# Lupa imports
try:
    from lupa import LuaRuntime
    LUPA_AVAILABLE = True
except ImportError:
    LUPA_AVAILABLE = False
    print("[!] lupa not installed. VM execution disabled.")

# ============================================================
# Configuration
# ============================================================

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    print("[!] DISCORD_TOKEN not set in environment variables.")

ALLOWED_EXTENSIONS = {'.lua', '.txt'}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
TIMEOUT_SECONDS = 30

app = Flask(__name__)

# ============================================================
# Logging setup
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================
# Lua Engine (lupa-based)
# ============================================================

class LuaEngine:
    """Lua VM execution engine using lupa (LuaJIT/Lua 5.5)."""

    _instance = None

    def __init__(self):
        if not LUPA_AVAILABLE:
            self.available = False
            return
        
        try:
            self.lua = LuaRuntime(unpack_returned_tuples=True)
            self._setup()
            self.available = True
        except Exception as e:
            self.available = False
            logger.error(f"Lua init error: {e}")

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
        try:
            self.lua.execute(setup_lua)
        except Exception as e:
            logger.error(f"Lua setup error: {e}")

    def execute_and_capture(self, code: str, timeout: float = 20) -> Tuple[bool, str, List[str]]:
        """Execute Lua code and capture print output + loadstring calls."""
        if not self.available:
            return False, "lupa not available", []

        runner_lua = r"""
local code = ...

local _orig_print = print
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

        try:
            result = self.lua.execute(runner_lua, code)
            
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
            err_str = str(e)
            return False, err_str, []

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
                        logger.info("Decompressed content is VM-wrapped, executing...")
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

        if engine.available:
            ok, source, prints = engine.execute_and_capture(code, timeout=30)
            if source and len(source) > 10 and "bit32" not in source[:200]:
                return source, {"method": "VM execution (loadstring capture)"}
            if ok and prints:
                recovered = SourceReconstructor.from_prints(prints)
                return recovered, {"method": "VM execution (print trace)", "print_count": len(prints)}

        return None, {"method": "static analysis only", "vm_size": len(vm_code)}

class IronBrewDeobfuscator:
    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if "LOL!" not in code:
            return None

        if engine.available:
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
    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if "WAN OBFUSCATE" not in code and "WAN OBFUSCATOR" not in code:
            return None

        if engine.available:
            ok, source, prints = engine.execute_and_capture(code, timeout=20)
            if source and len(source) > 5 and "WAN" not in source[:50]:
                return source, {"method": "VM execution (loadstring capture)"}
            if ok and prints:
                recovered = SourceReconstructor.from_prints(prints)
                return recovered, {"method": "VM execution (print trace)", "print_count": len(prints)}

        return None, {"method": "requires VM execution"}

class MoonSecDeobfuscator:
    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if "MoonSec" not in code and "moonsec" not in code.lower():
            return None

        if engine.available:
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
    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if "Clyde" not in code:
            return None

        if engine.available:
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
    """WeAreDev Obfuscator v1.0.0 - Full decompiler with execution tracing."""
    
    M_OFFSET = 472584 - 466871  # 5713

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if not engine.available:
            return None

        obf = re.sub(r'^--\[\[.*?\]\]\s*', '', code)

        # Phase 1: Decode P-table
        if verbose:
            logger.info("Phase 1: Decoding P-table string constants...")
        P_decoded = WeAreDevDeobfuscator._decode_p_table(obf, engine)
        if not P_decoded:
            if verbose:
                logger.info("Failed to decode P-table")
            return None

        string_map = WeAreDevDeobfuscator._build_string_map(obf, P_decoded)
        real_strings = {k: v for k, v in string_map.items()
                        if v and not re.match(r'^[A-Za-z0-9]{8,20}$', v)}

        if verbose:
            logger.info(f"P-table: {len(P_decoded)} entries, {len(real_strings)} meaningful strings")

        # Phase 2: Execute VM with tracing
        if verbose:
            logger.info("Phase 2: Executing VM with full tracing (15s timeout)...")

        prints, trace, errors = WeAreDevDeobfuscator._execute_vm_traced(obf)

        if verbose:
            logger.info(f"Captured: {len(prints)} prints, {len(trace)} trace entries, {len(errors)} errors")

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
    def _execute_vm_traced(obf: str) -> Tuple[List[str], List[str], List[str]]:
        """Execute VM via subprocess with tracing."""
        obf_file = tempfile.mktemp(suffix='.lua', prefix='wearedev_v3_')
        with open(obf_file, 'w') as f:
            f.write(obf)

        runner_script = os.path.join(os.path.dirname(__file__), 'wearedev_vm_runner.py')
        
        # If runner script doesn't exist, create it inline
        if not os.path.exists(runner_script):
            runner_script = create_vm_runner_script()
        
        try:
            result = subprocess.run(
                [sys.executable, runner_script, obf_file],
                capture_output=True, text=True, timeout=15
            )
        except subprocess.TimeoutExpired:
            result = subprocess.CompletedProcess([], 1, stdout='', stderr='timeout')
        except Exception:
            result = subprocess.CompletedProcess([], 1, stdout='', stderr='error')
        finally:
            if os.path.exists(obf_file):
                os.unlink(obf_file)

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

        return '\n'.join(lines)

class GenericVMDeobfuscator:
    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if engine.available:
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

# ============================================================
# Utility Functions
# ============================================================

def eval_arith(expr: str) -> Optional[int]:
    """Evaluate obfuscated arithmetic like -418876+418904."""
    expr = expr.strip().replace('-(-', '+(')
    try:
        return int(eval(expr))
    except:
        return None

def create_vm_runner_script() -> str:
    """Create the VM runner script file."""
    runner_path = os.path.join(os.path.dirname(__file__), 'wearedev_vm_runner.py')
    
    # The script content is embedded in the main file
    runner_content = '''#!/usr/bin/env python3
"""WeAreDev VM runner v3 - Comprehensive execution tracing."""
import sys
from lupa import LuaRuntime

TRACER_LUA = r'''
local _trace = {}
local _trace_n = 0
local _orig_print = print

local function safe_tostring(v)
    if type(v) == "string" then
        return "\\"" .. v:gsub("\\"", "\\\\\\"") .. "\\""
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
    local line = table.concat(strs, "\\t")
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

def main():
    if len(sys.argv) < 2:
        print("[EX]No input file")
        return

    with open(sys.argv[1], 'r', encoding='utf-8', errors='replace') as f:
        code = f.read()

    lua = LuaRuntime(unpack_returned_tuples=True)
    try:
        lua.execute(TRACER_LUA + '\\n' + code)
        print('[DONE]')
    except Exception as e:
        err_str = str(e)
        if len(err_str) > 500:
            err_str = err_str[:500] + '...'
        print(f'[EX]{err_str}')

if __name__ == '__main__':
    main()
'''
    
    with open(runner_path, 'w') as f:
        f.write(runner_content)
    
    return runner_path

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

    def deobfuscate(self, code: str, name: str = "input") -> Tuple[str, str, dict]:
        """Deobfuscate Lua code. Returns (obfuscator_name, recovered_source, metadata)"""
        detected = ObfuscatorDetector.detect(code)
        if self.verbose:
            logger.info(f"File: {name}")
            logger.info(f"Size: {len(code):,} chars")
            logger.info(f"Detected: {detected or 'Unknown'}")

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
                logger.info(f"Trying {cls_name}...")

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
                    logger.error(f"{cls_name} error: {e}")
                meta["error"] = str(e)

        if not source and prints:
            source = SourceReconstructor.from_prints(prints)
            meta["reconstructed_from"] = "print traces"

        if not source:
            source = f"-- Deobfuscation incomplete\n-- Obfuscator: {obf_name}\n-- The script uses VM-based obfuscation.\n-- Full source recovery requires manual VM analysis."

        return obf_name, source, meta

    def detect_only(self, code: str) -> str:
        return ObfuscatorDetector.detect(code) or "Unknown/Clear text"

# ============================================================
# Flask Web Routes
# ============================================================

@app.route('/')
def index():
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <title>Lua Deobfuscation Toolkit v3.0</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 900px; margin: 50px auto; padding: 20px; background: #f5f5f5; }
        h1 { color: #2c3e50; }
        .container { background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        textarea { width: 100%; height: 300px; font-family: monospace; padding: 10px; border: 1px solid #ddd; border-radius: 5px; }
        select, input[type="file"] { padding: 8px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; }
        button { padding: 10px 30px; background: #3498db; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; }
        button:hover { background: #2980b9; }
        .result { margin-top: 20px; background: #2c3e50; color: #ecf0f1; padding: 20px; border-radius: 5px; overflow: auto; max-height: 500px; font-family: monospace; font-size: 13px; white-space: pre-wrap; }
        .info { background: #ecf0f1; padding: 10px; border-radius: 5px; margin: 10px 0; }
        .error { color: #e74c3c; }
        .success { color: #2ecc71; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🔧 Lua Deobfuscation Toolkit v3.0</h1>
        <p>By Hunter Gay - Hunter Team Community</p>
        <hr>
        
        <form method="POST" action="/deobfuscate" enctype="multipart/form-data">
            <h3>📁 Upload Lua file:</h3>
            <input type="file" name="file" accept=".lua,.txt">
            <br>
            <button type="submit">Deobfuscate</button>
        </form>
        
        <hr>
        
        <form method="POST" action="/deobfuscate">
            <h3>✏️ Paste Lua code:</h3>
            <textarea name="code" placeholder="Paste obfuscated Lua code here..."></textarea>
            <br>
            <button type="submit">Deobfuscate</button>
        </form>
        
        <hr>
        
        <form method="POST" action="/detect">
            <h3>🔍 Detect only:</h3>
            <textarea name="code" placeholder="Paste Lua code to detect obfuscator type..." style="height:100px;"></textarea>
            <br>
            <button type="submit">Detect</button>
        </form>
        
        <div class="info">
            <b>Supported obfuscators:</b> IronBrew2, WAN OBFUSCATE, MoonSec V3, Clyde Protection v2, AstroProtect 2.2, WeAreDev v1.0.0, Base64+Compress, Generic VM
        </div>
    </div>
</body>
</html>
''')

@app.route('/deobfuscate', methods=['POST'])
def deobfuscate():
    """Deobfuscate Lua code from file upload or text paste."""
    code = None
    
    # Check for file upload
    if 'file' in request.files:
        file = request.files['file']
        if file and file.filename:
            if file.content_length > MAX_FILE_SIZE:
                return jsonify({'error': f'File too large. Max size: {MAX_FILE_SIZE/1024/1024:.1f}MB'}), 400
            
            try:
                code = file.read().decode('utf-8')
            except Exception as e:
                return jsonify({'error': f'Error reading file: {str(e)}'}), 400
    
    # Check for text paste
    if not code and 'code' in request.form:
        code = request.form['code']
    
    if not code:
        return jsonify({'error': 'No code provided'}), 400
    
    if len(code) > MAX_FILE_SIZE:
        return jsonify({'error': f'Code too large. Max size: {MAX_FILE_SIZE/1024/1024:.1f}MB'}), 400
    
    try:
        deobf = LuaDeobfuscator(verbose=True)
        obf_name, source, meta = deobf.deobfuscate(code)
        
        return jsonify({
            'success': True,
            'obfuscator': obf_name,
            'source': source,
            'metadata': meta
        })
    except Exception as e:
        logger.error(f"Deobfuscation error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/detect', methods=['POST'])
def detect():
    """Detect obfuscator type only."""
    code = request.form.get('code', '')
    if not code:
        return jsonify({'error': 'No code provided'}), 400
    
    try:
        deobf = LuaDeobfuscator()
        detected = deobf.detect_only(code)
        return jsonify({'detected': detected})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy',
        'lupa_available': LUPA_AVAILABLE,
        'discord_available': DISCORD_AVAILABLE
    })

# ============================================================
# Discord Bot
# ============================================================

if DISCORD_AVAILABLE and DISCORD_TOKEN:
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix='!', intents=intents)

    @bot.event
    async def on_ready():
        logger.info(f'Discord bot logged in as {bot.user}')
        await bot.change_presence(activity=discord.Game(name="!deobf <file/link>"))

    @bot.command(name='deobf')
    async def deobf_command(ctx, *, arg: str = None):
        """Deobfuscate Lua code from file attachment or link."""
        # Check for file attachment
        if ctx.message.attachments:
            attachment = ctx.message.attachments[0]
            if not any(attachment.filename.endswith(ext) for ext in ['.lua', '.txt']):
                await ctx.send("❌ Please attach a .lua or .txt file.")
                return
            
            if attachment.size > MAX_FILE_SIZE:
                await ctx.send(f"❌ File too large. Max size: {MAX_FILE_SIZE/1024/1024:.1f}MB")
                return
            
            try:
                content = await attachment.read()
                code = content.decode('utf-8')
            except Exception as e:
                await ctx.send(f"❌ Error reading file: {str(e)}")
                return
            
            await process_deobf(ctx, code, attachment.filename)
            return
        
        # Check for URL
        if arg and arg.startswith('http'):
            try:
                response = requests.get(arg, timeout=10)
                if response.status_code != 200:
                    await ctx.send(f"❌ Failed to fetch URL: HTTP {response.status_code}")
                    return
                
                code = response.text
                if len(code) > MAX_FILE_SIZE:
                    await ctx.send(f"❌ File too large. Max size: {MAX_FILE_SIZE/1024/1024:.1f}MB")
                    return
                
                await process_deobf(ctx, code, arg.split('/')[-1])
            except Exception as e:
                await ctx.send(f"❌ Error fetching URL: {str(e)}")
            return
        
        await ctx.send("❌ Please attach a Lua file or provide a URL to a Lua script.\nUsage: `!deobf <url>` or attach a file.")

    async def process_deobf(ctx, code: str, filename: str):
        """Process deobfuscation and send results."""
        await ctx.send(f"🔧 Processing `{filename}`... This may take a moment.")
        
        try:
            deobf = LuaDeobfuscator(verbose=True)
            
            # Detect first
            detected = deobf.detect_only(code)
            await ctx.send(f"🔍 Detected: **{detected}**")
            
            # Deobfuscate
            obf_name, source, meta = deobf.deobfuscate(code, filename)
            
            # Build response
            response = f"**✅ Deobfuscation Complete!**\n"
            response += f"**Obfuscator:** {obf_name}\n"
            response += f"**Method:** {meta.get('method', 'N/A')}\n"
            
            if 'print_count' in meta:
                response += f"**Print Outputs:** {meta['print_count']}\n"
            if 'trace_entries' in meta:
                response += f"**Trace Entries:** {meta['trace_entries']}\n"
            if 'p_entries' in meta:
                response += f"**P-table Entries:** {meta['p_entries']}\n"
            if 'strings_decoded' in meta:
                response += f"**Strings Decoded:** {meta['strings_decoded']}\n"
            
            # Send source code (split if too long)
            if len(source) > 1900:
                # Split into parts
                parts = [source[i:i+1900] for i in range(0, len(source), 1900)]
                await ctx.send(response)
                for i, part in enumerate(parts):
                    await ctx.send(f"```lua\nPart {i+1}/{len(parts)}:\n{part}\n```")
            else:
                await ctx.send(f"{response}\n```lua\n{source}\n```")
                
        except Exception as e:
            logger.error(f"Deobf error: {e}")
            await ctx.send(f"❌ Error during deobfuscation: {str(e)}")

    @bot.command(name='detect')
    async def detect_command(ctx, *, arg: str = None):
        """Detect obfuscator type from file attachment or link."""
        if ctx.message.attachments:
            attachment = ctx.message.attachments[0]
            try:
                content = await attachment.read()
                code = content.decode('utf-8')
                deobf = LuaDeobfuscator()
                detected = deobf.detect_only(code)
                await ctx.send(f"🔍 Detected: **{detected}**")
            except Exception as e:
                await ctx.send(f"❌ Error: {str(e)}")
            return
        
        if arg and arg.startswith('http'):
            try:
                response = requests.get(arg, timeout=10)
                if response.status_code == 200:
                    deobf = LuaDeobfuscator()
                    detected = deobf.detect_only(response.text)
                    await ctx.send(f"🔍 Detected: **{detected}**")
                else:
                    await ctx.send(f"❌ HTTP {response.status_code}")
            except Exception as e:
                await ctx.send(f"❌ Error: {str(e)}")
            return
        
        await ctx.send("Please attach a Lua file or provide a URL.")

    @bot.command(name='help')
    async def help_command(ctx):
        embed = discord.Embed(
            title="Lua Deobfuscation Toolkit v3.0",
            description="By Hunter Gay - Hunter Team Community",
            color=0x3498db
        )
        embed.add_field(
            name="Commands",
            value=(
                "`!deobf` - Deobfuscate attached Lua file\n"
                "`!deobf <url>` - Deobfuscate Lua from URL\n"
                "`!detect` - Detect obfuscator type\n"
                "`!help` - Show this help"
            ),
            inline=False
        )
        embed.add_field(
            name="Supported Obfuscators",
            value=(
                "IronBrew2, WAN OBFUSCATE, MoonSec V3, "
                "Clyde Protection v2, AstroProtect 2.2, "
                "WeAreDev v1.0.0, Base64+Compress, Generic VM"
            ),
            inline=False
        )
        embed.add_field(
            name="Limits",
            value=f"Max file size: {MAX_FILE_SIZE/1024/1024:.1f}MB\nTimeout: {TIMEOUT_SECONDS}s",
            inline=False
        )
        await ctx.send(embed=embed)

# ============================================================
# Run Flask + Discord
# ============================================================

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)

if __name__ == '__main__':
    import threading
    
    # Start Flask in a separate thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Start Discord bot if token is available
    if DISCORD_AVAILABLE and DISCORD_TOKEN:
        try:
            logger.info("Starting Discord bot...")
            bot.run(DISCORD_TOKEN)
        except Exception as e:
            logger.error(f"Discord bot error: {e}")
    else:
        # Keep Flask running
        logger.info("Flask server running...")
        flask_thread.join()