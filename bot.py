"""
Discord bot: slash commands, reaction-based role opt-in, weekly reminders.
Configure reminders with /setup-reminder (server owner only). Run: python bot.py
Requires DISCORD_TOKEN in .env and the bot invited with applications.commands scope.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("Set DISCORD_TOKEN in .env (see .env.example)")
# Optional: comma-separated server IDs so slash commands sync to those guilds immediately.
# Without it, global sync can take up to ~1 hour to appear everywhere.
DISCORD_GUILD_ID_RAW = os.getenv("DISCORD_GUILD_ID", "").strip()


def _parse_guild_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for chunk in raw.split(","):
        s = chunk.strip()
        if not s:
            continue
        ids.append(int(s))
    return ids

DATA_PATH = Path(__file__).resolve().parent / "reminders.json"
_reminder_lock = asyncio.Lock()

_WEEKDAY_ALIASES: dict[str, int] = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}


def _default_data() -> dict[str, Any]:
    return {"reminders": [], "welcome": {}, "opt_in_roles": []}


async def _load_reminders() -> dict[str, Any]:
    def _read() -> dict[str, Any]:
        if not DATA_PATH.is_file():
            return _default_data()
        try:
            raw = DATA_PATH.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return _default_data()
        if not isinstance(data, dict) or "reminders" not in data:
            return _default_data()
        if not isinstance(data["reminders"], list):
            data["reminders"] = []
        welcome = data.get("welcome")
        if not isinstance(welcome, dict):
            data["welcome"] = {}
        opt_in = data.get("opt_in_roles")
        if not isinstance(opt_in, list):
            data["opt_in_roles"] = []
        return data

    return await asyncio.to_thread(_read)


async def _save_reminders(data: dict[str, Any]) -> None:
    def _write() -> None:
        DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = DATA_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(DATA_PATH)

    await asyncio.to_thread(_write)


def _guild_reminders(data: dict[str, Any], guild_id: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in data["reminders"]:
        if not isinstance(r, dict):
            continue
        if int(r.get("guild_id", 0)) == guild_id:
            out.append(r)
    return out


def _parse_schedule(text: str) -> tuple[int, int, int]:
    """Return (weekday 0=Mon..6=Sun, hour, minute) in UTC."""
    parts = text.strip().split()
    if len(parts) < 2:
        raise ValueError('Use e.g. `Friday 00:00` or `4 09:30` (weekday hour:minute UTC).')
    day_token = parts[0].lower().rstrip(",")
    time_token = parts[1]
    if day_token.isdigit():
        weekday = int(day_token)
        if weekday < 0 or weekday > 6:
            raise ValueError("Numeric weekday must be 0–6 (Monday=0, Sunday=6).")
    else:
        weekday = _WEEKDAY_ALIASES.get(day_token)
        if weekday is None:
            raise ValueError(f"Unknown weekday: {parts[0]!r}.")
    if ":" not in time_token:
        raise ValueError("Time must look like HH:MM (24h UTC), e.g. 00:00 or 9:30.")
    h_str, m_str = time_token.split(":", 1)
    hour = int(h_str)
    minute = int(m_str)
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("Invalid hour or minute.")
    return weekday, hour, minute


def _emoji_matches_rule(stored: str | None, emoji: discord.PartialEmoji) -> bool:
    if stored is None or stored == "":
        return True
    return str(emoji) == stored


def _guild_opt_in_roles(data: dict[str, Any], guild_id: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rows = data.get("opt_in_roles")
    if not isinstance(rows, list):
        return out
    for r in rows:
        if not isinstance(r, dict):
            continue
        if int(r.get("guild_id", 0)) == guild_id:
            out.append(r)
    return out


intents = discord.Intents.default()
# Needed for welcome messages via on_member_join (enable "Server Members Intent" in the Developer Portal too).
intents.members = True

class Bot(commands.Bot):
    async def setup_hook(self) -> None:
        guild_ids = _parse_guild_ids(DISCORD_GUILD_ID_RAW)
        if guild_ids:
            any_ok = False
            for gid in guild_ids:
                guild = discord.Object(id=gid)
                try:
                    self.tree.copy_global_to(guild=guild)
                    synced = await self.tree.sync(guild=guild)
                    print(
                        f"Slash commands synced to guild {gid} ({len(synced)}) "
                        "— should show right away there."
                    )
                    any_ok = True
                except discord.Forbidden as e:
                    if getattr(e, "code", None) == 50001:
                        print(
                            f"Slash sync skipped for guild {gid}: Missing Access (50001). "
                            "The bot is not in that server, or the guild ID is wrong — invite the bot "
                            "or remove that ID from DISCORD_GUILD_ID."
                        )
                    else:
                        print(f"Slash sync forbidden for guild {gid}: {e}")
                except discord.HTTPException as e:
                    print(f"Slash sync failed for guild {gid} ({e.status}): {e.text}")

            if not any_ok:
                print(
                    "No guild sync succeeded; falling back to global slash sync "
                    "(commands can take up to ~1 hour to appear in servers the bot is in)."
                )
                try:
                    synced = await self.tree.sync()
                    print(f"Global slash sync OK ({len(synced)} commands).")
                except discord.HTTPException as e:
                    print(f"Global slash sync also failed ({e.status}): {e.text}")
        else:
            try:
                synced = await self.tree.sync()
                print(
                    f"Slash commands synced globally ({len(synced)}). "
                    "New/updated commands may take up to ~1 hour to appear. "
                    "Set DISCORD_GUILD_ID (comma-separated) in .env for instant sync."
                )
            except discord.HTTPException as e:
                print(f"Slash command sync failed ({e.status}): {e.text}")


# when_mentioned avoids the "message content intent" warning; we only use slash commands, not !prefix.
bot = Bot(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

# Temporary in-memory state for guided setup flows (keyed by user+guild).
# Railway restarts will clear these; that's fine because they're only used during setup.
# Capture state for guided setup flows (keyed by user+guild).
# Stored as: {"message_id": int, "mode": str, "role_id": Optional[int], "emojis": set[str]}
_optin_capture_state: dict[tuple[int, int], dict[str, Any]] = {}


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: BaseException) -> None:
    cmd = getattr(interaction.command, "name", "?")
    print(f"App command error (/{cmd}): {error!r}")
    # Avoid "Application did not respond" by replying gracefully when possible.
    try:
        msg = (
            "Sorry — that command errored.\n"
            "If you're setting up welcomes, double-check the bot has permission to send messages "
            "in the target channel, then try again."
        )
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


async def _send_ephemeral(interaction: discord.Interaction, message: str) -> None:
    """Send an ephemeral message whether or not we've already responded."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


