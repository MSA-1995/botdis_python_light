import asyncio
from collections import deque
from dataclasses import dataclass, field
import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from datetime import datetime, timezone

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
STORE_PATH = DATA_DIR / "store.json"

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
PORT = int(os.getenv("PORT", "8000") or 8000)

DEFAULT_ROOM_NAME = os.getenv("ROOM_NAME_TEMPLATE", "🎙️ S3 {username}")
DEFAULT_LIMIT = int(os.getenv("DEFAULT_LIMIT", "0") or 0)
DEFAULT_BITRATE = int(os.getenv("DEFAULT_BITRATE", "64000") or 64000)
AUTO_DELETE_DELAY = int(os.getenv("AUTO_DELETE_DELAY", "3") or 3)
CREATE_COOLDOWN = 5
MUSIC_SEARCH_PROVIDER = os.getenv("MUSIC_SEARCH_PROVIDER", "soundcloud").strip().lower()
MUSIC_AUDIO_MODE = os.getenv("MUSIC_AUDIO_MODE", "opus").strip().lower()  # opus أفضل من pcm
MUSIC_DOWNLOAD_BEFORE_PLAY = os.getenv("MUSIC_DOWNLOAD_BEFORE_PLAY", "true").strip().lower() in {"1", "true", "yes", "on"}
APP_VERSION = "2026-05-24.1"
INSTANCE_ID = os.getenv("KOYEB_DEPLOYMENT_ID") or os.getenv("HOSTNAME") or f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
INSTANCE_STARTED_AT = time.time()
INSTANCE_LOCK_CHANNEL_ID = int(os.getenv("INSTANCE_LOCK_CHANNEL_ID", "0") or 0)
INSTANCE_LOCK_CHANNEL_NAME = os.getenv("SINGLETON_CHANNEL_NAME", "🤖・bot-status")
INSTANCE_LOCK_MARKER = "BOTDIS_MUSIC_SINGLETON_LEASE"
INSTANCE_LOCK_CHECK_SECONDS = int(os.getenv("LEASE_CHECK_SECONDS", "15") or 15)
INSTANCE_LOCK_STALE_SECONDS = int(os.getenv("LEASE_STALE_SECONDS", "45") or 45)


def now() -> int:
    return int(time.time())


def load_store() -> dict[str, Any]:
    if not STORE_PATH.exists():
        return {"guilds": {}, "rooms": {}}
    try:
        with STORE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("guilds", {})
        data.setdefault("rooms", {})
        return data
    except json.JSONDecodeError:
        return {"guilds": {}, "rooms": {}}


def save_store() -> None:
    with STORE_PATH.open("w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)


store = load_store()
create_cooldowns: dict[int, int] = {}
delete_tasks: dict[int, asyncio.Task] = {}


@dataclass
class MusicBot:
    token: str
    client: discord.Client
    voice_channel_id: int | None = None


@dataclass
class MusicSession:
    bot: MusicBot
    guild_id: int
    voice_channel_id: int
    text_channel_id: int | None
    queue: deque[dict[str, str]] = field(default_factory=deque)
    now_playing: dict[str, str] | None = None


music_bots: list[MusicBot] = []
music_sessions: dict[int, MusicSession] = {}
instance_lock_started = False
instance_lock_logged = False


def env_config(guild_id: int) -> dict[str, Any] | None:
    category_id = int(os.getenv("CATEGORY_ID", "0") or 0)
    join_channel_id = int(os.getenv("JOIN_CHANNEL_ID", "0") or 0)
    log_channel_id = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)
    if not category_id or not join_channel_id:
        return None
    return {
        "category_id": category_id,
        "join_channel_id": join_channel_id,
        "log_channel_id": log_channel_id,
        "room_name_template": DEFAULT_ROOM_NAME,
        "default_limit": DEFAULT_LIMIT,
        "default_bitrate": DEFAULT_BITRATE,
    }


def get_guild_config(guild_id: int) -> dict[str, Any] | None:
    return env_config(guild_id) or store["guilds"].get(str(guild_id))


def room_name(template: str, member: discord.Member) -> str:
    return template.replace("{username}", member.display_name).replace("{user}", member.name)[:100]


def room_embed(room: dict[str, Any], owner: discord.Member | None) -> discord.Embed:
    owner_text = owner.mention if owner else f"<@{room['owner_id']}>"
    status = []
    status.append("🔒 مقفل" if room.get("locked") else "🔓 مفتوح")
    status.append("👁️ مخفي" if room.get("hidden") else "👁️ ظاهر")

    embed = discord.Embed(
        title="لوحة تحكم الروم",
        description="استخدم الأزرار للتحكم السريع بالروم.",
        color=0x5865F2,
    )
    embed.add_field(name="المالك", value=owner_text, inline=True)
    embed.add_field(name="الحد", value=str(room.get("limit", 0) or "بلا حد"), inline=True)
    embed.add_field(name="الجودة", value=f"{int(room.get('bitrate', DEFAULT_BITRATE) / 1000)} kbps", inline=True)
    embed.add_field(name="الحالة", value=" - ".join(status), inline=False)
    return embed


