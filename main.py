

import discord
from discord import app_commands
from discord.ext import commands
import discord.ext.tasks as tasks
import asyncio
import re
import os
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path
from urllib.parse import urlparse
import os
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()


# ============================================================
# CONFIGURATION
# ============================================================
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_TOKEN")
LUA51_BIN = '/tmp/lua-5.1.5/src/lua'
ROBLOX_ENV_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'roblox_env.lua')
MAX_FILE_SIZE = 500_000  # 500KB max
MAX_URL_SIZE = 1_000_000

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
# discord.py command tree setup is done via @bot.tree(), remove standalone tree
# tree = app_commands.CommandTree()

# ============================================================
# WEAREDEV DEOBFUSCATION ENGINE
# ============================================================

def eval_arith(expr: str):
    """Safely evaluate an arithmetic expression like -763782-(-763791)."""
    expr = expr.strip()
    if re.match(r'^[0-9+\-*/()\s]+$', expr):
        try:
            return eval(expr)
        except Exception:
            return None
    return None


def resolve_lua_escapes(s: str) -> str:
    """Resolve \\NNN escape sequences in a Lua string."""
    result = []
    i = 0
    while i < len(s):
        if s[i] == '\\' and i + 3 < len(s) and s[i+1:i+4].isdigit():
            code = int(s[i+1:i+4])
            result.append(chr(code))
            i += 4
        elif s[i] == '\\' and i + 1 < len(s):
            esc = s[i+1]
            if esc == 'n': result.append('\n')
            elif esc == 'r': result.append('\r')
            elif esc == 't': result.append('\t')
            elif esc == '"': result.append('"')
            elif esc == "'": result.append("'")
            elif esc == '\\': result.append('\\')
            else: result.append(esc)
            i += 2
        else:
            result.append(s[i])
            i += 1
    return ''.join(result)


def extract_ptable(code: str, pvar: str):
    """Extract P-table entries from obfuscated Lua code."""
    pattern = rf'local\s+{re.escape(pvar)}\s*=\s*\{{'
    m = re.search(pattern, code)
    if not m:
        return [], 0, 0
    start = m.end()
    depth = 1
    pos = start
    while pos < len(code) and depth > 0:
        if code[pos] == '{': depth += 1
        elif code[pos] == '}': depth -= 1
        pos += 1
    end = pos - 1
    block = code[start:end]
    entries = re.findall(r'"(.*?)"', block)
    return entries, m.start(), end


def extract_alphabet(code: str, avar: str):
    """Extract the custom base64 alphabet table."""
    pattern = rf'local\s+{re.escape(avar)}\s*=\s*\{{'
    m = re.search(pattern, code)
    if not m:
        return {}
    start = m.end()
    depth = 1
    pos = start
    while pos < len(code) and depth > 0:
        if code[pos] == '{': depth += 1
        elif code[pos] == '}': depth -= 1
        pos += 1
    end_pos = pos - 1
    block = code[start:end_pos]
    alphabet = {}
    for entry in re.split(r'[,;]', block):
        entry = entry.strip()
        if not entry:
            continue
        for pat in [r'\["(.*?)"\]\s*=\s*(.*)', r'"(.*?)"\s*=\s*(.*)', r'(\w+)\s*=\s*(.*)']:
            m2 = re.match(pat, entry)
            if m2:
                key = resolve_lua_escapes(m2.group(1))
                val = eval_arith(m2.group(2))
                if val is not None:
                    alphabet[key] = val
                break
    return alphabet


def parse_shuffle_ranges(shuffle_code: str):
    """Parse shuffle loop to extract reverse ranges (1-based Lua indices)."""
    ranges = []
    m = re.search(r'ipairs\s*\(\s*\{(.*?)\}\s*\)', shuffle_code, re.DOTALL)
    if not m:
        return ranges
    inner = m.group(1)
    for pair in re.findall(r'\{([^}]+)\}', inner):
        parts = re.split(r'[,;]', pair)
        if len(parts) == 2:
            a = eval_arith(parts[0].strip())
            b = eval_arith(parts[1].strip())
            if a is not None and b is not None:
                ranges.append((a, b))
    return ranges


