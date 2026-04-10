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
    return {"reminders": []}


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


intents = discord.Intents.default()
# Server Members Intent is not required: role changes use REST when the member
# isn't cached. Enable it in the Developer Portal only if you add features that need member lists.

class Bot(commands.Bot):
    async def setup_hook(self) -> None:
        try:
            guild_ids = _parse_guild_ids(DISCORD_GUILD_ID_RAW)
            if guild_ids:
                for gid in guild_ids:
                    guild = discord.Object(id=gid)
                    self.tree.copy_global_to(guild=guild)
                    synced = await self.tree.sync(guild=guild)
                    print(
                        f"Slash commands synced to guild {gid} ({len(synced)}) "
                        "— should show right away there."
                    )
            else:
                synced = await self.tree.sync()
                print(
                    f"Slash commands synced globally ({len(synced)}). "
                    "New/updated commands may take up to ~1 hour to appear. "
                    "Set DISCORD_GUILD_ID (comma-separated) in .env for instant sync."
                )
        except discord.HTTPException as e:
            print(f"Slash command sync failed ({e.status}): {e.text}")
            raise


bot = Bot(command_prefix="!", intents=intents, help_command=None)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: BaseException) -> None:
    cmd = getattr(interaction.command, "name", "?")
    print(f"App command error (/{cmd}): {error!r}")


async def _ensure_guild_owner(interaction: discord.Interaction) -> bool:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return False
    if interaction.user.id != guild.owner_id:
        await interaction.response.send_message("Only the **server owner** can use this command.", ephemeral=True)
        return False
    return True


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


@bot.event
async def on_ready() -> None:
    assert bot.user is not None
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    if not reminder_tick.is_running():
        reminder_tick.start()


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

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    data = await _load_reminders()
    matched: list[dict[str, Any]] = []
    for r in data["reminders"]:
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
    for r in data["reminders"]:
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
