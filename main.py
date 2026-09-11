
import re
import io
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

try:
    from api_names_db import API_NAMES
except ImportError:
    API_NAMES = frozenset()


# ============================================================
# Render-friendly env bootstrap (Start Command = only: python main.py)
# Sets PATH / tool dirs so Prometheus, Ironveil, LuaObfuscator, Moonsec work
# without export in Render Start Command.
# ============================================================
def _bootstrap_deobf_env() -> None:
    import os
    from pathlib import Path
    home = Path.home()
    cwd = Path.cwd()
    # Prefer directory of this file as app root
    try:
        app_root = Path(__file__).resolve().parent
    except NameError:
        app_root = cwd

    def _add_path(p: Path) -> None:
        if not p or not p.exists():
            return
        cur = os.environ.get("PATH", "")
        s = str(p)
        if s not in cur.split(os.pathsep):
            os.environ["PATH"] = s + os.pathsep + cur

    # Node (installed to $HOME/node during build)
    _add_path(home / "node" / "bin")
    _add_path(Path("/opt/render/project/src") / "node" / "bin")

    # .NET — prefer project-local install (survives Render runtime), then $HOME
    os.environ.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")
    os.environ.setdefault("DOTNET_NOLOGO", "1")
    for dotnet_root in (
        app_root / ".dotnet",
        cwd / ".dotnet",
        Path("/opt/render/project/src/.dotnet"),
        home / ".dotnet",
    ):
        if (dotnet_root / "dotnet").is_file() or (dotnet_root / "dotnet.exe").is_file():
            os.environ["DOTNET_ROOT"] = str(dotnet_root)
            _add_path(dotnet_root)
            _add_path(dotnet_root / "tools")
            break
        if dotnet_root.is_dir():
            os.environ.setdefault("DOTNET_ROOT", str(dotnet_root))
            _add_path(dotnet_root)
            _add_path(dotnet_root / "tools")

    # Tool repo locations (cloned next to main.py during build)
    pairs = [
        ("PROMETHEUS_DEOBF_DIR", app_root / "Prometheus-Deobfuscator"),
        ("IRONVEIL_DEOBF_DIR", app_root / "Ironveil-Deobfuscator-V1" / "deobfuscator"),
        ("LUAOBF_DEOBF_DIR", app_root / "LuaObfuscator-Deobfuscator"),
        ("MOONSEC_DEOBF_DIR", app_root / "MoonsecDeobfuscator"),
        ("LUAU_VMP_DIR", app_root / "luau-vmp-deobf"),
        ("DEOBF_TOOLS_DIR", app_root),
    ]
    for key, p in pairs:
        if key not in os.environ or not os.environ.get(key):
            if p.exists():
                os.environ[key] = str(p)

    # Log once so Render logs show what was found
    try:
        import shutil
        print("[bootstrap] PATH has node=", shutil.which("node"),
              "dotnet=", shutil.which("dotnet"))
        print("[bootstrap] PROMETHEUS=", os.environ.get("PROMETHEUS_DEOBF_DIR"))
        print("[bootstrap] IRONVEIL=", os.environ.get("IRONVEIL_DEOBF_DIR"))
        print("[bootstrap] LUAOBF=", os.environ.get("LUAOBF_DEOBF_DIR"))
        print("[bootstrap] MOONSEC=", os.environ.get("MOONSEC_DEOBF_DIR"))
    except Exception as e:
        print("[bootstrap] log error:", e)


_bootstrap_deobf_env()


def _extract_lua_strings(code: str, mode: str = "all") -> List[str]:
    """Expanded string/identifier extractor using full Roblox API name DB."""
    strings = set()
    keywords = {
        "and", "break", "do", "else", "elseif", "end", "false", "for", "function",
        "goto", "if", "in", "local", "nil", "not", "or", "repeat", "return",
        "then", "true", "until", "while", "continue",
    }

    # 1) Quoted string literals
    for quote, content in re.findall(r'''(["'])((?:(?!\1).)*)\1''', code, re.DOTALL):
        if content:
            strings.add(content)

    # 2) Long bracket strings
    for s in re.findall(r'\[=*\[(.*?)\]=*\]', code, re.DOTALL):
        if s and s.strip():
            strings.add(s.strip())

    # 3) Identifiers
    identifiers = re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\b', code)
    for ident in identifiers:
        if len(ident) > 1 and ident not in keywords:
            strings.add(ident)

    # 4) API names from full DB
    for ident in set(identifiers):
        if ident in API_NAMES:
            strings.add(ident)

    # 5) Table keys: t["Key"] / t.Key
    for key in re.findall(r'\[[\'"]([A-Za-z_][A-Za-z0-9_]*)[\'"]\]', code):
        strings.add(key)
    for key in re.findall(r'\.([A-Za-z_][A-Za-z0-9_]*)\b', code):
        if key not in keywords:
            strings.add(key)

    # 6) loadstring / load payloads
    for s in re.findall(r'(?:loadstring|load)\s*\(\s*["\']([^"\']+)["\']', code):
        if s:
            strings.add(s)

    # 7) print / warn / error / assert messages
    for s in re.findall(r'(?:print|warn|error|assert)\s*\(\s*["\']([^"\']+)["\']', code):
        if s:
            strings.add(s)

    # 8) Common API call string args
    for s in re.findall(
        r'(?:GetService|WaitForChild|FindFirstChild|FindFirstChildOfClass|FindFirstChildWhichIsA|'
        r'FindFirstAncestor|GetAttribute|SetAttribute|FireServer|InvokeServer|BindToRenderStep|'
        r'HttpGet|HttpPost|GetDataStore|require)\s*\(\s*["\']([^"\']+)["\']',
        code,
    ):
        if s:
            strings.add(s)

    # 9) Enum.X.Y
    for a, b in re.findall(r'\bEnum\.([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)', code):
        strings.add(a)
        strings.add(b)
        strings.add("Enum." + a)
        strings.add("Enum." + a + "." + b)

    if mode == "simple":
        result = []
        for s in strings:
            if s in API_NAMES or (len(s) > 4 and s[0].islower()) or s.startswith("Enum."):
                result.append(s)
        return list(set(result))
    return list(strings)



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
        if s[i] == '\\' and i + 1 < len(s) and '0' <= s[i+1] <= '9':
            num_str = ''
            j = i + 1
            while j < len(s) and j < i + 4 and '0' <= s[j] <= '9':
                num_str += s[j]
                j += 1
            if num_str:
                try:
                    codepoint = int(num_str)
                    if 0 <= codepoint <= 0x10FFFF:
                        result.append(chr(codepoint))
                    else:
                        result.append(s[i:j])
                except (ValueError, OverflowError):
                    result.append(s[i:j])
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
# v12: Simple multi-layer wrapper peeler ("Miyu Hub"-style)
# ============================================================
# Some redistribution "hubs" wrap an already-obfuscated script (e.g. a
# WeAreDevs payload) in one or more extra layers of trivial byte-level
# encoding -- typically:
#   local function NAME()
#       local VAR = "\ddd\ddd..."   -- giant escaped-byte string
#       <loop building a new string via string.char(SIMPLE_EXPR)>
#       (loadstring or load)(result)()
#   end
#   NAME()
# The "encryption" here is trivial (byte complement, fixed-key XOR,
# additive/subtractive shift) and adds no real protection -- it's just
# packaging. This peels those layers in pure Python (no Lua VM needed)
# before running normal obfuscator detection, so `.l` can see straight
# through to the REAL underlying obfuscator (WeAreDevs, etc.) instead of
# reporting "Unknown".

def _peel_extract_balanced(s: str, open_paren_idx: int):
    """s[open_paren_idx] must be '('. Returns (inner_text, idx_after_close)."""
    depth = 0
    for i in range(open_paren_idx, len(s)):
        if s[i] == '(':
            depth += 1
        elif s[i] == ')':
            depth -= 1
            if depth == 0:
                return s[open_paren_idx + 1:i], i + 1
    return None, None


