#!/usr/bin/env python3
"""
Discord bot wrapper for lua_deobf_toolkit.py

Command:
  .log   (attach a .lua/.txt file to the message)

Behavior:
  - Downloads the attached file
  - Runs it through LuaDeobfuscator
  - Strips comments (-- line comments and --[[ ]] block comments,
    including the toolkit's own header comments) from the recovered source
  - Replies with the cleaned source, as a code block if short enough,
    otherwise as a .lua file attachment

Requirements:
  pip install discord.py
  pip install flask      # keep-alive server so Render sees the service as up
  pip install lupa       # optional but recommended, enables VM execution

Setup:
  1. Put this file in the SAME folder as lua_deobf_toolkit.py
  2. Set your bot token below (or via env var DISCORD_BOT_TOKEN)
  3. python lua_deobf_bot.py
"""

import os
import re
import tempfile
import threading

import discord
from discord.ext import commands
from flask import Flask

from lua_deobf_toolkit import LuaDeobfuscator

# ============================================================
# Config
# ============================================================

TOKEN = os.environ.get("DISCORD_TOKEN)
COMMAND_PREFIX = "."
DISCORD_MSG_LIMIT = 1900  # leave headroom under the 2000 char hard limit


# ============================================================
# Keep-alive web server
# ============================================================
# Render (and most host-a-web-service platforms) expect the process to
# bind a port and answer HTTP requests, or it marks the service as down
# and restarts/kills it. The Discord bot itself doesn't need HTTP for
# anything — this Flask app exists purely to keep Render happy.

keep_alive_app = Flask(__name__)


@keep_alive_app.route("/")
def _health():
    return "Bot is running."


def _run_keep_alive():
    port = int(os.environ.get("PORT", 8080))
    keep_alive_app.run(host="0.0.0.0", port=port)


def start_keep_alive():
    threading.Thread(target=_run_keep_alive, daemon=True).start()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

# Deobfuscator is stateful (loads a Lua VM once), reuse a single instance
deobfuscator = LuaDeobfuscator(verbose=False)


# ============================================================
# Comment stripping
# ============================================================

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
    # Remove block comments --[[ ]], --[=[ ]=], --[==[ ]==], ...
    source = re.sub(r"--\[(=*)\[.*?\]\1\]", "", source, flags=re.DOTALL)

    # Remove line comments, but skip lines that are inside a long string
    # (best-effort: just strip a trailing "-- ..." per line)
    cleaned_lines = []
    for line in source.split("\n"):
        # Don't touch the line if "--" only appears inside quotes in an
        # obvious way; this is a heuristic, not a parser.
        idx = line.find("--")
        if idx != -1:
            # crude quote-balance check before the "--"
            before = line[:idx]
            if before.count('"') % 2 == 0 and before.count("'") % 2 == 0:
                line = before.rstrip()
        cleaned_lines.append(line)

    result = "\n".join(cleaned_lines)

    # Collapse runs of blank lines left behind by stripped comments
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


# ============================================================
# Bot events / commands
# ============================================================

@bot.event
async def on_ready():
    print(f"[+] Logged in as {bot.user} (id={bot.user.id})")


@bot.command(name="log")
async def log_cmd(ctx: commands.Context):
    """.log — attach a .lua/.txt file to deobfuscate it and strip comments."""

    if not ctx.message.attachments:
        await ctx.reply("Attach a `.lua` or `.txt` file with the command.")
        return

    attachment = ctx.message.attachments[0]
    if not attachment.filename.lower().endswith((".lua", ".txt")):
        await ctx.reply("File must be `.lua` or `.txt`.")
        return

    status_msg = await ctx.reply(f"⏳ Deobfuscating `{attachment.filename}`...")

    try:
        raw_bytes = await attachment.read()
        code = raw_bytes.decode("utf-8", errors="replace")

        obf_name, source, meta = deobfuscator.deobfuscate(code, attachment.filename)
        cleaned = strip_lua_comments(source)

        header = f"Obfuscator detected: **{obf_name}**\n"

        if not cleaned.strip():
            # deobfuscate() returned only comment lines (e.g. "Requires VM
            # execution for full deobfuscation") -> stripping comments left
            # nothing. Surface the real reason instead of sending blank output.
            reason = source.strip() or "No source could be recovered."
            await status_msg.edit(
                content=(
                    f"{header}⚠️ Nothing left after stripping comments — "
                    f"the deobfuscator itself didn't recover real source, "
                    f"it only returned notes:\n```\n{reason}\n```"
                    f"{'(lupa not installed — install it for VM execution)' if not deobfuscator.engine.available else ''}"
                )
            )
            return

        if len(cleaned) <= DISCORD_MSG_LIMIT:
            await status_msg.edit(
                content=f"{header}```lua\n{cleaned}\n```"
            )
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
        await status_msg.edit(content=f"❌ Error: `{e}`")


if __name__ == "__main__":
    if TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        print("[!] Set DISCORD_BOT_TOKEN env var or edit TOKEN in this file.")
    start_keep_alive()
    bot.run(TOKEN)
