import os
import json
import asyncio
import time
import random
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Literal
from dotenv import load_dotenv
import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp
from concurrent.futures import ThreadPoolExecutor, as_completed

def debug(*msg):
    print("DEBUG:", *msg)

load_dotenv()

TOKEN = os.getenv("TOKEN")
CACHE_DIR = "cache"
PLAYLISTS_FILE = "playlists.json"
SETTINGS_FILE = "settings.json"

ADMIN_ID = os.getenv("ADMIN_ID")

os.makedirs(CACHE_DIR, exist_ok=True)

INTENTS = discord.Intents.default()
INTENTS.voice_states = True
INTENTS.message_content = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

LoopMode = Literal["off", "track", "queue"]

SHUTDOWN_PENDING = False
MAINTENANCE_MODE = False


REQUESTS_FILE = "blocked_requests.json"

def load_blocked_requests():
    if not os.path.exists(REQUESTS_FILE):
        return []
    with open(REQUESTS_FILE, "r") as f:
        return json.load(f)

def save_blocked_requests(data):
    with open(REQUESTS_FILE, "w") as f:
        json.dump(data, f, indent=2)

BLOCKED_REQUESTS = load_blocked_requests()

def requests_blocked(uid: int) -> bool:
    return uid in BLOCKED_REQUESTS

def block_requests(uid: int):
    if uid not in BLOCKED_REQUESTS:
        BLOCKED_REQUESTS.append(uid)
        save_blocked_requests(BLOCKED_REQUESTS)

def unblock_requests(uid: int):
    if uid in BLOCKED_REQUESTS:
        BLOCKED_REQUESTS.remove(uid)
        save_blocked_requests(BLOCKED_REQUESTS)


# ========================= DATA CLASSES =========================
@dataclass
class Track:
    url: str
    title: str
    id: str
    file_path: str
    duration: int
    requested_by: str
    thumbnail: Optional[str] = None
    genre_hint: Optional[str] = None


@dataclass
class GuildState:
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    current: Optional[Track] = None
    voice: Optional[discord.VoiceClient] = None
    volume: float = 0.5
    autoplay: bool = False
    mode_247: bool = False
    loop_mode: LoopMode = "off"
    play_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    played_history: List[str] = field(default_factory=list)


states: Dict[int, GuildState] = {}


def get_state(gid: int) -> GuildState:
    if gid not in states:
        states[gid] = GuildState()
    return states[gid]


# ========================= PLAYLIST STORAGE =========================
def load_playlists() -> Dict[str, dict]:
    if not os.path.exists(PLAYLISTS_FILE):
        return {}
    with open(PLAYLISTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_playlists(data: Dict[str, dict]):
    with open(PLAYLISTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ========================= SETTINGS (BLOCKED CONTROLS) =========================
def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return {"blocked_controls": []}
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_settings(data):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


SETTINGS = load_settings()


def controls_blocked(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return False
    return user_id in SETTINGS.get("blocked_controls", [])


def block_user_controls(user_id: int):
    if user_id == ADMIN_ID:
        return
    if user_id not in SETTINGS.get("blocked_controls", []):
        SETTINGS["blocked_controls"].append(user_id)
        save_settings(SETTINGS)


def unblock_user_controls(user_id: int):
    if user_id in SETTINGS.get("blocked_controls", []):
        SETTINGS["blocked_controls"].remove(user_id)
        save_settings(SETTINGS)

YDL_INFO = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "default_search": "ytsearch",
    "js_runtimes": {"node": {}},
    "remote_components": ["ejs:github"],
}

YDL_DL = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "js_runtimes": {"node": {}},
    "remote_components": ["ejs:github"],
    "outtmpl": os.path.join(CACHE_DIR, "%(id)s.%(ext)s"),
    "postprocessors": [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }
    ],
}

YDL_PLAYLIST = {
    "quiet": True,
    "extract_flat": True,
    "skip_download": True,
}


def cache_meta_path(video_id: str) -> str:
    return os.path.join(CACHE_DIR, f"{video_id}.json")


def load_cached(video_id: str) -> Optional[Track]:
    meta = cache_meta_path(video_id)
    if not os.path.exists(meta):
        return None
    with open(meta, "r", encoding="utf-8") as f:
        d = json.load(f)
    return Track(**d)