def shuffle_array(arr, ranges):
    """Apply shuffle: reverse subarrays at given 1-based Lua indices."""
    arr = list(arr)
    for start, end in ranges:
        s = start - 1
        e = end - 1
        while s < e:
            arr[s], arr[e] = arr[e], arr[s]
            s += 1
            e -= 1
    return arr


def decode_custom_base64(alphabet: dict, encoded_str: str) -> bytes:
    """Decode a custom base64 encoded string using the alphabet."""
    result = []
    bits = 0
    bit_buffer = 0
    for ch in encoded_str:
        if ch not in alphabet:
            continue
        val = alphabet[ch]
        bit_buffer = (bit_buffer << 6) | val
        bits += 6
        while bits >= 8:
            bits -= 8
            byte = (bit_buffer >> bits) & 0xFF
            result.append(byte)
    return bytes(result)


def find_shuffle_code(code: str, ptable_end: int) -> str:
    """Find the shuffle for-loop after P-table."""
    after = code[ptable_end:]
    m = re.search(r'for\s+\w+\s*,\s*\w+\s+in\s+ipairs\s*\(', after)
    if not m:
        return ''
    remaining = after[m.start():]
    m2 = re.search(r'end\s*end\s*end', remaining)
    if m2:
        return remaining[:m2.end()]
    m2 = re.search(r'end\s*end', remaining[200:])
    if m2:
        return remaining[:200 + m2.end()]
    return remaining[:800]


def detect_wearedev(code: str) -> dict:
    """Detect WeAreDev obfuscation and extract structure info."""
    info = {
        'is_wearedev': False,
        'has_header': False,
        'pvar': None,
        'avar': None,
        'ptable_count': 0,
        'alphabet_count': 0,
        'shuffle_ranges': [],
        'decoded_strings': [],
        'errors': [],
    }
    
    # Check header
    header = re.match(r'--\[\[.*?wearedev.*?\]\]\s*', code, re.IGNORECASE)
    if header:
        info['has_header'] = True
        code = code[header.end():]
    
    # Detect P-table
    ptable_match = re.search(r'local\s+(\w+)\s*=\s*\{"', code)
    if not ptable_match:
        info['errors'].append('No P-table found')
        return info
    
    info['is_wearedev'] = True
    info['pvar'] = ptable_match.group(1)
    
    # Extract P-table
    entries, ps, pe = extract_ptable(code, info['pvar'])
    info['ptable_count'] = len(entries)
    if len(entries) == 0:
        info['errors'].append('P-table empty')
        return info
    
    # Find shuffle code
    shuffle_code = find_shuffle_code(code, pe)
    ranges = parse_shuffle_ranges(shuffle_code)
    info['shuffle_ranges'] = ranges
    
    # Shuffle
    if ranges:
        entries = shuffle_array(entries, ranges)
    
    # Find alphabet table (second local X={...} after P-table)
    all_locals = list(re.finditer(r'local\s+(\w+)\s*=\s*\{', code))
    if len(all_locals) < 2:
        info['errors'].append('Alphabet table not found')
        return info
    
    info['avar'] = all_locals[1].group(1)
    alphabet = extract_alphabet(code, info['avar'])
    info['alphabet_count'] = len(alphabet)
    
    if len(alphabet) != 64:
        # Try all local tables
        for lm in all_locals[1:]:
            try_alpha = extract_alphabet(code, lm.group(1))
            if len(try_alpha) == 64:
                alphabet = try_alpha
                info['avar'] = lm.group(1)
                info['alphabet_count'] = 64
                break
    
    if len(alphabet) < 32:
        info['errors'].append(f'Alphabet too small: {len(alphabet)} entries')
        return info
    
    # Decode all entries
    for i, entry in enumerate(entries):
        resolved = resolve_lua_escapes(entry)
        decoded = decode_custom_base64(alphabet, resolved)
        try:
            text = decoded.decode('utf-8', errors='strict')
            if len(text) >= 1 and all(32 <= ord(c) < 127 or c in '\n\r\t' for c in text):
                info['decoded_strings'].append((i, text))
        except (UnicodeDecodeError, ValueError):
            pass
    
    return info