def _peel_one_wrapper_layer(code: str) -> Optional[str]:
    m = re.search(
        r'local\s+function\s+(\w+)\(\)\s*local\s+\w+\s*=\s*"((?:\\\d{1,3})+)"',
        code
    )
    if not m:
        return None
    escaped = m.group(2)
    byte_vals = [int(x) for x in re.findall(r'\\(\d{1,3})', escaped)]
    if len(byte_vals) < 16:
        return None  # too short to be a real wrapped payload

    sc_idx = code.find('string.char(', m.end())
    if sc_idx == -1 or sc_idx - m.end() > 400:
        return None
    inner, _ = _peel_extract_balanced(code, sc_idx + len('string.char'))
    if inner is None or ':byte(' not in inner:
        return None

    norm = re.sub(r'\w+:byte\(\w+\)', 'BYTE', inner).strip()

    def evaluate(b: int) -> Optional[int]:
        if norm == '255-BYTE':
            return 255 - b
        mm = re.search(r'bit32\.bxor\(BYTE,\s*(\d+)\)', norm)
        if mm:
            return b ^ int(mm.group(1))
        mm = re.search(r'^BYTE\s*~\s*(\d+)$', norm)
        if mm:
            return b ^ int(mm.group(1))
        mm = re.search(r'^\(?BYTE\s*-\s*(\d+)\)?', norm)
        if mm:
            return (b - int(mm.group(1))) % 256
        mm = re.search(r'^\(?BYTE\s*\+\s*(\d+)\)?', norm)
        if mm:
            return (b + int(mm.group(1))) % 256
        return None

    out = bytearray()
    for b in byte_vals:
        v = evaluate(b)
        if v is None:
            return None
        out.append(v & 0xFF)
    try:
        return out.decode('utf-8')
    except UnicodeDecodeError:
        return out.decode('utf-8', errors='replace')


def peel_wrapper_layers(code: str, max_layers: int = 6, verbose: bool = False) -> Tuple[str, int]:
    """Repeatedly unwrap simple hub-style byte-encoding layers.
    Returns (possibly-unwrapped code, number of layers peeled)."""
    layers = 0
    cur = code
    for _ in range(max_layers):
        nxt = _peel_one_wrapper_layer(cur)
        if nxt is None:
            break
        layers += 1
        cur = nxt
        if verbose:
            print(f"[*] Peeled wrapper layer {layers} ({len(cur):,} chars)")
    return cur, layers


# ============================================================
# v12: Luau -> standard-Lua syntax transpile (compound assignment)
# ============================================================
# lupa binds to LuaJIT/PUC-Lua, NOT Luau (Roblox's language fork) -- so
# genuine Luau-only syntax (like compound assignment operators, which
# standard Lua has never supported in any version) fails to even LOAD,
# with a generic "syntax error near '+'"-style message that gives no hint
# it's a language-dialect mismatch rather than a real bug. This rewrites
# the small, common subset (`X += Y` etc.) into plain `X = X + (Y)` before
# handing code to the Lua engine. Conservative on purpose: only matches
# when the right-hand side is a single simple token (identifier or
# number), which covers compiled-VM-dispatch-style code; anything with a
# more complex RHS is left alone rather than risk mistranslating it.

_LUAU_COMPOUND_OPS = ['+=', '-=', '//=', '*=', '/=', '..=', '^=', '%=']
_LUAU_COMPOUND_RE = re.compile(
    r'([A-Za-z_]\w*(?:\.[A-Za-z_]\w*|\[[^\[\]]{1,80}\])*)'
    r'\s*(\+=|-=|//=|\*=|/=|\.\.=|\^=|%=)\s*'
    r'([A-Za-z_]\w*|\d+(?:\.\d+)?)'
)


def transpile_luau_compound_ops(code: str) -> Tuple[str, int]:
    if not any(op in code for op in _LUAU_COMPOUND_OPS):
        return code, 0
    count = 0

    def _repl(m):
        nonlocal count
        lhs, op, rhs = m.group(1), m.group(2), m.group(3)
        count += 1
        return f"{lhs} = {lhs} {op[:-1]} ({rhs})"

    new_code = _LUAU_COMPOUND_RE.sub(_repl, code)
    return new_code, count


# ============================================================
# Obfuscator Detector (v5: improved, more types)
# ============================================================

class ObfuscatorDetector:
    # v5: expanded signatures - ordered by specificity (most specific first)
    SIGNATURES = [
        ("Ironveil", ["Ironveil", "ironveil"]),
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
    def _extract_strings(code: str, mode: str = "all") -> List[str]:
        return _extract_lua_strings(code, mode)


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
        lines.append("-- If you expected .NET Moonsec: check Render logs for [!] Moonsec external failed")
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
    def _extract_strings_static(code: str, mode: str = "simple") -> List[str]:
        return _extract_lua_strings(code, mode)

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



# ============================================================
# External deobfuscator tools (Node / optional .NET / Lune)
# Same model as Prometheus: detect -> subprocess -> source
# Missing tool = silent None -> fall back to Python path
# ============================================================

def _tool_roots() -> list:
    """Candidate roots for cloned deobfuscator repos."""
    roots = []
    here = None
    try:
        here = Path(__file__).resolve().parent
    except NameError:
        here = Path.cwd()
    for base in (here, here.parent, Path.cwd(), Path("/home/workdir/artifacts")):
        if base and base not in roots:
            roots.append(base)
    env_root = os.environ.get("DEOBF_TOOLS_DIR")
    if env_root:
        roots.insert(0, Path(env_root))
    return roots


def _find_tool_dir(*relative_parts_options) -> Optional[Path]:
    """Find first existing tool directory among option path tuples."""
    for root in _tool_roots():
        for parts in relative_parts_options:
            cand = root.joinpath(*parts)
            try:
                if cand.is_dir():
                    return cand
            except OSError:
                continue
    return None


def _find_prometheus_dir() -> Optional[Path]:
    env = os.environ.get("PROMETHEUS_DEOBF_DIR") or os.environ.get("PDEOBF_DIR")
    if env and (Path(env) / "bin" / "pdeobf.js").is_file():
        return Path(env)
    return _find_tool_dir(
        ("Prometheus-Deobfuscator",),
        ("tools", "Prometheus-Deobfuscator"),
    )


def _find_ironveil_entry() -> Optional[Path]:
    """Return path to Ironveil deobfuscator/index.js"""
    env = os.environ.get("IRONVEIL_DEOBF_DIR")
    if env:
        p = Path(env)
        for c in (p / "index.js", p / "deobfuscator" / "index.js"):
            if c.is_file():
                return c
    d = _find_tool_dir(
        ("Ironveil-Deobfuscator-V1", "deobfuscator"),
        ("Ironveil-Deobfuscator-V1",),
        ("tools", "Ironveil-Deobfuscator-V1", "deobfuscator"),
    )
    if d is None:
        return None
    for c in (d / "index.js", d / "deobfuscator" / "index.js"):
        if c.is_file():
            return c
    return None


def _find_luaobf_entry() -> Optional[Path]:
    """Return path to LuaObfuscator-Deobfuscator/index.js"""
    env = os.environ.get("LUAOBF_DEOBF_DIR")
    if env and (Path(env) / "index.js").is_file():
        return Path(env) / "index.js"
    d = _find_tool_dir(
        ("LuaObfuscator-Deobfuscator",),
        ("tools", "LuaObfuscator-Deobfuscator"),
    )
    if d and (d / "index.js").is_file():
        return d / "index.js"
    return None


def _find_moonsec_project() -> Optional[Path]:
    """Find MoonsecDeobfuscator.csproj"""
    env = os.environ.get("MOONSEC_DEOBF_DIR")
    roots = []
    if env:
        roots.append(Path(env))
    d = _find_tool_dir(("MoonsecDeobfuscator",), ("tools", "MoonsecDeobfuscator"))
    if d:
        roots.append(d)
    for root in roots:
        for c in (
            root / "MoonsecDeobfuscator.csproj",
            root / "src" / "MoonsecDeobfuscator.csproj",
        ):
            if c.is_file():
                return c
        hits = list(root.glob("**/*.csproj"))
        if hits:
            return hits[0]
    return None


def _find_moonsec_dll() -> Optional[Path]:
    """Find built MoonsecDeobfuscator.dll under bin/."""
    env = os.environ.get("MOONSEC_DEOBF_DIR")
    search_roots = []
    if env:
        search_roots.append(Path(env))
    d = _find_tool_dir(("MoonsecDeobfuscator",), ("tools", "MoonsecDeobfuscator"))
    if d:
        search_roots.append(d)
    for root in search_roots:
        for pattern in (
            "**/MoonsecDeobfuscator.dll",
            "bin/Release/**/MoonsecDeobfuscator.dll",
            "bin/Debug/**/MoonsecDeobfuscator.dll",
        ):
            hits = sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
            if hits:
                return hits[0]
    return None



def _run_node_script(script: Path, args: list, timeout: int = 90, verbose: bool = False, cwd=None) -> Optional[str]:
    """Run node script; prefer reading -o/--out file, else stdout."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        if verbose:
            print("  [!] node not found")
        return None
    if not script or not Path(script).is_file():
        return None
    script = Path(script)
    cmd = [node, str(script)] + list(args)
    if verbose:
        print(f"  [*] node: {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(cwd or script.parent),
        )
    except subprocess.TimeoutExpired:
        if verbose:
            print("  [!] node tool timed out")
        return None
    except Exception as e:
        if verbose:
            print(f"  [!] node tool error: {e}")
        return None
    out_path = None
    for i, a in enumerate(args):
        if a in ("-o", "--out") and i + 1 < len(args):
            out_path = Path(args[i + 1])
            break
    if out_path and out_path.is_file():
        try:
            text = out_path.read_text(encoding="utf-8", errors="replace")
            if text.strip():
                return text
        except OSError:
            pass
    out = (proc.stdout or "").strip()
    out = re.sub(r"\x1b\[[0-9;]*m", "", out)
    if out and len(out) > 20 and "Missing input" not in out:
        return out
    if proc.returncode != 0 and verbose:
        print(f"  [!] node exit {proc.returncode}: {(proc.stderr or '')[:300]}")
    return None


def run_prometheus_deobf(code: str, timeout: int = 90, verbose: bool = False) -> Optional[str]:
    """WeAreDev / Prometheus VM decompiler via Node pdeobf.js."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        if verbose:
            print("  [!] Prometheus: node not found")
        return None
    root = _find_prometheus_dir()
    if root is None:
        if verbose:
            print("  [!] Prometheus: not found (set PROMETHEUS_DEOBF_DIR)")
        return None
    pdeobf = root / "bin" / "pdeobf.js"
    if not pdeobf.is_file():
        if verbose:
            print(f"  [!] Prometheus: missing {pdeobf}")
        return None
    tmp_in = tmp_out = None
    try:
        fd_in, tmp_in = tempfile.mkstemp(suffix=".lua", prefix="wad_in_")
        os.close(fd_in)
        fd_out, tmp_out = tempfile.mkstemp(suffix=".lua", prefix="wad_out_")
        os.close(fd_out)
        Path(tmp_in).write_text(code, encoding="utf-8", errors="replace")
        cmd = [node, str(pdeobf), tmp_in, "-o", tmp_out]
        if verbose:
            print(f"  [*] Prometheus: {' '.join(cmd)}")
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(root))
        out = Path(tmp_out).read_text(encoding="utf-8", errors="replace") if Path(tmp_out).is_file() else ""
        if not out.strip() or len(out.strip()) < 20:
            if verbose:
                print("  [!] Prometheus empty output")
            return None
        if verbose:
            print(f"  [+] Prometheus recovered {len(out):,} chars")
        return out
    except Exception as e:
        if verbose:
            print(f"  [!] Prometheus error: {e}")
        return None
    finally:
        for p in (tmp_in, tmp_out):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


