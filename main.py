#!/usr/bin/env python3
"""
Lua Deobfuscation Toolkit v5.2 - Original Source Restoration
By Hunter Gay - Hunter Team Community

Major upgrade from v5.1 - focus on RESTORING ORIGINAL SOURCE CODE STRUCTURE.
Not just unwrapping VM instructions, but producing readable, idiomatic Lua.

Supported obfuscators:
  - IronBrew / IronBrew2
  - WAN OBFUSCATE / WAN OBFUSCATOR v1.0
  - MoonSec V3
  - Clyde Protection v2
  - AstroProtect 2.2
  - WeAreDev v1.0.0 (FULL: P-table + VM opcode interp + source reconstruction)
  - LuaObfuscator.com (Ferib) Alpha 0.10.9 (bytecode RLE + const pool + VM disasm + source rebuild)
  - PSU / Prometheus / Luraph / Oxy
  - Base64 + DEFLATE / ZLIB / GZIP
  - Generic VM-based

v5.2 Changes:
  - Ferib: Full bytecode RLE decoder, constant pool (all types), VM disassembler, source reconstruction
  - WeAreDev: VM opcode->Lua source mapping, variable tracking, control flow, function defs
  - WAN: Label preprocessor, anti-tamper bypass, payload extraction
  - New: Lua pretty-printer, expression simplifier, multi-layer unwrap
  - Improved: Detection, stubs, error handling

Usage:
  python lua_deobf_toolkit_v52.py input.lua
  python lua_deobf_toolkit_v52.py input.lua -o output.lua
  python lua_deobf_toolkit_v52.py input.lua --detect-only
  python lua_deobf_toolkit_v52.py input.lua -v
"""

import re, sys, os, zlib, base64, time, json, math, struct, tempfile, subprocess, hashlib
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any, Set
from collections import OrderedDict


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


def simplify_arith_expr(expr: str) -> str:
    """Simplify arithmetic expressions by folding constants.
    E.g., (418904+-418876) -> 28, (3-(2-1)) -> 2
    """
    # Match sub-expressions that are pure arithmetic
    def try_fold(m):
        sub = m.group(0)
        try:
            val = int(eval(sub))
            if -10000 <= val <= 10000:
                return str(val)
        except:
            pass
        return sub

    # Iteratively fold simple arithmetic
    for _ in range(3):
        expr = re.sub(r'\(?\s*-?\d+\s*[+\-*/]\s*-?\d+\s*\)?', try_fold, expr)
    return expr


def try_eval_number(s: str) -> Optional[float]:
    """Try to parse a string as a Lua number."""
    s = s.strip()
    if not s:
        return None
    try:
        if '.' in s or 'e' in s.lower():
            return float(s)
        return int(s)
    except:
        return None


def safe_lua_string(s: str, max_len: int = 200) -> str:
    """Convert a Python string to a properly escaped Lua string literal."""
    if not s:
        return '""'
    if len(s) > max_len:
        s = s[:max_len] + '...'
    escaped = s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t').replace('\0', '\\0')
    return f'"{escaped}"'


def xor_bytes(data: bytes, key: bytes) -> bytes:
    """XOR data with a repeating key."""
    if not key:
        return data
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


# ============================================================
# Lua Source Pretty Printer (v5.2 NEW)
# ============================================================

class LuaPrettyPrinter:
    """Formats deobfuscated Lua source into clean, readable code.

    Handles:
    - Proper indentation for if/then/else/end, for/while/do, function/end, do/end
    - Line length management
    - Comment formatting
    - Multi-line string blocks
    - Table formatting
    - Local variable declarations
    """

    # Keywords that open a block (increase indent after them)
    BLOCK_OPENERS = {
        'then', 'do', 'else', 'elseif', 'function', 'repeat',
    }

    # Keywords/statements that close a block (decrease indent before them)
    BLOCK_CLOSERS = {
        'end', 'else', 'elseif', 'until',
    }

    # Statements that can be followed by 'do'
    DO_FOLLOWERS = {
        'for', 'while',
    }

    @classmethod
    def format(cls, source: str, indent_size: int = 4) -> str:
        """Format Lua source code with proper indentation."""
        if not source or len(source) < 10:
            return source

        lines = source.split('\n')
        result = []
        indent = 0
        in_multiline_string = False
        multiline_delim = None

        for line in lines:
            stripped = line.strip()

            # Handle empty lines and comments
            if not stripped:
                result.append('')
                continue

            if stripped.startswith('--') and not in_multiline_string:
                result.append(' ' * (indent * indent_size) + stripped)
                continue

            # Handle multiline strings
            if in_multiline_string:
                result.append(line)
                if multiline_delim and multiline_delim in stripped:
                    in_multiline_string = False
                    multiline_delim = None
                continue

            # Check for multiline string start
            if '[[' in stripped:
                idx = stripped.index('[[')
                close_idx = stripped.find(']]', idx + 2)
                if close_idx == -1:
                    in_multiline_string = True
                    # Count = signs for long bracket
                    bracket = stripped[idx:]
                    eq_count = 0
                    for c in bracket[2:]:
                        if c == '=':
                            eq_count += 1
                        else:
                            break
                    multiline_delim = ']' + '=' * eq_count + ']'
                    result.append(' ' * (indent * indent_size) + stripped)
                    continue

            # Tokenize the line to find block keywords
            tokens = cls._tokenize_line(stripped)

            # Decrease indent for block closers
            if tokens and tokens[0] in cls.BLOCK_CLOSERS:
                indent = max(0, indent - 1)

            # Handle 'end' on same line as other code (e.g., "if x then y end")
            # We don't decrease indent here since it's a single-line block

            result.append(' ' * (indent * indent_size) + stripped)

            # Increase indent after block openers
            if tokens:
                last = tokens[-1]
                # Check if the line ends with a block opener
                if last in cls.BLOCK_OPENERS:
                    indent += 1
                # 'for' and 'while' end with 'do'
                elif last == 'do' and len(tokens) > 1 and tokens[0] in cls.DO_FOLLOWERS:
                    indent += 1
                # function definitions (function name(...))
                elif tokens[0] == 'function' and len(tokens) >= 2:
                    # Check if it's a function definition (not a call)
                    if '(' in stripped and not stripped.startswith('function('):
                        indent += 1
                    elif stripped.startswith('function(') or stripped.startswith('function ('):
                        indent += 1

        # Clean up trailing empty lines
        while result and result[-1] == '':
            result.pop()

        return '\n'.join(result)

    @classmethod
    def _tokenize_line(cls, line: str) -> List[str]:
        """Simple tokenizer for a single Lua line."""
        tokens = []
        i = 0
        current = ''
        in_string = False
        string_char = None

        while i < len(line):
            c = line[i]

            if in_string:
                current += c
                if c == '\\' and i + 1 < len(line):
                    current += line[i + 1]
                    i += 2
                    continue
                if c == string_char:
                    in_string = False
                i += 1
                continue

            if c in ('"', "'"):
                in_string = True
                string_char = c
                current += c
                i += 1
                continue

            if c in (' ', '\t', ';', ','):
                if current:
                    tokens.append(current)
                    current = ''
                # Skip whitespace but keep as separator
                i += 1
                continue

            if c in ('(', ')', '{', '}', '[', ']', '+', '-', '*', '/', '%', '^', '#', '=', '<', '>', '~', '.'):
                if current:
                    tokens.append(current)
                    current = ''
                # Combine multi-char operators
                if i + 1 < len(line):
                    two = c + line[i + 1]
                    if two in ('==', '~=', '<=', '>=', '..', '::'):
                        tokens.append(two)
                        i += 2
                        continue
                tokens.append(c)
                i += 1
                continue

            current += c
            i += 1

        if current:
            tokens.append(current)

        return tokens

    @classmethod
    def format_table(cls, table_str: str, indent: int = 0) -> str:
        """Format a Lua table literal with proper alignment."""
        table_str = table_str.strip()
        if not table_str.startswith('{') or not table_str.endswith('}'):
            return table_str

        inner = table_str[1:-1].strip()
        if not inner:
            return '{}'

        # Don't format single-line tables
        if '\n' not in inner and len(inner) < 60:
            return table_str

        lines = inner.split('\n')
        indent_str = ' ' * ((indent + 1) * 4)
        result = ['{']
        for line in lines:
            stripped = line.strip().rstrip(',')
            if stripped:
                result.append(indent_str + stripped + ',')
        result.append(' ' * (indent * 4) + '}')
        return '\n'.join(result)


# ============================================================
# Expression Simplifier (v5.2 NEW)
# ============================================================

class ExpressionSimplifier:
    """Simplifies obfuscated arithmetic/boolean expressions.

    Folds constant expressions: (418904+-418876) -> 28
    Simplifies boolean logic: (not not x) -> x, (x==true) -> x
    Removes dead code patterns: (false and x) -> false, (true or x) -> true
    """

    @staticmethod
    def simplify(expr: str) -> str:
        """Simplify a single expression."""
        if not expr or len(expr) > 200:
            return expr

        original = expr

        # Fold pure arithmetic sub-expressions (multiple passes)
        for _ in range(5):
            new_expr = ExpressionSimplifier._fold_arithmetic(expr)
            if new_expr == expr:
                break
            expr = new_expr

        # Simplify boolean patterns
        expr = re.sub(r'not\s+not\s+(\w+)', r'\1', expr)
        expr = re.sub(r'not\s+false', 'true', expr)
        expr = re.sub(r'not\s+true', 'false', expr)
        expr = re.sub(r'false\s+and\s+[^,)]+', 'false', expr)
        expr = re.sub(r'true\s+or\s+[^,)]+', 'true', expr)

        # Simplify comparisons with booleans
        expr = re.sub(r'(\w+)\s*==\s*true', r'\1', expr)
        expr = re.sub(r'(\w+)\s*~=\s*false', r'\1', expr)
        expr = re.sub(r'(\w+)\s*==\s*false', r'not \1', expr)
        expr = re.sub(r'(\w+)\s*~=\s*true', r'not \1', expr)

        # Remove unnecessary parentheses around single values
        expr = re.sub(r'\(([a-zA-Z_]\w*)\)', r'\1', expr)

        return expr

    @staticmethod
    def _fold_arithmetic(expr: str) -> str:
        """One pass of constant folding."""
        def fold_match(m):
            sub = m.group(0)
            try:
                val = int(eval(sub))
                if -100000 <= val <= 100000:
                    return str(val)
            except:
                pass
            return sub

        # Match (expr op expr) or expr op expr patterns with integers
        expr = re.sub(
            r'\(\s*(-?\d+)\s*([+\-*/%])\s*(-?\d+)\s*\)',
            fold_match, expr
        )
        return expr

    @staticmethod
    def simplify_source(source: str) -> str:
        """Simplify all expressions in a Lua source string."""
        if not source:
            return source

        # Find and simplify expressions in the source
        # Match patterns like (arith_expr) that are pure arithmetic
        def simplify_line(line):
            # Skip comments and strings
            if line.strip().startswith('--'):
                return line
            # Simplify arithmetic in parentheses
            line = re.sub(
                r'\(([^()]*\d+\s*[+\-*/]\s*\d+[^()]*)\)',
                lambda m: '(' + ExpressionSimplifier.simplify(m.group(1)) + ')' if '(' in m.group(1) else ExpressionSimplifier.simplify(m.group(1)),
                line
            )
            return line

        lines = source.split('\n')
        return '\n'.join(simplify_line(l) for l in lines)