# ============================================================
# LUA 5.1 EXECUTION FALLBACK
# ============================================================

def ensure_lua51():
    """Ensure Lua 5.1 is built and available."""
    if os.path.isfile(LUA51_BIN):
        return True
    lua_src = '/tmp/lua-5.1.5'
    if not os.path.isdir(lua_src):
        import urllib.request
        url = 'https://www.lua.org/ftp/lua-5.1.5.tar.gz'
        try:
            subprocess.run(['wget', '-q', url, '-O', '/tmp/lua51.tar.gz'], check=True, timeout=30)
            subprocess.run(['tar', 'xzf', '/tmp/lua51.tar.gz', '-C', '/tmp/'], check=True)
        except Exception:
            pass
    if os.path.isdir(lua_src):
        try:
            subprocess.run(['make', 'linux', '-C', lua_src + '/src'],
                           capture_output=True, timeout=60)
            # Build without readline
            subprocess.run(['gcc', '-O2', '-ULUA_USE_READLINE', '-c',
                          '-o', f'{lua_src}/src/lua.o', f'{lua_src}/src/lua.c'],
                          capture_output=True, timeout=30)
            subprocess.run(['gcc', '-o', LUA51_BIN, f'{lua_src}/src/lua.o',
                          f'{lua_src}/src/liblua.a', '-lm', '-Wl,-E', '-ldl'],
                          capture_output=True, timeout=30)
            return os.path.isfile(LUA51_BIN)
        except Exception:
            pass
    return False


def try_lua51_execution(code: str) -> tuple:
    """Try to deobfuscate using Lua 5.1 with Roblox env."""
    if not ensure_lua51():
        return None, 'Lua 5.1 not available'
    
    if not os.path.isfile(ROBLOX_ENV_SCRIPT):
        return None, 'Roblox env script not found'
    
    # Strip header
    header = re.match(r'--\[\[.*?\]\]\s*', code)
    if header:
        code = code[header.end():]
    
    # Write script to temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.lua', delete=False) as tf:
        tf.write(code)
        script_path = tf.name
    
    output_path = '/tmp/deobf_result.lua'
    
    try:
        result = subprocess.run(
            [LUA51_BIN, ROBLOX_ENV_SCRIPT, script_path],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, 'LUA_PATH': '/tmp/?.lua'}
        )
        
        if os.path.isfile(output_path):
            with open(output_path, 'r', errors='replace') as f:
                source = f.read()
            os.remove(output_path)
            if len(source) > 100:
                return source, 'success via Lua 5.1'
        
        stderr = result.stderr or ''
        stdout = result.stdout or ''
        error_msg = stderr[:300]
        if 'CAPTURED' in stdout:
            return None, 'Lua 5.1: partial capture (tamper detected)'
        return None, f'Lua 5.1: {error_msg}'
    except subprocess.TimeoutExpired:
        return None, 'Lua 5.1: timeout'
    except Exception as e:
        return None, f'Lua 5.1: {e}'
    finally:
        try:
            os.remove(script_path)
        except Exception:
            pass


# ============================================================
# STRING RECONSTRUCTION
# ============================================================