def save_cache(track: Track):
    with open(cache_meta_path(track.id), "w", encoding="utf-8") as f:
        json.dump(track.__dict__, f, indent=2)


def guess_genre_from_title(title: str) -> str:
    t = title.lower()
    keywords = [
        "phonk", "trap", "drill", "lofi", "lo-fi", "rock", "metal",
        "pop", "edm", "house", "dubstep", "nightcore", "remix",
        "jazz", "blues", "classical", "ambient"
    ]
    for k in keywords:
        if k in t:
            return k
    return "music"


class DownloadsView(discord.ui.View):
    def __init__(self, pages, page_index=0):
        super().__init__(timeout=60)
        self.pages = pages
        self.page_index = page_index

    def get_embed(self):
        page = self.pages[self.page_index]
        e = discord.Embed(
            title=f"Downloaded Songs (Page {self.page_index+1}/{len(self.pages)})",
            color=0x00ff99
        )
        e.description = "\n".join(page)
        return e

    @discord.ui.button(label="⬅ Prev", style=discord.ButtonStyle.gray)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page_index > 0:
            self.page_index -= 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Next ➡", style=discord.ButtonStyle.gray)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page_index < len(self.pages) - 1:
            self.page_index += 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)


async def fetch_track(query: str, requester: str) -> Track:
    debug("fetch_track called with query:", query)
    loop = asyncio.get_event_loop()

    def _probe():
        debug("Probing YouTube:", query)
        with yt_dlp.YoutubeDL(YDL_INFO) as y:
            info = y.extract_info(query, download=False)
            if "entries" in info:
                info = info["entries"][0]
            debug("Probe result ID:", info.get("id"))
            return info

    info = await loop.run_in_executor(None, _probe)
    vid = info["id"]

    cached = load_cached(vid)
    if cached:
        debug("Using cached track:", vid)
        cached.requested_by = requester
        return cached

    url = info.get("webpage_url", f"https://youtu.be/{vid}")
    title = info.get("title", "Unknown")
    duration = int(info.get("duration", 0))
    thumb = info.get("thumbnail")
    genre_hint = guess_genre_from_title(title)

    def _download():
        debug("Downloading:", url)
        with yt_dlp.YoutubeDL(YDL_DL) as y:
            info2 = y.extract_info(url, download=True)
            debug("Download complete for:", info2.get("id"))
            return info2

    await loop.run_in_executor(None, _download)

    file_path = os.path.join(CACHE_DIR, f"{vid}.mp3")
    exists = os.path.exists(file_path)
    size = os.path.getsize(file_path) if exists else 0
    debug("Downloaded file exists:", exists, "size:", size)

    track = Track(
        url=url,
        title=title,
        id=vid,
        file_path=file_path,
        duration=duration,
        requested_by=requester,
        thumbnail=thumb,
        genre_hint=genre_hint,
    )
    save_cache(track)
    return track


def extract_artist(title: str) -> str:
    separators = ["–", "-", "|", "—", "•", ":"]
    for sep in separators:
        if sep in title:
            left, right = title.split(sep, 1)
            left = left.strip()
            right = right.strip()

            left_clean = left.split("(")[0].strip()
            right_clean = right.split("(")[0].strip()

            if len(left_clean.split()) <= 3:
                return left_clean
            if len(right_clean.split()) <= 3:
                return right_clean

            return left_clean

    parts = title.split()
    return " ".join(parts[:2])


def fetch_track_sync(vid: str):
    url = f"https://youtu.be/{vid}"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    track = loop.run_until_complete(fetch_track(url, requester="CACHE"))
    loop.close()
    return track


async def fetch_artist_top_track(base_track: Track, requester: str, state: GuildState) -> Optional[Track]:
    artist = extract_artist(base_track.title)
    query = f"ytsearch10:{artist} top songs"
    debug("Autoplay search query:", query)

    loop = asyncio.get_event_loop()

    def _search():
        with yt_dlp.YoutubeDL({
            "quiet": True,
            "extract_flat": True,
            "skip_download": True
        }) as y:
            info = y.extract_info(query, download=False)
            return info.get("entries", [])

    results = await loop.run_in_executor(None, _search)
    debug("Search results:", len(results))

    for r in results:
        vid = r.get("id")
        if not vid:
            continue

        if vid in state.played_history:
            continue

        if any(q.id == vid for q in state.queue._queue):
            continue

        url = f"https://youtu.be/{vid}"
        return await fetch_track(url, requester)

    return None

