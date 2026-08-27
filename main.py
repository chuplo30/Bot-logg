import hashlib
import io
import os
import re
from typing import List, Tuple
from threading import Thread

import discord
from discord.ext import commands
from flask import Flask

# ============ CONFIG ============
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = "!"
MAX_FILE_SIZE = 8 * 1024 * 1024
MAX_FILES_PER_CMD = 8
ALLOWED_EXT = {".lua", ".txt", ".luau", ".lua.txt"}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# ============ LUPA ============
try:
    from lupa import LuaRuntime
    LUPA_AVAILABLE = True
except ImportError:
    LUPA_AVAILABLE = False


class LoggerEngine:

    # ---------- WAN decoder ----------
    @staticmethod
    def wan_xor(a: int, b: int) -> int:
        a %= 256
        b %= 256
        r, p = 0, 1
        for _ in range(8):
            x, y = a % 2, b % 2
            if x != y:
                r += p
            a = (a - x) // 2
            b = (b - y) // 2
            p *= 2
        return r

    @classmethod
    def decode_wan_strings(cls, text: str) -> List[str]:
        results = []
        # Tìm các bảng string escaped của WAN
        tables = re.findall(
            r'local\s+\w+\s*=\s*\{((?:"(?:\\[0-9]{1,3})+"\s*,?\s*){1,10})\}',
            text
        )
        for raw in tables:
            strs = re.findall(r'"((?:\\[0-9]{1,3})+)"', raw)
            if len(strs) < 1:
                continue

            # checksum
            checksum = 0
            for s in strs:
                for n in re.findall(r'\\([0-9]{1,3})', s):
                    checksum = (checksum + int(n)) % 256

            # thử các key phổ biến của WAN
            candidates = []
            for k1 in [12, 39, 42, 180, 181, 184, 209, 218]:
                for k2 in range(0, 256, 1):
                    candidates.append(cls.wan_xor(cls.wan_xor(checksum, k1), k2))

            for key in set(candidates):
                decoded = []
                ok = True
                for s in strs:
                    try:
                        bs = [int(x) for x in re.findall(r'\\([0-9]{1,3})', s)]
                        dec = "".join(chr(cls.wan_xor(b, key)) for b in bs)
                        if all(32 <= ord(c) <= 126 or c in "\n\t\r" for c in dec):
                            decoded.append(dec)
                        else:
                            ok = False
                            break
                    except Exception:
                        ok = False
                        break
                if ok and decoded:
                    for d in decoded:
                        if len(d.strip()) > 2:
                            results.append(d.strip())
                    break
        return list(dict.fromkeys(results))

    # ---------- Static ----------
    @staticmethod
    def extract_byte_arrays(text: str) -> List[str]:
        results = []
        for m in re.findall(r"\{([0-9\s,]{15,})\}", text):
            nums = re.findall(r"\d+", m)
            if len(nums) < 6:
                continue
            try:
                data = bytes(int(n) % 256 for n in nums)
                decoded = data.decode("utf-8", errors="ignore")
                readable = sum(32 <= ord(c) <= 126 for c in decoded)
                if len(decoded) > 4 and readable / max(len(decoded), 1) > 0.7:
                    results.append(decoded.strip())
            except Exception:
                pass
        return results

    @staticmethod
    def xor_bruteforce(text: str) -> List[str]:
        results = []
        for pat in [r'"((?:\\[0-9]{1,3}){4,})"', r"'((?:\\[0-9]{1,3}){4,})'"]:
            for s in re.findall(pat, text)[:30]:
                raw = [int(x) % 256 for x in re.findall(r"\\([0-9]{1,3})", s)]
                if len(raw) < 4:
                    continue
                best_score, best_dec = 0, ""
                for key in range(256):
                    dec = bytes(b ^ key for b in raw)
                    score = sum(32 <= b <= 126 or b in (9, 10, 13) for b in dec)
                    if score > best_score:
                        best_score = score
                        best_dec = dec.decode("utf-8", errors="replace")
                if best_score > len(raw) * 0.65 and len(best_dec.strip()) > 5:
                    results.append(best_dec.strip())
        return list(dict.fromkeys(results))

    @staticmethod
    def find_urls_webhooks(text: str) -> Tuple[List[str], List[str]]:
        webhooks = re.findall(
            r"https?://(?:discord(?:app)?\.com/api/webhooks|canary\.discord\.com/api/webhooks)/[^\s\"'`<>]+",
            text, re.I
        )
        urls = re.findall(r"https?://[^\s\"'`<>]+", text, re.I)
        urls = [u for u in urls if "discord.com/api/webhooks" not in u.lower()]
        return list(dict.fromkeys(webhooks)), list(dict.fromkeys(urls))[:15]

    # ---------- Sandbox (bay source) ----------
    @classmethod
    def sandbox_run(cls, code: str) -> Tuple[List[str], List[str], str]:
        if not LUPA_AVAILABLE:
            return [], [], "Lupa không khả dụng"

        logs = []
        dumped = []

        try:
            lua = LuaRuntime(unpack_returned_tuples=True)

            setup = r"""
            local captured = {}
            local dumped_sources = {}

            local function log(tag, val)
                table.insert(captured, string.format("[%s] %s", tostring(tag), tostring(val)))
            end

            local function http_hook(opts, ...)
                local url = opts
                if type(opts) == "table" then
                    url = opts.Url or opts.url or opts[1] or "unknown"
                end
                log("HTTP", tostring(url))
                return {StatusCode=200, Body='print("hooked")', Headers={}}
            end

            request = http_hook
            http_request = http_hook
            httprequest = http_hook
            syn = {request = http_hook, protect_gui = function() end}
            fluxus = {request = http_hook}
            krnl = {request = http_hook}
            delta = {request = http_hook}
            executor = {request = http_hook}

            loadstring = function(payload, ...)
                if type(payload) == "string" and #payload > 15 then
                    table.insert(dumped_sources, payload)
                    log("LOADSTRING", "Bắt được source (" .. #payload .. " chars)")
                end
                return function() end
            end
            load = loadstring

            print = function(...)
                local t = {}
                for i,v in ipairs({...}) do t[i] = tostring(v) end
                log("PRINT", table.concat(t, "\t"))
            end
            warn = print
            rconsoleprint = function(m) log("RCONSOLE", tostring(m)) end
            writefile = function(f,d) log("WRITEFILE", tostring(f).." -> "..tostring(d)) end
            appendfile = function(f,d) log("APPENDFILE", tostring(f).." -> "..tostring(d)) end

            local function dummy()
                return setmetatable({}, {
                    __index = function(_, k)
                        if k == "HttpGet" or k == "HttpGetAsync" or k == "HttpPost" then
                            return function(_, url)
                                log("GAME_HTTP", tostring(url))
                                return ""
                            end
                        end
                        return dummy()
                    end,
                    __call = function() return dummy() end,
                    __tostring = function() return "Instance" end
                })
            end
            game = dummy()
            workspace = dummy()
            script = dummy()
            Instance = {new = function() return dummy() end}
            getgenv = function() return _G end
            getrenv = function() return _G end
            getfenv = getfenv or function() return _G end
            _G = _G or {}

            return captured, dumped_sources
            """

            captured, dumped_sources = lua.eval(setup)
            lua.execute("debug.sethook(function() error('timeout') end, '', 400000)")

            try:
                lua.execute(code)
            except Exception as e:
                logs.append(f"[SANDBOX] {e}")

            for item in captured.values():
                logs.append(str(item))

            for src in dumped_sources.values():
                if isinstance(src, str) and len(src) > 20:
                    dumped.append(src)

            status = "Sandbox OK"
            return logs, dumped, status

        except Exception as e:
            return logs, dumped, f"Sandbox fail: {e}"

    # ---------- Analyze ----------
    @classmethod
    def analyze(cls, filename: str, data: bytes) -> str:
        text = data.decode("utf-8", errors="replace")
        sha = hashlib.sha256(data).hexdigest()[:16]

        is_wan = "WAN OBFUSCATE" in text[:200]

        sandbox_logs, dumped_sources, sandbox_status = cls.sandbox_run(text)
        wan_strs = cls.decode_wan_strings(text) if is_wan else []
        xor_res = cls.xor_bruteforce(text)
        byte_res = cls.extract_byte_arrays(text)
        webhooks, urls = cls.find_urls_webhooks(text)

        report = []
        report.append(f"FILE        : {filename}")
        report.append(f"SIZE        : {len(data):,} bytes")
        report.append(f"SHA         : {sha}")
        report.append(f"LUPA        : {'OK' if LUPA_AVAILABLE else 'OFF'}")
        report.append(f"TYPE        : {'WAN OBFUSCATE' if is_wan else 'Unknown / Other'}")
        report.append("")

        # 1. SOURCE DUMP (quan trọng nhất)
        report.append("=" * 55)
        report.append("SOURCE DUMP (loadstring / load)")
        report.append("=" * 55)
        if dumped_sources:
            report.append(f"[!!!] BẮT ĐƯỢC {len(dumped_sources)} SOURCE:")
            for i, src in enumerate(dumped_sources, 1):
                report.append(f"\n----- SOURCE #{i} ({len(src)} chars) -----")
                report.append(src[:3000] + ("\n... [cắt]" if len(src) > 3000 else ""))
        else:
            report.append("[-] Không bắt được loadstring payload")
            if is_wan:
                report.append("    → WAN VM nặng thường không dùng loadstring plain")
        report.append("")

        # 2. WAN strings
        if is_wan or wan_strs:
            report.append("=" * 55)
            report.append("WAN STRING TABLE")
            report.append("=" * 55)
            if wan_strs:
                for s in wan_strs:
                    report.append(f"  + {s}")
            else:
                report.append("  - Không decode được string table")
            report.append("")

        # 3. Dynamic logs
        report.append("=" * 55)
        report.append("DYNAMIC LOGS")
        report.append("=" * 55)
        report.append(f"Status: {sandbox_status}")
        if sandbox_logs:
            for l in sandbox_logs[:30]:
                report.append(f"  + {l}")
        else:
            report.append("  - Không có log")
        report.append("")

        # 4. Static
        report.append("=" * 55)
        report.append("STATIC DECODE")
        report.append("=" * 55)
        static = list(dict.fromkeys(xor_res + byte_res))
        if static:
            for s in static[:15]:
                report.append(f"  + {s[:180]}")
        else:
            report.append("  - Không decode được")
        report.append("")

        # 5. Network
        report.append("=" * 55)
        report.append("NETWORK / THREAT")
        report.append("=" * 55)
        if webhooks:
            report.append("[!!!] DISCORD WEBHOOK:")
            for w in webhooks:
                report.append(f"  → {w}")
        if urls:
            report.append("[!] URLs:")
            for u in urls:
                report.append(f"  → {u}")
        if not webhooks and not urls:
            report.append("✓ Không thấy webhook/url rõ")

        return "\n".join(report)