class RenameModal(discord.ui.Modal, title="تغيير اسم الروم"):
    name = discord.ui.TextInput(label="الاسم الجديد", max_length=100)

    def __init__(self, room_id: int):
        super().__init__(timeout=120)
        self.room_id = room_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        room = await get_owned_room(interaction, self.room_id)
        if not room:
            return
        channel = interaction.guild.get_channel(self.room_id)
        if isinstance(channel, discord.VoiceChannel):
            await channel.edit(name=str(self.name), reason="Temp room rename")
        room["name"] = str(self.name)
        save_store()
        await interaction.response.send_message("تم تغيير اسم الروم.", ephemeral=True)


class LimitModal(discord.ui.Modal, title="تحديد عدد الأعضاء"):
    limit = discord.ui.TextInput(label="العدد الأقصى (0 = بلا حد)", max_length=2)

    def __init__(self, room_id: int):
        super().__init__(timeout=120)
        self.room_id = room_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        room = await get_owned_room(interaction, self.room_id)
        if not room:
            return
        try:
            limit = max(0, min(99, int(str(self.limit))))
        except ValueError:
            await interaction.response.send_message("اكتب رقم صحيح من 0 إلى 99.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(self.room_id)
        if isinstance(channel, discord.VoiceChannel):
            await channel.edit(user_limit=limit, reason="Temp room limit")
        room["limit"] = limit
        save_store()
        await interaction.response.send_message("تم تحديث الحد.", ephemeral=True)


class BitrateModal(discord.ui.Modal, title="تغيير جودة الصوت"):
    bitrate = discord.ui.TextInput(label="الجودة kbps", placeholder="64", max_length=3)

    def __init__(self, room_id: int):
        super().__init__(timeout=120)
        self.room_id = room_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        room = await get_owned_room(interaction, self.room_id)
        if not room:
            return
        try:
            bitrate = max(8, min(384, int(str(self.bitrate)))) * 1000
        except ValueError:
            await interaction.response.send_message("اكتب رقم صحيح مثل 64.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(self.room_id)
        if isinstance(channel, discord.VoiceChannel):
            bitrate = min(bitrate, interaction.guild.bitrate_limit)
            await channel.edit(bitrate=bitrate, reason="Temp room bitrate")
        room["bitrate"] = bitrate
        save_store()
        await interaction.response.send_message("تم تحديث الجودة.", ephemeral=True)


class RoomControls(discord.ui.View):
    def __init__(self, room_id: int):
        super().__init__(timeout=None)
        self.room_id = room_id
        self.add_item(self.make_button("تغيير الاسم", discord.ButtonStyle.primary, "rename", self.rename))
        self.add_item(self.make_button("الحد", discord.ButtonStyle.secondary, "limit", self.limit))
        self.add_item(self.make_button("الجودة", discord.ButtonStyle.secondary, "bitrate", self.bitrate))
        self.add_item(self.make_button("قفل/فتح", discord.ButtonStyle.secondary, "lock", self.lock))
        self.add_item(self.make_button("إخفاء/إظهار", discord.ButtonStyle.secondary, "hide", self.hide))
        self.add_item(self.make_button("حذف", discord.ButtonStyle.danger, "delete", self.delete))

    def make_button(self, label: str, style: discord.ButtonStyle, action: str, callback: Any) -> discord.ui.Button:
        button = discord.ui.Button(label=label, style=style, custom_id=f"room:{action}:{self.room_id}")
        button.callback = callback
        return button

    async def rename(self, interaction: discord.Interaction) -> None:
        if await get_owned_room(interaction, self.room_id):
            await interaction.response.send_modal(RenameModal(self.room_id))

    async def limit(self, interaction: discord.Interaction) -> None:
        if await get_owned_room(interaction, self.room_id):
            await interaction.response.send_modal(LimitModal(self.room_id))

    async def bitrate(self, interaction: discord.Interaction) -> None:
        if await get_owned_room(interaction, self.room_id):
            await interaction.response.send_modal(BitrateModal(self.room_id))

    async def lock(self, interaction: discord.Interaction) -> None:
        room = await get_owned_room(interaction, self.room_id)
        if not room:
            return
        channel = interaction.guild.get_channel(self.room_id)
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message("الروم غير موجود.", ephemeral=True)
            return
        locked = not room.get("locked", False)
        await channel.set_permissions(interaction.guild.default_role, connect=not locked)
        room["locked"] = locked
        save_store()
        await interaction.response.send_message("تم قفل الروم." if locked else "تم فتح الروم.", ephemeral=True)

    async def hide(self, interaction: discord.Interaction) -> None:
        room = await get_owned_room(interaction, self.room_id)
        if not room:
            return
        channel = interaction.guild.get_channel(self.room_id)
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message("الروم غير موجود.", ephemeral=True)
            return
        hidden = not room.get("hidden", False)
        await channel.set_permissions(interaction.guild.default_role, view_channel=not hidden)
        room["hidden"] = hidden
        save_store()
        await interaction.response.send_message("تم إخفاء الروم." if hidden else "تم إظهار الروم.", ephemeral=True)

    async def delete(self, interaction: discord.Interaction) -> None:
        room = await get_owned_room(interaction, self.room_id)
        if not room:
            return
        await interaction.response.send_message("تم حذف الروم.", ephemeral=True)
        await delete_room(interaction.guild, self.room_id, "حذف من اللوحة")


intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


async def send_log(guild: discord.Guild, text: str) -> None:
    import os as _os
    log_channel_id = _os.getenv("LOG_CHANNEL_ID")
    channel = None
    if log_channel_id and log_channel_id.isdigit():
        channel = guild.get_channel(int(log_channel_id))
    if not channel:
        cfg = get_guild_config(guild.id)
        if cfg and cfg.get("log_channel_id"):
            channel = guild.get_channel(int(cfg["log_channel_id"]))
    if not channel:
        channel = discord.utils.get(guild.text_channels, name="📋・logs")
    if not channel or not hasattr(channel, "send"):
        return
    from datetime import datetime, timezone as _tz
    embed = discord.Embed(description=text, color=0x5865F2, timestamp=datetime.now(_tz.utc))
    embed.set_footer(text="نظام الحماية • MSA")
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    await channel.send(embed=embed)


async def get_instance_lock_channel() -> discord.TextChannel | None:
    channel_id = INSTANCE_LOCK_CHANNEL_ID
    if channel_id:
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.HTTPException:
                channel = None
        if isinstance(channel, discord.TextChannel):
            return channel

    for guild in bot.guilds:
        channel = discord.utils.get(guild.text_channels, name=INSTANCE_LOCK_CHANNEL_NAME)
        if isinstance(channel, discord.TextChannel):
            return channel

    for guild in bot.guilds:
        me = guild.me or guild.get_member(bot.user.id)
        if not me or not me.guild_permissions.manage_channels:
            continue
        try:
            return await guild.create_text_channel(
                name=INSTANCE_LOCK_CHANNEL_NAME,
                overwrites={
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_messages=True),
                },
                reason="Music bot status channel for singleton guard",
            )
        except discord.HTTPException:
            continue
    return None


async def cleanup_lock_messages_outside(target_channel: discord.TextChannel) -> None:
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.id == target_channel.id:
                continue
            try:
                async for message in channel.history(limit=50):
                    if message.author.id == bot.user.id and is_instance_lock_message(message):
                        await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                continue


async def find_lock_message(channel: discord.TextChannel) -> discord.Message | None:
    messages: list[discord.Message] = []
    try:
        async for message in channel.history(limit=100):
            if message.author.id == bot.user.id and is_instance_lock_message(message):
                messages.append(message)
    except discord.HTTPException:
        return None

    keep = messages[0] if messages else None
    for message in messages[1:]:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
    return keep


def is_instance_lock_message(message: discord.Message) -> bool:
    if message.content.startswith(INSTANCE_LOCK_MARKER):
        return True
    return any(
        embed.footer and embed.footer.text and INSTANCE_LOCK_MARKER in embed.footer.text
        for embed in message.embeds
    )


def read_instance_lock(message: discord.Message | None) -> dict[str, Any]:
    if not message:
        return {}
    if message.content.startswith(INSTANCE_LOCK_MARKER):
        raw = message.content[len(INSTANCE_LOCK_MARKER):].strip()
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {}
    if not message.embeds:
        return {}
    fields = {field.name: field.value for field in message.embeds[0].fields}
    return {
        "instance_id": fields.get("معرف النسخة"),
        "started_at": extract_discord_timestamp(fields.get("وقت التشغيل", "")),
        "heartbeat_at": extract_discord_timestamp(fields.get("آخر تحديث", "")),
    }


def extract_discord_timestamp(value: str) -> float:
    start = (value or "").find("<t:")
    if start == -1:
        return 0
    end = value.find(":", start + 3)
    if end == -1:
        end = value.find(">", start)
    try:
        return float(value[start + 3:end])
    except (ValueError, TypeError):
        return 0


def build_instance_lock_embed(payload: dict[str, Any]) -> discord.Embed:
    started_at = int(float(payload["started_at"]))
    heartbeat_at = int(float(payload["heartbeat_at"]))
    embed = discord.Embed(
        title="حالة تشغيل بوت الموسيقى",
        description="النسخة الحالية تعمل الآن.",
        color=0x3498DB,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="الحالة", value="Online", inline=False)
    embed.add_field(name="معرف النسخة", value=str(payload["instance_id"]), inline=False)
    embed.add_field(name="وقت التشغيل", value=f"<t:{started_at}:F>", inline=False)
    embed.add_field(name="آخر تحديث", value=f"<t:{heartbeat_at}:R>", inline=False)
    embed.add_field(name="الإصدار", value=APP_VERSION, inline=False)
    embed.set_footer(text=f"نظام التشغيل • {INSTANCE_LOCK_MARKER}")
    return embed


async def stop_current_instance() -> None:
    print("Newer music bot instance detected. Shutting down this instance.")
    for music_bot in music_bots:
        await music_bot.client.close()
    await bot.close()
    os._exit(0)


async def write_instance_lock() -> None:
    global instance_lock_logged
    channel = await get_instance_lock_channel()
    if channel is None:
        print("Instance lock disabled: bot-status channel not found and cannot be created.")
        return
    await cleanup_lock_messages_outside(channel)

    message = await find_lock_message(channel)
    payload = read_instance_lock(message)
    now_ts = time.time()
    owner_id = payload.get("instance_id")
    owner_started_at = float(payload.get("started_at", 0) or 0)
    heartbeat_at = float(payload.get("heartbeat_at", 0) or 0)
    owner_is_newer = owner_id != INSTANCE_ID and owner_started_at > INSTANCE_STARTED_AT
    owner_is_alive = now_ts - heartbeat_at < INSTANCE_LOCK_STALE_SECONDS

    if owner_is_newer and owner_is_alive:
        await stop_current_instance()
        return

    new_payload = {
        "instance_id": INSTANCE_ID,
        "started_at": INSTANCE_STARTED_AT,
        "heartbeat_at": now_ts,
    }
    embed = build_instance_lock_embed(new_payload)
    try:
        if message:
            await message.edit(content="", embed=embed)
        else:
            await channel.send(embed=embed)
        if not instance_lock_logged:
            instance_lock_logged = True
            print(f"Instance lock active in channel {channel.id}: {INSTANCE_ID}")
    except discord.HTTPException as exc:
        print(f"Instance lock failed: {exc}")


async def watch_instance_lock() -> None:
    await asyncio.sleep(10)
    while not bot.is_closed():
        await write_instance_lock()
        await asyncio.sleep(INSTANCE_LOCK_CHECK_SECONDS)


async def send_voice_panel(channel: discord.VoiceChannel, room: dict[str, Any]) -> None:
    owner = channel.guild.get_member(int(room["owner_id"]))
    try:
        message = await channel.send(embed=room_embed(room, owner), view=RoomControls(channel.id))
        room["control_message_id"] = message.id
        save_store()
    except (discord.Forbidden, AttributeError):
        pass


async def get_owned_room(interaction: discord.Interaction, room_id: int | None = None) -> dict[str, Any] | None:
    if not interaction.guild:
        return None
    if room_id is None:
        voice = getattr(interaction.user, "voice", None)
        room_id = voice.channel.id if voice and voice.channel else interaction.channel_id
    room = store["rooms"].get(str(room_id))
    if not room:
        await interaction.response.send_message("هذا ليس روم مؤقت تابع للبوت.", ephemeral=True)
        return None
    if int(room["owner_id"]) != interaction.user.id:
        await interaction.response.send_message("فقط مالك الروم يقدر يستخدم هذا التحكم.", ephemeral=True)
        return None
    return room


async def create_room(member: discord.Member, cfg: dict[str, Any]) -> None:
    guild = member.guild
    for room in store["rooms"].values():
        if int(room.get("guild_id", 0)) == guild.id and int(room.get("owner_id", 0)) == member.id:
            existing = guild.get_channel(int(room["channel_id"]))
            if isinstance(existing, discord.VoiceChannel):
                await member.move_to(existing, reason="Move to existing temp room")
                return

    template = cfg.get("room_name_template") or DEFAULT_ROOM_NAME
    name = room_name(template, member)
    category = guild.get_channel(int(cfg["category_id"]))
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=True),
        member: discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            move_members=True,
            mute_members=True,
            deafen_members=True,
            manage_channels=True,
        ),
    }
    channel = await guild.create_voice_channel(
        name=name,
        category=category if isinstance(category, discord.CategoryChannel) else None,
        bitrate=min(int(cfg.get("default_bitrate", DEFAULT_BITRATE)), guild.bitrate_limit),
        user_limit=int(cfg.get("default_limit", DEFAULT_LIMIT)),
        overwrites=overwrites,
        reason="Create temp room",
    )
    room = {
        "guild_id": guild.id,
        "channel_id": channel.id,
        "owner_id": member.id,
        "name": name,
        "limit": int(cfg.get("default_limit", DEFAULT_LIMIT)),
        "bitrate": int(cfg.get("default_bitrate", DEFAULT_BITRATE)),
        "locked": False,
        "hidden": False,
        "trusted": [],
        "banned": [],
        "created_at": now(),
    }
    # انشاء رول مؤقت يعطي صاحب الروم Send Messages بالروم فقط
    try:
        role = await guild.create_role(name=f"room-{channel.id}", reason="Temp room role")
        await channel.set_permissions(role, send_messages=True)
        await member.add_roles(role, reason="Temp room owner")
        room["temp_role_id"] = role.id
    except Exception:
        pass
    store["rooms"][str(channel.id)] = room
    save_store()
    await member.move_to(channel, reason="Move to temp room")
    await send_voice_panel(channel, room)
    await send_log(guild, f"🎙️ تم إنشاء روم {channel.mention} للعضو {member.mention}")