def reconstruct_source(info: dict) -> str:
    """Best-effort source reconstruction from decoded strings."""
    strings = info.get('decoded_strings', [])
    if not strings:
        return ''
    
    # Categorize strings
    lua_keywords = {'local', 'function', 'end', 'if', 'then', 'else', 'elseif',
                    'return', 'for', 'while', 'do', 'repeat', 'until', 'in',
                    'not', 'and', 'or', 'true', 'false', 'nil', 'break'}
    api_calls = {'GetService', 'FindFirstChild', 'WaitForChild', 'GetChildren',
                 'GetDescendants', 'IsA', 'Clone', 'Destroy', 'Connect',
                 'Fire', 'InvokeServer', 'FireServer', 'Wait', 'Play',
                 'Create', 'Insert', 'Remove', 'FindFirstChildOfClass',
                 'FindFirstChildWhichIsA', 'GetPropertyChangedSignal',
                 'Changed', 'ChildAdded', 'ChildRemoved'}
    
    lines = []
    lines.append('-- WeAreDev Deobfuscated (v5.5 reconstruction)')
    lines.append(f'-- Original had {info["ptable_count"]} P-table entries')
    lines.append(f'-- Extracted {len(strings)} readable strings')
    lines.append(f'-- Shuffle ranges: {info["shuffle_ranges"]}')
    lines.append('')
    
    # Group strings by category
    api_strings = [(i, s) for i, s in strings if any(s.startswith(k) or '.' + k in s for k in api_calls)]
    keyword_strings = [(i, s) for i, s in strings if s in lua_keywords]
    other_strings = [(i, s) for i, s in strings if (i, s) not in api_strings and (i, s) not in keyword_strings]
    
    if api_strings:
        lines.append('-- Roblox API Calls:')
        for idx, s in sorted(api_strings, key=lambda x: x[0]):
            lines.append(f'  -- [{idx}] {s}')
        lines.append('')
    
    if keyword_strings:
        lines.append('-- Lua Keywords:')
        for idx, s in sorted(keyword_strings, key=lambda x: x[0]):
            lines.append(f'  -- [{idx}] {s}')
        lines.append('')
    
    if other_strings:
        lines.append('-- Other Strings (possible identifiers/values):')
        for idx, s in sorted(other_strings, key=lambda x: x[0]):
            lines.append(f'  -- [{idx}] {s}')
        lines.append('')
    
    # Try to reconstruct a minimal script structure
    lines.append('-- === RECONSTRUCTED STRUCTURE ===')
    lines.append('-- Note: This is a best-effort reconstruction.')
    lines.append('-- Full deobfuscation requires VM execution in Roblox environment.')
    lines.append('')
    
    # Detect script type from strings
    all_str = ' '.join(s for _, s in strings)
    if 'CreateWindow' in all_str or 'AddToggle' in all_str or 'AddButton' in all_str:
        lines.append('-- Detected: GUI/Library script (possibly Rayfield or similar)')
    elif 'fireproximityprompt' in all_str or 'firetouchinterest' in all_str:
        lines.append('-- Detected: Game cheat/exploit script')
    elif 'GetService' in all_str and 'Players' in all_str:
        lines.append('-- Detected: Roblox game script')
    
    return '\n'.join(lines)


# ============================================================
# MAIN DEOBFUSCATION PIPELINE
# ============================================================

def deobfuscate(code: str, filename: str = 'script.lua') -> dict:
    """Full deobfuscation pipeline."""
    result = {
        'success': False,
        'method': None,
        'source': '',
        'analysis': None,
        'error': None,
        'filename': filename,
    }
    
    # Step 1: Detect
    info = detect_wearedev(code)
    result['analysis'] = info
    
    if not info['is_wearedev']:
        result['error'] = 'Not a WeAreDev obfuscated script'
        return result
    
    # Step 2: Try Lua 5.1 execution (best results)
    source, status = try_lua51_execution(code)
    if source and len(source) > 100:
        result['success'] = True
        result['method'] = 'Lua 5.1 execution'
        result['source'] = source
        return result
    
    # Step 3: Fall back to string reconstruction
    reconstructed = reconstruct_source(info)
    result['method'] = 'string reconstruction'
    result['source'] = reconstructed
    result['lua51_status'] = status
    result['success'] = len(reconstructed) > 200
    
    return result


# ============================================================
# URL FETCH
# ============================================================

async def fetch_lua_from_url(url: str) -> tuple:
    """Fetch Lua script content from URL."""
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None, f'HTTP {resp.status}'
                text = await resp.text()
                if len(text) > MAX_URL_SIZE:
                    return None, f'Content too large ({len(text)} bytes)'
                return text, None
    except ImportError:
        return None, 'aiohttp not installed (pip install aiohttp)'
    except Exception as e:
        return None, str(e)


# ============================================================
# DISCORD BOT COMMANDS
# ============================================================

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f'Bot ready! Guilds: {len(bot.guilds)}')