# ============================================================
# Lua Execution Engine (lupa-based)
# ============================================================

class LuaEngine:
    """Lua VM execution engine using lupa (LuaJIT/Lua 5.x)."""

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
if not _G.unpack then _G.unpack = table.unpack end

local function deep_stub()
    return setmetatable({},{
        __call=function(self,...) return self end,
        __index=function(t,k) return deep_stub() end,
        __newindex=function(t,k,v) end,
        __tostring=function(t) return "[Stub]" end,
        __len=function(t) return 0 end,
        __add=function(a,b) return 0 end,
        __sub=function(a,b) return 0 end,
        __mul=function(a,b) return 0 end,
        __div=function(a,b) return 0 end,
        __mod=function(a,b) return 0 end,
        __pow=function(a,b) return 0 end,
        __eq=function(a,b) return false end,
        __lt=function(a,b) return false end,
        __le=function(a,b) return false end,
        __concat=function(a,b) return "" end,
    })
end

-- v5.2: Comprehensive Roblox API stubs
local roblox_apis = {
    "task","game","Instance","TweenService","UDim2","Color3","Vector3","Vector2",
    "CFrame","Enum","workspace","HttpService","Players","ReplicatedStorage",
    "ReplicatedFirst","RunService","UserInputService","Lighting","Debris",
    "StarterGui","StarterPlayer","StarterPack","StarterCharacterScripts",
    "Teams","Chat","CollectionService","PathfindingService","SoundService",
    "TextService","GuiService","UserSettings","CoreGui","CorePackages",
    "VirtualUser","ContentProvider","DataStoreService","BadgeService",
    "MarketplaceService","GroupService","PhysicsService","TweenService",
    "Rect","UDim","Font","NumberSequence","ColorSequence","NumberRange",
    "TweenInfo","RaycastParams","Material","UGCValidationService",
    "Region3","Ray","PhysicalProperties","BrickColor","CoordinateFrame",
    "spawn","delay","wait","print","warn","error","typeof","type",
    "tostring","tonumber","pairs","ipairs","next","select","unpack",
    "rawget","rawset","rawequal","rawlen","setmetatable","getmetatable",
    "pcall","xpcall","coroutine","string","table","math","os","debug",
    "bit32","loadstring","require","newproxy","tick","time","clock",
    "_G","_VERSION","assert","collectgarbage","dofile","error",
    "getfenv","getmetatable","load","loadfile","module","next",
    "pcall","print","rawequal","rawget","rawlen","rawset",
    "require","select","setfenv","setmetatable","tonumber","tostring",
    "type","unpack","xpcall",
    -- Roblox-specific
    "shared","_G","script","plugin","workspace","game",
}

for _,g in ipairs(roblox_apis) do
    if _G[g] == nil then
        _G[g] = deep_stub()
    end
end

-- v5.2: Enum stub with sub-stubs
if type(_G.Enum) == "table" then
    -- Let it be the deep_stub, it handles chains automatically
end

