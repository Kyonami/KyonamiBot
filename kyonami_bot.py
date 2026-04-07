import os
import discord
import asyncio
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dataclasses import dataclass, field
from collections import deque
from typing import Deque, Dict, Optional, Tuple

TOKEN = os.getenv("DISCORD_TOKEN")
TEST_SERVER_ID = 1052089400533729300

intents = discord.Intents.default()
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)

# 끊김 방지용 reconnect 옵션
FFMPEG_BEFORE_OPTS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTS = "-vn"

# yt-dlp 동시 실행 제한(과부하 방지)
YTDLP_SEM = asyncio.Semaphore(2)
YTDLP_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "remote_components": ["ejs:github"],
    "js_runtimes": {"deno": {}},
    "no_warnings": True,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "ios", "web"],
            "player_skip": ["configs"],
        }
    },
}

@dataclass
class QueueItem:
    query: str
    requester: str
    title: Optional[str] = None

@dataclass
class GuildMusicState:
    queue: Deque[QueueItem] = field(default_factory=deque)
    now_playing: Optional[str] = None
    now_requester: Optional[str] = None
    now_playing_item: Optional[QueueItem] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    advancing: bool = False
    repeat: bool = False
    volume: float = 1.0
    exclude_mv: bool = True

guild_states: Dict[int, GuildMusicState] = {}

def get_state(guild_id: int) -> GuildMusicState:
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildMusicState()
    return guild_states[guild_id]

def extract_audio(query: str):
    """
    query: 유튜브 URL 또는 검색어
    return: (stream_url, title, duration_seconds, headers)  <- 리턴값 추가됨
    """
    with yt_dlp.YoutubeDL(YTDLP_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            info = info["entries"][0]
        
        return info["url"], info.get("title", "Unknown Title"), info.get("duration"), info.get("http_headers", {})

async def extract_audio_async(query: str):
    async with YTDLP_SEM:
        return await asyncio.to_thread(extract_audio, query)

async def start_next_track(guild: discord.Guild, text_channel: Optional[discord.abc.Messageable] = None):
    state = get_state(guild.id)

    async with state.lock:
        if state.advancing:
            return
        state.advancing = True

    try:
        async with state.lock:
            vc = guild.voice_client
            if not vc or not vc.is_connected():
                return
            if vc.is_playing() or vc.is_paused():
                return

            if state.repeat and state.now_playing_item:
                state.queue.append(state.now_playing_item)

            if not state.queue:
                state.now_playing = None
                state.now_requester = None
                state.now_playing_item = None
                await vc.disconnect()
                return

            item = state.queue.popleft()
            query = item.query
            requester = item.requester
            state.now_requester = requester
            state.now_playing_item = item 

        try:
            stream_url, title, _duration, headers = await extract_audio_async(query)
        except Exception as e:
            if text_channel:
                asyncio.run_coroutine_threadsafe(
                    text_channel.send(f"❌ 곡 정보를 불러오지 못했습니다: `{query}`\n(에러: {e})"),
                    bot.loop
                )
            async with state.lock:
                state.advancing = False
            asyncio.run_coroutine_threadsafe(start_next_track(guild, text_channel), bot.loop)
            return

        async with state.lock:
            state.now_playing = title
            vc = guild.voice_client
            if not vc or not vc.is_connected() or vc.is_playing() or vc.is_paused():
                return

        def _after_play(err: Optional[Exception]):
            if err and text_channel:
                asyncio.run_coroutine_threadsafe(
                    text_channel.send(f"재생 중 오류: {type(err).__name__}: {err}"),
                    bot.loop
                )
            asyncio.run_coroutine_threadsafe(start_next_track(guild, text_channel), bot.loop)

        header_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items()) if headers else ""
        custom_before_opts = FFMPEG_BEFORE_OPTS
        if header_str:
            custom_before_opts += f' -headers "{header_str}"'

        source = discord.FFmpegPCMAudio(
            stream_url,
            before_options=custom_before_opts,
            options=FFMPEG_OPTS
        )
        source = discord.PCMVolumeTransformer(source, volume=state.volume)
        vc.play(source, after=_after_play)

        if text_channel:
            await text_channel.send(f"▶️ 재생 시작: **{title}** (요청: {requester})")

    finally:
        async with state.lock:
            state.advancing = False