@bot.tree.command(name='deobf', description='Deobfuscate a WeAreDev Lua script')
async def deobf_cmd(
    interaction: discord.Interaction,
    script: str = None
):
    await interaction.response.defer(ephemeral=False)
    
    # Check attachments first
    lua_code = None
    if interaction.message and interaction.message.attachments:
        att = interaction.message.attachments[0]
        if att.size > MAX_FILE_SIZE:
            await interaction.followup.send(f'❌ File too large ({att.size} bytes). Max: {MAX_FILE_SIZE} bytes')
            return
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(att.url) as resp:
                    lua_code = await resp.text()
        except Exception as e:
            await interaction.followup.send(f'❌ Failed to download file: {e}')
            return
    elif script:
        lua_code = script
    else:
        await interaction.followup.send('❌ Please provide either a script text or attach a .lua/.txt file')
        return
    
    if not lua_code or len(lua_code.strip()) < 50:
        await interaction.followup.send('❌ Script too short or empty')
        return
    
    await interaction.followup.send('⏳ Deobfuscating...')
    
    try:
        result = deobfuscate(lua_code)
        
        if result['success'] and result['source']:
            source = result['source']
            method = result['method']
            
            # Split into chunks if too long
            if len(source) > 1900:
                chunks = [source[i:i+1900] for i in range(0, len(source), 1900)]
                await interaction.followup.send(
                    f'✅ **Deobfuscated via {method}** ({len(source)} chars)\n'
                    f'```lua\n{chunks[0]}\n```'
                )
                for chunk in chunks[1:]:
                    await interaction.channel.send(f'```lua\n{chunk}\n```')
            else:
                await interaction.followup.send(
                    f'✅ **Deobfuscated via {method}** ({len(source)} chars)\n'
                    f'```lua\n{source}\n```'
                )
            
            # Save to file
            outname = f'deobf_{interaction.id}.lua'
            outpath = os.path.join('/home/z/my-project/download', outname)
            with open(outpath, 'w') as f:
                f.write(source)
            
        else:
            analysis = result.get('analysis')
            error = result.get('error', 'Unknown error')
            lua51_status = result.get('lua51_status', '')
            
            msg = f'❌ **Deobfuscation failed**\n**Error:** {error}'
            if lua51_status:
                msg += f'\n**Lua 5.1:** {lua51_status}'
            if analysis:
                msg += f'\n**P-table:** {analysis["ptable_count"]} entries'
                msg += f'\n**Alphabet:** {analysis["alphabet_count"]} entries'
                msg += f'\n**Shuffle:** {analysis["shuffle_ranges"]}'
                msg += f'\n**Strings found:** {len(analysis.get("decoded_strings", []))}'
                if analysis.get('decoded_strings'):
                    sample = [s for _, s in analysis['decoded_strings'][:15]]
                    if sample:
                        msg += f'\n**Sample strings:** {sample}'
            
            await interaction.followup.send(msg[:2000])
            
    except Exception as e:
        await interaction.followup.send(f'❌ Internal error: {e}')


@bot.tree.command(name='deobf_url', description='Deobfuscate a WeAreDev script from URL')
async def deobf_url_cmd(
    interaction: discord.Interaction,
    url: str
):
    await interaction.response.defer(ephemeral=False)
    
    # Validate URL
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            await interaction.followup.send('❌ Invalid URL')
            return
    except Exception:
        await interaction.followup.send('❌ Invalid URL')
        return
    
    await interaction.followup.send(f'⏳ Fetching from `{url}`...')
    
    # Fetch
    content, fetch_error = await fetch_lua_from_url(url)
    if fetch_error:
        await interaction.followup.send(f'❌ Fetch failed: {fetch_error}')
        return
    if not content:
        await interaction.followup.send('❌ No content received')
        return
    
    await interaction.followup.send(f'⏳ Fetched {len(content)} bytes. Deobfuscating...')
    
    # Deobfuscate
    result = deobfuscate(content, filename=url.split('/')[-1])
    
    if result['success'] and result['source']:
        source = result['source']
        method = result['method']
        
        if len(source) > 1900:
            chunks = [source[i:i+1900] for i in range(0, len(source), 1900)]
            await interaction.followup.send(
                f'✅ **Deobfuscated from URL via {method}** ({len(source)} chars)\n'
                f'```lua\n{chunks[0]}\n```'
            )
            for chunk in chunks[1:]:
                await interaction.channel.send(f'```lua\n{chunk}\n```')
        else:
            await interaction.followup.send(
                f'✅ **Deobfuscated from URL via {method}** ({len(source)} chars)\n'
                f'```lua\n{source}\n```'
            )
    else:
        error = result.get('error', 'Unknown')
        await interaction.followup.send(f'❌ Deobfuscation failed: {error}')