async def _ensure_guild_owner(interaction: discord.Interaction) -> bool:
    guild = interaction.guild
    if guild is None:
        await _send_ephemeral(interaction, "Use this command in a server.")
        return False
    if interaction.user.id != guild.owner_id:
        await _send_ephemeral(interaction, "Only the **server owner** can use this command.")
        return False
    return True


async def _ensure_guild_manager(interaction: discord.Interaction) -> bool:
    """Allow server owner or admins to configure per-guild settings."""
    guild = interaction.guild
    if guild is None:
        await _send_ephemeral(interaction, "Use this command in a server.")
        return False
    if interaction.user.id == guild.owner_id:
        return True
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if member is not None:
        perms = member.guild_permissions
        if perms.administrator or perms.manage_guild:
            return True
    await _send_ephemeral(
        interaction,
        "You need **Manage Server** (or be the server owner) to configure welcome messages.",
    )
    return False


def _render_welcome_template(template: str, member: discord.Member) -> str:
    guild = member.guild
    return (
        template.replace("{user_mention}", member.mention)
        .replace("{user_name}", member.display_name)
        .replace("{server_name}", guild.name)
    )


def _parse_channel_reference(guild: discord.Guild, raw: str) -> int | None:
    """
    Parse a typed channel reference.
    Accepts: blank, "system", a raw ID, or a channel mention like <#123>.
    Returns channel_id or None (meaning "use system/default channel").
    """
    s = raw.strip()
    if not s:
        return None
    low = s.lower()
    if low in {"system", "default"}:
        return None
    if s.startswith("<#") and s.endswith(">"):
        inner = s[2:-1].strip()
        if inner.isdigit():
            return int(inner)
    if s.isdigit():
        return int(s)
    # As a convenience, allow "#general" style names (first match wins).
    if s.startswith("#"):
        name = s[1:].strip().lower()
        for ch in guild.text_channels:
            if ch.name.lower() == name:
                return ch.id
    return None


def _get_welcome_config(data: dict[str, Any], guild_id: int) -> dict[str, Any] | None:
    welcome = data.get("welcome")
    if not isinstance(welcome, dict):
        return None
    cfg = welcome.get(str(guild_id))
    if not isinstance(cfg, dict):
        return None
    return cfg


_MSG_LINK_RE = re.compile(r"discord\.com/channels/(\d+)/(\d+)/(\d+)")


def _parse_message_link(raw: str) -> tuple[int, int, int]:
    """
    Parse a Discord message link: https://discord.com/channels/<guild>/<channel>/<message>
    Returns (guild_id, channel_id, message_id).
    """
    s = raw.strip().strip("<>").strip()
    m = _MSG_LINK_RE.search(s)
    if not m:
        raise ValueError("Paste a full Discord message link like https://discord.com/channels/<guild>/<channel>/<message>")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _parse_emoji_list(raw: str | None) -> list[str | None]:
    """
    Parse comma-separated emojis. Blank/None means 'any emoji' (single entry None).
    Examples: "🐕, 🐈" or "<:name:123>, ✅"
    """
    if raw is None:
        return [None]
    s = raw.strip()
    if not s:
        return [None]
    parts = [p.strip() for p in s.split(",")]
    out: list[str] = [p for p in parts if p]
    return out or [None]