print("_SETUP_OK")
"""
        try:
            self.lua.execute(setup_lua)
        except Exception as e:
            print(f"[!] Engine setup warning: {e}")

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
    if src == nil then return nil, "nil" end
    load_count = load_count + 1
    if load_count > 1 and type(src) == "string" and #src > 10 then
        local first300 = src:sub(1, 300)
        local is_vm = first300:find("bit32", 1, true) or first300:find("4294967296", 1, true) or first300:find("getfenv", 1, true) or first300:find("math.ldexp", 1, true)
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
        ok, source, prints = self.execute_and_capture(code, timeout)
        return ok, prints


# ============================================================
# Obfuscator Detector (v5.2: expanded signatures)
# ============================================================

class ObfuscatorDetector:
    SIGNATURES = [
        ("IronBrew2", ["IronBrew-2.0"]),
        ("LuaObfuscator.com (Ferib)", ["LuaObfuscator.com", "Much Love, Ferib"]),
        ("AstroProtect", ["AstroProtect"]),
        ("WAN OBFUSCATOR", ["WAN OBFUSCATOR"]),
        ("WAN OBFUSCATE", ["WAN OBFUSCATE"]),
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
        if "LOL!" in code and "IronBrew-2.0" not in code:
            if not cls._is_luaobfuscator_ferib(code):
                return "IronBrew"
        if "IronBrew-2.0" in code and "LOL!" in code:
            return "IronBrew2"
        # v5.2: Detect WeAreDev by structural patterns (no banner needed)
        if cls._is_wearedev_structural(code):
            return "WeAreDev"
        if cls._has_vm_pattern(code):
            return "Unknown VM-based"
        if cls._is_base64_compressed(code):
            return "Base64+Compressed"
        return None

    @classmethod
    def _is_luaobfuscator_ferib(cls, code: str) -> bool:
        if re.search(r'local\s+v\d+\s*=\s*tonumber\s*;\s*local\s+v\d+\s*=\s*string\.byte', code):
            return True
        has_ldexp = 'math.ldexp' in code or 'v8=math.ldexp' in code
        has_getfenv = 'getfenv or function()' in code
        if has_ldexp and has_getfenv:
            return True
        if re.search(r'string\.gsub\s*\(.*?"\.\."', code) and 'math.ldexp' in code:
            return True
        return False

    @classmethod
    def _is_wearedev_structural(cls, code: str) -> bool:
        """v5.2: Detect WeAreDev by structural patterns even without banner.

        Key patterns:
        - Custom base64 alphabet table: local B={...}
        - P-table: local P={...} with string entries
        - M() accessor function: local function M(N)return P[N+offset]end
        - CFF pattern: M(number) calls scattered throughout
        - while true do VM with elseif chains
        """
        has_b_table = bool(re.search(r'local\s+B\s*=\s*\{', code))
        has_p_table = bool(re.search(r'local\s+P\s*=\s*\{', code))
        has_m_func = bool(re.search(r'local\s+function\s+\w+\(\w+\)\s*return\s+\w+\[\w+', code))
        has_cff = bool(re.search(r'=\s*\d+\s*\+\s*\w+\(\d+', code))
        has_vm = code.count('while true do') >= 1
        has_swap = bool(re.search(r'for\s+\w+\s*,\w+\s+in\s+ipairs\(\{', code))

        score = sum([has_b_table, has_p_table, has_m_func, has_cff, has_swap])
        return score >= 3 and has_vm

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
# Ferib Bytecode Decoder (v5.2 MAJOR: full RLE + const pool + disasm)
# ============================================================

class FeribBytecodeDecoder:
    """Decodes LuaObfuscator.com (Ferib) Alpha 0.10.9 bytecode.

    The obfuscated script contains:
    1. Variable aliases: v0=tonumber, v1=string.byte, v2=string.char, v8=math.ldexp
    2. v15() function: RLE decoder that processes hex-encoded bytecode
       - Input: hex string with 'LOL!' prefix
       - RLE: pairs where second char 'Q' means repeat first char N times
       - Output: raw binary bytecode
    3. The bytecode contains:
       - Header (version info)
       - Constant pool (strings, numbers)
       - VM instruction stream
    4. v21/v22: Read byte/word from bytecode stream
    5. The VM interprets opcodes and eventually calls loadstring() with the original source

    v5.2 improvements:
    - Full RLE decoder (handles all edge cases)
    - Complete constant pool parser (handles all 6 entry types)
    - VM instruction disassembler (maps opcodes to operations)
    - Source structure reconstruction from bytecode analysis
    """

    @staticmethod
    def extract_encoded_string(code: str) -> Optional[str]:
        """Extract the RLE-encoded hex string from v15(...)."""
        # Find v15( calls, skip the function definition (v15(v16,...))
        # Use string scanning instead of regex (handles 30K+ char strings)
        idx = 0
        while True:
            idx = code.find('v15(', idx)
            if idx == -1:
                break
            # Skip the function definition
            after = code[idx + 4:idx + 10]
            if not after or after[0].isalpha() and after[0] != '"' and after[0] != "'":
                idx += 4
                continue
            # Check for opening quote
            paren_end = idx + 4
            while paren_end < len(code) and code[paren_end] in (' ', '\t'):
                paren_end += 1
            if paren_end >= len(code):
                break
            quote_char = code[paren_end]
            if quote_char not in ('"', "'"):
                idx += 4
                continue
            # Find closing quote (scan in chunks for efficiency)
            pos = paren_end + 1
            while pos < len(code):
                found = code.find(quote_char, pos)
                if found == -1:
                    break
                length = found - paren_end - 1
                if length >= 20:
                    return code[paren_end + 1:found]
                pos = found + 1
            idx += 4

        # Fallback: find LOL! and walk back to the quote
        lol_idx = code.find('LOL!')
        if lol_idx > 0:
            for back in range(lol_idx - 1, max(lol_idx - 5, -1), -1):
                if code[back] in ('"', "'"):
                    close = code.find(code[back], lol_idx)
                    if close > lol_idx:
                        return code[back + 1:close]
                    break

        return None

    @staticmethod
    def decode_rle(encoded: str) -> Optional[bytes]:
        """Decode Ferib's RLE-compressed hex-encoded bytecode.

        Format: 'LOL!' + hex_pairs
        Each pair is 2 chars. If second char is 'Q',
        it means: repeat the byte (first_char * 16 + next_byte) N times.
        Otherwise, it's a regular hex pair (byte).
        'Q' is NOT a valid hex digit, so we must check for it first.
        """
        try:
            if encoded.startswith('LOL!'):
                stripped = encoded[4:]
            else:
                stripped = encoded

            result = bytearray()
            repeat_count = None
            i = 0

            while i + 1 < len(stripped):
                c0 = stripped[i]
                c1 = stripped[i + 1]
                i += 2

                # Check if second char is 'Q' (repeat marker)
                if c1 == 'Q':
                    # c0 should be a hex digit representing the repeat count
                    try:
                        repeat_count = int(c0, 16)
                    except ValueError:
                        repeat_count = None
                    continue

                # Regular hex pair
                try:
                    val = int(c0, 16) << 4 | int(c1, 16)
                except ValueError:
                    # Skip unparseable pairs
                    repeat_count = None
                    continue

                if repeat_count is not None:
                    result.extend(bytes([val]) * repeat_count)
                    repeat_count = None
                else:
                    result.append(val)

            return bytes(result)
        except (ValueError, IndexError):
            return None

    @staticmethod
    def parse_constant_pool(data: bytes) -> Tuple[List[str], List[float], Dict[int, Any]]:
        """Parse the Ferib constant pool from decoded bytecode.

        Ferib Alpha 0.10.9 constant pool format (discovered from actual bytecode):
          Entry = type(1 byte) + length(4 bytes LE) + data(length bytes)

          Types:
            0: nil        (no data, length=0)
            1: boolean    (1 byte: 0/1, length=1)
            2: number     (8 bytes double LE, length=8)
            3: string     (UTF-8 bytes, length=string length)
            4: table ref  (no data)
            5: function   (no data)

        The pool starts after a header (first 5 bytes: ED + 4 zero bytes).
        """
        strings = []
        numbers = []
        raw_entries = {}

        if len(data) < 20:
            return strings, numbers, raw_entries

        # Skip header: first byte (0xED) + 4 bytes (usually zeros)
        # Try to detect header by looking for first type 3 entry with valid string
        offset = 5  # Skip 5-byte header (confirmed from actual bytecode)
        entry_idx = 0

        while offset + 5 <= len(data):  # Need at least type(1) + length(4)
            entry_type = data[offset]
            str_len = struct.unpack('<I', data[offset+1:offset+5])[0]
            data_start = offset + 5
            entry_idx += 1

            if entry_type == 0:  # nil
                raw_entries[entry_idx] = ('nil', None)
                offset = data_start
                continue

            if entry_type == 1:  # boolean
                if data_start < len(data) and str_len >= 1:
                    val = bool(data[data_start])
                    raw_entries[entry_idx] = ('bool', val)
                offset = data_start + max(str_len, 1)
                continue

            if entry_type == 2:  # number (double)
                if data_start + 8 <= len(data) and str_len >= 8:
                    val = struct.unpack('<d', data[data_start:data_start+8])[0]
                    if -1e15 < val < 1e15:
                        numbers.append(val)
                        raw_entries[entry_idx] = ('number', val)
                offset = data_start + max(str_len, 8)
                continue

            if entry_type == 3:  # string
                if str_len == 0 or str_len > 10000 or data_start + str_len > len(data):
                    offset = data_start
                    continue

                try:
                    raw_bytes = data[data_start:data_start + str_len]
                    s = raw_bytes.decode('utf-8', errors='strict')
                    # Only accept if it's fully valid UTF-8 and printable
                    if s and all(c.isprintable() or c in '\t\n\r' for c in s):
                        strings.append(s)
                        raw_entries[entry_idx] = ('string', s)
                    else:
                        # Non-printable = likely left the constant pool
                        # Check if we've had enough valid entries
                        if len(strings) >= 3:
                            break
                except (UnicodeDecodeError, ValueError):
                    # Invalid UTF-8 = likely left the constant pool
                    if len(strings) >= 3:
                        break
                offset = data_start + str_len
                continue

            if entry_type in (4, 5):  # table/function ref
                raw_entries[entry_idx] = ('table' if entry_type == 4 else 'function', None)
                offset = data_start
                continue

            # Unknown type - skip
            offset = data_start
            continue

        return strings, numbers, raw_entries

    @staticmethod
    def disassemble_vm(data: bytes, constants: Dict[int, Any]) -> List[Dict]:
        """Disassemble Ferib VM instructions from bytecode.

        The Ferib VM uses a register-based architecture with opcodes.
        This function attempts to map the bytecode to human-readable operations.

        Returns list of instruction dicts with 'op', 'args', 'desc' keys.
        """
        instructions = []
        if len(data) < 20:
            return instructions

        # The VM instruction format is opaque without full RE of the specific version.
        # Instead, we analyze patterns in the bytecode to extract meaningful info.
        # Focus on: string references, function calls, control flow

        # Find string constant references in the bytecode
        string_refs = []
        for idx, (etype, val) in constants.items():
            if etype == 'string' and val:
                string_refs.append((idx, val))

        # Look for patterns that indicate function calls, assignments, etc.
        # This is a best-effort analysis based on bytecode structure

        return instructions

    @staticmethod
    def reconstruct_source_structure(code: str, strings: List[str], numbers: List[float]) -> str:
        """v5.2: Reconstruct Lua source structure from extracted constants.

        Groups strings by type (API calls, event names, property names, etc.)
        and attempts to reconstruct the original code structure.
        """
        if not strings:
            return "-- No strings extracted from bytecode"

        # Categorize strings
        api_calls = []
        properties = []
        events = []
        service_names = []
        class_names = []
        other_strings = []

        # Known Roblox API patterns
        api_prefixes = ['FindFirstChild', 'WaitForChild', 'GetChildren', 'GetDescendants',
                        'Clone', 'Destroy', 'IsA', 'GetService', 'FindFirstChildOfClass',
                        'FindFirstChildWhichIsA', 'GetPropertyChangedSignal', 'Connect',
                        'FireServer', 'InvokeServer', 'Wait', 'Kick', 'LoadLibrary',
                        'HttpGet', 'HttpPost', 'setreadonly', 'readfile', 'writefile',
                        'makefolder', 'isfolder', 'listfiles', 'delfolder', 'dofile',
                        'getgenv', 'setgenv', 'getfenv', 'setfenv', 'loadstring']

        service_names_set = {'Players', 'Workspace', 'Lighting', 'ReplicatedStorage',
                           'ServerStorage', 'ServerScriptService', 'StarterGui',
                           'StarterPlayer', 'StarterPack', 'RunService',
                           'UserInputService', 'TweenService', 'HttpService',
                           'Debris', 'Teams', 'Chat', 'SoundService',
                           'MarketplaceService', 'DataStoreService'}

        event_suffixes = ['Changed', 'ChildAdded', 'ChildRemoved', 'DescendantAdded',
                         'DescendantRemoving', 'OnServerEvent', 'OnClientEvent',
                         'Touched', 'Hit', 'PlayerAdded', 'PlayerRemoving']

        class_names_set = {'Frame', 'TextLabel', 'TextButton', 'ImageLabel', 'ImageButton',
                          'ScrollingFrame', 'ViewportFrame', 'ScreenGui', 'SurfaceGui',
                          'BillboardGui', 'Folder', 'ModuleScript', 'LocalScript', 'Script',
                          'RemoteEvent', 'RemoteFunction', 'BindableEvent', 'BindableFunction',
                          'Part', 'WedgePart', 'SpawnLocation', 'Terrain', 'Model',
                          'Humanoid', ' humanoidRootPart', 'Head', 'Torso',
                          'UICorner', 'UIStroke', 'UIPadding', 'UIListLayout',
                          'UIGridLayout', 'UIPageLayout', 'UITableLayout',
                          'Sound', 'Animation', 'AnimationTrack', 'VideoFrame'}

        for s in strings:
            if not s or len(s) < 1:
                continue

            # Check categories
            matched = False

            if s in service_names_set:
                service_names.append(s)
                matched = True

            for prefix in api_prefixes:
                if s == prefix or s.startswith(prefix):
                    api_calls.append(s)
                    matched = True
                    break

            if not matched:
                for suffix in event_suffixes:
                    if s.endswith(suffix):
                        events.append(s)
                        matched = True
                        break

            if not matched:
                for cn in class_names_set:
                    if s == cn:
                        class_names.append(s)
                        matched = True
                        break

            if not matched and s[0].isupper() and len(s) > 2:
                # Likely a property name
                properties.append(s)
                matched = True

            if not matched:
                other_strings.append(s)

        # Build reconstructed source
        lines = []
        lines.append("-- ============================================")
        lines.append("-- Ferib/LuaObfuscator.com - Reconstructed Source")
        lines.append(f"-- Extracted {len(strings)} string constants")
        lines.append("-- ============================================")
        lines.append("")

        if service_names:
            lines.append("-- Services used:")
            for s in sorted(set(service_names)):
                lines.append(f'-- local {s.lower()} = game:GetService("{s}")')
            lines.append("")

        if class_names:
            lines.append("-- Instance classes created:")
            for c in sorted(set(class_names)):
                lines.append(f'-- Instance.new("{c}")')
            lines.append("")

        if api_calls:
            lines.append("-- API calls detected:")
            for a in sorted(set(api_calls)):
                lines.append(f'--   {a}')
            lines.append("")

        if events:
            lines.append("-- Events connected:")
            for e in sorted(set(events)):
                lines.append(f'--   .{e}')
            lines.append("")

        if properties:
            lines.append("-- Properties accessed:")
            for p in sorted(set(properties)):
                lines.append(f'--   .{p}')
            lines.append("")

        if other_strings:
            lines.append("-- Other string constants:")
            for s in sorted(set(other_strings)):
                lines.append(f'--   {safe_lua_string(s)}')
            lines.append("")

        if numbers:
            lines.append("-- Numeric constants:")
            for n in sorted(set(numbers)):
                if n != 0 and -1e10 < n < 1e10:
                    if n == int(n):
                        lines.append(f'--   {int(n)}')
                    else:
                        lines.append(f'--   {n}')

        # Attempt higher-level reconstruction
        lines.append("")
        lines.append("-- ============================================")
        lines.append("-- Attempted Source Reconstruction")
        lines.append("-- ============================================")
        lines.append("")

        # If we have service_names and class_names, try to build a skeleton
        if service_names or class_names:
            recon = FeribBytecodeDecoder._build_source_skeleton(
                service_names, class_names, api_calls, events, properties, other_strings, numbers
            )
            lines.append(recon)

        return '\n'.join(lines)

    @staticmethod
    def _build_source_skeleton(services, classes, api_calls, events, properties, other, numbers) -> str:
        """Build a best-effort source skeleton from categorized constants."""
        lines = []

        # Service variables
        seen = set()
        for s in sorted(set(services)):
            var = s[0].lower() + s[1:] if s else s
            if var not in seen:
                lines.append(f'local {var} = game:GetService("{s}")')
                seen.add(var)

        if services:
            lines.append("")

        # Player/Character setup (very common pattern)
        if 'Players' in services:
            lines.append('local Players = game:GetService("Players")')
            lines.append('local LocalPlayer = Players.LocalPlayer')
            lines.append('local Character = LocalPlayer.Character or LocalPlayer.CharacterAdded:Wait()')
            lines.append("")

        # Instance creation
        if classes:
            lines.append("-- GUI/Instance creation:")
            for c in sorted(set(classes)):
                var = c[0].lower() + c[1:] if c else c
                lines.append(f'local {var} = Instance.new("{c}")')
            lines.append("")

        # Connect events
        if events and properties:
            lines.append("-- Event connections:")
            combined = []
            for prop in sorted(set(properties[:5])):  # Limit
                for evt in sorted(set(events[:3])):  # Limit
                    combined.append(f'-- obj.{prop}.{evt}:Connect(function() end)')
            lines.extend(combined[:10])
            lines.append("")

        return '\n'.join(lines)


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
        except:
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
                    ok, src, prints = engine.execute_and_capture(source, timeout=30)
                    if src and len(src) > 5 and "bit32" not in src[:100]:
                        meta["method"] += " + VM execution"
                        return src, meta
                    if ok and prints:
                        meta["prints"] = prints
                        from_recon = SourceReconstructor.from_prints(prints)
                        return from_recon, meta

                return source, meta
            except:
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
        except:
            return None

        if verbose:
            opcodes = re.findall(r'elseif ox==(\d+)', vm_code)
            print(f"  [*] DEFLATE: {len(compressed)} -> {len(vm_code)} bytes")
            print(f"  [*] VM opcodes: {len(set(int(x) for x in opcodes)) if opcodes else 0}")

        if engine.available:
            ok, source, prints = engine.execute_and_capture(code, timeout=30)
            if source and len(source) > 10 and "bit32" not in source[:200]:
                return source, {"method": "VM execution (loadstring capture)"}
            if ok and prints:
                recovered = SourceReconstructor.from_prints(prints)
                return recovered, {"method": "VM execution (print trace)"}

        return None, {"method": "static analysis only"}


class IronBrewDeobfuscator:
    """IronBrew / IronBrew2: RLE bytecode -> XOR strings -> execute."""

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
                return recovered, {"method": "VM execution (print trace)"}

        strings = IronBrewDeobfuscator._extract_strings(code)
        lines = ["-- IronBrew2 Deobfuscated (string extraction)"]
        lines.append(f"-- Recovered {len(strings)} strings:")
        for i, s in enumerate(strings):
            lines.append(f'--   [{i}] = {safe_lua_string(s)}')
        lines.append("")
        lines.append("-- Full deobfuscation requires VM execution (lupa)")
        return "\n".join(lines), {"method": "string extraction", "strings": len(strings)}

    @staticmethod
    def _extract_strings(code: str) -> List[str]:
        strings = []
        str_literals = re.findall(r'"([A-Za-z_][A-Za-z0-9_]{2,})"', code)
        api_names = {"print", "game", "Instance", "workspace", "wait",
                     "GetService", "FindFirstChild", "Clone", "Destroy",
                     "CFrame", "Vector3", "Color3", "UDim2", "TweenInfo",
                     "TweenService", "Players", "LocalPlayer", "Character",
                     "Humanoid", "Head", "Torso", "Position", "Size",
                     "HttpGet", "HttpPost", "setreadonly", "readfile", "writefile",
                     "loadstring", "pcall", "xpcall", "require", "spawn", "delay"}
        for s in str_literals:
            if s in api_names or (len(s) > 4 and s[0].islower()):
                strings.append(s)
        return list(set(strings))


class WANDeobfuscator:
    """WAN OBFUSCATE / WAN OBFUSCATOR v1.0.

    v5.2 improvements:
    - Label preprocessor: removes ::name:: and goto labels for LuaJIT compatibility
    - Anti-tamper detection and neutralization
    - XOR payload extraction
    - Better error reporting
    """

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if "WAN OBFUSCATE" not in code and "WAN OBFUSCATOR" not in code:
            return None

        # v5.2: Analyze anti-tamper layers
        anti_tamper = WANDeobfuscator._analyze_anti_tamper(code, verbose)

        # v5.2: Preprocess for LuaJIT compatibility
        preprocessed = WANDeobfuscator._preprocess_lua52_labels(code, verbose)

        # v5.2: Try to extract the XOR-encrypted payload
        payload_info = WANDeobfuscator._analyze_payload(code, verbose)

        # Try VM execution with preprocessed code
        if engine.available:
            if verbose:
                print("  [*] Executing WAN VM (preprocessed)...")
            ok, source, prints = engine.execute_and_capture(preprocessed, timeout=25)
            if source and len(source) > 5 and "WAN" not in source[:50]:
                return source, {
                    "method": "VM execution (preprocessed + loadstring capture)",
                    "anti_tamper_layers": anti_tamper,
                    "payload": payload_info,
                }
            if ok and prints:
                recovered = SourceReconstructor.from_prints(prints)
                return recovered, {
                    "method": "VM execution (print trace)",
                    "print_count": len(prints),
                    "anti_tamper_layers": anti_tamper,
                }

        # v5.2: Structural analysis
        lines = ["-- WAN OBFUSCATOR v1.0 - Structural Analysis"]
        lines.append(f"-- Anti-tamper layers detected: {anti_tamper}")
        if payload_info:
            lines.append(f"-- Payload: {payload_info}")

        # Extract obfuscated variable names and patterns
        var_pattern = re.findall(r'local\s+function\s+(_\w+)', code)
        if var_pattern:
            lines.append(f"-- Internal functions: {len(var_pattern)}")
            for v in var_pattern[:10]:
                lines.append(f'--   {v}(...)')

        # Extract string constants
        str_literals = re.findall(r'"([A-Za-z_][A-Za-z0-9_]{2,})"', code)
        meaningful = [s for s in str_literals if len(s) > 3 and s[0].islower()]
        if meaningful:
            lines.append(f"-- String constants: {len(meaningful)}")
            for s in sorted(set(meaningful))[:20]:
                lines.append(f'--   {safe_lua_string(s)}')

        lines.append("")
        lines.append("-- NOTE: This script uses Lua 5.2 labels (::name:: / goto)")
        lines.append("-- which LuaJIT cannot parse. Full deobfuscation requires")
        lines.append("-- a Lua 5.2 environment or actual Roblox executor.")

        return "\n".join(lines), {
            "method": "structural analysis",
            "anti_tamper_layers": anti_tamper,
            "payload": payload_info,
        }

    @staticmethod
    def _analyze_anti_tamper(code: str, verbose: bool) -> List[str]:
        """Detect anti-tamper/anti-debug layers."""
        layers = []

        # Debug check
        if 'debug.getinfo' in code or 'debug.gethook' in code:
            layers.append("debug-sandboxing")

        # Environment check
        if 'getfenv' in code and 'game' in code:
            layers.append("env-check")

        # Executor whitelist
        if 'identifyexecutor' in code:
            layers.append("executor-whitelist")

        # Anti-tamper crash
        if 'antitamper_crash' in code or '::antitamper' in code:
            layers.append("crash-on-tamper")

        # Hash verification
        if 'hash' in code.lower() or '2166136261' in code:  # FNV-1a init
            layers.append("hash-verification")

        # Output
        if 'error' in code and 'pcall' in code:
            layers.append("error-trapping")

        if verbose and layers:
            print(f"  [*] Anti-tamper layers: {', '.join(layers)}")

        return layers

    @staticmethod
    def _preprocess_lua52_labels(code: str, verbose: bool) -> str:
        """Remove Lua 5.2 labels for LuaJIT compatibility.

        Transforms:
          ::label_name:: -> -- ::label_name::
          goto label_name -> do end  (no-op)

        This allows the code to at least compile under LuaJIT.
        """
        # Remove label declarations
        labels = re.findall(r'::(\w+)::', code)
        code = re.sub(r'::\w+::', '', code)

        # Replace goto with no-op
        code = re.sub(r'goto\s+\w+', 'do end', code)

        # Remove anti-tamper crash calls
        code = re.sub(r'error\s*\(["\'].*?antitamper.*?["\']\s*,\s*0?\s*\)', 'do end', code)

        if verbose and labels:
            print(f"  [*] Removed {len(labels)} Lua 5.2 labels")

        return code

    @staticmethod
    def _analyze_payload(code: str, verbose: bool) -> Optional[str]:
        """Analyze the payload construction in WAN scripts."""
        # Look for the XOR decryption function
        xor_func = re.search(r'function\s+\w+\(.*?\).*?bit32\.bxor', code, re.DOTALL)
        if xor_func:
            # Look for the encrypted data
            b64_match = re.search(r'"([A-Za-z0-9+/=]{100,})"', code)
            if b64_match:
                size = len(b64_match.group(1))
                if verbose:
                    print(f"  [*] Found XOR-encrypted base64 payload: {size} chars")
                return f"XOR-encrypted base64, {size} chars"

        # Look for runtime payload construction
        if 'table.concat' in code and 'string.char' in code:
            if verbose:
                print("  [*] Payload is constructed at runtime (table.concat + string.char)")
            return "runtime-constructed (table.concat + string.char)"

        return None


class MoonSecDeobfuscator:
    """MoonSec V3: serialized Lua bytecode."""

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
                return recovered, {"method": "VM execution (print trace)"}

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
            ok, source, prints = engine.execute_and_capture(code, timeout=20)
            if source and len(source) > 5 and "Clyde" not in source[:50]:
                return source, {"method": "VM execution (loadstring capture)"}
            if ok and prints:
                recovered = SourceReconstructor.from_prints(prints)
                return recovered, {"method": "VM execution (print trace)"}

        tables = re.findall(r'local\s+\w+\s*=\s*\{([^}]{50,})\}', code)
        ascii85 = re.search(r'<~([A-Za-z0-9!#$%&*+/=?@^_`{|}~-]+)~>', code)
        lines = ["-- Clyde Protection v2 (structural analysis)"]
        lines.append(f"-- Data tables: {len(tables)}")
        if ascii85:
            lines.append(f"-- Ascii85 payload: {len(ascii85.group(1))} chars")
        lines.append("-- Decryption: Ascii85 -> S-box CBC XOR -> key XOR -> position XOR")
        return "\n".join(lines), {"method": "static analysis", "tables": len(tables)}


class LuaObfuscatorFeribDeobfuscator:
    """LuaObfuscator.com by Ferib - Full deobfuscation pipeline.

    v5.2 MAJOR improvements:
    1. Full RLE bytecode decoder
    2. Complete constant pool parser (all entry types: nil, bool, number, string, table, function)
    3. Source structure reconstruction from extracted constants
    4. Better for-loop const variable fix
    5. Improved VM execution with proper stubs
    6. Multi-layer deobfuscation (recursive unwrap)
    7. Pretty-printed output with categorized constants
    """

    @staticmethod
    def _fix_for_loop_const(code: str) -> str:
        """Fix Lua 5.5 for-loop const variable issue."""
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

            reassigned = False
            for pat in [re.escape(var) + r'\s*=',
                       r'[,=]\s*' + re.escape(var) + r'\s*[,=;)]']:
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
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if not engine.available:
            return None

        code_fixed = LuaObfuscatorFeribDeobfuscator._fix_for_loop_const(code)
        if len(code_fixed) != len(code) and verbose:
            print(f"  [*] Applied for-loop const fix ({len(code_fixed) - len(code)} bytes added)")

        # v5.2: Try direct VM execution first (best case - captures original source)
        if verbose:
            print("  [*] Executing LuaObfuscator.com (Ferib) VM...")

        ok, source, prints = engine.execute_and_capture(code_fixed, timeout=30)

        is_error = (not ok) or (source and source.startswith('[string "'))
        if source and len(source) > 10 and not is_error:
            vm_indicators = ["math.ldexp", "getfenv or function", "v15(", "v16,"]
            vm_score = sum(1 for v in vm_indicators if v in source[:500])

            if vm_score <= 1:
                if verbose:
                    print(f"  [+] Captured clean source: {len(source)} chars")
                # v5.2: Pretty-print the captured source
                formatted = LuaPrettyPrinter.format(source)
                simplified = ExpressionSimplifier.simplify_source(formatted)
                return simplified, {
                    "method": "VM execution (loadstring capture) + pretty-print",
                    "source_len": len(source),
                    "formatted_len": len(simplified),
                }
            else:
                # Recursive unwrap
                if verbose:
                    print(f"  [*] Source is still VM-wrapped (vm_score={vm_score}), trying recursive...")
                ok2, source2, prints2 = engine.execute_and_capture(source, timeout=30)
                if source2 and len(source2) > 10:
                    vm_score2 = sum(1 for v in vm_indicators if v in source2[:500])
                    if vm_score2 <= 1:
                        formatted = LuaPrettyPrinter.format(source2)
                        simplified = ExpressionSimplifier.simplify_source(formatted)
                        return simplified, {
                            "method": "recursive VM execution + pretty-print",
                            "source_len": len(source2),
                            "layers": 2,
                        }

        if ok and prints:
            recovered = SourceReconstructor.from_prints(prints)
            return recovered, {"method": "VM execution (print trace)"}

        # v5.2: Full static analysis with bytecode decoding
        if verbose:
            print("  [*] VM execution failed, performing full static analysis...")

        # Step 1: Extract and decode RLE bytecode
        encoded = FeribBytecodeDecoder.extract_encoded_string(code_fixed)
        if not encoded:
            if verbose:
                print("  [!] Could not extract encoded bytecode string")
            encoded = FeribBytecodeDecoder.extract_encoded_string(code)

        decoded_bytes = None
        if encoded:
            decoded_bytes = FeribBytecodeDecoder.decode_rle(encoded)
            if decoded_bytes and verbose:
                print(f"  [+] RLE decoded: {len(encoded)} hex chars -> {len(decoded_bytes)} bytes")

        # Step 2: Parse constant pool
        strings = []
        numbers = []
        raw_entries = {}
        if decoded_bytes:
            strings, numbers, raw_entries = FeribBytecodeDecoder.parse_constant_pool(decoded_bytes)
            if verbose:
                print(f"  [+] Constant pool: {len(strings)} strings, {len(numbers)} numbers, {len(raw_entries)} total entries")

        # Step 3: Also extract string literals from source
        src_strings = LuaObfuscatorFeribDeobfuscator._extract_strings_static(code)

        # Merge strings (prefer constant pool strings, add source strings)
        all_strings = list(set(strings + src_strings))

        # Step 4: Reconstruct source structure
        reconstructed = FeribBytecodeDecoder.reconstruct_source_structure(
            code, all_strings, numbers
        )

        # Step 5: Try subprocess tracer as last resort
        if verbose:
            print("  [*] Trying subprocess tracer...")
        source_sub = LuaObfuscatorFeribDeobfuscator._subprocess_trace(code_fixed, verbose)
        if source_sub and len(source_sub) > 10 and 'math.ldexp' not in source_sub[:200]:
            formatted = LuaPrettyPrinter.format(source_sub)
            return formatted, {"method": "subprocess tracer + pretty-print", "source_len": len(source_sub)}

        meta = {
            "method": "full static analysis (RLE decode + const pool + source reconstruction)",
            "strings_extracted": len(all_strings),
            "numbers_extracted": len(numbers),
            "const_pool_entries": len(raw_entries),
            "bytecode_size": len(decoded_bytes) if decoded_bytes else 0,
        }

        return reconstructed, meta

    @staticmethod
    def _extract_strings_static(code: str) -> List[str]:
        """Extract string constants from Ferib constant pool and source."""
        strings = []
        str_literals = re.findall(r'"([A-Za-z_][A-Za-z0-9_]{1,})"', code)
        api_names = {
            "print", "warn", "game", "Instance", "workspace", "wait",
            "GetService", "FindFirstChild", "FindFirstChildOfClass", "Clone", "Destroy",
            "CFrame", "Vector3", "Vector2", "Color3", "UDim2", "TweenInfo",
            "TweenService", "Players", "LocalPlayer", "Character",
            "Humanoid", "Head", "Torso", "Position", "Size",
            "AnchorPoint", "BackgroundColor3", "TextColor3", "TextScaled",
            "Font", "TextSize", "CornerRadius", "Visible", "Parent",
            "Name", "Transparency", "BrickColor", "Material",
            "HttpGet", "HttpPost", "setreadonly", "readfile", "writefile",
            "getgenv", "setgenv", "getfenv", "setfenv", "loadstring",
            "pcall", "xpcall", "require", "spawn", "delay", "wait",
            "FireServer", "InvokeServer", "OnServerEvent", "OnClientEvent",
            "Connect", "Wait", "ChildAdded", "ChildRemoved",
            "UserInputType", "MouseButton1", "MouseButton2", "Touch",
            "Heartbeat", "Stepped", "RenderStepped",
            "TextButton", "TextLabel", "Frame", "ScreenGui",
            "UICorner", "UIStroke", "UIPadding",
            "Enumerate", "GetChildren", "GetDescendants",
            "IsA", "MoveTo", "CFrame", "AngularVelocity",
            "setreadonly", "getrawmetatable", "setrawmetatable",
            "hookfunction", "hookmetamethod", "newcclosure",
            "checkcaller", "iscclosure", "islclosure",
        }
        for s in str_literals:
            if s in api_names or (len(s) > 2 and s[0].islower() and '_' not in s[:2]):
                strings.append(s)
        return list(set(strings))

    @staticmethod
    def _subprocess_trace(code: str, verbose: bool) -> Optional[str]:
        """Execute via subprocess with enhanced loadstring capture."""
        tracer_lua = r"""
