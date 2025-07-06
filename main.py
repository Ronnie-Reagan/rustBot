import asyncio
import json
import os
import time
from datetime import datetime, timezone
from dotenv import load_dotenv

import discord
from discord.ext import commands

from rustplus import RustSocket, ServerDetails, ProtobufEvent
from rustplus.events import ChatEventPayload
from rustplus.structs import RustTeamMember

from PIL import Image, ImageDraw
import requests

# === Load config ===
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SAVE_FILE = "player_stats.json"

with open("info.json", "r") as f:
    info = json.load(f)

IP = info["ip"]
PORT = int(info["port"])
STEAM_ID = int(info["playerId"])
PLAYER_TOKEN = int(info["playerToken"])

# === Discord setup ===
intents = discord.Intents.default()
intents.message_content = True  # REQUIRED for command recognition
bot = commands.Bot(command_prefix="!", intents=intents)
DISCORD_CHANNEL = 1391224969509208084 # my discord channel for notifications

# === Rust+ setup ===
server = ServerDetails(IP, PORT, STEAM_ID, PLAYER_TOKEN)
socket = RustSocket(server)

# === State Tracking ===
last_positions = {}
idle_timers = {}
idle_notify_intervals = {}
player_online = {}
player_seen = {}
movement_trail = {}
last_update = {}
map_seed = None
map_size = None