class SetupWelcomeModal(discord.ui.Modal, title="Set up welcome message"):
    welcome_channel = discord.ui.TextInput(
        label="Welcome channel",
        placeholder="#welcome or <#123> or 123 (blank=default)",
        max_length=100,
        required=False,
    )
    welcome_message = discord.ui.TextInput(
        label="Welcome message",
        placeholder="Welcome {user_mention} to {server_name}! ({user_name} supported)",
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        print(f"Welcome modal error: {error!r}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Sorry — saving welcome settings failed. Please try again.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Sorry — saving welcome settings failed. Please try again.",
                    ephemeral=True,
                )
        except discord.HTTPException:
            pass

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        # Respond immediately to avoid Discord's 3s timeout.
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await _ensure_guild_manager(interaction):
            return

        raw_channel = str(self.welcome_channel.value or "")
        channel_id = _parse_channel_reference(interaction.guild, raw_channel)
        if raw_channel.strip() and channel_id is None:
            await interaction.followup.send(
                "I couldn't understand that channel.\n"
                "Type `#channel`, paste a channel mention like `<#123...>`, or paste the channel ID.\n"
                "Or leave it blank to use the server's system/default channel.",
                ephemeral=True,
            )
            return

        template = str(self.welcome_message.value or "").strip()
        if not template:
            await _send_ephemeral(interaction, "Welcome message cannot be empty.")
            return

        cfg: dict[str, Any] = {"enabled": True, "channel_id": channel_id, "template": template}
        async with _reminder_lock:
            data = await _load_reminders()
            welcome = data.get("welcome")
            if not isinstance(welcome, dict):
                welcome = {}
                data["welcome"] = welcome
            welcome[str(interaction.guild.id)] = cfg
            await _save_reminders(data)

        # Channel to use for join events: configured channel, else system channel, else do nothing.
        ch_desc = f"<#{channel_id}>" if isinstance(channel_id, int) else "(system/default channel)"
        preview_user = interaction.user if isinstance(interaction.user, discord.Member) else None
        preview = _render_welcome_template(template, preview_user) if preview_user else template
        await interaction.followup.send(
            "Welcome settings saved.\n"
            f"- Channel: {ch_desc}\n"
            f"- Preview: {preview}",
            ephemeral=True,
        )


@bot.tree.command(name="setup-welcome", description="Server owner: configure welcome message for new members")
async def setup_welcome(interaction: discord.Interaction) -> None:
    # Keep this command name for discoverability, but allow admins too.
    if not await _ensure_guild_manager(interaction):
        return
    try:
        await interaction.response.send_modal(SetupWelcomeModal())
    except discord.HTTPException:
        await _send_ephemeral(interaction, "Sorry — I couldn't open the welcome setup popup. Please try again.")


welcome_group = app_commands.Group(name="welcome", description="Configure welcome messages for new members")


@welcome_group.command(name="set", description="Owner/admin: set the welcome channel and message template")
@app_commands.describe(
    channel="Channel to post welcome messages in (omit to use system/default channel)",
    message="Template supports {user_mention}, {user_name}, {server_name}",
)
async def welcome_set(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
    message: str | None = None,
) -> None:
    if not await _ensure_guild_manager(interaction):
        return
    assert interaction.guild is not None

    async with _reminder_lock:
        data = await _load_reminders()
        welcome = data.get("welcome")
        if not isinstance(welcome, dict):
            welcome = {}
            data["welcome"] = welcome
        cfg = welcome.get(str(interaction.guild.id))
        if not isinstance(cfg, dict):
            cfg = {"enabled": True, "channel_id": None, "template": "Welcome {user_mention} to {server_name}!"}

        if channel is not None:
            cfg["channel_id"] = channel.id
        if message is not None:
            m = message.strip()
            if not m:
                await interaction.response.send_message("Message cannot be empty.", ephemeral=True)
                return
            cfg["template"] = m
        cfg["enabled"] = True
        welcome[str(interaction.guild.id)] = cfg
        await _save_reminders(data)

    ch_id = cfg.get("channel_id")
    ch_desc = f"<#{int(ch_id)}>" if isinstance(ch_id, int) else "(system/default channel)"
    await interaction.response.send_message(
        "Welcome messages enabled and saved.\n"
        f"- Channel: {ch_desc}\n"
        f"- Template: {cfg.get('template')}\n"
        "Tip: run `/welcome test` to preview.",
        ephemeral=True,
    )


@welcome_group.command(name="disable", description="Owner/admin: disable welcome messages for this server")
async def welcome_disable(interaction: discord.Interaction) -> None:
    if not await _ensure_guild_manager(interaction):
        return
    assert interaction.guild is not None
    async with _reminder_lock:
        data = await _load_reminders()
        welcome = data.get("welcome")
        if not isinstance(welcome, dict):
            welcome = {}
            data["welcome"] = welcome
        cfg = welcome.get(str(interaction.guild.id))
        if not isinstance(cfg, dict):
            cfg = {"enabled": False, "channel_id": None, "template": ""}
        cfg["enabled"] = False
        welcome[str(interaction.guild.id)] = cfg
        await _save_reminders(data)
    await interaction.response.send_message("Welcome messages disabled for this server.", ephemeral=True)


@welcome_group.command(name="show", description="Owner/admin: show current welcome settings")
async def welcome_show(interaction: discord.Interaction) -> None:
    if not await _ensure_guild_manager(interaction):
        return
    assert interaction.guild is not None
    data = await _load_reminders()
    cfg = _get_welcome_config(data, interaction.guild.id)
    if not cfg:
        await interaction.response.send_message("No welcome settings saved yet. Use `/welcome set`.", ephemeral=True)
        return
    enabled = bool(cfg.get("enabled", True))
    ch_id = cfg.get("channel_id")
    ch_desc = f"<#{int(ch_id)}>" if isinstance(ch_id, int) else "(system/default channel)"
    template = str(cfg.get("template") or "").strip() or "(empty)"
    await interaction.response.send_message(
        f"Welcome settings:\n- Enabled: **{enabled}**\n- Channel: {ch_desc}\n- Template: {template}",
        ephemeral=True,
    )


@welcome_group.command(name="test", description="Owner/admin: preview the welcome message (ephemeral)")
async def welcome_test(interaction: discord.Interaction) -> None:
    if not await _ensure_guild_manager(interaction):
        return
    assert interaction.guild is not None
    data = await _load_reminders()
    cfg = _get_welcome_config(data, interaction.guild.id)
    if not cfg or not cfg.get("enabled", True):
        await interaction.response.send_message("Welcome messages are not enabled. Use `/welcome set`.", ephemeral=True)
        return
    template = str(cfg.get("template") or "").strip()
    if not template:
        await interaction.response.send_message("Welcome template is empty. Use `/welcome set`.", ephemeral=True)
        return
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    preview = _render_welcome_template(template, member) if member else template
    await interaction.response.send_message(f"Preview:\n{preview}", ephemeral=True)


bot.tree.add_command(welcome_group)


@welcome_group.command(name="setup", description="Owner/admin: open a popup to configure welcome settings")
async def welcome_setup(interaction: discord.Interaction) -> None:
    if not await _ensure_guild_manager(interaction):
        return
    await interaction.response.send_modal(SetupWelcomeModal())


@bot.tree.command(name="disable-welcome", description="Server owner: disable welcome message for new members")
async def disable_welcome(interaction: discord.Interaction) -> None:
    if not await _ensure_guild_owner(interaction):
        return
    assert interaction.guild is not None
    async with _reminder_lock:
        data = await _load_reminders()
        welcome = data.get("welcome")
        if not isinstance(welcome, dict):
            welcome = {}
            data["welcome"] = welcome
        cfg = welcome.get(str(interaction.guild.id))
        if isinstance(cfg, dict):
            cfg["enabled"] = False
            welcome[str(interaction.guild.id)] = cfg
        await _save_reminders(data)
    await interaction.response.send_message("Welcome messages disabled for this server.", ephemeral=True)


@bot.tree.command(name="test-welcome", description="Server owner: send a test welcome message (to you)")
async def test_welcome(interaction: discord.Interaction) -> None:
    if not await _ensure_guild_owner(interaction):
        return
    assert interaction.guild is not None
    data = await _load_reminders()
    cfg = _get_welcome_config(data, interaction.guild.id)
    if not cfg or not cfg.get("enabled", True):
        await interaction.response.send_message("Welcome messages are not enabled. Use `/setup-welcome`.", ephemeral=True)
        return
    template = str(cfg.get("template") or "").strip()
    if not template:
        await interaction.response.send_message("Welcome template is missing. Re-run `/setup-welcome`.", ephemeral=True)
        return
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    msg = _render_welcome_template(template, member) if member else template
    await interaction.response.send_message(f"Test welcome message:\n{msg}", ephemeral=True)


class SetupReminderModal(discord.ui.Modal, title="Set up weekly reminder"):
    message_id = discord.ui.TextInput(
        label="Message ID (react on this message)",
        placeholder="e.g. 1491731894893543465",
        max_length=22,
    )
    reaction_emoji = discord.ui.TextInput(
        label="Reaction emoji (leave blank = any emoji)",
        placeholder="e.g. 🐕 or <:name:123456789>",
        max_length=100,
        required=False,
    )
    schedule_utc = discord.ui.TextInput(
        label="Weekly time (UTC)",
        placeholder="e.g. Friday 00:00",
        max_length=32,
    )
    role_id = discord.ui.TextInput(
        label="Role ID (assign + ping)",
        placeholder="Right-click role → Copy ID",
        max_length=22,
    )
    reminder_channel_id = discord.ui.TextInput(
        label="Reminder channel ID (blank = this channel)",
        placeholder="Where the weekly ping is sent",
        max_length=22,
        required=False,
    )

    def __init__(self) -> None:
        super().__init__()

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("Only the **server owner** can submit this form.", ephemeral=True)
            return
        try:
            mid = int(str(self.message_id.value).strip())
            rid = int(str(self.role_id.value).strip())
        except ValueError:
            await interaction.response.send_message("Message ID and Role ID must be numbers.", ephemeral=True)
            return
        ch_raw = str(self.reminder_channel_id.value or "").strip()
        if ch_raw:
            try:
                cid = int(ch_raw)
            except ValueError:
                await interaction.response.send_message("Reminder channel ID must be a number.", ephemeral=True)
                return
        else:
            cid = interaction.channel_id
            if cid is None:
                await interaction.response.send_message("Could not determine this channel.", ephemeral=True)
                return

        emoji_val = str(self.reaction_emoji.value or "").strip() or None

        try:
            weekday, hour, minute = _parse_schedule(str(self.schedule_utc.value))
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        role = interaction.guild.get_role(rid)
        if role is None:
            await interaction.response.send_message("Role not found in this server. Check the Role ID.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(cid)
        if not isinstance(channel, discord.abc.Messageable):
            await interaction.response.send_message("Reminder channel not found or not text-based.", ephemeral=True)
            return

        bot_member = interaction.guild.me
        if bot_member is None:
            await interaction.response.send_message("Bot member not available.", ephemeral=True)
            return
        if role >= bot_member.top_role and interaction.guild.owner_id != bot_member.id:
            await interaction.response.send_message(
                "Move the bot's role **above** the target role in Server Settings → Roles.",
                ephemeral=True,
            )
            return

        rid_str = str(uuid.uuid4())
        row = {
            "id": rid_str,
            "guild_id": interaction.guild.id,
            "message_id": mid,
            "emoji": emoji_val,
            "weekday": weekday,
            "hour": hour,
            "minute": minute,
            "role_id": rid,
            "channel_id": cid,
            "last_fired_slot": None,
        }

        async with _reminder_lock:
            data = await _load_reminders()
            data["reminders"].append(row)
            await _save_reminders(data)

        wd_name = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][weekday]
        await interaction.response.send_message(
            f"Saved reminder **`{rid_str[:8]}…`**\n"
            f"- React message `{mid}` with {emoji_val or 'any emoji'} → assign role {role.mention}\n"
            f"- Every **{wd_name}** at **{hour:02d}:{minute:02d} UTC** → ping in {channel.mention}",
            ephemeral=True,
        )


@bot.tree.command(name="setup-reminder", description="Server owner: add a weekly reaction + role reminder")
async def setup_reminder(interaction: discord.Interaction) -> None:
    if not await _ensure_guild_owner(interaction):
        return
    await interaction.response.send_modal(SetupReminderModal())


reminder_group = app_commands.Group(name="reminder", description="Configure weekly reminders (easier setup)")


@reminder_group.command(name="add", description="Owner/admin: add a weekly reminder (supports multiple emojis)")
@app_commands.describe(
    message_link="Copy Message Link from the target message",
    emojis="Comma-separated emojis (blank = any emoji)",
    schedule_utc="Example: Friday 00:00 (UTC)",
    role="Role to assign on reaction and ping weekly",
    channel="Channel to send weekly pings in (omit = use current channel)",
)
async def reminder_add(
    interaction: discord.Interaction,
    message_link: str,
    schedule_utc: str,
    role: discord.Role,
    emojis: str | None = None,
    channel: discord.TextChannel | None = None,
) -> None:
    if not await _ensure_guild_manager(interaction):
        return
    assert interaction.guild is not None

    try:
        link_guild_id, link_channel_id, mid = _parse_message_link(message_link)
    except ValueError as e:
        await _send_ephemeral(interaction, str(e))
        return
    if link_guild_id != interaction.guild.id:
        await _send_ephemeral(interaction, "That message link is from a different server.")
        return

    try:
        weekday, hour, minute = _parse_schedule(schedule_utc)
    except ValueError as e:
        await _send_ephemeral(interaction, str(e))
        return

    target_channel: discord.abc.Messageable | None = channel
    if target_channel is None:
        ch_id = interaction.channel_id
        if ch_id is not None:
            ch = interaction.guild.get_channel(ch_id)
            if isinstance(ch, discord.abc.Messageable):
                target_channel = ch
    if target_channel is None:
        await _send_ephemeral(interaction, "Could not determine which channel to post weekly pings in.")
        return

    bot_member = interaction.guild.me
    if bot_member is None:
        await _send_ephemeral(interaction, "Bot member not available.")
        return
    if role >= bot_member.top_role and interaction.guild.owner_id != bot_member.id:
        await _send_ephemeral(interaction, "Move the bot's role **above** the target role in Server Settings → Roles.")
        return

    emoji_list = _parse_emoji_list(emojis)
    created_ids: list[str] = []
    async with _reminder_lock:
        data = await _load_reminders()
        for e in emoji_list:
            rid_str = str(uuid.uuid4())
            row = {
                "id": rid_str,
                "guild_id": interaction.guild.id,
                "message_id": mid,
                "emoji": e,
                "weekday": weekday,
                "hour": hour,
                "minute": minute,
                "role_id": role.id,
                "channel_id": getattr(target_channel, "id", interaction.channel_id),
                "last_fired_slot": None,
            }
            data["reminders"].append(row)
            created_ids.append(rid_str)
        await _save_reminders(data)

    wd_name = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][weekday]
    emoji_desc = "any emoji" if emoji_list == [None] else ", ".join([str(x) for x in emoji_list if x])
    await _send_ephemeral(
        interaction,
        "Saved reminder(s).\n"
        f"- Message: `{mid}` (from link)\n"
        f"- Emojis: {emoji_desc}\n"
        f"- Weekly: **{wd_name}** at **{hour:02d}:{minute:02d} UTC**\n"
        f"- Role: {role.mention}\n"
        f"- Ping channel: {getattr(target_channel, 'mention', '(selected)')}\n"
        f"- IDs: {', '.join([cid[:8] + '…' for cid in created_ids])}",
    )


