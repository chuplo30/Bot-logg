import hashlib
import io
import os
import re
import traceback
from typing import List, Tuple

import discord
from discord.ext import commands

# ============ CONFIG ============
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = "."
MAX_FILE_SIZE = 8 * 1024 * 1024          # 8MB
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
    """Static + Dynamic logger cho nhiều loại obfuscator"""

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
                    results.append(f"[ByteArray] {decoded.strip()}")
            except Exception:
                pass
        return results

    @staticmethod
    def xor_bruteforce(text: str) -> List[str]:
        results = []
        patterns = [
            r'"((?:\\[0-9]{1,3}){4,})"',
            r"'((?:\\[0-9]{1,3}){4,})'",
        ]
        for pat in patterns:
            for s in re.findall(pat, text)[:25]:
                raw = [int(x) % 256 for x in re.findall(r"\\([0-9]{1,3})", s)]
                if len(raw) < 4:
                    continue
                best_key, best_score, best_dec = 0, 0, ""
                for key in range(256):
                    dec = bytes(b ^ key for b in raw)
                    score = sum(32 <= b <= 126 or b in (9, 10, 13) for b in dec)
                    if score > best_score:
                        best_score = score
                        best_key = key
                        best_dec = dec.decode("utf-8", errors="replace")
                if best_score > len(raw) * 0.65 and len(best_dec.strip()) > 3:
                    results.append(f"[XOR key={best_key}] {best_dec.strip()}")
        return list(dict.fromkeys(results))

    @staticmethod
    def find_urls_webhooks(text: str) -> Tuple[List[str], List[str]]:
        webhooks = re.findall(
            r"https?://(?:discord(?:app)?\.com/api/webhooks|canary\.discord\.com/api/webhooks)/[^\s\"'`<>]+",
            text,
            re.I,
        )
        urls = re.findall(r"https?://[^\s\"'`<>]+", text, re.I)
        # lọc bớt url rác
        urls = [u for u in urls if "discord.com/api/webhooks" not in u.lower()]
        return list(dict.fromkeys(webhooks)), list(dict.fromkeys(urls))[:20]

    @classmethod
    def sandbox_run(cls, code: str) -> Tuple[List[str], str]:
        if not LUPA_AVAILABLE:
            return [], "Lupa không khả dụng"

        logs = []
        try:
            lua = LuaRuntime(unpack_returned_tuples=True)

            setup = """
            local captured = {}
            local function log(tag, val)
                table.insert(captured, string.format("[%s] %s", tostring(tag), tostring(val)))
            end

            local function http_hook(opts, ...)
                local url = opts
                if type(opts) == "table" then
                    url = opts.Url or opts.url or opts[1] or "unknown"
                end
                log("HTTP", url)
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

            loadstring = function(c, ...)
                log("LOADSTRING", c)
                return function() end
            end
            load = loadstring

            print = function(...)
                local t = {}
                for i,v in ipairs({...}) do t[i] = tostring(v) end
                log("PRINT", table.concat(t, "\\t"))
            end
            warn = print
            rconsoleprint = function(m) log("RCONSOLE", m) end
            writefile = function(f,d) log("WRITEFILE", f.." -> "..tostring(d)) end
            appendfile = function(f,d) log("APPENDFILE", f.." -> "..tostring(d)) end

            -- Roblox mocks
            local function dummy()
                return setmetatable({}, {
                    __index = function(t,k)
                        if k == "HttpGet" or k == "HttpGetAsync" or k == "HttpPost" then
                            return function(_, url) log("GAME_HTTP", url) return "" end
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
            getfenv = getfenv or function() return _G end
            _G = _G or {}

            return captured
            """

            captured = lua.eval(setup)
            # giới hạn thời gian chạy
            lua.execute("debug.sethook(function() error('timeout') end, '', 200000)")
            try:
                lua.execute(code)
            except Exception as e:
                logs.append(f"[SANDBOX_ERROR] {e}")

            for item in captured.values():
                logs.append(str(item))

            return logs, "Sandbox OK"
        except Exception as e:
            return logs, f"Sandbox fail: {e}"

    @classmethod
    def analyze(cls, filename: str, data: bytes) -> str:
        text = data.decode("utf-8", errors="replace")
        sha = hashlib.sha256(data).hexdigest()[:16]

        sandbox_logs, sandbox_status = cls.sandbox_run(text)
        xor_res = cls.xor_bruteforce(text)
        byte_res = cls.extract_byte_arrays(text)
        webhooks, urls = cls.find_urls_webhooks(text)

        report = []
        report.append(f"FILE: {filename}")
        report.append(f"SIZE: {len(data):,} bytes | SHA: {sha}")
        report.append(f"LUPA: {'OK' if LUPA_AVAILABLE else 'OFF'}")
        report.append("")

        report.append("=== DYNAMIC SANDBOX ===")
        report.append(f"Status: {sandbox_status}")
        if sandbox_logs:
            for l in sandbox_logs[:40]:
                report.append(f"  + {l}")
        else:
            report.append("  - Không bắt được gì")
        report.append("")

        report.append("=== STATIC DECODE ===")
        static = list(dict.fromkeys(xor_res + byte_res))
        if static:
            for s in static[:25]:
                report.append(f"  + {s}")
        else:
            report.append("  - Không decode được chuỗi")
        report.append("")

        report.append("=== NETWORK / THREAT ===")
        if webhooks:
            report.append("  [!!!] DISCORD WEBHOOK:")
            for w in webhooks:
                report.append(f"      → {w}")
        if urls:
            report.append("  [!] URLs:")
            for u in urls:
                report.append(f"      → {u}")
        if not webhooks and not urls:
            report.append("  ✓ Không thấy webhook/url rõ")

        return "\n".join(report)