async def delete_room(guild: discord.Guild, channel_id: int, reason: str) -> None:
    room = store["rooms"].pop(str(channel_id), None)
    save_store()
    task = delete_tasks.pop(channel_id, None)
    if task:
        task.cancel()
    # حذف الرول المؤقت اذا موجود
    if room and room.get("temp_role_id"):
        try:
            role = guild.get_role(int(room["temp_role_id"]))
            if role:
                await role.delete(reason="Temp room deleted")
        except Exception:
            pass
    channel = guild.get_channel(channel_id)
    if channel:
        await channel.delete(reason=reason)
    if room:
        await send_log(guild, f"🗑️ تم حذف روم **{room.get('name', channel_id)}**: {reason}")


def get_helper_tokens() -> list[str]:
    raw = os.getenv("EXTRA_BOT_TOKENS", "")
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    for index in range(1, 12):
        token = os.getenv(f"VOICE_BOT_{index}", "").strip()
        if token:
            tokens.append(token)

    unique: list[str] = []
    seen = {TOKEN}
    for token in tokens:
        if token not in seen:
            unique.append(token)
            seen.add(token)
    return unique[:5]


async def start_music_bots() -> None:
    helper_intents = discord.Intents.default()
    helper_intents.guilds = True
    helper_intents.voice_states = True

    for token in get_helper_tokens():
        client = discord.Client(intents=helper_intents)
        music_bot = MusicBot(token=token, client=client)
        music_bots.append(music_bot)

        @client.event
        async def on_ready(client: discord.Client = client) -> None:
            print(f"Music helper logged in as {client.user}")

        asyncio.create_task(client.start(token))