@bot.tree.command(name='deobf_info', description='Analyze a WeAreDev script')
async def deobf_info_cmd(
    interaction: discord.Interaction,
    script: str = None
):
    await interaction.response.defer(ephemeral=False)
    
    lua_code = None
    if interaction.message and interaction.message.attachments:
        att = interaction.message.attachments[0]
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(att.url) as resp:
                    lua_code = await resp.text()
        except Exception:
            pass
    elif script:
        lua_code = script
    else:
        await interaction.followup.send('❌ Please provide a script')
        return
    
    info = detect_wearedev(lua_code)
    
    if not info['is_wearedev']:
        await interaction.followup.send('❌ Not detected as WeAreDev obfuscated')
        return
    
    msg = '**WeAreDev Script Analysis**\n'
    msg += f'**Header:** {"Yes" if info["has_header"] else "No"}\n'
    msg += f'**P-table variable:** `{info["pvar"]}` ({info["ptable_count"]} entries)\n'
    msg += f'**Alphabet variable:** `{info["avar"]}` ({info["alphabet_count"]} entries)\n'
    msg += f'**Shuffle ranges:** {info["shuffle_ranges"]}\n'
    msg += f'**Readable strings:** {len(info["decoded_strings"])}\n'
    
    if info['decoded_strings']:
        sample = [s for _, s in info['decoded_strings'][:20]]
        msg += f'\n**String samples:** {sample}'
    
    if info['errors']:
        msg += f'\n**Issues:** {info["errors"]}'
    
    await interaction.followup.send(msg[:2000])


# ============================================================
# CLONE ROBLOX ENV TO DOWNLOAD DIR
# ============================================================