@bot.event
async def on_ready():
    print(f"[ONLINE] {bot.user} | Lupa: {LUPA_AVAILABLE}")


@bot.command(name="log")
async def log_cmd(ctx: commands.Context):
    files = []
    # lấy attachment
    for att in ctx.message.attachments[:MAX_FILES_PER_CMD]:
        if att.size > MAX_FILE_SIZE:
            await ctx.send(f"❌ `{att.filename}` quá lớn (max 8MB)")
            continue
        ext = os.path.splitext(att.filename.lower())[1]
        if ext not in ALLOWED_EXT and not att.filename.lower().endswith(".lua.txt"):
            await ctx.send(f"⚠️ Bỏ qua `{att.filename}` (chỉ nhận .lua/.txt)")
            continue
        data = await att.read()
        files.append((att.filename, data))

    # lấy code paste
    content = ctx.message.content[len(PREFIX + "log"):].strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:lua|luau)?\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    if content and len(content) > 20:
        files.append(("pasted_code.lua", content.encode("utf-8")))

    if not files:
        await ctx.send("❌ Gửi file `.lua`/`.txt` (có thể nhiều file) hoặc paste code sau `!log`")
        return

    msg = await ctx.send(f"🔎 Đang quét **{len(files)}** file...")

    reports = []
    for name, data in files:
        try:
            reports.append(LoggerEngine.analyze(name, data))
        except Exception as e:
            reports.append(f"FILE: {name}\nLỖI: {type(e).__name__}: {e}")

    full = "\n\n" + ("=" * 60) + "\n\n".join(reports)

    if len(full) <= 1900:
        await msg.edit(content=f"```text\n{full}\n```")
    else:
        buf = io.BytesIO(full.encode("utf-8"))
        await msg.edit(content=f"✅ Xong **{len(files)}** file", attachments=[
            discord.File(buf, filename="logger_report.txt")
        ])


@bot.command(name="ping")
async def ping(ctx):
    await ctx.send(f"Pong | Lupa: `{LUPA_AVAILABLE}`")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Thiếu DISCORD_TOKEN")
    bot.run(TOKEN)