async def wait_for_music_bots() -> None:
    if not music_bots:
        return
    for _ in range(50):
        if all(bot_item.client.is_ready() for bot_item in music_bots):
            return
        await asyncio.sleep(0.2)


def get_user_voice_channel(interaction: discord.Interaction) -> discord.VoiceChannel | None:
    voice_state = getattr(interaction.user, "voice", None)
    if voice_state and isinstance(voice_state.channel, discord.VoiceChannel):
        return voice_state.channel
    return None


def find_session_by_voice(channel_id: int) -> MusicSession | None:
    return music_sessions.get(channel_id)


async def get_or_create_music_session(interaction: discord.Interaction) -> MusicSession | None:
    if not interaction.guild:
        return None

    voice_channel = get_user_voice_channel(interaction)
    if not voice_channel:
        await interaction.followup.send("ادخل روم صوتي أولاً.", ephemeral=True)
        return None

    existing = find_session_by_voice(voice_channel.id)
    if existing:
        return existing

    await wait_for_music_bots()
    free_bot = next((bot_item for bot_item in music_bots if bot_item.voice_channel_id is None), None)

    if free_bot is None and not music_bots:
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.channel != voice_channel:
            await interaction.followup.send("البوت الأساسي مشغول في روم ثاني. أضف VOICE_BOT tokens لتشغيل أكثر من روم.", ephemeral=True)
            return None
        if not voice_client:
            voice_client = await voice_channel.connect()
        session = MusicSession(
            bot=MusicBot(token=TOKEN, client=bot, voice_channel_id=voice_channel.id),
            guild_id=interaction.guild.id,
            voice_channel_id=voice_channel.id,
            text_channel_id=interaction.channel_id,
        )
        music_sessions[voice_channel.id] = session
        return session

    if free_bot is None:
        await interaction.followup.send("كل بوتات الموسيقى مشغولة الآن. الحد الحالي 5 رومات.", ephemeral=True)
        return None

    helper_guild = free_bot.client.get_guild(interaction.guild.id)
    if not helper_guild:
        await interaction.followup.send("بوت الموسيقى المساعد غير موجود في هذا السيرفر.", ephemeral=True)
        return None
    helper_channel = helper_guild.get_channel(voice_channel.id)
    if not isinstance(helper_channel, discord.VoiceChannel):
        await interaction.followup.send("ما قدرت أوصل لروم الصوت من البوت المساعد.", ephemeral=True)
        return None

    await helper_channel.connect()
    free_bot.voice_channel_id = voice_channel.id
    session = MusicSession(
        bot=free_bot,
        guild_id=interaction.guild.id,
        voice_channel_id=voice_channel.id,
        text_channel_id=interaction.channel_id,
    )
    music_sessions[voice_channel.id] = session
    return session