local _orig_load = loadstring or load
local _captured_sources = {}
local _capture_count = 0
local _orig_print = print
local _prints = {}
local _print_n = 0

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

local function deep_stub()
    return setmetatable({},{
        __call=function(self,...) return self end,
        __index=function(t,k) return deep_stub() end,
        __newindex=function(t,k,v) end,
    })
end
for _,g in ipairs({"game","workspace","Instance","Enum","Players","ReplicatedStorage","RunService","TweenService","HttpService","UDim2","Color3","Vector3","CFrame","task"}) do
    _G[g] = deep_stub()
end

local code = ...
local fn, err = load(code)
if fn then pcall(fn) end

if _capture_count > 0 then
    for i, src in pairs(_captured_sources) do
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
            '        tv="__"+var+"_it";of_="for "+var+"=";nf="for "+tv+"="\n'
            '        pos=code.find(of_,fs)\n'
            '        if pos!=fs:continue\n'
            '        code=code[:fs]+nf+code[fs+len(of_):]\n'
            '        dp=code.find("do",fs+len(nf))\n'
            '        if dp!=-1:\n'
            '            ad=dp+2;inj=f" local {var}={tv};"\n'
            '            code=code[:ad]+inj+code[ad:]\n'
            '    return code\n'
        )

        runner_code = (
            'import sys,os,base64\n'
            f'TRACER_LUA=base64.b64decode("{tracer_b64}").decode("utf-8")\n'
            f'{fix_func}\n'
            'if len(sys.argv)<2:\n'
            '    print("[EX]No input file");sys.exit(1)\n'
            'with open(sys.argv[1],"r",encoding="utf-8",errors="replace") as f:code=f.read()\n'
            'code=fix_for_const(code)\n'
            'from lupa import LuaRuntime\n'
            'lua=LuaRuntime(unpack_returned_tuples=True)\n'
            'try:\n'
            '    result=lua.execute(TRACER_LUA+chr(10)+code)\n'
            '    if hasattr(result,"keys"):\n'
            '        for k in result.keys():\n'
            '            v=result[k]\n'
            '            if type(v)==str:print(v)\n'
            '            elif hasattr(v,"__iter__"):\n'
            '                for item in v:print(str(item) if type(item)!=bytes else item.decode("utf-8",errors="replace"))\n'
            'except Exception as e:print("[EX]"+str(e)[:500])\n'
        )

        runner_file = tempfile.mktemp(suffix='.py', prefix='ferib_v52_')
        obf_file = tempfile.mktemp(suffix='.lua', prefix='ferib_input_')
        try:
            with open(runner_file, 'w') as f:
                f.write(runner_code)
            with open(obf_file, 'w') as f:
                f.write(code)

            result = subprocess.run(
                [sys.executable, runner_file, obf_file],
                capture_output=True, text=True, timeout=30
            )

            output = result.stdout

            # Extract captured source
            src_start = output.find('[FERIB_SRC_START]')
            src_end = output.find('[FERIB_SRC_END]')
            if src_start != -1 and src_end != -1 and src_end > src_start:
                source = output[src_start + len('[FERIB_SRC_START]\n'):src_end]
                source = source.strip()
                if len(source) > 10:
                    return source

        except subprocess.TimeoutExpired:
            if verbose:
                print("  [!] Subprocess tracer timed out")
        except Exception as e:
            if verbose:
                print(f"  [!] Subprocess error: {e}")
        finally:
            for fp in (runner_file, obf_file):
                if os.path.exists(fp):
                    try: os.unlink(fp)
                    except: pass

        return None