def run_ironveil_deobf(code: str, timeout: int = 90, verbose: bool = False) -> Optional[str]:
    entry = _find_ironveil_entry()
    if not entry:
        if verbose:
            print("  [!] Ironveil: not found")
        return None
    tmp_in = tmp_out = None
    try:
        fd_in, tmp_in = tempfile.mkstemp(suffix=".lua", prefix="iv_in_")
        os.close(fd_in)
        fd_out, tmp_out = tempfile.mkstemp(suffix=".lua", prefix="iv_out_")
        os.close(fd_out)
        Path(tmp_in).write_text(code, encoding="utf-8", errors="replace")
        out = _run_node_script(entry, [tmp_in, tmp_out], timeout=timeout, verbose=verbose, cwd=entry.parent)
        if out and len(out.strip()) > 20:
            if verbose:
                print(f"  [+] Ironveil recovered {len(out):,} chars")
            return out
        if Path(tmp_out).is_file():
            t = Path(tmp_out).read_text(encoding="utf-8", errors="replace")
            if t.strip():
                return t
        return None
    except Exception as e:
        if verbose:
            print(f"  [!] Ironveil error: {e}")
        return None
    finally:
        for p in (tmp_in, tmp_out):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


def run_luaobfuscator_deobf(code: str, timeout: int = 90, verbose: bool = False) -> Optional[str]:
    entry = _find_luaobf_entry()
    if not entry:
        if verbose:
            print("  [!] LuaObfuscator tool: not found")
        return None
    tmp_in = tmp_out = None
    try:
        fd_in, tmp_in = tempfile.mkstemp(suffix=".lua", prefix="lo_in_")
        os.close(fd_in)
        fd_out, tmp_out = tempfile.mkstemp(suffix=".lua", prefix="lo_out_")
        os.close(fd_out)
        Path(tmp_in).write_text(code, encoding="utf-8", errors="replace")
        out = _run_node_script(
            entry, [tmp_in, "--out", tmp_out, "--stdout"],
            timeout=timeout, verbose=verbose, cwd=entry.parent,
        )
        if out and len(out.strip()) > 20:
            if verbose:
                print(f"  [+] LuaObfuscator tool recovered {len(out):,} chars")
            return out
        if Path(tmp_out).is_file():
            t = Path(tmp_out).read_text(encoding="utf-8", errors="replace")
            if t.strip():
                return t
        return None
    except Exception as e:
        if verbose:
            print(f"  [!] LuaObfuscator tool error: {e}")
        return None
    finally:
        for p in (tmp_in, tmp_out):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


