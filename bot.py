"""
Minimal Discord bot — slash command /ping. Run: python bot.py (with venv active).
Requires DISCORD_TOKEN in .env and the bot invited with applications.commands scope.
"""

from __future__ import annotations

import datetime as dt
import os

import discord
from discord.ext import commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("Set DISCORD_TOKEN in .env (see .env.example)")

REACTION_MESSAGE_ID = 1491731894893543465
PET_ABILITIES_ROLE_ID = 1491728760989421568
REMINDER_CHANNEL_ID = 1491731205060821073
# Set to a string like "🐾" to only respond to that emoji; leave None to accept any emoji on the message.
REACTION_EMOJI: str | None = None


intents = discord.Intents.default()
# Uncomment if you add prefix commands or read message text:
# intents.message_content = True


class Bot(commands.Bot):
    async def setup_hook(self) -> None:
        await self.tree.sync()


bot = Bot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def on_ready() -> None:
    assert bot.user is not None
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    if not weekly_pet_abilities_reminder.is_running():
        weekly_pet_abilities_reminder.start()


def _emoji_matches(emoji: discord.PartialEmoji) -> bool:
    if REACTION_EMOJI is None:
        return True
    return str(emoji) == REACTION_EMOJI


async def _get_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None


@tasks.loop(time=dt.time(hour=0, minute=0, tzinfo=dt.timezone.utc))
async def weekly_pet_abilities_reminder() -> None:
    if dt.datetime.now(dt.timezone.utc).weekday() != 4:  # Friday
        return

    channel = bot.get_channel(REMINDER_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(REMINDER_CHANNEL_ID)
        except discord.NotFound:
            return

    if not isinstance(channel, discord.abc.Messageable):
        return

    await channel.send(
        f"<@&{PET_ABILITIES_ROLE_ID}> Reminder: reset your pet abilities.",
        allowed_mentions=discord.AllowedMentions(roles=True),
    )


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    if payload.guild_id is None:
        return
    if payload.message_id != REACTION_MESSAGE_ID:
        return
    if not _emoji_matches(payload.emoji):
        return
    if payload.user_id == bot.user.id:  # type: ignore[union-attr]
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    role = guild.get_role(PET_ABILITIES_ROLE_ID)
    if role is None:
        return

    member = await _get_member(guild, payload.user_id)
    if member is None or member.bot:
        return

    await member.add_roles(role, reason="Opt-in to pet abilities reminder via reaction")


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent) -> None:
    if payload.guild_id is None:
        return
    if payload.message_id != REACTION_MESSAGE_ID:
        return
    if not _emoji_matches(payload.emoji):
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    role = guild.get_role(PET_ABILITIES_ROLE_ID)
    if role is None:
        return

    member = await _get_member(guild, payload.user_id)
    if member is None or member.bot:
        return

    await member.remove_roles(role, reason="Opt-out of pet abilities reminder via reaction")


@bot.tree.command(name="ping", description="Health check — replies with Pong!")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("Pong!")


if __name__ == "__main__":
    bot.run(TOKEN)