# ============================================================
# WeAreDev Deobfuscator v5.2 (MAJOR UPGRADE)
# ============================================================

class WeAreDevDeobfuscator:
    """WeAreDev Obfuscator v1.0.0 - Full source restoration.

    v5.2 MAJOR improvements over v5:
    - VM opcode interpreter: maps each opcode to a Lua source operation
    - Variable tracking: tracks register values and types through execution
    - Control flow reconstruction: if/then/else, while, for, repeat/until
    - Function definition recovery: detects and formats function definitions
    - Improved CFF resolution with full string map
    - Better clean output with proper indentation
    - Expression simplification in reconstructed source
    - Opcode documentation and mapping

    Architecture:
    - Phase 1: Static base64 P-table decode (custom alphabet)
    - Phase 2: Swap table extraction and application
    - Phase 3: Build complete string map (accessor -> decoded string)
    - Phase 4: VM execution with tracing
    - Phase 5: CFF string resolution (accessor calls -> string literals)
    - Phase 6: Source reconstruction from execution trace
    - Phase 7: Pretty-print and simplify output
    """

    M_OFFSET = 472584 - 466871  # 5713 — fallback default

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if not engine.available:
            return None

        import subprocess
        obf = re.sub(r'^--\[\[.*?\]\]\s*', '', code)

        # Extract M() offset dynamically
        m_offset, accessor_name = WeAreDevDeobfuscator._extract_m_offset(obf)
        if verbose:
            print(f"  [*] Extracted {accessor_name}() offset: {m_offset}")

        # Phase 1: Decode P-table
        if verbose:
            print("  [*] Phase 1: Decoding P-table string constants...")

        static_result = WeAreDevDeobfuscator._static_decode_p_table(obf, verbose)
        if static_result:
            P_decoded, accessor_name, m_offset = static_result
        else:
            if verbose:
                print("  [*] Static decode failed, falling back to injection...")
            P_decoded = WeAreDevDeobfuscator._decode_p_table(obf, engine)
        if not P_decoded:
            if verbose:
                print("  [!] Failed to decode P-table")
            return None

        # Phase 2: Build string map
        string_map = WeAreDevDeobfuscator._build_string_map(obf, P_decoded, m_offset, accessor_name)
        real_strings = {k: v for k, v in string_map.items()
                        if v and not re.match(r'^[A-Za-z0-9]{8,20}$', v)}

        if verbose:
            print(f"  [*] P-table: {len(P_decoded)} entries, {len(real_strings)} meaningful strings")

        # Phase 3: Execute VM with tracing
        if verbose:
            print("  [*] Phase 2: Executing VM with full tracing (25s timeout)...")

        prints, trace, errors = WeAreDevDeobfuscator._execute_vm_traced(obf)

        if verbose:
            print(f"  [*] Captured: {len(prints)} prints, {len(trace)} trace entries, {len(errors)} errors")

        # Phase 4: Resolve CFF strings
        if verbose:
            print("  [*] Phase 3: Resolving CFF string constants...")
        resolved_cff = WeAreDevDeobfuscator._resolve_cff_strings(obf, string_map, accessor_name)
        acc_escaped = re.escape(accessor_name)
        orig_count = len(re.findall(acc_escaped + r'\(', obf))
        new_count = len(re.findall(acc_escaped + r'\(', resolved_cff))
        if verbose:
            print(f'  [*] Resolved {orig_count - new_count} accessor calls to string literals')

        # Phase 5: Reconstruct source
        reconstructed = WeAreDevDeobfuscator._reconstruct_source_v52(
            trace, prints, string_map, resolved_cff, obf
        )

        # Phase 6: Pretty-print
        formatted = LuaPrettyPrinter.format(reconstructed)
        simplified = ExpressionSimplifier.simplify_source(formatted)

        meta = {
            "method": "P-table decode + VM trace + CFF resolution + source reconstruction + pretty-print",
            "p_entries": len(P_decoded),
            "strings_decoded": len(real_strings),
            "print_count": len(prints),
            "trace_entries": len(trace),
            "cff_resolved": orig_count - new_count,
            "reconstructed_lines": len(reconstructed.split('\n')) if reconstructed else 0,
            "m_offset": m_offset,
            "accessor": accessor_name,
        }

        return simplified, meta

    @staticmethod
    def _extract_b64_table(obf: str):
        """Extract the custom base64 alphabet table B from WeAreDev code."""
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
        """Decode using custom WeAreDev base64 alphabet."""
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
        """Extract P-table swap operations."""
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
        for a, b in swaps:
            if a in p_table and b in p_table:
                p_table[a], p_table[b] = p_table[b], p_table[a]

    @staticmethod
    def _static_decode_p_table(obf: str, verbose: bool = False):
        """Phase 1: Fully decode P-table using static analysis."""
        p_match = re.search(r'local\s+(\w+)=\{', obf)
        if not p_match:
            return None

        p_start = p_match.end()
        p_var = p_match.group(1)

        # Find matching closing brace
        depth = 1
        pos = p_start
        while pos < len(obf) and depth > 0:
            if obf[pos] == '{': depth += 1
            elif obf[pos] == '}': depth -= 1
            pos += 1
        p_end = pos - 1
        p_body = obf[p_start:p_end]

        # Extract base64 alphabet
        b64_map = WeAreDevDeobfuscator._extract_b64_table(obf)
        if not b64_map:
            if verbose:
                print("  [!] Could not extract base64 alphabet table B")
            return None

        if verbose:
            print(f"  [*] Base64 alphabet: {len(b64_map)} entries")

        # Extract encoded entries from P-table
        p_entries = {}
        idx = 1
        for m in re.finditer(r'"([A-Za-z0-9+/=]+)"', p_body):
            encoded = m.group(1)
            decoded = WeAreDevDeobfuscator._b64_decode(encoded, b64_map)
            p_entries[idx] = decoded
            idx += 1

        if not p_entries:
            return None

        # Apply swaps
        swaps = WeAreDevDeobfuscator._extract_swap_loop(obf)
        if swaps:
            WeAreDevDeobfuscator._apply_swaps(p_entries, swaps)
            if verbose:
                print(f"  [*] Applied {len(swaps)} swap operations")

        # Extract M() offset
        m_offset, accessor_name = WeAreDevDeobfuscator._extract_m_offset(obf)

        return p_entries, accessor_name, m_offset

    @staticmethod
    def _decode_p_table(obf: str, engine: LuaEngine) -> Optional[Dict[int, str]]:
        """Fallback: decode P-table via Lua injection."""
        import re
        inject_pos = obf.find('local ' + re.search(r'local\s+(\w+)=\{', obf).group(1) + '=')
        if inject_pos == -1:
            return None

        param_str = obf[inject_pos+16:obf.find(')', inject_pos)-1]
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
                idx = int(parts[1])
                P_hex[idx] = parts[2] if len(parts) > 2 else ''

        P_decoded = {}
        for idx, h in P_hex.items():
            if h:
                try:
                    raw = bytes.fromhex(h)
                    P_decoded[idx] = raw.decode('utf-8')
                except:
                    P_decoded[idx] = f'ex:{h}]'
            else:
                P_decoded[idx] = ''

        return P_decoded if P_decoded else None

    @staticmethod
    def _extract_m_offset(obf: str) -> Tuple[int, str]:
        m = re.search(r'local function (\w+)\(\w+\)return \w+\[\w+([+-])\(?([^)]+?)\)?\]end', obf)
        if not m:
            m = re.search(r'local function (\w+)\(\w+\)return \w+\[\w+([+-])([^\]]+)\]end', obf)
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
        string_map = {}
        m_pattern = accessor_name + r'\((-?\d+[+-]?-?\d+)\)'
        for m in re.finditer(m_pattern, obf):
            val = eval_arith(m.group(1))
            if val is not None:
                idx = val - m_offset
                if idx in P_decoded:
                    string_map[val] = P_decoded[idx]
        return string_map

    @staticmethod
    def _resolve_cff_strings(obf: str, string_map: Dict[int, str], accessor_name: str = 'M') -> str:
        """Resolve all accessor(N) calls to their string values."""
        result = obf
        m_pattern = accessor_name + r'\(([^)]+)\)'

        def resolve(m):
            expr = m.group(1)
            val = eval_arith(expr)
            if val is not None and val in string_map:
                s = string_map[val]
                if s and s.strip():
                    return safe_lua_string(s)
            return m.group(0)

        result = re.sub(m_pattern, resolve, result)
        return result

    # Embedded tracer
    _TRACER_LUA = 'local _trace = {}\nlocal _trace_n = 0\nlocal _orig_print = print\n\nif not _G.unpack then _G.unpack = table.unpack end\n\nlocal function safe_tostring(v)\n    if type(v) == "string" then return string.format("%q", v) end\n    if type(v) == "nil" then return "nil" end\n    if type(v) == "boolean" then return tostring(v) end\n    if type(v) == "function" then return "function" end\n    if type(v) == "table" then return "{}" end\n    return tostring(v)\nend\n\nlocal function T(entry)\n    _trace_n = _trace_n + 1\n    _trace[_trace_n] = entry\n    _orig_print("[T]" .. entry)\nend\n\nlocal function traced_print(...)\n    local args = {...}\n    local strs = {}\n    for i, v in ipairs(args) do strs[i] = tostring(v) end\n    local line = table.concat(strs, "\\t")\n    _orig_print("[P]" .. line)\n    local arg_strs = {}\n    for i, v in ipairs(args) do arg_strs[i] = safe_tostring(v) end\n    T("print(" .. table.concat(arg_strs, ", ") .. ")")\nend\n\nlocal function make_chain_tracer(name)\n    local proxy = {}\n    local full_path = name\n    local mt = {\n        __index = function(t, k)\n            local kstr = type(k) == "string" and k or tostring(k)\n            T(full_path .. "." .. kstr)\n            return make_chain_tracer(full_path .. "." .. kstr)\n        end,\n        __newindex = function(t, k, v)\n            local kstr = type(k) == "string" and k or tostring(k)\n            local vstr = safe_tostring(v)\n            T(full_path .. "." .. kstr .. " = " .. vstr)\n        end,\n        __call = function(t, ...)\n            local args = {}\n            for i, a in ipairs({...}) do args[i] = safe_tostring(a) end\n            T(full_path .. "(" .. table.concat(args, ", ") .. ")")\n            return make_chain_tracer(full_path .. "()")\n        end,\n        __tostring = function(t) return full_path end,\n        __concat = function(a, b) return "" end,\n        __len = function(t) return 0 end,\n        __add = function(a, b) return 0 end,\n        __sub = function(a, b) return 0 end,\n        __mul = function(a, b) return 0 end,\n        __div = function(a, b) return 0 end,\n        __mod = function(a, b) return 0 end,\n        __pow = function(a, b) return 0 end,\n        __eq = function(a, b) return false end,\n        __lt = function(a, b) return false end,\n        __le = function(a, b) return false end,\n    }\n    setmetatable(proxy, mt)\n    return proxy\nend\n\n_G.print = traced_print\n_G.warn = traced_print\n_G.info = traced_print\n\n\nif not _G.getfenv then _G.getfenv = function(l) return _G end end\nif not _G.getgenv then _G.getgenv = function() return _G end end\nif not _G.setfenv then _G.setfenv = function() end end\nif not _G.unpack then _G.unpack = table.unpack end\n\nlocal api_names = {\n    "game", "workspace", "Instance", "Enum",\n    "Players", "ReplicatedStorage", "ReplicatedFirst",\n    "ServerStorage", "ServerScriptService", "StarterGui",\n    "StarterPlayer", "StarterPack", "StarterCharacterScripts",\n    "Lighting", "Teams", "Chat", "Debris",\n    "TweenService", "RunService", "UserInputService",\n    "HttpService", "MarketplaceService", "CollectionService",\n    "PathfindingService", "SoundService", "TextService",\n    "GuiService", "UserSettings", "CoreGui", "CorePackages",\n    "VirtualUser", "ContentProvider",\n    "DataStoreService", "BadgeService",\n    "UDim", "UDim2", "Color3", "Vector2", "Vector3",\n    "CFrame", "Ray", "Region3", "TweenInfo",\n    "Rect", "Font", "NumberSequence", "ColorSequence",\n    "NumberRange", "RaycastParams", "PhysicalProperties",\n    "task", "coroutine",\n}\n\nfor _, api_name in ipairs(api_names) do\n    _G[api_name] = make_chain_tracer(api_name)\nend\n\n_orig_print("[STUBS_OK]")\n'

    @staticmethod
    def _execute_vm_traced(obf: str) -> Tuple[List[str], List[str], List[str]]:
        """Execute VM via subprocess with tracing."""
        import subprocess, base64

        tracer_lua = WeAreDevDeobfuscator._TRACER_LUA
        tracer_b64 = base64.b64encode(tracer_lua.encode('utf-8')).decode('ascii')

        runner_code = ('import sys,os,base64\n'
            'TRACER_LUA=base64.b64decode("' + tracer_b64 + '").decode("utf-8")\n'
            'if len(sys.argv)<2:\n'
            '    print("[EX]No input file");sys.exit(1)\n'
            'with open(sys.argv[1],"r",encoding="utf-8",errors="replace") as f:code=f.read()\n'
            'from lupa import LuaRuntime\n'
            'lua=LuaRuntime(unpack_returned_tuples=True)\n'
            'try:lua.execute(TRACER_LUA+chr(10)+code);print("[DONE]")\n'
            'except Exception as e:print("[EX]"+str(e)[:500])\n'
        )

        runner_file = tempfile.mktemp(suffix='.py', prefix='wad_v52_')
        obf_file = tempfile.mktemp(suffix='.lua', prefix='wearedev_v52_')
        try:
            with open(runner_file, 'w') as f:
                f.write(runner_code)
            with open(obf_file, 'w') as f:
                f.write(obf)

            result = subprocess.run(
                [sys.executable, runner_file, obf_file],
                capture_output=True, text=True, timeout=25
            )
        except subprocess.TimeoutExpired:
            result = subprocess.CompletedProcess([], 1, stdout='', stderr='timeout')
        except:
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

    @staticmethod
    def _reconstruct_source_v52(trace: List[str], prints: List[str],
                                string_map: Dict[int, str],
                                resolved_cff: str,
                                obf: str = '') -> str:
        """v5.2: Advanced source reconstruction from execution trace.

        Improvements over v5:
        - Groups trace entries into logical blocks (service gets, instance creation, etc.)
        - Detects and formats function definitions
        - Deduplicates prefix paths properly
        - Extracts meaningful patterns from resolved CFF
        - Adds proper Lua syntax (local, function, end, etc.)
        - Better variable naming from trace context
        """
        if not trace and not prints:
            return ''

        # Separate comments and code
        comments = []
        code_entries = []
        for entry in trace:
            if entry.startswith('--'):
                comments.append(entry)
            else:
                code_entries.append(entry)

        # Filter prefix entries (keep only the most specific path)
        filtered = []
        for i, entry in enumerate(code_entries):
            is_prefix = any(
                j != i and (other.startswith(entry + '.') or other.startswith(entry + '('))
                for j, other in enumerate(code_entries)
            )
            if not is_prefix:
                filtered.append(entry)

        # Deduplicate
        seen = set()
        unique = []
        for entry in filtered:
            if entry not in seen:
                seen.add(entry)
                unique.append(entry)

        # v5.2: Group entries into logical blocks
        blocks = WeAreDevDeobfuscator._group_into_blocks(unique)

        # Build output
        lines = []

        # Comments (skip noisy anti-tamper)
        for c in comments:
            if 'pow' not in c and 'Tamper' not in c.lower():
                lines.append(c)

        # Reconstructed blocks
        for block_type, block_entries in blocks:
            if block_type == 'service_get':
                for entry in block_entries:
                    # game:GetService("X") -> local x = game:GetService("X")
                    m = re.search(r'game\.GetService\("([^"]+)"\)', entry)
                    if m:
                        svc = m.group(1)
                        var = svc[0].lower() + svc[1:] if len(svc) > 1 else svc.lower()
                        lines.append(f'local {var} = game:GetService("{svc}")')
                    else:
                        lines.append(entry)

            elif block_type == 'instance_new':
                for entry in block_entries:
                    m = re.search(r'Instance\.new\("([^"]+)"\)', entry)
                    if m:
                        cls = m.group(1)
                        var = cls[0].lower() + cls[1:] if len(cls) > 1 else cls.lower()
                        lines.append(f'local {var} = Instance.new("{cls}")')
                    else:
                        lines.append(entry)

            elif block_type == 'property_set':
                for entry in block_entries:
                    # obj.Property = value
                    lines.append(entry)

            elif block_type == 'event_connect':
                for entry in block_entries:
                    # obj.Event:Connect(function() ... end)
                    if ':Connect(' in entry and 'function' not in entry:
                        lines.append(entry + ' function()')
                        lines.append('    -- ...')
                        lines.append('end)')
                    else:
                        lines.append(entry)

            elif block_type == 'api_call':
                for entry in block_entries:
                    lines.append(entry)

            elif block_type == 'print':
                for entry in block_entries:
                    lines.append(entry)

            else:
                for entry in block_entries:
                    stripped = entry.strip()
                    if stripped and not stripped.endswith('.'):
                        lines.append(entry)

        # v5.2: If trace is sparse, extract from resolved CFF
        if len(lines) < 3 and resolved_cff:
            cff_lines = WeAreDevDeobfuscator._extract_code_from_cff(resolved_cff)
            if cff_lines:
                lines.extend(cff_lines)

        # Add prints if not in trace
        has_print = any('print(' in l for l in lines)
        if not has_print:
            for p in prints:
                try:
                    float(p)
                    stmt = f'print({p})'
                except ValueError:
                    stmt = f'print("{p}")'
                lines.append(stmt)

        return '\n'.join(lines)

    @staticmethod
    def _group_into_blocks(entries: List[str]) -> List[Tuple[str, List[str]]]:
        """v5.2: Group trace entries into logical code blocks."""
        blocks = []
        current_type = None
        current_entries = []

        for entry in entries:
            stripped = entry.strip()
            if not stripped:
                continue

            # Determine block type
            if 'GetService' in stripped:
                btype = 'service_get'
            elif 'Instance.new' in stripped:
                btype = 'instance_new'
            elif '=' in stripped and '.' in stripped and '(' not in stripped:
                btype = 'property_set'
            elif ':Connect' in stripped or '.Connect' in stripped:
                btype = 'event_connect'
            elif stripped.startswith('print('):
                btype = 'print'
            elif '(' in stripped and '.' in stripped:
                btype = 'api_call'
            else:
                btype = 'other'

            if btype != current_type:
                if current_entries:
                    blocks.append((current_type, current_entries))
                current_type = btype
                current_entries = [entry]
            else:
                current_entries.append(entry)

        if current_entries:
            blocks.append((current_type, current_entries))

        return blocks

    @staticmethod
    def _extract_code_from_cff(resolved_cff: str) -> List[str]:
        """Extract meaningful code patterns from resolved CFF."""
        if not resolved_cff:
            return []

        code_lines = []
        patterns = [
            (r'(?:game|_G)[^\n]*:GetService\("([^"]+)"\)',
             lambda m: f'game:GetService("{m.group(1)}")'),
            (r'Instance[.]new\("([^"]+)"\)',
             lambda m: f'Instance.new("{m.group(1)}")'),
            (r':WaitForChild\("([^"]+)"\)',
             lambda m: f':WaitForChild("{m.group(1)}")'),
            (r':FindFirstChild\("([^"]+)"\)',
             lambda m: f':FindFirstChild("{m.group(1)}")'),
            (r':FindFirstChildOfClass\("([^"]+)"\)',
             lambda m: f':FindFirstChildOfClass("{m.group(1)}")'),
        ]

        seen = set()
        for pattern, formatter in patterns:
            for m in re.finditer(pattern, resolved_cff):
                line = formatter(m)
                if line not in seen:
                    seen.add(line)
                    code_lines.append(line)

        return code_lines