def setup_roblox_env():
    """Copy roblox env Lua script to download directory."""
    env_content = '''-- Roblox env for Lua 5.1 deobfuscation
local function fake() return setmetatable({},{__index=function(self,k)
  if k=="FindFirstChild" then return function() return fake() end end
  if k=="WaitForChild" then return function(self,n) return setmetatable({Name=n},getmetatable(self)) end end
  if k=="GetChildren" then return function() return {} end end
  if k=="GetDescendants" then return function() return {} end end
  if k=="Destroy" then return function() end end
  if k=="Clone" then return function() return fake() end end
  if k=="IsA" then return function() return false end end
  if k=="Parent" then return nil end
  if k=="Name" then return "Instance" end
  if k=="GetPropertyChangedSignal" then return function() return {Connect=function() end,Wait=function() end} end end
  if k=="Changed" then return {Connect=function() end} end
  if k=="ChildAdded" then return {Connect=function() end} end
  if k=="AncestryChanged" then return {Connect=function() end} end
  return fake()
end}) end
local FE = {Connect=function() return {} end, Wait=function() end, Fire=function() end}
game = setmetatable({},{__index=function(self,svc)
  if svc=="Workspace" or svc=="workspace" then return setmetatable({CurrentCamera=fake()},getmetatable(fake())) end
  if svc=="Players" then return setmetatable({LocalPlayer=setmetatable({Character=fake(),GetMouse=function() return {Hit=fake(),Target=fake()} end},getmetatable(fake())),GetPlayers=function() return {} end,PlayerAdded=FE,PlayerRemoving=FE},getmetatable(fake())) end
  if svc=="Lighting" then return fake() end
  if svc=="ReplicatedStorage" then return fake() end
  if svc=="UserInputService" then return setmetatable({InputBegan=FE,InputEnded=FE,IsKeyDown=function() return false end},getmetatable(fake())) end
  if svc=="TweenService" then return setmetatable({Create=function(s,o,i) return {Play=function() end} end},getmetatable(fake())) end
  if svc=="RunService" then return setmetatable({Heartbeat=FE,RenderStepped=FE,Stepped=FE},getmetatable(fake())) end
  if svc=="Debris" then return setmetatable({AddItem=function() end},getmetatable(fake())) end
  if svc=="HttpService" then return setmetatable({HttpGetAsync=function() return "" end},getmetatable(fake())) end
  return fake()
end,GetService=function(self,s) return rawget(self,s) or self[s] end,Players=setmetatable({},getmetatable(fake())),Workspace=fake()})
workspace = game.Workspace or fake()
Instance = setmetatable({},{__index=function(self,k) if k=="new" then return function(c,p) return fake() end end return nil end})
Vector3 = Vector3 or setmetatable({},{__call=function(c,x,y,z) return {X=x or 0,Y=y or 0,Z=z or 0} end})
Color3 = Color3 or setmetatable({},{__call=function(c,r,g,b) return {R=r or 0,G=g or 0,B=b or 0} end})
UDim = UDim or setmetatable({},{__call=function(c,s,o) return {Scale=s or 0,Offset=o or 0} end})
UDim2 = UDim2 or setmetatable({},{__call=function(c,x,y) return x end})
Enum = Enum or setmetatable({},{__index=function(self,k) return setmetatable({},{__index=function(t,k2) return k2 end}) end})
task = task or {spawn=function(f) f() end, wait=function() end}
spawn = spawn or function(f) f() end
wait = wait or function() end
tick = tick or os.time
time = time or os.clock
loadstring = function(s,...) if type(s)=="string" and #s>50 then local f=io.open("/tmp/deobf_result.lua","w") if f then f:write(s) f:close() end print("[CAPTURED] "..#s.." chars") return function() end end return nil,"no" end
local ok, err = pcall(function() dofile(arg[1]) end)
print("")
print("=== RESULT ===")
print("OK: "..tostring(ok))
print("Error: "..tostring(err and string.sub(tostring(err),1,500) or "none"))
local f=io.open("/tmp/deobf_result.lua","r")
if f then print("Captured: "..#f:read("*a").." chars") f:close() else print("No capture") end
'''
    
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'roblox_env.lua')
    with open(env_path, 'w') as f:
        f.write(env_content)
    print(f'Roblox env written to {env_path}')


# ============================================================
# MAIN
# ============================================================

if __name__ == '__main__':
    setup_roblox_env()
    
    if DISCORD_BOT_TOKEN == 'YOUR_TOKEN_HERE':
        print('Warning: No DISCORD_BOT_TOKEN set. Running in CLI mode.')
        if len(sys.argv) > 1:
            filepath = sys.argv[1]
            if filepath.startswith('http://') or filepath.startswith('https://'):
                print(f'Fetching from URL: {filepath}')
                import urllib.request
                try:
                    req = urllib.request.Request(filepath, headers={'User-Agent': 'Mozilla/5.0'})
                    resp = urllib.request.urlopen(req, timeout=30)
                    code = resp.read().decode('utf-8', errors='replace')
                    print(f'Fetched {len(code)} bytes')
                except Exception as e:
                    print(f'Fetch failed: {e}')
                    sys.exit(1)
            else:
                with open(filepath, 'r', errors='replace') as f:
                    code = f.read()
                print(f'Loaded {len(code)} bytes from {filepath}')
            
            result = deobfuscate(code, os.path.basename(filepath))
            print(f'Success: {result["success"]}')
            print(f'Method: {result["method"]}')
            print(f'Error: {result.get("error")}')
            
            if result['source']:
                outpath = os.path.join('/home/z/my-project/download', f'deobf_{os.path.basename(filepath)}')
                with open(outpath, 'w') as f:
                    f.write(result['source'])
                print(f'Output: {outpath} ({len(result["source"])} chars)')
        else:
            print('Usage: python main.py <file.lua|url>')
    else:
print("Starting Discord bot...")
keep_alive()
bot.run(DISCORD_BOT_TOKEN)