def get_session_voice_client(session: MusicSession) -> discord.VoiceClient | None:
    guild = session.bot.client.get_guild(session.guild_id)
    return guild.voice_client if guild else None


async def close_music_session(session: MusicSession) -> None:
    voice_client = get_session_voice_client(session)
    if voice_client:
        voice_client.stop()
        await voice_client.disconnect(force=True)
    session.bot.voice_channel_id = None
    music_sessions.pop(session.voice_channel_id, None)


def ytdlp_extract(query: str) -> dict[str, str]:
    import yt_dlp

    is_url = query.startswith(("http://", "https://"))
    default_search = "ytsearch" if MUSIC_SEARCH_PROVIDER == "youtube" else "scsearch"
    options = {
        "format": "bestaudio/best",
        "quiet": True,
        "default_search": default_search,
        "noplaylist": True,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        search_query = query if is_url else f"{default_search}:{query}"
        info = ydl.extract_info(search_query, download=False)
        if "entries" in info:
            info = info["entries"][0]
        return {
            "title": info.get("title") or "Unknown title",
            "webpage_url": info.get("webpage_url") or query,
            "stream_url": info["url"],
        }


def ytdlp_download(track: dict[str, str]) -> str:
    import yt_dlp

    download_dir = Path(tempfile.gettempdir()) / "botdis_music"
    download_dir.mkdir(parents=True, exist_ok=True)
    safe_id = uuid.uuid4().hex
    outtmpl = str(download_dir / f"{safe_id}.%(ext)s")
    options = {
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "quiet": True,
        "noplaylist": True,
        "outtmpl": outtmpl,
        "windowsfilenames": True,
        "retries": 3,
        "fragment_retries": 3,
        "skip_unavailable_fragments": True,
        "ignoreerrors": False,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(track["webpage_url"], download=True)
        return ydl.prepare_filename(info)


def get_ffmpeg_executable() -> str:
    # أولاً: تحقق من المتغير البيئي
    configured = os.getenv("FFMPEG_EXECUTABLE", "").strip()
    if configured and shutil.which(configured):
        return configured

    # ثانياً: FFmpeg المثبت على النظام (الأفضل والأكثر استقراراً)
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    
    # ثالثاً: المسارات المعروفة على Heroku/Koyeb
    known_paths = [
        "/usr/bin/ffmpeg",
        "/app/.apt/usr/bin/ffmpeg",
        "/workspace/.apt/usr/bin/ffmpeg",
    ]
    for path in known_paths:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    # أخيراً: imageio_ffmpeg كـ fallback (أقل استقراراً)
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"  # نأمل إنه موجود في PATH


async def play_next(session: MusicSession, retry_count: int = 0) -> None:
    voice_client = get_session_voice_client(session)
    if not voice_client or not voice_client.is_connected():
        return
    if not session.queue:
        session.now_playing = None
        return

    track = session.queue.popleft()
    session.now_playing = track
    source_path = track["stream_url"]
    temp_file: str | None = None

    if MUSIC_DOWNLOAD_BEFORE_PLAY:
        try:
            temp_file = await asyncio.to_thread(ytdlp_download, track)
            source_path = temp_file
        except Exception as exc:
            print(f"Music download failed in voice {session.voice_channel_id}: {type(exc).__name__}: {exc}")
            await play_next(session)
            return

    before_options = "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    ffmpeg_executable = get_ffmpeg_executable()
    
    try:
        if MUSIC_AUDIO_MODE == "opus":
            # Opus mode - أفضل للاستقرار وأقل استهلاك للموارد
            source = await discord.FFmpegOpusAudio.from_probe(
                source_path,
                executable=ffmpeg_executable,
                method="fallback",
                before_options="-nostdin" if temp_file else before_options,
                options="-vn -b:a 96k -ar 48000 -ac 2",
            )
        else:
            # PCM mode - جودة أعلى لكن أقل استقراراً
            source = discord.FFmpegPCMAudio(
                source_path,
                executable=ffmpeg_executable,
                before_options="-nostdin" if temp_file else before_options,
                options="-vn -ar 48000 -ac 2 -f s16le -loglevel warning",
            )
    except Exception as exc:
        print(f"FFmpeg source creation failed: {type(exc).__name__}: {exc}")
        if temp_file:
            try:
                Path(temp_file).unlink(missing_ok=True)
            except OSError:
                pass
        await play_next(session)
        return

    def after(error: Exception | None) -> None:
        if temp_file:
            try:
                Path(temp_file).unlink(missing_ok=True)
            except OSError:
                pass
        
        if error:
            error_str = str(error)
            print(f"Music playback error in voice {session.voice_channel_id}: {error}")
            
            # إذا كان خطأ FFmpeg crash (-11, -9, etc.) نحاول مرة ثانية
            if "code -11" in error_str or "code -9" in error_str or "code 1" in error_str:
                if retry_count < 2:
                    print(f"Retrying playback (attempt {retry_count + 2}/3)...")
                    # نرجع الأغنية للقائمة ونحاول مرة ثانية
                    session.queue.appendleft(track)
                    bot.loop.call_soon_threadsafe(
                        asyncio.create_task, 
                        play_next(session, retry_count + 1)
                    )
                    return
        
        bot.loop.call_soon_threadsafe(asyncio.create_task, play_next(session))

    try:
        voice_client.play(source, after=after)
    except Exception as exc:
        print(f"voice_client.play() failed: {type(exc).__name__}: {exc}")
        if temp_file:
            try:
                Path(temp_file).unlink(missing_ok=True)
            except OSError:
                pass
        bot.loop.call_soon_threadsafe(asyncio.create_task, play_next(session))
        return
        
    if session.text_channel_id:
        channel = bot.get_channel(session.text_channel_id)
        if channel and hasattr(channel, "send"):
            await channel.send(f"▶️ Now playing: **{track['title']}**")


async def schedule_empty_delete(guild: discord.Guild, channel_id: int) -> None:
    async def runner() -> None:
        await asyncio.sleep(AUTO_DELETE_DELAY)
        channel = guild.get_channel(channel_id)
        humans_in_room = [m for m in channel.members if not m.bot]
        if isinstance(channel, discord.VoiceChannel) and not humans_in_room:
            await delete_room(guild, channel_id, "الروم فارغ")

    task = delete_tasks.get(channel_id)
    if task:
        task.cancel()
    delete_tasks[channel_id] = asyncio.create_task(runner())


@bot.event
async def on_ready() -> None:
    global instance_lock_started
    for room_id in list(store["rooms"]):
        bot.add_view(RoomControls(int(room_id)))

    if GUILD_ID:
        guild_obj = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild_obj)
        await bot.tree.sync(guild=guild_obj)
    else:
        await bot.tree.sync()

    print(f"Logged in as {bot.user} ({bot.user.id})")
    if not instance_lock_started:
        instance_lock_started = True
        await write_instance_lock()
        asyncio.create_task(watch_instance_lock())


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
    if member.bot:
        return

    cfg = get_guild_config(member.guild.id)
    if cfg and after.channel and after.channel.id == int(cfg["join_channel_id"]):
        last_create = create_cooldowns.get(member.id, 0)
        if now() - last_create >= CREATE_COOLDOWN:
            create_cooldowns[member.id] = now()
            await create_room(member, cfg)
        return

    if before.channel and str(before.channel.id) in store["rooms"]:
        room = store["rooms"][str(before.channel.id)]
        channel = before.channel
        humans = [m for m in channel.members if not m.bot]
        # اذا المالك طلع ينحذف الروم فوراً بغض النظر عن وجود اشخاص آخرين
        if int(room.get("owner_id", 0)) == member.id:
            await delete_room(member.guild, channel.id, "المالك غادر الروم")
        elif not humans:
            await schedule_empty_delete(member.guild, channel.id)


@bot.tree.command(name="setup", description="إعداد نظام الرومات المؤقتة")
@app_commands.checks.has_permissions(administrator=True)
async def setup(
    interaction: discord.Interaction,
    category: discord.CategoryChannel,
    log_channel: discord.TextChannel | None = None,
) -> None:
    if not interaction.guild:
        return
    join_channel = next(
        (
            channel
            for channel in category.voice_channels
            if channel.name in {"➕ إنشاء روم", "+ إنشاء روم", "Create Room"}
        ),
        None,
    )
    if join_channel is None:
        join_channel = await interaction.guild.create_voice_channel(
            "➕ إنشاء روم",
            category=category,
            reason="Temp room setup",
        )
    duplicate_names = {"➕ إنشاء روم", "+ إنشاء روم", "Create Room"}
    for channel in list(category.voice_channels):
        if channel.id != join_channel.id and channel.name in duplicate_names:
            try:
                await channel.delete(reason="Remove duplicate temp room join channel")
            except discord.HTTPException:
                pass
    store["guilds"][str(interaction.guild.id)] = {
        "category_id": category.id,
        "join_channel_id": join_channel.id,
        "log_channel_id": log_channel.id if log_channel else 0,
        "room_name_template": DEFAULT_ROOM_NAME,
        "default_limit": DEFAULT_LIMIT,
        "default_bitrate": DEFAULT_BITRATE,
    }
    save_store()
    await interaction.response.send_message(
        f"تم الإعداد. ادخل {join_channel.mention} لإنشاء روم مؤقت.",
        ephemeral=True,
    )


@bot.tree.command(name="room_info", description="عرض معلومات رومك المؤقت")
async def room_info(interaction: discord.Interaction) -> None:
    voice = getattr(interaction.user, "voice", None)
    room_id = voice.channel.id if voice and voice.channel else interaction.channel_id
    room = store["rooms"].get(str(room_id))
    if not room:
        await interaction.response.send_message("أنت لست داخل روم مؤقت.", ephemeral=True)
        return
    owner = interaction.guild.get_member(int(room["owner_id"])) if interaction.guild else None
    await interaction.response.send_message(embed=room_embed(room, owner), ephemeral=True)


@bot.tree.command(name="room_panel", description="إرسال لوحة تحكم جديدة لرومك")
async def room_panel(interaction: discord.Interaction) -> None:
    room = await get_owned_room(interaction)
    if not room:
        return
    channel = interaction.guild.get_channel(int(room["channel_id"]))
    if not isinstance(channel, discord.VoiceChannel):
        await interaction.response.send_message("الروم غير موجود.", ephemeral=True)
        return
    await send_voice_panel(channel, room)
    await interaction.response.send_message("تم إرسال اللوحة.", ephemeral=True)


@bot.tree.command(name="room_claim", description="أخذ ملكية روم مؤقت إذا كان مالكه خارج الروم")
async def room_claim(interaction: discord.Interaction) -> None:
    voice = getattr(interaction.user, "voice", None)
    if not voice or not voice.channel:
        await interaction.response.send_message("ادخل الروم أولاً.", ephemeral=True)
        return
    room = store["rooms"].get(str(voice.channel.id))
    if not room:
        await interaction.response.send_message("هذا ليس روم مؤقت.", ephemeral=True)
        return
    owner = interaction.guild.get_member(int(room["owner_id"]))
    if owner and owner in voice.channel.members:
        await interaction.response.send_message("مالك الروم موجود.", ephemeral=True)
        return
    room["owner_id"] = interaction.user.id
    save_store()
    await interaction.response.send_message("تم نقل الملكية لك.", ephemeral=True)


@bot.tree.command(name="room_kick", description="طرد عضو من رومك")
async def room_kick(interaction: discord.Interaction, member: discord.Member) -> None:
    room = await get_owned_room(interaction)
    if not room:
        return
    if member.voice and member.voice.channel and member.voice.channel.id == int(room["channel_id"]):
        await member.move_to(None, reason="Temp room kick")
    await interaction.response.send_message("تم طرد العضو.", ephemeral=True)


@bot.tree.command(name="room_ban", description="حظر عضو من رومك")
async def room_ban(interaction: discord.Interaction, member: discord.Member) -> None:
    room = await get_owned_room(interaction)
    if not room:
        return
    channel = interaction.guild.get_channel(int(room["channel_id"]))
    if isinstance(channel, discord.VoiceChannel):
        await channel.set_permissions(member, view_channel=False, connect=False)
        if member.voice and member.voice.channel == channel:
            await member.move_to(None, reason="Temp room ban")
    if member.id not in room["banned"]:
        room["banned"].append(member.id)
    save_store()
    await interaction.response.send_message("تم حظر العضو.", ephemeral=True)


@bot.tree.command(name="room_unban", description="فك حظر عضو من رومك")
async def room_unban(interaction: discord.Interaction, member: discord.Member) -> None:
    room = await get_owned_room(interaction)
    if not room:
        return
    channel = interaction.guild.get_channel(int(room["channel_id"]))
    if isinstance(channel, discord.VoiceChannel):
        await channel.set_permissions(member, overwrite=None)
    room["banned"] = [user_id for user_id in room["banned"] if user_id != member.id]
    save_store()
    await interaction.response.send_message("تم فك الحظر.", ephemeral=True)


@bot.tree.command(name="room_trust", description="السماح لعضو بالدخول لرومك")
async def room_trust(interaction: discord.Interaction, member: discord.Member) -> None:
    room = await get_owned_room(interaction)
    if not room:
        return
    channel = interaction.guild.get_channel(int(room["channel_id"]))
    if isinstance(channel, discord.VoiceChannel):
        await channel.set_permissions(member, view_channel=True, connect=True, speak=True)
    if member.id not in room["trusted"]:
        room["trusted"].append(member.id)
    save_store()
    await interaction.response.send_message("تمت إضافة الثقة.", ephemeral=True)


@bot.tree.command(name="room_transfer", description="نقل ملكية رومك")
async def room_transfer(interaction: discord.Interaction, member: discord.Member) -> None:
    room = await get_owned_room(interaction)
    if not room:
        return
    room["owner_id"] = member.id
    save_store()
    await interaction.response.send_message(f"تم نقل الملكية إلى {member.mention}.", ephemeral=True)


@bot.tree.command(name="play", description="تشغيل أغنية من رابط أو بحث")
async def play(interaction: discord.Interaction, query: str) -> None:
    await interaction.response.defer()
    try:
        session = await get_or_create_music_session(interaction)
    except RuntimeError as exc:
        await interaction.followup.send(f"Voice error: {exc}", ephemeral=True)
        return
    except Exception as exc:
        await interaction.followup.send(f"Could not join voice: {type(exc).__name__}", ephemeral=True)
        return

    if not session or not interaction.guild:
        return

    try:
        track = await asyncio.to_thread(ytdlp_extract, query)
    except Exception as exc:
        await interaction.followup.send(f"ما قدرت أجيب المقطع. السبب: {type(exc).__name__}", ephemeral=True)
        return

    session.queue.append(track)
    voice_client = get_session_voice_client(session)

    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        await interaction.followup.send(f"تمت الإضافة للقائمة: **{track['title']}**")
        return

    await interaction.followup.send(f"جاري التشغيل: **{track['title']}**")
    try:
        await play_next(session)
    except Exception as exc:
        await interaction.followup.send(f"ما قدرت أشغل الصوت. السبب: {type(exc).__name__}", ephemeral=True)


@bot.tree.command(name="skip", description="تخطي الأغنية الحالية")
async def skip(interaction: discord.Interaction) -> None:
    voice_channel = get_user_voice_channel(interaction)
    session = find_session_by_voice(voice_channel.id) if voice_channel else None
    if not session:
        await interaction.response.send_message("ما فيه شيء شغال.", ephemeral=True)
        return
    voice_client = get_session_voice_client(session)
    if not voice_client:
        await interaction.response.send_message("ما فيه شيء شغال.", ephemeral=True)
        return
    voice_client.stop()
    await interaction.response.send_message("تم التخطي.")


@bot.tree.command(name="stop", description="إيقاف الموسيقى ومسح القائمة")
async def stop(interaction: discord.Interaction) -> None:
    voice_channel = get_user_voice_channel(interaction)
    session = find_session_by_voice(voice_channel.id) if voice_channel else None
    if not session:
        await interaction.response.send_message("ما فيه شيء شغال في رومك.", ephemeral=True)
        return
    session.queue.clear()
    session.now_playing = None
    await close_music_session(session)
    await interaction.response.send_message("تم إيقاف الموسيقى ومسح القائمة.")


@bot.tree.command(name="queue", description="عرض قائمة التشغيل")
async def queue_cmd(interaction: discord.Interaction) -> None:
    voice_channel = get_user_voice_channel(interaction)
    session = find_session_by_voice(voice_channel.id) if voice_channel else None
    if not session:
        await interaction.response.send_message("القائمة فارغة.", ephemeral=True)
        return
    current = session.now_playing
    queue = list(session.queue)[:10]
    lines = []
    if current:
        lines.append(f"Now: {current['title']}")
    if queue:
        lines.extend(f"{idx}. {track['title']}" for idx, track in enumerate(queue, start=1))
    if not lines:
        lines.append("القائمة فارغة.")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "bot": bool(bot.user)})


async def start_health_server() -> None:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Health server running on port {PORT}")


async def main() -> None:
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")
    print(f"BotDis Python Light version {APP_VERSION}")
    print(f"Music search provider: {MUSIC_SEARCH_PROVIDER}")
    print(f"Music audio mode: {MUSIC_AUDIO_MODE}")
    print(f"FFmpeg executable: {get_ffmpeg_executable()}")
    await start_health_server()
    await start_music_bots()
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