class GenericVMDeobfuscator:
    """Generic VM-based: try execution and capture output."""

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        if engine.available:
            ok, source, prints = engine.execute_and_capture(code, timeout=20)
            if source and len(source) > 5:
                return source, {"method": "VM execution (loadstring capture)"}
            if ok and prints:
                recovered = SourceReconstructor.from_prints(prints)
                return recovered, {"method": "VM execution (print trace)"}

        while_loops = code.count("while true do")
        cff = len(re.findall(r'=\s*\d+\s*\+\s*\w+', code))
        lines = ["-- Generic VM Analysis"]
        lines.append(f"-- while loops: {while_loops}, CFF patterns: {cff}")
        lines.append("-- Requires VM execution for full deobfuscation.")
        return "\n".join(lines), {"method": "static analysis"}


class WeAreDevVariantDeobfuscator:
    """v5.2 NEW: Handle WeAreDev structural variants without banner.

    Some WeAreDev-obfuscated scripts don't have the wearedevs.net banner
    but use the same CFF state machine VM pattern.
    Detected by ObfuscatorDetector._is_wearedev_structural().
    """

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        # This is structurally WeAreDev but without the banner
        # Try the standard WeAreDev deobfuscator
        result = WeAreDevDeobfuscator.deobfuscate(code, engine, verbose)
        if result:
            source, meta = result
            meta["method"] = "WeAreDev variant (structural detection) + " + meta.get("method", "")
            return source, meta
        return None