bot.tree.add_command(reminder_group)


optin_group = app_commands.Group(name="optin", description="Opt-in roles via reacting to a message")


class _OptInSetupModal(discord.ui.Modal, title="Opt-in roles (reaction → role)"):
    message_link = discord.ui.TextInput(
        label="Message link",
        placeholder="Right-click message → Copy Message Link",
        max_length=100,
    )

    def __init__(self, *, requester_id: int) -> None:
        super().__init__()
        self.requester_id = requester_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Acknowledge quickly, then show a role picker.
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.user.id != self.requester_id:
            await interaction.followup.send("This setup popup isn't for you. Run `/optin setup` yourself.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.followup.send("Use this command in a server.", ephemeral=True)
            return
        try:
            link_guild_id, _link_channel_id, mid = _parse_message_link(str(self.message_link.value))
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        if link_guild_id != interaction.guild.id:
            await interaction.followup.send("That message link is from a different server.", ephemeral=True)
            return

        view = _OptInRolePickerView(
            requester_id=interaction.user.id,
            guild_id=interaction.guild.id,
            message_id=mid,
        )
        await interaction.followup.send(
            "Almost done — create emoji→role mappings.\n"
            f"- Message: `{mid}`\n"
            "Flow:\n"
            "1) Pick a role\n"
            "2) Click **Capture emoji**\n"
            "3) React once on the target message with the emoji for that role\n"
            "4) Click **Add mapping**\n"
            "Repeat for each role, then click **Finish**.",
            ephemeral=True,
            view=view,
        )


class _OptInRolePickerView(discord.ui.View):
    def __init__(self, *, requester_id: int, guild_id: int, message_id: int) -> None:
        super().__init__(timeout=10 * 60)
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.message_id = message_id
        self.selected_role_id: int | None = None
        self.captured_emojis: set[str] = set()
        # Pending mappings: emoji string -> role_id
        self.emoji_to_role: dict[str, int] = {}

        role_select = discord.ui.RoleSelect(placeholder="Pick a role to map", min_values=1, max_values=1)
        role_select.callback = self._on_roles_selected  # type: ignore[assignment]
        self.add_item(role_select)

    async def _on_roles_selected(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who started setup can change this.", ephemeral=True)
            return
        item = self.children[0]
        values = getattr(item, "values", [])
        self.selected_role_id = int(values[0].id) if values else None
        await interaction.response.send_message(
            "Role selected. Click **Capture emoji** to record the emoji for this role.",
            ephemeral=True,
        )

    @discord.ui.button(label="Capture emoji", style=discord.ButtonStyle.blurple)
    async def capture_emoji(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who started setup can use this.", ephemeral=True)
            return
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await interaction.response.send_message("This setup is for a different server.", ephemeral=True)
            return
        if self.selected_role_id is None:
            await interaction.response.send_message("Pick a role first.", ephemeral=True)
            return
        _optin_capture_state[(self.requester_id, self.guild_id)] = {
            "message_id": self.message_id,
            "mode": "optin_role_map",
            "role_id": self.selected_role_id,
            "emojis": self.captured_emojis,
        }
        await interaction.response.send_message(
            "Emoji capture is ON.\n"
            "Now react **once** on the target message with the emoji for the selected role.\n"
            "Then come back and click **Add mapping**.",
            ephemeral=True,
        )

    @discord.ui.button(label="Add mapping", style=discord.ButtonStyle.green)
    async def add_mapping(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who started setup can do this.", ephemeral=True)
            return
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await interaction.response.send_message("This setup is for a different server.", ephemeral=True)
            return
        if self.selected_role_id is None:
            await interaction.response.send_message("Pick a role first.", ephemeral=True)
            return
        if not self.captured_emojis:
            await interaction.response.send_message("No emoji captured yet. Click **Capture emoji** and react once.", ephemeral=True)
            return

        # Use the most recently captured emoji for the current role.
        captured = list(self.captured_emojis)
        emoji = captured[-1]
        self.emoji_to_role[emoji] = self.selected_role_id
        await interaction.response.send_message(
            f"Mapped {emoji} → <@&{self.selected_role_id}>. Pick another role and repeat, or click **Finish**.",
            ephemeral=True,
        )

    @discord.ui.button(label="Finish", style=discord.ButtonStyle.green)
    async def finish(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who started setup can finish.", ephemeral=True)
            return
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await interaction.response.send_message("This setup is for a different server.", ephemeral=True)
            return
        if not self.emoji_to_role:
            await interaction.response.send_message("Add at least one emoji→role mapping first.", ephemeral=True)
            return

        bot_member = interaction.guild.me
        if bot_member is None:
            await interaction.response.send_message("Bot member not available.", ephemeral=True)
            return
        for rid in set(self.emoji_to_role.values()):
            role = interaction.guild.get_role(int(rid))
            if role is None:
                continue
            if role >= bot_member.top_role and interaction.guild.owner_id != bot_member.id:
                await interaction.response.send_message(
                    f"Move the bot's role **above** {role.mention} in Server Settings → Roles.",
                    ephemeral=True,
                )
                return

        created_ids: list[str] = []
        async with _reminder_lock:
            data = await _load_reminders()
            rows = data.get("opt_in_roles")
            if not isinstance(rows, list):
                rows = []
                data["opt_in_roles"] = rows
            for emoji, rid in self.emoji_to_role.items():
                oid = str(uuid.uuid4())
                rows.append(
                    {
                        "id": oid,
                        "guild_id": self.guild_id,
                        "message_id": self.message_id,
                        "emoji": emoji,
                        "role_id": int(rid),
                    }
                )
                created_ids.append(oid)
            await _save_reminders(data)

        pairs_desc = "\n".join([f"- {e} → <@&{rid}>" for e, rid in list(self.emoji_to_role.items())[:25]])
        await interaction.response.send_message(
            "Saved opt-in role rule(s).\n"
            f"- Message: `{self.message_id}`\n"
            f"- Created: {len(created_ids)} rule(s)\n"
            f"{pairs_desc}",
            ephemeral=True,
        )
        _optin_capture_state.pop((self.requester_id, self.guild_id), None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.gray)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who started setup can cancel this.", ephemeral=True)
            return
        await interaction.response.send_message("Cancelled opt-in setup.", ephemeral=True)
        _optin_capture_state.pop((self.requester_id, self.guild_id), None)
        self.stop()


@optin_group.command(name="setup", description="Owner/admin: guided setup (popup + role picker)")
async def optin_setup(interaction: discord.Interaction) -> None:
    if not await _ensure_guild_manager(interaction):
        return
    await interaction.response.send_modal(_OptInSetupModal(requester_id=interaction.user.id))


@optin_group.command(name="add", description="Owner/admin: create opt-in role(s) for emoji reactions")
@app_commands.describe(
    message_link="Copy Message Link from the target message",
    role="Role to add/remove when users react/unreact",
    emojis="Comma-separated emojis (blank = any emoji)",
)
async def optin_add(
    interaction: discord.Interaction,
    message_link: str,
    role: discord.Role,
    emojis: str | None = None,
) -> None:
    if not await _ensure_guild_manager(interaction):
        return
    assert interaction.guild is not None

    try:
        link_guild_id, _link_channel_id, mid = _parse_message_link(message_link)
    except ValueError as e:
        await _send_ephemeral(interaction, str(e))
        return
    if link_guild_id != interaction.guild.id:
        await _send_ephemeral(interaction, "That message link is from a different server.")
        return

    bot_member = interaction.guild.me
    if bot_member is None:
        await _send_ephemeral(interaction, "Bot member not available.")
        return
    if role >= bot_member.top_role and interaction.guild.owner_id != bot_member.id:
        await _send_ephemeral(interaction, "Move the bot's role **above** the target role in Server Settings → Roles.")
        return

    emoji_list = _parse_emoji_list(emojis)
    created_ids: list[str] = []
    async with _reminder_lock:
        data = await _load_reminders()
        rows = data.get("opt_in_roles")
        if not isinstance(rows, list):
            rows = []
            data["opt_in_roles"] = rows
        for e in emoji_list:
            oid = str(uuid.uuid4())
            rows.append(
                {
                    "id": oid,
                    "guild_id": interaction.guild.id,
                    "message_id": mid,
                    "emoji": e,
                    "role_id": role.id,
                }
            )
            created_ids.append(oid)
        await _save_reminders(data)

    emoji_desc = "any emoji" if emoji_list == [None] else ", ".join([str(x) for x in emoji_list if x])
    await _send_ephemeral(
        interaction,
        "Saved opt-in role rule(s).\n"
        f"- Message: `{mid}` (from link)\n"
        f"- Emojis: {emoji_desc}\n"
        f"- Role: {role.mention}\n"
        f"- IDs: {', '.join([cid[:8] + '…' for cid in created_ids])}",
    )


@optin_group.command(name="list", description="Owner/admin: list opt-in role rules in this server")
async def optin_list(interaction: discord.Interaction) -> None:
    if not await _ensure_guild_manager(interaction):
        return
    assert interaction.guild is not None
    data = await _load_reminders()
    rows = _guild_opt_in_roles(data, interaction.guild.id)
    if not rows:
        await _send_ephemeral(interaction, "No opt-in roles configured. Use `/optin add`.")
        return
    lines: list[str] = []
    for r in rows[:50]:
        emoji_s = r.get("emoji") or "(any)"
        lines.append(
            f"**`{r.get('id','')}`** — msg `{r.get('message_id')}` / {emoji_s} / role `{r.get('role_id')}`"
        )
    await _send_ephemeral(interaction, "\n".join(lines)[:4000])


@optin_group.command(name="delete", description="Owner/admin: delete one opt-in role rule by ID")
@app_commands.describe(rule_id="Full ID from /optin list")
async def optin_delete(interaction: discord.Interaction, rule_id: str) -> None:
    if not await _ensure_guild_manager(interaction):
        return
    assert interaction.guild is not None
    rid = rule_id.strip()
    async with _reminder_lock:
        data = await _load_reminders()
        rows = data.get("opt_in_roles")
        if not isinstance(rows, list):
            rows = []
            data["opt_in_roles"] = rows
        before = len(rows)
        rows = [
            r
            for r in rows
            if not (isinstance(r, dict) and r.get("id") == rid and int(r.get("guild_id", 0)) == interaction.guild.id)
        ]
        data["opt_in_roles"] = rows
        if len(rows) == before:
            await _send_ephemeral(interaction, "No opt-in rule with that ID in this server.")
            return
        await _save_reminders(data)
    await _send_ephemeral(interaction, "Deleted that opt-in rule.")


bot.tree.add_command(optin_group)


@bot.tree.command(name="list-reminders", description="Server owner: list configured reminders in this server")
async def list_reminders(interaction: discord.Interaction) -> None:
    if not await _ensure_guild_owner(interaction):
        return
    assert interaction.guild is not None
    data = await _load_reminders()
    rows = _guild_reminders(data, interaction.guild.id)
    if not rows:
        await interaction.response.send_message("No reminders configured. Use `/setup-reminder`.", ephemeral=True)
        return
    lines: list[str] = []
    wd_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for r in rows:
        e = r.get("emoji")
        emoji_s = e if e else "(any)"
        wd = int(r.get("weekday", 0))
        h, m = int(r.get("hour", 0)), int(r.get("minute", 0))
        lines.append(
            f"**`{r['id']}`** — msg `{r['message_id']}` / {emoji_s} / "
            f"{wd_names[wd]} {h:02d}:{m:02d} UTC / role `{r['role_id']}` / ch `{r['channel_id']}`"
        )
    await interaction.response.send_message("\n".join(lines)[:4000], ephemeral=True)


@bot.tree.command(name="delete-reminder", description="Server owner: remove one reminder by ID")
@app_commands.describe(reminder_id="Full ID from /list-reminders")
async def delete_reminder(interaction: discord.Interaction, reminder_id: str) -> None:
    if not await _ensure_guild_owner(interaction):
        return
    assert interaction.guild is not None
    rid = reminder_id.strip()
    async with _reminder_lock:
        data = await _load_reminders()
        before = len(data["reminders"])
        data["reminders"] = [
            r
            for r in data["reminders"]
            if not (isinstance(r, dict) and r.get("id") == rid and int(r.get("guild_id", 0)) == interaction.guild.id)
        ]
        if len(data["reminders"]) == before:
            await interaction.response.send_message("No reminder with that ID in this server.", ephemeral=True)
            return
        await _save_reminders(data)
    await interaction.response.send_message("Deleted that reminder.", ephemeral=True)


_REMINDER_HELP = """
**Reminders — super simple**

The bot does **two** things together:
1. Someone clicks an emoji on **one special post** → they get a **role** (like a name tag).
2. **Once a week** the bot writes in a channel and **pings that role** so those people see it.

**You** are the boss of the server, so **only you** can set this up.

**Commands**
• `/reminder add` — easiest setup (message link + pick role/channel + optional multiple emojis).
• `/setup-reminder` — legacy setup form (IDs). Still works.
• `/list-reminders` — shows everything you set up (copy the long ID if you need it).
• `/delete-reminder` — type the ID to remove one setup.

**The form boxes (what to type)**
• **Message ID** — the post people should click. Turn on **Developer Mode** (Settings → Advanced), then **right‑click the message → Copy Message ID**. It’s a long number.
• **Reaction emoji** — the exact emoji they must click (like a dog). Leave it **empty** if *any* emoji on that post is OK.
• **Weekly time (UTC)** — when the bot should shout each week. Example: `Friday 00:00` means Friday at midnight **UTC** (not your local clock unless you live in UTC).
• **Role ID** — the name tag people get. **Right‑click the role → Copy Role ID** (Developer Mode on).
• **Reminder channel ID** — which **room** the weekly shout goes in. **Right‑click the channel → Copy Channel ID**. Leave **empty** to use the room where you ran the command.

**Important**
• Put the bot’s role **above** the role it hands out (Server Settings → Roles → drag).
• The role must be **mentionable** (or you gave the bot permission to ping roles), or people won’t get notified.

**Still stuck?** Run `/ping` to check the bot is awake, then try `/setup-reminder` again.
""".strip()


@bot.tree.command(
    name="help-reminder",
    description="Server owner: simple guide for reminder commands (ephemeral)",
)
async def help_reminder(interaction: discord.Interaction) -> None:
    if not await _ensure_guild_owner(interaction):
        return
    await interaction.response.send_message(_REMINDER_HELP, ephemeral=True)


@bot.event
async def on_ready() -> None:
    assert bot.user is not None
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    if not reminder_tick.is_running():
        reminder_tick.start()


@bot.event
async def on_member_join(member: discord.Member) -> None:
    data = await _load_reminders()
    cfg = _get_welcome_config(data, member.guild.id)
    if not cfg or not cfg.get("enabled", True):
        return
    template = str(cfg.get("template") or "").strip()
    if not template:
        return

    channel: discord.abc.Messageable | None = None
    channel_id = cfg.get("channel_id")
    if isinstance(channel_id, int):
        ch = member.guild.get_channel(channel_id)
        if isinstance(ch, discord.abc.Messageable):
            channel = ch
    if channel is None:
        sys_ch = member.guild.system_channel
        if isinstance(sys_ch, discord.abc.Messageable):
            channel = sys_ch
    if channel is None:
        return

    try:
        await channel.send(_render_welcome_template(template, member))
    except discord.HTTPException:
        return


@tasks.loop(minutes=1)
async def reminder_tick() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    slot = now.strftime("%Y-%m-%d %H:%M")
    to_send: list[tuple[dict[str, Any], discord.abc.Messageable, int]] = []

    async with _reminder_lock:
        data = await _load_reminders()
        changed = False
        for r in data["reminders"]:
            if not isinstance(r, dict):
                continue
            try:
                if int(r["weekday"]) != now.weekday():
                    continue
                if int(r["hour"]) != now.hour or int(r["minute"]) != now.minute:
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            if r.get("last_fired_slot") == slot:
                continue

            guild_id = int(r["guild_id"])
            channel_id = int(r["channel_id"])
            role_id = int(r["role_id"])

            guild = bot.get_guild(guild_id)
            if guild is None:
                continue
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                continue
            role = guild.get_role(role_id)
            if role is None:
                continue

            r["last_fired_slot"] = slot
            changed = True
            to_send.append((r, channel, role_id))

        if changed:
            await _save_reminders(data)

    for _r, channel, role_id in to_send:
        try:
            await channel.send(
                f"<@&{role_id}> Reminder (configured by server owner).",
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        except discord.HTTPException:
            continue


@reminder_tick.before_loop
async def _before_reminder_tick() -> None:
    await bot.wait_until_ready()


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    if payload.guild_id is None:
        return
    if payload.user_id == bot.user.id:  # type: ignore[union-attr]
        return

    # If a server owner/admin is in the opt-in setup wizard, capture emojis they react with.
    state = _optin_capture_state.get((payload.user_id, payload.guild_id))
    if isinstance(state, dict) and int(state.get("message_id", 0)) == payload.message_id:
        emojis = state.get("emojis")
        if isinstance(emojis, set):
            emojis.add(str(payload.emoji))
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    data = await _load_reminders()
    matched: list[dict[str, Any]] = []
    for r in list(data.get("reminders") or []) + list(data.get("opt_in_roles") or []):
        if not isinstance(r, dict):
            continue
        if int(r.get("guild_id", 0)) != guild.id:
            continue
        if int(r.get("message_id", 0)) != payload.message_id:
            continue
        stored = r.get("emoji")
        stored_s = str(stored) if stored is not None else None
        if not _emoji_matches_rule(stored_s, payload.emoji):
            continue
        matched.append(r)

    if not matched:
        return

    if not await _reaction_user_is_human(guild, payload.user_id):
        return

    for r in matched:
        try:
            role_id = int(r["role_id"])
        except (KeyError, TypeError, ValueError):
            continue
        role = guild.get_role(role_id)
        if role is None:
            continue
        try:
            await _add_role(guild, payload.user_id, role, "Opt-in via reaction (reminder bot)")
        except discord.HTTPException:
            continue


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent) -> None:
    if payload.guild_id is None:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    data = await _load_reminders()
    matched: list[dict[str, Any]] = []
    for r in list(data.get("reminders") or []) + list(data.get("opt_in_roles") or []):
        if not isinstance(r, dict):
            continue
        if int(r.get("guild_id", 0)) != guild.id:
            continue
        if int(r.get("message_id", 0)) != payload.message_id:
            continue
        stored = r.get("emoji")
        stored_s = str(stored) if stored is not None else None
        if not _emoji_matches_rule(stored_s, payload.emoji):
            continue
        matched.append(r)

    if not matched:
        return

    if not await _reaction_user_is_human(guild, payload.user_id):
        return

    for r in matched:
        try:
            role_id = int(r["role_id"])
        except (KeyError, TypeError, ValueError):
            continue
        role = guild.get_role(role_id)
        if role is None:
            continue
        try:
            await _remove_role(guild, payload.user_id, role, "Opt-out via reaction (reminder bot)")
        except discord.HTTPException:
            continue


async def _reaction_user_is_human(guild: discord.Guild, user_id: int) -> bool:
    member = guild.get_member(user_id)
    if member is not None:
        return not member.bot
    try:
        user = await bot.fetch_user(user_id)
    except discord.NotFound:
        return False
    return not user.bot


async def _add_role(guild: discord.Guild, user_id: int, role: discord.Role, reason: str) -> None:
    member = guild.get_member(user_id)
    if member is not None:
        await member.add_roles(role, reason=reason)
    else:
        await guild._state.http.add_role(guild.id, user_id, role.id, reason=reason)


async def _remove_role(guild: discord.Guild, user_id: int, role: discord.Role, reason: str) -> None:
    member = guild.get_member(user_id)
    if member is not None:
        await member.remove_roles(role, reason=reason)
    else:
        await guild._state.http.remove_role(guild.id, user_id, role.id, reason=reason)


@bot.tree.command(name="ping", description="Health check — replies with Pong!")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("Pong!")


if __name__ == "__main__":
    bot.run(TOKEN)