def run_luraph_vmp_deobf(code: str, timeout: int = 180, verbose: bool = False) -> Optional[str]:
    """Optional Luraph via luauvmp CLI."""
    import shutil
    import subprocess
    cli = shutil.which("luauvmp")
    if not cli:
        env = os.environ.get("LUAU_VMP_DIR")
        if env and (Path(env) / "luauvmp").is_file():
            cli = str(Path(env) / "luauvmp")
    if not cli:
        if verbose:
            print("  [!] luauvmp not found")
        return None
    tmp_dir = tempfile.mkdtemp(prefix="luraph_")
    tmp_in = None
    try:
        fd_in, tmp_in = tempfile.mkstemp(suffix=".lua", prefix="lr_in_")
        os.close(fd_in)
        Path(tmp_in).write_text(code, encoding="utf-8", errors="replace")
        out_dir = Path(tmp_dir) / "recovered"
        cmd = [cli, "luraph-full", tmp_in, "-o", str(out_dir), "--no-lua-expert", "--force"]
        if verbose:
            print(f"  [*] Luraph-VMP: {' '.join(cmd)}")
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        for name in ("program.luaexpert.luau", "program.decompiled.luau", "program.pseudo.lua", "embedded_main.luau"):
            p = out_dir / name
            if p.is_file() and p.stat().st_size > 20:
                return p.read_text(encoding="utf-8", errors="replace")
        return None
    except Exception as e:
        if verbose:
            print(f"  [!] Luraph-VMP error: {e}")
        return None
    finally:
        if tmp_in and os.path.exists(tmp_in):
            try:
                os.unlink(tmp_in)
            except OSError:
                pass
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def run_moonsec_deobf(code: str, timeout: int = 180, verbose: bool = False) -> Optional[str]:
    """MoonSec V3 via `dotnet run --project` (preferred) or built DLL."""
    import shutil
    import subprocess
    dotnet = shutil.which("dotnet")
    if not dotnet:
        # common install locations from dotnet-install.sh
        for cand in (
            Path.home() / ".dotnet" / "dotnet",
            Path("/root/.dotnet/dotnet"),
            Path(os.environ.get("DOTNET_ROOT", "")) / "dotnet",
        ):
            if cand.is_file():
                dotnet = str(cand)
                break
    if not dotnet:
        if verbose:
            print("  [!] Moonsec: dotnet not found (install .NET 9 + set PATH)")
        return None

    proj = _find_moonsec_project()
    dll = _find_moonsec_dll()
    if not proj and not dll:
        if verbose:
            print("  [!] Moonsec: project/dll not found (set MOONSEC_DEOBF_DIR)")
        return None

    tmp_in = tmp_out = None
    try:
        fd_in, tmp_in = tempfile.mkstemp(suffix=".lua", prefix="ms_in_")
        os.close(fd_in)
        fd_out, tmp_out = tempfile.mkstemp(suffix=".txt", prefix="ms_out_")
        os.close(fd_out)
        Path(tmp_in).write_text(code, encoding="utf-8", errors="replace")

        env = os.environ.copy()
        env.setdefault("DOTNET_ROOT", str(Path.home() / ".dotnet"))
        env["PATH"] = env.get("DOTNET_ROOT", "") + os.pathsep + env.get("PATH", "")
        env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
        env["DOTNET_NOLOGO"] = "1"

        cmds = []
        if proj:
            # dotnet run --project X.csproj -c Release -- -dis -i in -o out
            cmds.append([
                dotnet, "run", "--project", str(proj), "-c", "Release", "--no-build",
                "--", "-dis", "-i", tmp_in, "-o", tmp_out,
            ])
            # if --no-build fails (never built), allow build
            cmds.append([
                dotnet, "run", "--project", str(proj), "-c", "Release",
                "--", "-dis", "-i", tmp_in, "-o", tmp_out,
            ])
        if dll:
            cmds.append([dotnet, str(dll), "-dis", "-i", tmp_in, "-o", tmp_out])

        last_err = ""
        for cmd in cmds:
            if verbose:
                print(f"  [*] Moonsec: {' '.join(cmd)}")
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout, env=env,
                    cwd=str(proj.parent) if proj else None,
                )
                last_err = (proc.stderr or proc.stdout or "")[:400]
            except subprocess.TimeoutExpired:
                last_err = "timeout"
                continue
            except Exception as e:
                last_err = str(e)
                continue

            if Path(tmp_out).is_file():
                t = Path(tmp_out).read_text(encoding="utf-8", errors="replace")
                if t.strip() and len(t.strip()) > 30:
                    if verbose:
                        print(f"  [+] Moonsec recovered {len(t):,} chars")
                    return "-- MoonSec V3 (dotnet deobfuscator)\n" + t
            # clear empty out for next try
            try:
                if Path(tmp_out).is_file():
                    Path(tmp_out).write_text("")
            except OSError:
                pass

        if verbose:
            print(f"  [!] Moonsec failed: {last_err}")
        return None
    except Exception as e:
        if verbose:
            print(f"  [!] Moonsec error: {e}")
        return None
    finally:
        for p in (tmp_in, tmp_out):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass



def run_luraph_vmp_deobf(code: str, timeout: int = 180, verbose: bool = False) -> Optional[str]:
    """Optional: luau-vmp-deobf (Luraph v14). Needs luauvmp + preferably Lune."""
    import shutil
    import subprocess
    cli = _find_luauvmp()
    if not cli:
        if verbose:
            print("  [!] luauvmp not found (pip install -e luau-vmp-deobf + Lune)")
        return None
    tmp_dir = tempfile.mkdtemp(prefix="luraph_")
    tmp_in = None
    try:
        fd_in, tmp_in = tempfile.mkstemp(suffix=".lua", prefix="lr_in_")
        os.close(fd_in)
        Path(tmp_in).write_text(code, encoding="utf-8", errors="replace")
        out_dir = Path(tmp_dir) / "recovered"
        cmd = [cli, "luraph-full", tmp_in, "-o", str(out_dir), "--no-lua-expert", "--force"]
        if verbose:
            print(f"  [*] Luraph-VMP: {' '.join(cmd)}")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        # Prefer readable outputs
        for name in (
            "program.luaexpert.luau",
            "program.decompiled.luau",
            "program.pseudo.lua",
            "embedded_main.luau",
        ):
            p = out_dir / name
            if p.is_file() and p.stat().st_size > 20:
                t = p.read_text(encoding="utf-8", errors="replace")
                if verbose:
                    print(f"  [+] Luraph-VMP recovered {name} ({len(t):,} chars)")
                return t
        # any .luau/.lua under out
        if out_dir.is_dir():
            for p in sorted(out_dir.rglob("*.luau")) + sorted(out_dir.rglob("*.lua")):
                if p.stat().st_size > 40:
                    return p.read_text(encoding="utf-8", errors="replace")
        if verbose:
            print(f"  [!] Luraph-VMP no output (exit {proc.returncode})")
        return None
    except Exception as e:
        if verbose:
            print(f"  [!] Luraph-VMP error: {e}")
        return None
    finally:
        if tmp_in and os.path.exists(tmp_in):
            try:
                os.unlink(tmp_in)
            except OSError:
                pass
        import shutil as _sh
        try:
            _sh.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def run_external_deobf_for_detected(detected: Optional[str], code: str, verbose: bool = False) -> Optional[Tuple[str, str]]:
    """
    Try external tools based on detected obfuscator name.
    Returns (source, method_label) or None.
    """
    d = (detected or "").lower()

    # WeAreDev / Prometheus obfuscator family
    if "wearedev" in d or d == "prometheus" or "psu" in d:
        out = run_prometheus_deobf(code, verbose=verbose)
        if out:
            return out, "Prometheus-Deobfuscator (Node)"

    # Ironveil (and sometimes mislabeled ironbrew-like)
    if "ironveil" in d or "iron veil" in d:
        out = run_ironveil_deobf(code, verbose=verbose)
        if out:
            return out, "Ironveil-Deobfuscator (Node)"

    # LuaObfuscator.com / Ferib
    if "luaobfuscator" in d or "ferib" in d:
        out = run_luaobfuscator_deobf(code, verbose=verbose)
        if out:
            return out, "LuaObfuscator-Deobfuscator (Node)"

    # MoonSec
    if "moonsec" in d or "moon sec" in d:
        out = run_moonsec_deobf(code, verbose=verbose)
        if out:
            return out, "MoonsecDeobfuscator (.NET)"

    # Luraph
    if "luraph" in d:
        out = run_luraph_vmp_deobf(code, verbose=verbose)
        if out:
            return out, "luau-vmp-deobf (Luraph)"

    return None