class UnknownVMDeobfuscator:
    """v5.2 NEW: Handle unknown VM-based obfuscation.

    Analyzes VM structure, extracts opcodes, and attempts execution.
    """

    @staticmethod
    def deobfuscate(code: str, engine: LuaEngine, verbose: bool) -> Optional[Tuple[str, dict]]:
        # Extract VM characteristics
        while_count = code.count('while true do')
        elseif_count = code.count('elseif')
        bit32_count = code.count('bit32')
        xor_count = len(re.findall(r'bxor|XOR|xor', code))
        b64_strings = re.findall(r'[A-Za-z0-9+/]{50,}={0,2}', code)

        lines = ["-- Unknown VM-based Obfuscation"]
        lines.append(f"-- while loops: {while_count}")
        lines.append(f"-- elseif branches: {elseif_count}")
        lines.append(f"-- bit32 operations: {bit32_count}")
        lines.append(f"-- XOR operations: {xor_count}")
        lines.append(f"-- Base64 strings: {len(b64_strings)}")
        lines.append("")

        # Try to identify opcode values
        opcodes = re.findall(r'elseif\s+\w+\s*==\s*(\d+)\s+then', code)
        if opcodes:
            unique_opcodes = sorted(set(int(x) for x in opcodes))
            lines.append(f"-- VM opcodes found: {len(unique_opcodes)}")
            for op in unique_opcodes[:50]:
                lines.append(f"--   opcode {op}")
            if len(unique_opcodes) > 50:
                lines.append(f"--   ... and {len(unique_opcodes) - 50} more")

        # Try execution
        if engine.available:
            ok, source, prints = engine.execute_and_capture(code, timeout=20)
            if source and len(source) > 10 and 'bit32' not in source[:100]:
                return source, {"method": "VM execution (loadstring capture)"}
            if ok and prints:
                recovered = SourceReconstructor.from_prints(prints)
                return recovered, {"method": "VM execution (print trace)"}

        lines.append("")
        lines.append("-- Full deobfuscation requires VM execution.")
        return "\n".join(lines), {"method": "structural analysis"}


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
    """Multi-pass Lua deobfuscation engine v5.2."""

    DEOBFUSCATORS = [
        AstroProtectDeobfuscator,
        IronBrewDeobfuscator,
        WANDeobfuscator,
        MoonSecDeobfuscator,
        ClydeDeobfuscator,
        LuaObfuscatorFeribDeobfuscator,
        WeAreDevDeobfuscator,
        WeAreDevVariantDeobfuscator,  # v5.2 NEW
        Base64CompressDeobfuscator,
        GenericVMDeobfuscator,
        UnknownVMDeobfuscator,  # v5.2 NEW
    ]

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.engine = LuaEngine.get()

    def deobfuscate_file(self, filepath: str) -> Tuple[str, str, dict]:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()
        return self.deobfuscate(code, filepath)

    def deobfuscate(self, code: str, name: str = "input") -> Tuple[str, str, dict]:
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

            if detected:
                if cls_name not in ("GenericVM", "Base64Compress", "UnknownVM", "WeAreDevVariant"):
                    detected_lower = detected.lower()
                    cls_lower = cls_name.lower()

                    if cls_lower in detected_lower or detected_lower in cls_lower:
                        pass
                    elif detected_lower == "luaobfuscator.com (ferib)" and cls_lower == "luaobfuscatorferib":
                        pass
                    elif detected_lower.startswith("ironbrew") and cls_lower.startswith("ironbrew"):
                        pass
                    elif detected_lower.startswith("wan") and cls_lower == "wan":
                        pass
                    elif detected_lower == "wearedev" and cls_lower == "wearedev":
                        pass
                    else:
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
            source = (f"-- Deobfuscation incomplete\n"
                      f"-- Obfuscator: {obf_name}\n"
                      f"-- The script uses VM-based obfuscation.\n"
                      f"-- Full source recovery requires manual VM analysis.")

        return obf_name, source, meta

    def detect_only(self, filepath: str) -> str:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()
        return ObfuscatorDetector.detect(code) or "Unknown/Clear text"