async def fetch_title_async(query: str) -> Optional[str]:
    def _fetch() -> Optional[str]:
        opts = {
            **YTDLP_OPTS,
            "quiet": True,
            "noplaylist": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return info.get("title")

    async with YTDLP_SEM:
        try:
            return await asyncio.to_thread(_fetch)
        except Exception:
            return None

async def prefetch_queue_item_title(guild_id: int, item: QueueItem):
    title = await fetch_title_async(item.query)
    if not title:
        return
    state = get_state(guild_id)
    async with state.lock:
        item.title = title

async def update_play_message_with_title(guild_id: int, item: QueueItem, qpos: int, msg: discord.Message):
    title = await fetch_title_async(item.query)
    if not title:
        try:
            await msg.edit(content=f"✅ 큐에 추가됨 (#{qpos}): `{item.query}`")
        except Exception:
            pass
        return

    state = get_state(guild_id)
    async with state.lock:
        item.title = title

    try:
        await msg.edit(content=f"✅ 큐에 추가됨 (#{qpos}): **{title}** (요청: {item.requester})")
    except Exception:
        pass

async def update_playnext_message_with_title(guild_id: int, item: QueueItem, msg: discord.Message):
    title = await fetch_title_async(item.query)
    if not title:
        try:
            await msg.edit(content=f"⚡ 다음 곡으로 추가됨: `{item.query}`")
        except Exception:
            pass
        return

    state = get_state(guild_id)
    async with state.lock:
        item.title = title

    try:
        await msg.edit(content=f"⚡ 다음 곡으로 추가됨: **{title}** (요청: {item.requester})")
    except Exception:
        pass


@bot.event
async def on_ready():
    # 특정 서버에만 즉시 동기화 (개발용)
    print(f"Test server id: {TEST_SERVER_ID}")
    guild = discord.Object(id=TEST_SERVER_ID)
    bot.tree.clear_commands(guild=guild)
    await bot.tree.sync(guild=guild)
    await bot.tree.sync()
    
    print(f"Logged in as {bot.user}")

# ======================
# Command Implementation
# ======================
async def play_impl(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)

    if not interaction.user or not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("멤버 정보가 없어서 처리 불가", ephemeral=True)
        return

    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        voice_state = interaction.user.voice
        if not voice_state or not voice_state.channel:
            await interaction.followup.send("먼저 음성 채널에 들어가줘", ephemeral=True)
            return
        vc = await voice_state.channel.connect()

    state = get_state(interaction.guild.id)

    search_query = query
    if state.exclude_mv and not query.startswith("http"):
        search_query = f"{query} audio"

    item = QueueItem(query=search_query, requester=interaction.user.display_name)

    async with state.lock:
        state.queue.append(item)
        qpos = len(state.queue)

    msg = await interaction.followup.send(
        f"✅ 큐에 추가됨 (#{qpos}): (제목 불러오는 중...)",
        wait=True
    )

    asyncio.create_task(update_play_message_with_title(interaction.guild.id, item, qpos, msg))

    if not vc.is_playing() and not vc.is_paused():
        await start_next_track(interaction.guild, interaction.channel)

async def queue_impl(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)

    lines = []

    repeat_status = "🔁 반복 모드: **ON**" if state.repeat else "➡️ 반복 모드: **OFF**"
    lines.append(repeat_status)

    if state.now_playing:
        lines.append(f"🎧 Now: **{state.now_playing}** (요청: {state.now_requester})")
    else:
        lines.append("🎧 Now: (없음)")

    if not state.queue:
        lines.append("📭 Queue: (비어있음)")
    else:
        lines.append("📜 Queue:")
        for i, item in enumerate(list(state.queue)[:10], start=1):
            show = item.title or "(제목 불러오는 중...)"
            lines.append(f"{i}. {show} (요청: {item.requester})")
        if len(state.queue) > 10:
            lines.append(f"... +{len(state.queue)-10}개 더")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)