def build_nowplaying_embed(state: GuildState) -> discord.Embed:
    if not state.current:
        e = discord.Embed(title="Nothing playing", color=0x555555)
        return e

    t = state.current
    e = discord.Embed(
        title="Now Playing",
        description=f"**{t.title}**\nRequested by: {t.requested_by}",
        color=0x00ff99,
        url=t.url,
    )
    if t.thumbnail:
        e.set_thumbnail(url=t.thumbnail)

    dur = t.duration
    mins = dur // 60
    secs = dur % 60
    e.add_field(name="Duration", value=f"{mins:02d}:{secs:02d}", inline=True)
    e.add_field(name="Volume", value=f"{int(state.volume*100)}%", inline=True)
    e.add_field(name="Loop Mode", value=state.loop_mode, inline=True)
    e.add_field(name="Autoplay", value="On" if state.autoplay else "Off", inline=True)
    e.add_field(name="24/7", value="On" if state.mode_247 else "Off", inline=True)

    q_items: List[Track] = list(state.queue._queue)
    if q_items:
        preview = "\n".join([f"**{i+1}.** {x.title}" for i, x in enumerate(q_items[:10])])
    else:
        preview = "_Empty_"
    e.add_field(name="Queue", value=preview, inline=False)

    return e


class DashboardView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=60)
        self.guild_id = guild_id

    @discord.ui.button(label="⏯ Pause/Resume", style=discord.ButtonStyle.blurple)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(self.guild_id)
        debug("Pause/Resume pressed")
        if not state.voice:
            return await interaction.response.send_message("Not connected.", ephemeral=True)
        if controls_blocked(interaction.user.id):
            return await interaction.response.send_message(
                "You are blocked from controlling playback.",
                ephemeral=True
            )

        if state.voice.is_paused():
            state.voice.resume()
        elif state.voice.is_playing():
            state.voice.pause()
        await interaction.response.edit_message(embed=build_nowplaying_embed(state), view=self)

    @discord.ui.button(label="⏭ Skip", style=discord.ButtonStyle.green)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(self.guild_id)
        debug("Skip pressed")

        if controls_blocked(interaction.user.id):
            return await interaction.response.send_message(
                "You are blocked from controlling playback.",
                ephemeral=True
            )
        if state.voice and state.voice.is_playing():
            state.voice.stop()
        await interaction.response.edit_message(embed=build_nowplaying_embed(state), view=self)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.red)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(self.guild_id)
        debug("Stop pressed")
        if controls_blocked(interaction.user.id):
            return await interaction.response.send_message(
                "You are blocked from controlling playback.",
                ephemeral=True
            )
        if state.voice:
            state.voice.stop()
        state.current = None
        while not state.queue.empty():
            state.queue.get_nowait()
        await interaction.response.edit_message(embed=build_nowplaying_embed(state), view=self)

    @discord.ui.button(label="🔉 -", style=discord.ButtonStyle.gray)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(self.guild_id)
        if controls_blocked(interaction.user.id):
            return await interaction.response.send_message(
                "You are blocked from controlling playback.",
                ephemeral=True
            )
        state.volume = max(0.0, state.volume - 0.1)
        debug("Volume down:", state.volume)
        if state.voice and state.voice.source:
            state.voice.source.volume = state.volume
        await interaction.response.edit_message(embed=build_nowplaying_embed(state), view=self)

    @discord.ui.button(label="🔊 +", style=discord.ButtonStyle.gray)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(self.guild_id)
        if controls_blocked(interaction.user.id):
            return await interaction.response.send_message(
                "You are blocked from controlling playback.",
                ephemeral=True
            )
        max_vol = 2.5 if interaction.user.id == ADMIN_ID else 2.0
        state.volume = min(max_vol, state.volume + 0.1)
        debug("Volume up:", state.volume)
        if state.voice and state.voice.source:
            state.voice.source.volume = state.volume
        await interaction.response.edit_message(embed=build_nowplaying_embed(state), view=self)

    @discord.ui.button(label="♾ Autoplay", style=discord.ButtonStyle.secondary)
    async def toggle_autoplay(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(self.guild_id)
        if controls_blocked(interaction.user.id):
            return await interaction.response.send_message(
                "You are blocked from controlling playback.",
                ephemeral=True
            )
        state.autoplay = not state.autoplay
        debug("Autoplay toggled:", state.autoplay)
        await interaction.response.edit_message(embed=build_nowplaying_embed(state), view=self)

    @discord.ui.button(label="24/7", style=discord.ButtonStyle.secondary)
    async def toggle_247(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(self.guild_id)
        if controls_blocked(interaction.user.id):
            return await interaction.response.send_message(
                "You are blocked from controlling playback.",
                ephemeral=True
            )
        state.mode_247 = not state.mode_247
        debug("24/7 toggled:", state.mode_247)
        await interaction.response.edit_message(embed=build_nowplaying_embed(state), view=self)


async def ensure_voice(interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
    if not interaction.user or not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("You must be in a voice channel.", ephemeral=True)
        return None
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("Join a voice channel first.", ephemeral=True)
        return None

    state = get_state(interaction.guild_id)
    if state.voice and state.voice.is_connected():
        return state.voice

    debug("Connecting to voice channel:", interaction.user.voice.channel.id)
    vc = await interaction.user.voice.channel.connect()
    state.voice = vc
    debug("Voice connected")
    return vc


async def play_loop(guild_id: int):
    global SHUTDOWN_PENDING
    state = get_state(guild_id)
    debug("play_loop start for guild:", guild_id)
    async with state.play_lock:
        while True:
            if SHUTDOWN_PENDING:
                debug("Shutdown pending — will stop after current track if any.")

            if state.queue.empty():
                debug("Queue empty in play_loop")
                if (state.autoplay or state.mode_247) and state.current and not SHUTDOWN_PENDING:
                    debug("Autoplay/24-7 active, fetching related")
                    nxt = await fetch_artist_top_track(state.current, state.current.requested_by, state)
                    if nxt:
                        await state.queue.put(nxt)
                        debug("Autoplay queued:", nxt.title)
                    else:
                        debug("No related track found")
                        if not state.mode_247:
                            break
                else:
                    break

            if SHUTDOWN_PENDING and state.current is None and state.queue.empty():
                debug("Shutdown: no track playing and queue empty.")
                break

            track: Track = await state.queue.get()
            state.current = track
            debug("Dequeued track:", track.title)

            if not state.voice or not state.voice.is_connected():
                debug("No voice connection, breaking play_loop")
                break

            exists = os.path.exists(track.file_path)
            size = os.path.getsize(track.file_path) if exists else 0
            debug("About to play file exists:", exists, "size:", size)
            if not exists or size == 0:
                debug("File invalid, skipping track")
                state.current = None
                continue

            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(track.file_path),
                volume=state.volume
            )

            def after_play(err):
                if err:
                    debug("FFmpeg error:", err)

            debug("Starting playback:", track.title)
            state.voice.play(source, after=after_play)

            while state.voice.is_playing() or state.voice.is_paused():
                await asyncio.sleep(0.5)

            debug("Playback finished:", track.title)
            state.played_history.append(track.id)

            if SHUTDOWN_PENDING:
                debug("Shutdown: stopping after current track.")
                break

            if state.loop_mode == "track":
                debug("Loop mode track, requeueing same track")
                await state.queue.put(track)
            elif state.loop_mode == "queue":
                debug("Loop mode queue, appending track to end")
                await state.queue.put(track)

            if state.queue.empty() and not (state.autoplay or state.mode_247):
                debug("Queue empty and no autoplay/24-7, breaking")
                break

        debug("play_loop end for guild:", guild_id)
        state.current = None
        if not state.mode_247 and state.voice and state.voice.is_connected():
            debug("Auto-disconnecting (24/7 off)")
            try:
                await state.voice.disconnect()
            except Exception as e:
                debug("Error disconnecting:", e)
            state.voice = None

        if SHUTDOWN_PENDING:
            debug("Shutdown: closing bot after play loop.")
            await bot.close()


async def queue_via_text_phrase(message: discord.Message, query: str):
    if not message.guild:
        return
    user = message.author
    guild = message.guild
    channel = message.channel

    if not isinstance(user, discord.Member):
        return

    if not user.voice or not user.voice.channel:
        await channel.send("Join a voice channel first.")
        return

    state = get_state(guild.id)

    if not state.voice or not state.voice.is_connected():
        debug("Connecting to voice channel via text phrase:", user.voice.channel.id)
        vc = await user.voice.channel.connect()
        state.voice = vc

    debug("Text phrase play with query:", query)
    track = await fetch_track(query, user.display_name)
    await state.queue.put(track)
    await channel.send(f"Queued **{track.title}** (via `hey boombox`)")
    if not state.voice.is_playing() and not state.voice.is_paused():
        bot.loop.create_task(play_loop(guild.id))

def maintenance_block(interaction: discord.Interaction) -> bool:
    if MAINTENANCE_MODE and interaction.user.id != ADMIN_ID:
        return True
    return False

@bot.tree.command(name="block_requests", description="Admin: block a user from requesting songs")
async def block_requests_cmd(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != ADMIN_ID:
        return await interaction.response.send_message("Only owner can use this.", ephemeral=True)

    block_requests(user.id)
    await interaction.response.send_message(f"Blocked **{user.display_name}** from requesting songs.")

@bot.tree.command(name="unblock_requests", description="Admin: unblock a user")
async def unblock_requests_cmd(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != ADMIN_ID:
        return await interaction.response.send_message("Only owner can use this.", ephemeral=True)

    unblock_requests(user.id)
    await interaction.response.send_message(f"Unblocked **{user.display_name}**.")


@bot.tree.command(name="play", description="Play a YouTube URL or search term")
@app_commands.describe(query="YouTube URL or search term")
async def play_cmd(interaction: discord.Interaction, query: str):
    if maintenance_block(interaction):
        return await interaction.response.send_message("🛠 Bot is in maintenance mode.", ephemeral=True)
    if requests_blocked(interaction.user.id):
        return await interaction.response.send_message("You are blocked from requesting songs.", ephemeral=True)


    await interaction.response.defer(thinking=True)
    debug("/play called with:", query)
    vc = await ensure_voice(interaction)
    if not vc:
        return

    state = get_state(interaction.guild_id)
    track = await fetch_track(query, interaction.user.display_name)
    await state.queue.put(track)
    debug("Queued track:", track.title)

    await interaction.followup.send(f"Queued **{track.title}**")

    if not state.voice.is_playing() and not state.voice.is_paused():
        debug("Starting play_loop from /play")
        bot.loop.create_task(play_loop(interaction.guild_id))


@bot.tree.command(name="queue", description="Show the current queue")
async def queue_cmd(interaction: discord.Interaction):
    if maintenance_block(interaction):
        return await interaction.response.send_message("🛠 Bot is in maintenance mode.", ephemeral=True)

    state = get_state(interaction.guild_id)
    q_items: List[Track] = list(state.queue._queue)
    debug("/queue called, items:", len(q_items))
    if not q_items and not state.current:
        return await interaction.response.send_message("Queue is empty.")

    desc = ""
    if state.current:
        desc += f"**Now:** {state.current.title} (by {state.current.requested_by})\n\n"
    if q_items:
        for i, t in enumerate(q_items, start=1):
            desc += f"**{i}.** {t.title} (by {t.requested_by})\n"
    else:
        desc += "_No upcoming tracks._"

    e = discord.Embed(title="Queue", description=desc, color=0x00ff99)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="queue_clear", description="Clear the queue")
async def queue_clear_cmd(interaction: discord.Interaction):
    if maintenance_block(interaction):
        return await interaction.response.send_message("🛠 Bot is in maintenance mode.", ephemeral=True)

    state = get_state(interaction.guild_id)
    while not state.queue.empty():
        state.queue.get_nowait()
    await interaction.response.send_message("Cleared the queue.")


@bot.tree.command(name="queue_shuffle", description="Shuffle the queue")
async def queue_shuffle_cmd(interaction: discord.Interaction):
    if maintenance_block(interaction):
        return await interaction.response.send_message("🛠 Bot is in maintenance mode.", ephemeral=True)

    state = get_state(interaction.guild_id)
    items = list(state.queue._queue)
    if not items:
        return await interaction.response.send_message("Queue is empty.")
    random.shuffle(items)
    state.queue = asyncio.Queue()
    for t in items:
        await state.queue.put(t)
    await interaction.response.send_message("Shuffled the queue.")


@bot.tree.command(name="queue_remove", description="Remove a song from the queue by position")
async def queue_remove_cmd(interaction: discord.Interaction, position: int):
    if maintenance_block(interaction):
        return await interaction.response.send_message("🛠 Bot is in maintenance mode.", ephemeral=True)

    state = get_state(interaction.guild_id)
    items = list(state.queue._queue)
    if position < 1 or position > len(items):
        return await interaction.response.send_message("Invalid position.")
    removed = items.pop(position - 1)
    state.queue = asyncio.Queue()
    for t in items:
        await state.queue.put(t)
    await interaction.response.send_message(f"Removed **{removed.title}** from the queue.")

@bot.tree.command(name="loop", description="Set loop mode")
@app_commands.describe(mode="off / track / queue")
async def loop_cmd(interaction: discord.Interaction, mode: Literal["off", "track", "queue"]):
    if maintenance_block(interaction):
        return await interaction.response.send_message("🛠 Bot is in maintenance mode.", ephemeral=True)

    state = get_state(interaction.guild_id)
    state.loop_mode = mode
    debug("/loop set to:", mode)
    await interaction.response.send_message(f"Loop mode set to **{mode}**.")


@bot.tree.command(name="dashboard", description="Show a control dashboard")
async def dashboard_cmd(interaction: discord.Interaction):
    if maintenance_block(interaction):
        return await interaction.response.send_message("🛠 Bot is in maintenance mode.", ephemeral=True)

    state = get_state(interaction.guild_id)
    debug("/dashboard called")
    e = build_nowplaying_embed(state)
    view = DashboardView(interaction.guild_id)
    await interaction.response.send_message(embed=e, view=view)


@bot.tree.command(name="help", description="Show all bot commands")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Boombox Help Menu",
        description="Here are all available commands:",
        color=0x00ff99
    )

    embed.add_field(
        name="Music Commands",
        value=(
            "**/play <query>** — Play a song or search YouTube\n"
            "**/queue** — Show the current queue\n"
            "**/queue_clear** — Clear the queue\n"
            "**/queue_shuffle** — Shuffle the queue\n"
            "**/queue_remove <pos>** — Remove a song by position\n"
            "**/loop <mode>** — Loop off / track / queue\n"
            "**/leave** — Disconnect the bot\n"
            "**/dashboard** — Playback controls\n"
            "**hey boombox <query>** — Text‑based quick play"
        ),
        inline=False
    )

    embed.add_field(
        name="Playlist Commands",
        value=(
            "**/playlist_create <name>** — Create a playlist\n"
            "**/playlist_add <name> <query>** — Add a song\n"
            "**/playlist_play <name>** — Play a playlist\n"
            "**/playlist_cache <name>** — Cache all songs (FAST)\n"
            "**/playlist_delete <name>** — Delete a playlist\n"
            "**/downloads** — View downloaded songs"
        ),
        inline=False
    )

    embed.add_field(
        name="🛠 Admin / System",
        value=(
            "**/shutdown** — Finish current song then shut down\n"
            "**/restart** — Close bot (you restart it)\n"
            "**/maintenance <on/off>** — Toggle maintenance mode\n"
            "Blocked controls system for griefers"
        ),
        inline=False
    )

    embed.set_footer(text="Boombox Music Bot • Made by Dev")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leave", description="Disconnect the bot from voice")
async def leave_cmd(interaction: discord.Interaction):
    if maintenance_block(interaction):
        return await interaction.response.send_message("🛠 Bot is in maintenance mode.", ephemeral=True)

    state = get_state(interaction.guild_id)
    debug("/leave called")
    if state.voice and state.voice.is_connected():
        await state.voice.disconnect()
        state.voice = None
        state.current = None
        while not state.queue.empty():
            state.queue.get_nowait()
        await interaction.response.send_message("Disconnected and cleared queue.")
    else:
        await interaction.response.send_message("Not connected.")


@bot.tree.command(name="playlist_create", description="Create a playlist")
async def playlist_create_cmd(interaction: discord.Interaction, name: str):
    if maintenance_block(interaction):
        return await interaction.response.send_message("🛠 Bot is in maintenance mode.", ephemeral=True)

    debug("/playlist_create:", name)
    pls = load_playlists()
    if name in pls:
        return await interaction.response.send_message("Playlist already exists.", ephemeral=True)
    pls[name] = {
        "name": name,
        "tracks": [],
        "created_by": interaction.user.id,
    }
    save_playlists(pls)
    await interaction.response.send_message(f"Created playlist **{name}**.")


@bot.tree.command(name="playlist_add", description="Add a track to a playlist")
async def playlist_add_cmd(interaction: discord.Interaction, name: str, query: str):
    if maintenance_block(interaction):
        return await interaction.response.send_message("🛠 Bot is in maintenance mode.", ephemeral=True)

    await interaction.response.defer(thinking=True)
    debug("/playlist_add:", name, query)
    pls = load_playlists()
    if name not in pls:
        return await interaction.followup.send("Playlist not found.", ephemeral=True)

    track = await fetch_track(query, interaction.user.display_name)
    pls[name]["tracks"].append(track.id)
    save_playlists(pls)
    await interaction.followup.send(f"Added **{track.title}** to playlist **{name}**.")

@bot.tree.command(name="forcejoin", description="Force the bot to join your voice channel")
async def forcejoin_cmd(interaction: discord.Interaction):

    # Make sure the user is in a voice channel
    if not interaction.user.voice:
        return await interaction.response.send_message(
            "You must be in a voice channel.", ephemeral=True
        )

    channel = interaction.user.voice.channel
    state = get_state(interaction.guild_id)

    # Disconnect if already connected
    if state.voice and state.voice.is_connected():
        try:
            await state.voice.disconnect(force=True)
        except:
            pass

    # Connect to the user's voice channel
    vc = await channel.connect()
    state.voice = vc

    await interaction.response.send_message(
        f"Joined **{channel.name}**."
    )

@bot.tree.command(name="playlist_play", description="Play a playlist")
async def playlist_play_cmd(interaction: discord.Interaction, name: str):
    if maintenance_block(interaction):
        return await interaction.response.send_message("🛠 Bot is in maintenance mode.", ephemeral=True)
    if requests_blocked(interaction.user.id):
        return await interaction.response.send_message("You are blocked from requesting songs.", ephemeral=True)


    await interaction.response.defer(thinking=True)
    debug("/playlist_play:", name)
    pls = load_playlists()
    if name not in pls:
        return await interaction.followup.send("Playlist not found.", ephemeral=True)

    vc = await ensure_voice(interaction)
    if not vc:
        return

    state = get_state(interaction.guild_id)
    ids = pls[name]["tracks"]
    if not ids:
        return await interaction.followup.send("Playlist is empty.", ephemeral=True)

    for vid in ids:
        cached = load_cached(vid)
        if cached:
            cached.requested_by = interaction.user.display_name
            await state.queue.put(cached)
            debug("Playlist queued cached track:", cached.title)

    await interaction.followup.send(f"Queued playlist **{name}** ({len(ids)} tracks).")

    if not state.voice.is_playing() and not state.voice.is_paused():
        debug("Starting play_loop from /playlist_play")
        bot.loop.create_task(play_loop(interaction.guild_id))


@bot.tree.command(name="playlist_delete", description="Delete a playlist")
async def playlist_delete_cmd(interaction: discord.Interaction, name: str):
    if maintenance_block(interaction):
        return await interaction.response.send_message("🛠 Bot is in maintenance mode.", ephemeral=True)

    debug("/playlist_delete:", name)
    pls = load_playlists()
    if name not in pls:
        return await interaction.response.send_message("Playlist not found.", ephemeral=True)
    del pls[name]
    save_playlists(pls)
    await interaction.response.send_message(f"Deleted playlist **{name}**.")


@bot.tree.command(name="downloads", description="List all downloaded songs (5 per page)")
async def downloads_cmd(interaction: discord.Interaction):
    if maintenance_block(interaction):
        return await interaction.response.send_message("🛠 Bot is in maintenance mode.", ephemeral=True)

    files = [f for f in os.listdir(CACHE_DIR) if f.endswith(".json")]

    if not files:
        return await interaction.response.send_message("No downloaded songs found.")

    tracks = []
    for meta_file in files:
        with open(os.path.join(CACHE_DIR, meta_file), "r", encoding="utf-8") as f:
            data = json.load(f)
            title = data.get("title", "Unknown Title")
            vid = data.get("id", "???")
            tracks.append((title, vid))

    tracks.sort(key=lambda x: x[0].lower())

    pages = []
    for i in range(0, len(tracks), 5):
        chunk = tracks[i:i+5]
        page_lines = [f"**{t[0]}**\nID: `{t[1]}`" for t in chunk]
        pages.append(page_lines)

    view = DownloadsView(pages)
    await interaction.response.send_message(embed=view.get_embed(), view=view)

def make_bar(done, total, length=20):
    if total == 0:
        return "░" * length
    filled = int((done / total) * length)
    return "█" * filled + "░" * (length - filled)


@bot.tree.command(name="playlist_cache", description="Download/cache all songs in a playlist (FAST)")
async def playlist_cache_cmd(interaction: discord.Interaction, name: str):
    if maintenance_block(interaction):
        return await interaction.response.send_message("🛠 Bot is in maintenance mode.", ephemeral=True)

    await interaction.response.defer(thinking=True)
    debug("/playlist_cache:", name)

    pls = load_playlists()
    if name not in pls:
        return await interaction.followup.send("Playlist not found.", ephemeral=True)

    ids = pls[name]["tracks"]
    if not ids:
        return await interaction.followup.send("Playlist is empty.", ephemeral=True)

    total = len(ids)
    done = 0
    failed = 0

    msg = await interaction.followup.send(
        f"Starting multithreaded caching of **{total}** tracks...\n"
        f"`[{'░'*20}] 0%`"
    )
    #ADMIN gets faster downloads
    max_workers = 12 if interaction.user.id == ADMIN_ID else 6
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {executor.submit(fetch_track_sync, vid): vid for vid in ids}

    start = time.time()

    for future in as_completed(futures):
        vid = futures[future]
        try:
            future.result()
            done += 1
        except Exception as e:
            failed += 1
            debug("Cache failed:", vid, e)

        percent = int((done / total) * 100)
        bar = make_bar(done, total)

        elapsed = time.time() - start
        speed = done / elapsed if elapsed > 0 else 0
        remaining = (total - done) / speed if speed > 0 else 0
        eta = time.strftime("%M:%S", time.gmtime(remaining)) if remaining > 0 else "00:00"

        await msg.edit(
            content=(
                f"Caching playlist **{name}** ({total} tracks)\n"
                f"`[{bar}] {percent}%`\n"
                f"Done: **{done}** | Failed: **{failed}**\n"
                f"ETA: **{eta}**\n"
                f"Workers: **{max_workers}**"
            )
        )

    await msg.edit(
        content=(
            f"Finished caching playlist **{name}**.\n"
            f"Downloaded: **{done}**\n"
            f"Failed: **{failed}**"
        )
    )


@bot.tree.command(name="shutdown", description="Finish current song then shut down the bot")
async def shutdown_cmd(interaction: discord.Interaction):
    global SHUTDOWN_PENDING
    if interaction.user.id != ADMIN_ID:
        return await interaction.response.send_message("Only the bot owner can use this.", ephemeral=True)

    SHUTDOWN_PENDING = True
    await interaction.response.send_message("Shutdown initiated. Bot will stop after the current song ends.")

    state = get_state(interaction.guild_id)
    if not state.voice or not state.voice.is_playing():
        await interaction.followup.send("No song playing — shutting down now.")
        await bot.close()


@bot.tree.command(name="restart", description="Close the bot (you must restart it externally)")
async def restart_cmd(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        return await interaction.response.send_message("Only the bot owner can use this.", ephemeral=True)

    await interaction.response.send_message("Restarting bot (process will exit).")
    await bot.close()


@bot.tree.command(name="maintenance", description="Toggle maintenance mode")
async def maintenance_cmd(interaction: discord.Interaction, mode: Literal["on", "off"]):
    global MAINTENANCE_MODE
    if interaction.user.id != ADMIN_ID:
        return await interaction.response.send_message("Only the bot owner can use this.", ephemeral=True)

    MAINTENANCE_MODE = (mode == "on")
    await interaction.response.send_message(
        f"🛠 Maintenance mode is now **{mode.upper()}**."
    )

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} ({bot.user.id})")
    print("Slash commands synced.")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.lower().strip()
    if content.startswith("hey boombox"):
        query = content.replace("hey boombox", "", 1).strip()
        if not query:
            await message.channel.send("Say `hey boombox <song or url>`.")
        else:
            await queue_via_text_phrase(message, query)

    await bot.process_commands(message)

bot.run(TOKEN)