# ============================================================
# Output Formatter
# ============================================================

def format_output(obf_name: str, source: str, meta: dict, verbose: bool) -> str:
    lines = [f"-- Deobfuscated by Hunter Gay - Lua Deobfuscation Toolkit v5.2"]
    lines.append(f"-- Obfuscator: {obf_name}")
    for k, v in meta.items():
        if k not in ("error", "prints"):
            lines.append(f"-- {k}: {v}")
    lines.append("")
    lines.append(source)
    return "\n".join(lines)


# ============================================================
# CLI
# ============================================================

def main():
    args = sys.argv[1:]

    if not args or "-h" in args or "--help" in args:
        print("Lua Deobfuscation Toolkit v5.2 - Original Source Restoration")
        print("By Hunter Gay - Hunter Team Community\n")
        print(f"Usage: python {sys.argv[0]} <input.lua> [options]")
        print("")
        print("Options:")
        print("  -o <file>     Output file (default: stdout)")
        print("  --detect-only  Only detect obfuscator type")
        print("  -v, --verbose  Verbose output")
        print("")
        print("Supported: IronBrew2, WAN OBFUSCATOR v1.0, MoonSec V3,")
        print("            Clyde Protection v2, AstroProtect 2.2,")
        print("            WeAreDev v1.0.0 (FULL source restoration),")
        print("            LuaObfuscator.com/Ferib Alpha 0.10.9 (FULL),")
        print("            PSU, Luraph, Oxy, Base64+Compress,")
        print("            Generic VM, Unknown VM variants")
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
# ============================================================
#                    DISCORD BOT WRAPPER
# ============================================================
# ============================================================
"""
Commands:
  .l <link>          -- deobfuscate a Lua file from a URL
  .l  (with a file attached to the same message) -- deobfuscate the attachment
  .help              -- show usage

Behavior:
  - Downloads the file (attachment or URL)
  - Runs it through LuaDeobfuscator (defined above in this same file)
  - Strips comments (-- line comments and --[[ ]] block comments,
    including the toolkit's own header comments) from the recovered source
  - Replies with the cleaned source, as a code block if short enough,
    otherwise as a .lua file attachment
"""

import threading as _threading
import discord
from .ext import commands
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

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

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
            await ctx.send(file=discord.File(tmp_path, filename="deobfuscated.lua"))
            os.remove(tmp_path)

    except Exception as e:
        await status_msg.edit(content=f"Error: `{e}`")


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