class WeAreDevDeobfuscator:
    """WeAreDev v1.0.0 decompiler - v5.6 with arg-trace + CFF block extraction, enhanced tracer,
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
        # --- Phase 0: Prometheus VM decompiler (Node) — best quality for WeAreDev ---
        prom = run_prometheus_deobf(code, timeout=90, verbose=verbose)
        if prom:
            meta = {
                "method": "Prometheus-Deobfuscator (Node VM lift)",
                "p_entries": 0,
                "strings_decoded": 0,
                "print_count": 0,
                "trace_entries": 0,
            }
            return prom, meta

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

    _TRACER_LUA = 'local _trace = {}\nlocal _trace_n = 0\nlocal _orig_print = print\nlocal _hb = 0\nlocal _HB_MAX = 5\nlocal _inst_n = 0\n\nif not _G.unpack then _G.unpack = table.unpack end\nif not getfenv then getfenv = function() return _G end end\nif not setfenv then setfenv = function() end end\nif not newproxy then newproxy = function(u) local t={} if u then setmetatable(t,{}) end return t end end\n\nlocal bit32 = {}\nlocal function U(x) x=x or 0; if x<0 then x=x+4294967296 end; return x%4294967296 end\nfunction bit32.bxor(a,b) a,b=U(a),U(b);local r,p=0,1;for i=0,31 do local ba,bb=a%2,b%2;if ba~=bb then r=r+p end;a=(a-ba)/2;b=(b-bb)/2;p=p*2 end;return r end\nfunction bit32.band(a,b) a,b=U(a),U(b);local r,p=0,1;for i=0,31 do local ba,bb=a%2,b%2;if ba==1 and bb==1 then r=r+p end;a=(a-ba)/2;b=(b-bb)/2;p=p*2 end;return r end\nfunction bit32.bor(a,b) a,b=U(a),U(b);local r,p=0,1;for i=0,31 do local ba,bb=a%2,b%2;if ba==1 or bb==1 then r=r+p end;a=(a-ba)/2;b=(b-bb)/2;p=p*2 end;return r end\nfunction bit32.bnot(a) return 4294967295-U(a) end\nfunction bit32.lshift(a,n) a=U(a);n=(n or 0)%32;if n<0 then n=n+32 end;return (a*(2^n))%4294967296 end\nfunction bit32.rshift(a,n) a=U(a);n=(n or 0)%32;if n<0 then n=n+32 end;return math.floor(a/(2^n)) end\n_G.bit32 = bit32\n\nlocal function T(entry)\n    _trace_n = _trace_n + 1\n    if _trace_n > 4000 then return end\n    _trace[_trace_n] = entry\n    _orig_print("[T]" .. entry)\nend\n\nlocal function path_of(v)\n    if type(v) ~= "table" then return tostring(v) end\n    local mt = getmetatable(v)\n    if mt and mt.__path then return mt.__path end\n    if mt and mt.__tostring then return tostring(v) end\n    return "{}"\nend\n\nlocal function fmt_arg(v)\n    local t = type(v)\n    if t == "string" then return string.format("%q", v) end\n    if t == "number" then\n        if v ~= v then return "nan" end\n        if v == math.floor(v) and math.abs(v) < 1e12 then return string.format("%d", v) end\n        return tostring(v)\n    end\n    if t == "boolean" or t == "nil" then return tostring(v) end\n    if t == "function" then return "function" end\n    if t == "table" then return path_of(v) end\n    return t\nend\n\nlocal function vec3(x,y,z)\n    local o = {X = x or 0, Y = y or 0, Z = z or 0}\n    local mt = {}\n    mt.__path = string.format("Vector3.new(%.4g, %.4g, %.4g)", o.X, o.Y, o.Z)\n    mt.__tostring = function() return mt.__path end\n    mt.__add = function(a,b) return vec3((a.X or 0)+(b.X or 0),(a.Y or 0)+(b.Y or 0),(a.Z or 0)+(b.Z or 0)) end\n    mt.__sub = function(a,b) return vec3((a.X or 0)-(b.X or 0),(a.Y or 0)-(b.Y or 0),(a.Z or 0)-(b.Z or 0)) end\n    mt.__mul = function(a,b)\n        if type(b)=="number" then return vec3(a.X*b,a.Y*b,a.Z*b) end\n        if type(a)=="number" then return vec3(b.X*a,b.Y*a,b.Z*a) end\n        return vec3(0,0,0)\n    end\n    mt.__div = function(a,b) if type(b)=="number" and b~=0 then return vec3(a.X/b,a.Y/b,a.Z/b) end return vec3(0,0,0) end\n    mt.__unm = function(a) return vec3(-a.X,-a.Y,-a.Z) end\n    mt.__index = function(t,k)\n        if k=="Magnitude" then return math.sqrt(t.X*t.X+t.Y*t.Y+t.Z*t.Z) end\n        if k=="Unit" then local m=math.sqrt(t.X*t.X+t.Y*t.Y+t.Z*t.Z); if m==0 then return vec3(0,0,0) end; return vec3(t.X/m,t.Y/m,t.Z/m) end\n        return rawget(t,k)\n    end\n    return setmetatable(o, mt)\nend\n\nlocal function cf(x,y,z)\n    local o = {X = x or 0, Y = y or 0, Z = z or 0}\n    local mt = {}\n    mt.__path = string.format("CFrame.new(%.4g, %.4g, %.4g)", o.X, o.Y, o.Z)\n    mt.__tostring = function() return mt.__path end\n    mt.__add = function(a,b) return cf(a.X+(b.X or 0),a.Y+(b.Y or 0),a.Z+(b.Z or 0)) end\n    mt.__mul = function(a,b) if type(b)=="table" and b.X then return cf(a.X+b.X,a.Y+b.Y,a.Z+b.Z) end return a end\n    mt.__index = function(t,k)\n        if k=="Position" or k=="p" then return vec3(t.X,t.Y,t.Z) end\n        if k=="LookVector" then return vec3(0,0,-1) end\n        if k=="RightVector" then return vec3(1,0,0) end\n        if k=="UpVector" then return vec3(0,1,0) end\n        return rawget(t,k)\n    end\n    return setmetatable(o, mt)\nend\n\nlocal NUM_KEYS = {\n    Health=100, MaxHealth=100, WalkSpeed=16, JumpPower=50, JumpHeight=7.2,\n    Transparency=0, BackgroundTransparency=0, TextSize=14, ZIndex=1,\n    LayoutOrder=0, Rotation=0, UserId=1, Volume=1, PlaybackSpeed=1,\n}\n\nlocal function make_tracer(path)\n    local props = {}\n    local obj = {}\n    local mt = { __path = path }\n    mt.__tostring = function() return path end\n    mt.__len = function() return 2 end\n    mt.__call = function(self, ...)\n        local n = select("#", ...)\n        local args = {...}\n        local as = {}\n        for i=1,n do as[i] = fmt_arg(args[i]) end\n        local is_hb = path:find("Heartbeat") or path:find("RenderStepped")\n        if is_hb then\n            _hb = _hb + 1\n            if _hb > _HB_MAX then return make_tracer(path .. "_ret") end\n        end\n        T(path .. "(" .. table.concat(as, ", ") .. ")")\n\n        if path:match("GetService$") and type(args[1]) == "string" then\n            return make_tracer("game.GetService(" .. args[1] .. ")")\n        end\n        if path == "Instance.new" or path:find("Instance%.new") then\n            local cls = args[1]\n            if type(cls) ~= "string" and type(args[2]) == "string" then cls = args[2] end\n            cls = tostring(cls or "?")\n            _inst_n = _inst_n + 1\n            local inst = make_tracer("Instance<" .. cls .. "#" .. _inst_n .. ">")\n            return inst\n        end\n        if path:find("WaitForChild") or path:find("FindFirstChild") or path:find("FindFirstChildOfClass") or path:find("FindFirstChildWhichIsA") then\n            return make_tracer(path_of(self) .. "[" .. tostring(args[1] or "?") .. "]")\n        end\n        if path:find("GetPlayers") then\n            local list = { make_tracer("Player1"), make_tracer("Player2") }\n            setmetatable(list, {\n                __path = "GetPlayers()",\n                __len = function() return 2 end,\n                __index = function(_,k)\n                    if type(k)=="number" then return list[k] or make_tracer("Player["..k.."]") end\n                    return make_tracer("GetPlayers()."..tostring(k))\n                end,\n            })\n            return list\n        end\n        if path:find("GetChildren") or path:find("GetDescendants") then\n            return { make_tracer(path_of(self)..".C1"), make_tracer(path_of(self)..".C2") }\n        end\n        if path:find("IsA") then return true end\n        if path:find("fromRGB") or path:find("fromHSV") then\n            return make_tracer("Color3(" .. table.concat(as, ",") .. ")")\n        end\n        if path:find("UDim2%.new") or path == "UDim2.new" then\n            return make_tracer("UDim2(" .. table.concat(as, ",") .. ")")\n        end\n        if path:find("UDim%.new") or path == "UDim.new" then\n            return make_tracer("UDim(" .. table.concat(as, ",") .. ")")\n        end\n        if path:find("Vector3%.new") or path == "Vector3.new" then\n            return vec3(tonumber(args[1]) or 0, tonumber(args[2]) or 0, tonumber(args[3]) or 0)\n        end\n        if path:find("CFrame%.new") or path == "CFrame.new" then\n            return cf(tonumber(args[1]) or 0, tonumber(args[2]) or 0, tonumber(args[3]) or 0)\n        end\n        if path:find("HttpGet") or path:find("HttpPost") then\n            T("HTTP " .. tostring(args[1]))\n            return "--http-body"\n        end\n        if path:find("GetState") then return make_tracer("Enum.HumanoidStateType.Running") end\n        return make_tracer(path .. "_ret")\n    end\n    mt.__index = function(t, k)\n        local key = tostring(k)\n        if props[key] ~= nil then return props[key] end\n        if NUM_KEYS[key] ~= nil then return NUM_KEYS[key] end\n        if key == "Position" then return vec3(0,5,0) end\n        if key == "Size" then return vec3(2,2,1) end\n        if key == "CFrame" then return cf(0,5,0) end\n        if key == "Velocity" or key == "AssemblyLinearVelocity" then return vec3(0,0,0) end\n        if key == "LookVector" then return vec3(0,0,-1) end\n        if key == "Character" then return make_tracer(path .. ".Character") end\n        if key == "LocalPlayer" then return make_tracer(path .. ".LocalPlayer") end\n        if key == "Humanoid" then return make_tracer(path .. ".Humanoid") end\n        if key == "HumanoidRootPart" then return make_tracer(path .. ".HumanoidRootPart") end\n        if key == "Parent" then return make_tracer(path .. ".Parent") end\n        if key == "Animation" then return make_tracer(path .. ".Animation") end\n        if key == "AnimationId" then return "rbxassetid://0" end\n        if key == "Connect" or key == "connect" or key == "Once" then\n            return function(ev, fn)\n                T(path .. ":Connect()")\n                if type(fn) == "function" then\n                    if path:find("Heartbeat") or path:find("RenderStepped") then\n                        if _hb < _HB_MAX then\n                            _hb = _hb + 1\n                            local ok, err = pcall(fn, 0.016)\n                            if not ok then T("HANDLER_ERR " .. tostring(err):sub(1,160)) end\n                        end\n                    else\n                        local ok, err = pcall(fn, make_tracer(path .. ":arg"))\n                        if not ok then T("HANDLER_ERR " .. tostring(err):sub(1,160)) end\n                    end\n                end\n                return make_tracer("Connection")\n            end\n        end\n        if key == "Wait" then\n            return function() return 0.016 end\n        end\n        if key == "TweenPosition" or key == "TweenSize" or key == "Play" or key == "Stop" or key == "Destroy" or key == "FireServer" or key == "InvokeServer" or key == "ChangeState" or key == "Move" or key == "MoveTo" or key == "GetState" then\n            return function(self2, ...)\n                local n = select("#", ...)\n                local args = {...}\n                local as = {}\n                for i=1,n do as[i] = fmt_arg(args[i]) end\n                T(path .. "." .. key .. "(" .. table.concat(as, ", ") .. ")")\n                if key == "GetState" then return make_tracer("Enum.HumanoidStateType.Running") end\n                return make_tracer("Ret")\n            end\n        end\n        if key == "GetPropertyChangedSignal" then\n            return function(_, name) return make_tracer(path .. ".Signal[" .. tostring(name) .. "]") end\n        end\n        if _hb <= _HB_MAX then\n            T(path .. "." .. key)\n        end\n        return make_tracer(path .. "." .. key)\n    end\n    mt.__newindex = function(t, k, v)\n        props[tostring(k)] = v\n        T(path .. "." .. tostring(k) .. " = " .. fmt_arg(v))\n    end\n    for _, op in ipairs({"__add","__sub","__mul","__div","__mod","__pow","__unm","__lt","__le","__eq","__concat"}) do\n        mt[op] = function(a, b)\n            local na = type(a)=="number" and a or 0\n            local nb = type(b)=="number" and b or 0\n            if op=="__add" then return na+nb end\n            if op=="__sub" then return na-nb end\n            if op=="__mul" then return na*nb end\n            if op=="__div" then return nb~=0 and na/nb or 0 end\n            if op=="__mod" then return nb~=0 and na%nb or 0 end\n            if op=="__pow" then return na^nb end\n            if op=="__unm" then return -na end\n            if op=="__lt" then return na<nb end\n            if op=="__le" then return na<=nb end\n            if op=="__eq" then return rawequal(a,b) end\n            if op=="__concat" then return tostring(a)..tostring(b) end\n            return 0\n        end\n    end\n    return setmetatable(obj, mt)\nend\n\n_G.print = function(...)\n    local args = {...}\n    local strs = {}\n    for i=1, select("#", ...) do strs[i] = tostring(args[i]) end\n    _orig_print("[P]" .. table.concat(strs, "\\t"))\nend\n_G.warn = _G.print\n\nlocal rl = loadstring or load\n_G.loadstring = function(src, ...)\n    if type(src) == "string" and #src > 8 then\n        T("LOADSTRING len=" .. #src)\n    end\n    if type(src) ~= "string" and type(src) ~= "function" then return nil, "bad" end\n    return rl(src, ...)\nend\n_G.load = _G.loadstring\n\n_G.Instance = {\n    new = function(cls, parent)\n        _inst_n = _inst_n + 1\n        local name = tostring(cls)\n        T(\'Instance.new("\' .. name .. \'")\')\n        local inst = make_tracer("Instance<" .. name .. "#" .. _inst_n .. ">")\n        if parent ~= nil then\n            T("Instance<" .. name .. "#" .. _inst_n .. ">.Parent = " .. path_of(parent))\n        end\n        return inst\n    end\n}\nsetmetatable(_G.Instance, { __path = "Instance", __index = function(_,k) return make_tracer("Instance." .. tostring(k)) end })\n\n_G.game = make_tracer("game")\n_G.workspace = make_tracer("workspace")\n_G.Workspace = _G.workspace\n_G.Enum = make_tracer("Enum")\n_G.Color3 = make_tracer("Color3")\n_G.UDim2 = make_tracer("UDim2")\n_G.UDim = make_tracer("UDim")\n_G.Vector3 = { new = function(x,y,z) T("Vector3.new("..tostring(x)..", "..tostring(y)..", "..tostring(z)..")"); return vec3(x,y,z) end }\nsetmetatable(_G.Vector3, { __path="Vector3", __index=function(_,k) return make_tracer("Vector3."..tostring(k)) end })\n_G.Vector2 = make_tracer("Vector2")\n_G.CFrame = { new = function(x,y,z) T("CFrame.new("..tostring(x)..", "..tostring(y)..", "..tostring(z)..")"); return cf(x,y,z) end }\nsetmetatable(_G.CFrame, { __path="CFrame", __index=function(_,k) return make_tracer("CFrame."..tostring(k)) end })\n_G.TweenInfo = make_tracer("TweenInfo")\n_G.BrickColor = make_tracer("BrickColor")\n_G.Ray = make_tracer("Ray")\n_G.Region3 = make_tracer("Region3")\n_G.NumberRange = make_tracer("NumberRange")\n_G.NumberSequence = make_tracer("NumberSequence")\n_G.ColorSequence = make_tracer("ColorSequence")\n_G.Rect = make_tracer("Rect")\n_G.Font = make_tracer("Font")\n_G.RaycastParams = make_tracer("RaycastParams")\n_G.PhysicalProperties = make_tracer("PhysicalProperties")\n_G.shared = {}\n_G.script = make_tracer("script")\n_G._G = _G\n\n_G.task = {\n    wait = function(n) T("task.wait(" .. tostring(n) .. ")") end,\n    spawn = function(fn)\n        T("task.spawn()")\n        if type(fn)=="function" then local ok,e=pcall(fn); if not ok then T("task.spawn err: "..tostring(e):sub(1,160)) end end\n    end,\n    defer = function(fn) if type(fn)=="function" then pcall(fn) end end,\n    delay = function(n, fn) T("task.delay("..tostring(n)..")"); if type(fn)=="function" then pcall(fn) end end,\n}\n_G.wait = function(n) T("wait("..tostring(n)..")") end\n_G.spawn = function(fn) if type(fn)=="function" then pcall(fn) end end\n_G.tick = function() return os.clock() end\n_G.time = function() return os.clock() end\n_G.require = function(m) T("require("..tostring(m)..")"); return make_tracer("Module") end\n\n_orig_print("[STUBS_OK]")\n'

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
            lines.append('-- [[ Deobfuscated by Lua Deobfuscator Bot v5.6 ]]')
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
        # v12: auto-peel simple "hub"-style wrapper layers (byte complement/
        # XOR/shift encoding) before detection, so a WeAreDevs (etc.) payload
        # re-wrapped by a redistribution hub is seen for what it really is
        # instead of reporting "Unknown".
        code, peeled_layers = peel_wrapper_layers(code, verbose=self.verbose)
        if self.verbose and peeled_layers:
            print(f"[*] Auto-unwrapped {peeled_layers} hub-style layer(s)")

        # v12: fix Luau-only compound-assignment syntax (`+=` etc.) that
        # would otherwise fail to even parse under lupa's Lua engine
        # (LuaJIT/PUC-Lua do not support this Luau-only syntax).
        code, n_transpiled = transpile_luau_compound_ops(code)
        if self.verbose and n_transpiled:
            print(f"[*] Transpiled {n_transpiled} Luau compound-assignment op(s) to standard Lua")

        detected = ObfuscatorDetector.detect(code)
        if self.verbose:
            print(f"[*] File: {name}")
            print(f"[*] Size: {len(code):,} chars")
            print(f"[*] Detected: {detected or 'Unknown'}")

        source = None
        meta = {"detected": detected}
        obf_name = detected or "Unknown"
        prints = []

        # External tools first (Prometheus / Ironveil / LuaObfuscator / Moonsec / Luraph)
        try:
            # Always verbose for MoonSec so Render logs show why dotnet path failed
            ext_verbose = self.verbose or (
                detected is not None and "moonsec" in detected.lower()
            )
            ext = run_external_deobf_for_detected(detected, code, verbose=ext_verbose)
            if ext:
                source, method = ext
                meta["method"] = method
                obf_name = detected or method
                print(f"[+] External tool OK: {method}")
                return obf_name, source, meta
            else:
                # Record diagnostics for Discord message
                det_l = (detected or "").lower()
                if "moonsec" in det_l:
                    import shutil
                    meta["external_error"] = (
                        f"Moonsec external failed | dotnet={shutil.which('dotnet') or os.environ.get('DOTNET_ROOT')} "
                        f"| project={_find_moonsec_project()} | dll={_find_moonsec_dll()} "
                        f"| MOONSEC_DEOBF_DIR={os.environ.get('MOONSEC_DEOBF_DIR')}"
                    )
                    print(f"[!] {meta['external_error']}")
        except Exception as e:
            print(f"[!] External tool error: {e}")
            meta["external_error"] = str(e)

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
import asyncio
import aiohttp
import requests

TOKEN = os.environ.get("DISCORD_TOKEN")
COMMAND_PREFIX = "."
DISCORD_MSG_LIMIT = 1900
MAX_FETCH_BYTES = 5 * 1024 * 1024

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
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

deobfuscator = LuaDeobfuscator(verbose=False)


WAN_BANNER = """--[[ 
█░░░█ █▀█ █▄░█
▀▄▀▄▀ █▀█ █░▀█

   WAN DEOBFUSCATOR 
  
]]
"""


def upload_to_pastefy(content: str, title: str = "WAN DEOBFUSCATOR") -> Optional[str]:
    url = "https://pastefy.app/api/v2/paste"
    payload = {
        "title": title,
        "content": content,
        "type": "PASTE",
        "visibility": "UNLISTED",
    }
    try:
        response = requests.post(url, json=payload, timeout=60)
        response.raise_for_status()
        data = response.json()
        paste_obj = data.get("paste", {})
        paste_id = paste_obj.get("id") if isinstance(paste_obj, dict) else data.get("id")
        if paste_id:
            return f"https://pastefy.app/{paste_id}/raw"
    except Exception as e:
        print(f"[!] Pastefy error: {e}")
    return None


def upload_to_rubis(content: str, title: str = "WAN DEOBFUSCATOR") -> Optional[str]:
    """Upload to Rubis; return only the raw URL."""
    url = "https://api.rubis.app/v2/scrap"
    params = {"public": "true", "accessKey": "true", "title": title}
    headers = {"accept": "application/json", "Content-Type": "text/plain"}
    try:
        response = requests.post(
            url, params=params, headers=headers,
            data=content.encode("utf-8"), timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        raw = data.get("raw")
        if isinstance(raw, str) and raw.startswith("http"):
            return raw
        scrap_id = data.get("scrapID")
        if scrap_id:
            return f"https://api.rubis.app/v2/scrap/{scrap_id}/raw"
    except Exception as e:
        print(f"[!] Rubis error: {e}")
    return None


def with_wan_banner(source: str) -> str:
    s = (source or "").lstrip()
    if s.startswith("--[[") and "WAN DEOBFUSCATOR" in s[:200]:
        return source
    return WAN_BANNER + "\n" + (source or "")


def _is_url(text: str) -> bool:
    text = (text or "").strip().strip("<>")
    return bool(re.match(
        r'^(?:http|ftp)s?://'
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|'
        r'localhost|'
        r'\d{1,3}(?:\.\d{1,3}){3}|'
        r'\[?[A-F0-9]*:[A-F0-9:]+\]?)'
        r'(?::\d+)?'
        r'(?:/?|[/?]\S+)$',
        text, re.IGNORECASE,
    ))


def _normalize_raw_url(link: str) -> str:
    link = link.strip().strip("<>")
    if "github.com" in link and "/blob/" in link:
        link = link.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    if "gist.github.com/" in link and "gist.githubusercontent.com" not in link:
        link = link.replace("gist.github.com/", "gist.githubusercontent.com/")
        if not link.rstrip("/").endswith("/raw"):
            link = link.rstrip("/") + "/raw"
    m = re.match(r'https?://(?:www\.)?pastebin\.com/(?!raw/)([A-Za-z0-9]+)/?$', link)
    if m:
        link = f"https://pastebin.com/raw/{m.group(1)}"
    return link


def _clean_url_arg(text: str) -> str:
    """Strip Discord markdown / embeds noise so .l matches .get URL handling."""
    if not text:
        return ""
    t = text.strip()
    # Discord often wraps links as <https://...>
    t = t.strip("<>").strip()
    # If user pasted extra text, pull first http(s) URL
    m = re.search(r'https?://[^\s<>]+', t)
    if m:
        t = m.group(0)
    # trailing punctuation from chat
    t = t.rstrip(').,;]\'"')
    return t.strip()


async def _http_get_text(url: str) -> str:
    """Download full body as text — same reliability as simple .get sample."""
    url = _normalize_raw_url(_clean_url_arg(url) if url else url)
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("That doesn't look like a valid link.")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
    }
    timeout = aiohttp.ClientTimeout(total=180, connect=30, sock_read=120)
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=timeout, allow_redirects=True) as resp:
                if resp.status != 200:
                    raise ValueError(f"Link returned HTTP {resp.status}.")
                # Full body — do NOT truncate
                text = await resp.text(errors="replace")
    except aiohttp.ClientError as e:
        raise ValueError(f"Failed to download link: {type(e).__name__}: {e}") from e
    except asyncio.TimeoutError as e:
        raise ValueError("Link download timed out (180s).") from e
    if text is None:
        raise ValueError("Link returned empty body.")
    if len(text.encode("utf-8", errors="replace")) > MAX_FETCH_BYTES:
        raise ValueError(f"File too large. Max is {MAX_FETCH_BYTES:,} bytes.")
    return text


async def _send_text_content(ctx, content: str, filename: str = "code.txt", edit_msg=None):
    """Send full text: code block if short, otherwise Discord file (BytesIO — no truncation)."""
    if content is None:
        content = ""
    if len(content) <= DISCORD_MSG_LIMIT:
        body = f"```\n{content}\n```"
        if edit_msg is not None:
            await edit_msg.edit(content=body)
        else:
            await ctx.reply(body)
        return
    data = io.BytesIO(content.encode("utf-8", errors="replace"))
    file = discord.File(data, filename=filename)
    if edit_msg is not None:
        await edit_msg.edit(content=f"Fetched **{len(content):,}** chars:")
        await ctx.send(file=file)
    else:
        await ctx.reply(file=file)



class UploadChoiceView(discord.ui.View):
    def __init__(self, content: str, title: str = "WAN DEOBFUSCATOR", timeout: float = 120):
        super().__init__(timeout=timeout)
        self.content = content
        self.title = title

    @discord.ui.button(label="Pastefy", style=discord.ButtonStyle.danger)
    async def pastefy_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        link = await asyncio.to_thread(upload_to_pastefy, self.content, self.title)
        if link:
            await interaction.followup.send(f"[click]({link})", ephemeral=True)
        else:
            await interaction.followup.send("Upload Pastefy failed.", ephemeral=True)

    @discord.ui.button(label="Rubis", style=discord.ButtonStyle.success)
    async def rubis_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        link = await asyncio.to_thread(upload_to_rubis, self.content, self.title)
        if link:
            await interaction.followup.send(f"[click]({link})", ephemeral=True)
        else:
            await interaction.followup.send("Upload Rubis failed.", ephemeral=True)




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
    """Load script from attachment or link (.l / .d / .upload). Same download path as .get."""
    # Prefer explicit link argument; attachment only when no link given
    if link and str(link).strip():
        link = _clean_url_arg(link)
        if not (link.startswith("http://") or link.startswith("https://")):
            raise ValueError("That doesn't look like a valid link.")
        text = await _http_get_text(link)
        filename = _normalize_raw_url(link).rsplit("/", 1)[-1].split("?")[0] or "link.lua"
        if not filename.lower().endswith((".lua", ".txt")):
            filename = filename + ".lua"
        return filename, text

    if ctx.message.attachments:
        attachment = ctx.message.attachments[0]
        if not attachment.filename.lower().endswith((".lua", ".txt")):
            raise ValueError("File must be `.lua` or `.txt`.")
        raw = await attachment.read()
        return attachment.filename, raw.decode("utf-8", errors="replace")

    raise ValueError("Attach a `.lua`/`.txt` file, or give a link: `.l <link>`")



@bot.event
async def on_ready():
    print(f"[+] Logged in as {bot.user} (id={bot.user.id})")


@bot.command(name="l")
async def l_cmd(ctx: commands.Context, *, link: Optional[str] = None):
    try:
        filename, code = await _fetch_source(ctx, link)
    except ValueError as e:
        await ctx.reply(str(e))
        return

    status_msg = await ctx.reply(f"<a:loader:1547584320544448542> prossing `{filename}`...")

    try:
        obf_name, source, meta = deobfuscator.deobfuscate(code, filename)
        cleaned = strip_lua_comments(source)
        cleaned = with_wan_banner(cleaned)

        header = f"Obfuscator detected: **{obf_name}**\n"
        if meta.get("method"):
            header += f"Method: `{meta['method']}`\n"

        rubis_link = None
        if cleaned.strip() and cleaned.strip() != WAN_BANNER.strip():
            rubis_link = await asyncio.to_thread(
                upload_to_rubis, cleaned, f"WAN DEOBF — {filename}"
            )
        if rubis_link:
            header += f"<:rubis:1546171167281254550>: [click]({rubis_link})\n"

        if not cleaned.strip() or cleaned.strip() == WAN_BANNER.strip():
            reason = source.strip() or "No source could be recovered."
            extra = ""
            if meta.get("external_error"):
                extra = f"\nExternal tool: `{meta['external_error'][:500]}`"
            if meta.get("method"):
                extra += f"\nFallback method: `{meta.get('method')}`"
            await status_msg.edit(
                content=(
                    f"{header}Nothing left after stripping comments -- "
                    f"the deobfuscator itself didn't recover real source, "
                    f"it only returned notes:\n```\n{reason}\n```"
                    f"{extra}"
                    f"{' (lupa not installed -- install it for VM execution)' if not deobfuscator.engine.available else ''}"
                )
            )
            return

        if len(cleaned) <= DISCORD_MSG_LIMIT:
            await status_msg.edit(content=f"{header}```lua\n{cleaned}\n```")
        else:
            await status_msg.edit(content=header + f"({len(cleaned):,} chars)")
            data = io.BytesIO(cleaned.encode("utf-8", errors="replace"))
            await ctx.send(file=discord.File(data, filename="deobfuscated.lua"))

    except Exception as e:
        await status_msg.edit(content=f"Error: `{e}`")



@bot.command(name="d")
async def d_cmd(ctx: commands.Context, *, link: Optional[str] = None):
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



@bot.command(name="upload")
async def upload_cmd(ctx: commands.Context, *, link: Optional[str] = None):
    """Upload attachment or link. Choose Pastefy (red) or Rubis (green)."""
    try:
        if ctx.message.attachments or link:
            filename, code = await _fetch_source(ctx, link)
        else:
            await ctx.reply(
                "Attach a `.lua`/`.txt` file or give a link, then pick a host:\n"
                "`.upload` + attach  |  `.upload <url>`"
            )
            return
    except ValueError as e:
        await ctx.reply(str(e))
        return

    if not code.strip():
        await ctx.reply("Empty content.")
        return

    view = UploadChoiceView(code, title=f"WAN UPLOAD — {filename}")
    await ctx.reply(
        f"**Upload** `{filename}` ({len(code):,} chars)\n"
        f" host: **Pastefy**  **Rubis**",
        view=view,
    )


@bot.command(name="get")
async def get_cmd(ctx: commands.Context, *, content: str = None):
    """Fetch full raw content from a URL, or echo text. Never truncates source."""
    if content is None or not str(content).strip():
        await ctx.reply("Usage: `.get <url>` or `.get <text>`")
        return
    raw_arg = content.strip()
    # Extract URL if present (Discord may wrap <url>)
    url_candidate = _clean_url_arg(raw_arg) if ("http://" in raw_arg or "https://" in raw_arg) else raw_arg
    try:
        if _is_url(url_candidate) or url_candidate.startswith("http://") or url_candidate.startswith("https://"):
            status = await ctx.reply(f"Fetching `{url_candidate[:80]}`...")
            try:
                text = await _http_get_text(url_candidate)
            except ValueError as e:
                await status.edit(content=str(e))
                return
            # Always send FULL body
            await _send_text_content(ctx, text, filename="code.txt", edit_msg=status)
        else:
            await _send_text_content(ctx, raw_arg, filename="code.txt")
    except Exception as e:
        await ctx.reply(f"Error: `{e}`")



bot.remove_command('help')

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    embed = discord.Embed(
        title="Lua Deobfuscator",
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
        value="`.l https://raw.../script.lua",
        inline=False,
    )
    embed.add_field(
        name=".get <url|text>",
        value="Fetch raw content from a URL or echo text.",
        inline=False,
    )
    embed.add_field(
        name=".upload",
        value="Upload content — **<:paterfy:1546171965897973820>Pastefy** (red) or **<:rubis:1546171167281254550>Rubis** (green). Returns `[click](raw_url)`.",
        inline=False,
    )
    embed.add_field(
        name=".detect",
        value="Attach a `.lua` or `.txt` file and run `.d` to check obfuscate type",
        inline=False,
    )
    embed.add_field(
        name="Supported obfuscators",
        value="<:wearedev:1539221658257064056> WeAreDev, IronBrew2, WAN OBFUSCATE.",
        inline=False,
    )
    embed.add_field(
        name=".deo/deobf",
        value="Attach a `.lua` or `.txt` file and run `.d` to deobfuscate",
        inline=False,
    )
    embed.set_footer(text="Comments are stripped from the recovered source automatically.")
    await ctx.reply(embed=embed)


if __name__ == "__main__":
    _bootstrap_deobf_env()
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