async def skip_impl(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected() or (not vc.is_playing() and not vc.is_paused()):
        await interaction.followup.send("지금 재생 중인 곡이 없어")
        return

    vc.stop()
    await interaction.followup.send("⏭️ 스킵")

async def stop_impl(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    async with state.lock:
        state.queue.clear()
        state.now_playing = None
        state.now_requester = None
        state.repeat = False 

    vc = interaction.guild.voice_client
    if vc and vc.is_connected():
        if vc.is_playing() or vc.is_paused():
            vc.stop()

    await interaction.response.send_message("⏹️ 정지 + 큐 초기화", ephemeral=True)

async def remove_impl(interaction: discord.Interaction, index: int):
    state = get_state(interaction.guild.id)

    async with state.lock:
        qlen = len(state.queue)
        if qlen == 0:
            await interaction.response.send_message("📭 큐가 비어있음", ephemeral=True)
            return

        if index < 1 or index > qlen:
            await interaction.response.send_message(
                f"❌ 인덱스 범위가 아님. (1 ~ {qlen})",
                ephemeral=True
            )
            return

        items = list(state.queue)
        removed = items.pop(index - 1)
        state.queue = deque(items)

    await interaction.response.send_message(
        f"🗑️ 삭제됨 #{index}: `{removed.title}` (요청: {removed.requester})",
        ephemeral=True
    )

async def clear_impl(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)

    async with state.lock:
        cleared = len(state.queue)
        state.queue.clear()

    await interaction.response.send_message(
        f"🧹 큐 초기화 완료 (삭제된 곡: {cleared}개)",
        ephemeral=True
    )

async def repeat_impl(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)

    async with state.lock:
        state.repeat = not state.repeat
        new_state = state.repeat

    if new_state:
        await interaction.response.send_message("🔁 반복 모드 **ON** — 큐가 계속 순환합니다.", ephemeral=True)
    else:
        await interaction.response.send_message("➡️ 반복 모드 **OFF** — 재생 후 큐에서 제거됩니다.", ephemeral=True)

async def playnext_impl(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)

    if not interaction.user or not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("멤버 정보가 없어서 처리 불가", ephemeral=True)
        return

    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        voice_state = interaction.user.voice
        if not voice_state or not voice_state.channel:
            await interaction.followup.send("먼저 음성 채널에 들어가줘", ephemeral=True)
            return
        vc = await voice_state.channel.connect()

    state = get_state(interaction.guild.id)

    search_query = query
    if state.exclude_mv and not query.startswith("http"):
        search_query = f"{query} audio"

    item = QueueItem(query=search_query, requester=interaction.user.display_name)

    async with state.lock:
        state.queue.appendleft(item)

    msg = await interaction.followup.send(
        f"⚡ 다음 곡으로 추가됨: (제목 불러오는 중...)",
        wait=True
    )

    asyncio.create_task(update_playnext_message_with_title(interaction.guild.id, item, msg))

    if not vc.is_playing() and not vc.is_paused():
        await start_next_track(interaction.guild, interaction.channel)

async def volume_impl(interaction: discord.Interaction, level: int):
    if not 0 <= level <= 200:
        await interaction.response.send_message(
            "❌ 볼륨은 0 ~ 200 사이로 설정해줘", ephemeral=True
        )
        return

    state = get_state(interaction.guild.id)

    async with state.lock:
        state.volume = level / 100.0  

        vc = interaction.guild.voice_client
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = state.volume

    await interaction.response.send_message(
        f"🔊 볼륨 설정: **{level}%**", ephemeral=True
    )
async def mvfilter_impl(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)

    async with state.lock:
        state.exclude_mv = not state.exclude_mv
        new_state = state.exclude_mv

    if new_state:
        await interaction.response.send_message(
            "🚫 **MV 제외 모드 ON** — 앞으로 노래를 검색할 때 공식 음원(유튜브 뮤직) 위주로 찾아옵니다.", 
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "🎬 **MV 제외 모드 OFF** — 기본 유튜브 검색을 사용합니다 (뮤직비디오가 나올 수 있습니다).", 
            ephemeral=True
        )

# =====================
# Korean alias commands
# =====================

@bot.tree.command(name="play", description="유튜브 URL 또는 검색어를 큐에 추가하고 재생")
@app_commands.describe(query="유튜브 URL 또는 검색어")
async def play_cmd(interaction: discord.Interaction, query: str):
    await play_impl(interaction, query)

@bot.tree.command(name="재생", description="유튜브 URL 또는 검색어를 큐에 추가하고 재생")
@app_commands.describe(query="유튜브 URL 또는 검색어")
async def play_kr(interaction: discord.Interaction, query: str):
    await play_impl(interaction, query)

@bot.tree.command(name="틀어", description="유튜브 URL 또는 검색어를 큐에 추가하고 재생")
@app_commands.describe(query="유튜브 URL 또는 검색어")
async def play_kr1(interaction: discord.Interaction, query: str):
    await play_impl(interaction, query)

@bot.tree.command(name="queue", description="현재 재생 큐를 보여줍니다.")
async def queue_cmd(interaction: discord.Interaction):
    await queue_impl(interaction)

@bot.tree.command(name="큐", description="현재 재생 큐를 보여줍니다.")
async def queue_kr(interaction: discord.Interaction):
    await queue_impl(interaction)

@bot.tree.command(name="목록", description="현재 재생 큐를 보여줍니다.")
async def queue_kr1(interaction: discord.Interaction):
    await queue_impl(interaction)

@bot.tree.command(name="skip", description="현재 재생 중인 곡을 스킵합니다.")
async def skip_cmd(interaction: discord.Interaction):
    await skip_impl(interaction)

@bot.tree.command(name="스킵", description="현재 재생 중인 곡을 스킵합니다.")
async def skip_kr(interaction: discord.Interaction):
    await skip_impl(interaction)

@bot.tree.command(name="넘겨", description="현재 재생 중인 곡을 스킵합니다.")
async def skip_kr1(interaction: discord.Interaction):
    await skip_impl(interaction)

@bot.tree.command(name="stop", description="재생 중단하고 큐를 비웁니다.")
async def stop_cmd(interaction: discord.Interaction):
    await stop_impl(interaction)

@bot.tree.command(name="정지", description="재생 중단하고 큐를 비웁니다.")
async def stop_kr(interaction: discord.Interaction):
    await stop_impl(interaction)

@bot.tree.command(name="그만", description="재생 중단하고 큐를 비웁니다.")
async def stop_kr1(interaction: discord.Interaction):
    await stop_impl(interaction)

@bot.tree.command(name="remove", description="큐에서 특정 인덱스의 곡을 삭제합니다. (1부터 시작)")
@app_commands.describe(index="삭제할 큐 인덱스(1부터)")
async def remove_cmd(interaction: discord.Interaction, index: int):
    await remove_impl(interaction, index)

@bot.tree.command(name="삭제", description="큐에서 특정 인덱스의 곡을 삭제합니다. (1부터 시작)")
@app_commands.describe(index="삭제할 큐 인덱스(1부터)")
async def remove_kr(interaction: discord.Interaction, index: int):
    await remove_impl(interaction, index)

@bot.tree.command(name="지워", description="큐에서 특정 인덱스의 곡을 삭제합니다. (1부터 시작)")
@app_commands.describe(index="삭제할 큐 인덱스(1부터)")
async def remove_kr1(interaction: discord.Interaction, index: int):
    await remove_impl(interaction, index)

@bot.tree.command(name="clear", description="큐(대기열)를 모두 비웁니다. 현재 재생은 유지됩니다.")
async def clear_cmd(interaction: discord.Interaction):
    await clear_impl(interaction)

@bot.tree.command(name="초기화", description="큐(대기열)를 모두 비웁니다. 현재 재생은 유지됩니다.")
async def clear_kr(interaction: discord.Interaction):
    await clear_impl(interaction)

@bot.tree.command(name="날려", description="큐(대기열)를 모두 비웁니다. 현재 재생은 유지됩니다.")
async def clear_kr1(interaction: discord.Interaction):
    await clear_impl(interaction)

@bot.tree.command(name="repeat", description="큐 반복 모드를 On/Off 토글합니다.")
async def repeat_cmd(interaction: discord.Interaction):
    await repeat_impl(interaction)

@bot.tree.command(name="반복", description="큐 반복 모드를 On/Off 토글합니다.")
async def repeat_kr(interaction: discord.Interaction):
    await repeat_impl(interaction)

@bot.tree.command(name="playnext", description="현재 재생 중인 곡 바로 다음에 곡을 추가합니다.")
@app_commands.describe(query="유튜브 URL 또는 검색어")
async def playnext_cmd(interaction: discord.Interaction, query: str):
    await playnext_impl(interaction, query)

@bot.tree.command(name="다음곡", description="현재 재생 중인 곡 바로 다음에 곡을 추가합니다.")
@app_commands.describe(query="유튜브 URL 또는 검색어")
async def playnext_kr(interaction: discord.Interaction, query: str):
    await playnext_impl(interaction, query)

@bot.tree.command(name="바로틀어", description="현재 재생 중인 곡 바로 다음에 곡을 추가합니다.")
@app_commands.describe(query="유튜브 URL 또는 검색어")
async def playnext_kr1(interaction: discord.Interaction, query: str):
    await playnext_impl(interaction, query)

@bot.tree.command(name="volume", description="볼륨을 조절합니다. (0 ~ 200, 기본값 100)")
@app_commands.describe(level="볼륨 값 (0 ~ 200)")
async def volume_cmd(interaction: discord.Interaction, level: int):
    await volume_impl(interaction, level)

@bot.tree.command(name="볼륨", description="볼륨을 조절합니다. (0 ~ 200, 기본값 100)")
@app_commands.describe(level="볼륨 값 (0 ~ 200)")
async def volume_kr(interaction: discord.Interaction, level: int):
    await volume_impl(interaction, level)

@bot.tree.command(name="mvfilter", description="뮤비 대신 깔끔한 공식 음원/가사 영상을 우선으로 찾도록 켜고 끕니다.")
async def mvfilter_cmd(interaction: discord.Interaction):
    await mvfilter_impl(interaction)

@bot.tree.command(name="음원만", description="뮤비 대신 깔끔한 공식 음원/가사 영상을 우선으로 찾도록 켜고 끕니다.")
async def mvfilter_kr(interaction: discord.Interaction):
    await mvfilter_impl(interaction)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("환경변수 DISCORD_TOKEN 이 비어있음")
    bot.run(TOKEN)