def format_minutes(seconds):
    minutes = int(seconds // 60)
    return f"{minutes//60}h {minutes%60}m" if minutes >= 60 else f"{minutes}m"

def world_to_image(x, y, map_size, image_width, image_height):
    scale = image_width / map_size
    px = int((x + (map_size / 2)) * scale)
    py = int((map_size / 2 - y) * scale)
    return px, py

def draw_trail(map_path, trail, map_size):
    img = Image.open(map_path).convert("RGBA")
    draw = ImageDraw.Draw(img)
    for sid, points in trail.items():
        coords = [world_to_image(x, y, map_size, img.width, img.height) for _, (x, y) in points]
        draw.line(coords, fill=(255, 0, 0, 255), width=3)
    output = "output_trail.png"
    img.save(output)
    return output

@bot.event
async def on_ready():
    print(f"✅ Discord bot is online as {bot.user}")
    retries = 0
    while retries < 5:
        try:
            await socket.connect()
            print("🔌 Connected to Rust+ successfully.")
            info = await socket.get_info()
            global map_seed, map_size
            map_seed = info.seed
            map_size = info.size
            break
        except Exception as e:
            print(f"⚠️ Rust+ connection failed: {e}")
            retries += 1
            await asyncio.sleep(5)
    else:
        print("❌ Failed to connect to Rust+ after 5 attempts.")
        return

    def on_team_chat(event: ChatEventPayload):
        msg = event.message
        username = msg.name
        message = msg.message
        formatted = f"💬 **{username}**: {message}"
        asyncio.create_task(send_to_discord(formatted))

    socket.on_protobuf_event(ProtobufEvent.TEAM_EVENT, on_team_chat)

# === Utilities ===

async def save_player_data_loop():
    while True:
        try:
            with open(SAVE_FILE, "w") as f:
                json.dump({str(k): v for k, v in player_seen.items()}, f, indent=2)
            print("💾 Player stats saved.")
        except Exception as e:
            print("❌ Failed to save player stats:", e)
        await asyncio.sleep(60)

def load_player_data():
    global player_seen
    try:
        with open(SAVE_FILE, "r") as f:
            player_seen = {int(k): v for k, v in json.load(f).items()}
            print("✅ Loaded player stats.")
    except FileNotFoundError:
        print("ℹ️ No previous player stats found, starting fresh.")
        player_seen = {}

async def send_to_discord(message):
    await bot.wait_until_ready()
    channel = bot.get_channel(DISCORD_CHANNEL)
    if channel:
        await channel.send(message)
    else:
        print("❌ Discord channel not found.")

def handle_idle(player: RustTeamMember):
    sid = player.steam_id
    now = time.time()
    pos = (round(player.x), round(player.y))

    if sid not in last_positions:
        last_positions[sid] = pos
    if sid not in idle_timers:
        idle_timers[sid] = 0
    if sid not in idle_notify_intervals:
        idle_notify_intervals[sid] = 5
    if sid not in movement_trail:
        movement_trail[sid] = []

    movement_trail[sid].append((now, pos))

    if pos == last_positions[sid]:
        idle_timers[sid] += 1
        notify_at = idle_notify_intervals[sid]

        if idle_timers[sid] == notify_at:
            asyncio.create_task(send_to_discord(
                f"🛑 **{player.name}** has been idle for {format_minutes(notify_at * 60)}."
            ))
            idle_notify_intervals[sid] = min(notify_at * 2, 60)
    else:
        if idle_timers[sid] >= 5:
            asyncio.create_task(send_to_discord(
                f"✅ **{player.name}** is no longer AFK. (Idle for {format_minutes(idle_timers[sid])})"
            ))
        idle_timers[sid] = 0
        idle_notify_intervals[sid] = 5
        last_positions[sid] = pos

def update_presence(player: RustTeamMember):
    sid = player.steam_id
    now = time.time()

    if sid not in player_seen:
        player_seen[sid] = {
            "name": player.name,
            "first": int(now),
            "last": int(now),
            "total": 0,
            "idle": 0
        }

    if sid not in last_update:
        last_update[sid] = now

    delta = int(now - last_update[sid])
    last_update[sid] = now

    if not player_online.get(sid) and player.is_online:
        asyncio.create_task(send_to_discord(f"✅ **{player.name}** logged in."))
    elif player_online.get(sid) and not player.is_online:
        asyncio.create_task(send_to_discord(f"❌ **{player.name}** logged out."))

    if player.is_online:
        player_seen[sid]["total"] += delta
        if idle_timers.get(sid, 0) >= 1:
            player_seen[sid]["idle"] += delta
        player_seen[sid]["last"] = int(now)

    player_online[sid] = player.is_online

# === Rust Polling ===
async def rust_polling_loop():
    while True:
        try:
            team_info = await socket.get_team_info()
            if not team_info or not team_info.members:
                print("⚠️ No team data available.")
                await asyncio.sleep(10)
                continue

            for player in team_info.members:
                update_presence(player)
                if player.is_online:
                    handle_idle(player)

        except Exception as e:
            print("Rust polling error:", e)
        await asyncio.sleep(60)

# === Bot Commands ===
@bot.command(name="trail")
async def trail(ctx, *, name: str):
    sid = next((k for k, v in player_seen.items() if v["name"].lower() == name.lower()), None)
    if not sid or sid not in movement_trail:
        return await ctx.send("\u274c Player not found or no trail.")

    map_url = f"https://rustmaps.com/map/{map_seed}/{map_size}.jpg"
    map_path = f"map_{map_seed}_{map_size}.jpg"
    if not os.path.exists(map_path):
        r = requests.get(map_url)
        with open(map_path, "wb") as f:
            f.write(r.content)

    img_path = draw_trail(map_path, {sid: movement_trail[sid]}, map_size)
    await ctx.send(file=discord.File(img_path))

@bot.command(name="stats")
async def stats(ctx, *, name: str):
    for sid, data in player_seen.items():
        if data["name"].lower() == name.lower():
            total = data["total"]
            idle = data["idle"]
            active = total - idle
            last_seen = datetime.fromtimestamp(data["last"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            first_seen = datetime.fromtimestamp(data["first"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            await ctx.send(
                f"📊 **Stats for {data['name']}**\n"
                f"🕐 First Seen: `{first_seen}`\n"
                f"👀 Last Seen: `{last_seen}`\n"
                f"⏱️ Total Time: `{format_minutes(total)}`\n"
                f"💤 Idle Time: `{format_minutes(idle)}`\n"
                f"⚡ Active Time: `{format_minutes(active)}`"
            )
            return

    await ctx.send(f"❌ Player '{name}' not found.")

@bot.command(name="players")
async def players(ctx):
    names = [data["name"] for data in player_seen.values()]
    if names:
        await ctx.send("🧍 Tracked players:\n" + ", ".join(sorted(names)))
    else:
        await ctx.send("⚠️ No player data loaded.")

@bot.command(name="topactive")
async def top_active(ctx):
    ranked = sorted(player_seen.values(), key=lambda d: d["total"] - d["idle"], reverse=True)
    lines = []
    for i, p in enumerate(ranked[:10]):
        active = p["total"] - p["idle"]
        lines.append(f"{i+1}. **{p['name']}** – {format_minutes(active)} active")
    await ctx.send("🏆 **Top Active Players:**\n" + "\n".join(lines) if lines else "No data.")

@bot.command(name="topidle")
async def top_idle(ctx):
    ranked = sorted(player_seen.values(), key=lambda d: d["idle"], reverse=True)
    lines = []
    for i, p in enumerate(ranked[:10]):
        lines.append(f"{i+1}. **{p['name']}** – {format_minutes(p['idle'])} idle")
    await ctx.send("💤 **Top Idle Players:**\n" + "\n".join(lines) if lines else "No data.")

# === Launch ===
async def main():
    load_player_data()
    await asyncio.gather(
        bot.start(DISCORD_TOKEN),
        rust_polling_loop(),
        save_player_data_loop()
    )

if __name__ == "__main__":
    asyncio.run(main())