# ============ Flask keep-alive ============
app = Flask(__name__)

@app.route("/")
def home():
    return f"Logger Bot running | Lupa: {LUPA_AVAILABLE}"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


# ============ Bot ============
@bot.event
async def on_ready():
    print(f"[ONLINE] {bot.user} | Lupa: {LUPA_AVAILABLE}")


@bot.command(name="log")
async def log_cmd(ctx: commands.Context):
    files = []
    for att in ctx.message.attachments[:MAX_FILES_PER_CMD]:
        if att.size > MAX_FILE_SIZE:
            await ctx.send(f"❌ `{att.filename}` quá lớn")
            continue
        ext = os.path.splitext(att.filename.lower())[1]
        if ext not in ALLOWED_EXT and not att.filename.lower().endswith(".lua.txt"):
            continue
        data = await att.read()
        files.append((att.filename, data))

    content = ctx.message.content[len(PREFIX + "log"):].strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:lua|luau)?\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    if content and len(content) > 30:
        files.append(("pasted.lua", content.encode()))

    if not files:
        await ctx.send("❌ Gửi file `.lua`/`.txt` hoặc paste code sau `!log`")
        return

    msg = await ctx.send(f"🔎 Đang quét **{len(files)}** file...")

    reports = []
    for name, data in files:
        try:
            reports.append(LoggerEngine.analyze(name, data))
        except Exception as e:
            reports.append(f"FILE: {name}\nLỖI: {e}")

    full = ("\n\n" + "=" * 60 + "\n\n").join(reports)

    if len(full) <= 1900:
        await msg.edit(content=f"```text\n{full}\n```")
    else:
        buf = io.BytesIO(full.encode("utf-8"))
        await msg.edit(
            content=f"✅ Xong **{len(files)}** file",
            attachments=[discord.File(buf, "logger_report.txt")]
        )


@bot.command(name="ping")
async def ping(ctx):
    await ctx.send(f"Pong | Lupa: `{LUPA_AVAILABLE}`")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Thiếu DISCORD_TOKEN")
    Thread(target=run_flask, daemon=True).start()
    bot.run(TOKEN)
