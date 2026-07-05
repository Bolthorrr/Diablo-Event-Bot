import logging
import logging.handlers
import os

import discord

from config import DISCORD_TOKEN, INTEGRATION_CHANNEL_ID, TRACKER_CHANNEL_ID, STATE_FILE_PATH
from storage import load_state, save_state

# ---------------------------
# LOGGING
# ---------------------------
logger = logging.getLogger("diablo_bot")
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(console_handler)

_log_dir = os.path.dirname(STATE_FILE_PATH) or "."
os.makedirs(_log_dir, exist_ok=True)
file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(_log_dir, "bot.log"), maxBytes=1_000_000, backupCount=3
)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(file_handler)

# ---------------------------
# DISCORD SETUP
# ---------------------------
# Plain Client, not commands.Bot - this bot has no slash/prefix commands by design,
# so there's no need for the command-handling layer (and no on_message/process_commands
# footgun to worry about as a result).
intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)

state = load_state(STATE_FILE_PATH)

# Fixed display order within each standing message.
D2R_SLOT_ORDER = ["terror_zone", "dclone"]
D4_SLOT_ORDER = ["helltide", "legion_event", "world_boss"]


def identify_slot(title: str):
    """
    Maps an incoming embed title to (group, slot).
    Matches on stable substrings rather than exact titles, since things like the
    ladder season name ("Reign of the Warlock") will change over time.
    Returns (None, None) if the title doesn't match anything we track.
    """
    t = title.lower()
    if "#terror-zone" in t:
        return "d2r", "terror_zone"
    if "#dclone-status" in t:
        return "d2r", "dclone"
    if "helltide" in t:
        return "d4", "helltide"
    if "legion event" in t:
        return "d4", "legion_event"
    if "world boss" in t:
        return "d4", "world_boss"
    return None, None


async def rebuild_and_send(channel: discord.TextChannel, group: str) -> None:
    """
    Rebuilds the full embed list for a group (d2r or d4) from stored state and
    either edits the existing standing message or creates a new one if it's
    missing (e.g. first run, or someone deleted it manually).
    """
    slot_order = D2R_SLOT_ORDER if group == "d2r" else D4_SLOT_ORDER
    embeds_dict = state[f"{group}_embeds"]
    embeds = [
        discord.Embed.from_dict(embeds_dict[slot])
        for slot in slot_order
        if embeds_dict.get(slot)
    ]

    if not embeds:
        return  # nothing captured for this group yet

    message_id = state.get(f"{group}_message_id")
    message = None

    if message_id:
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            logger.warning("Standing message for %s missing (deleted?) - will recreate.", group)
        except discord.Forbidden:
            logger.error("Missing permission to fetch standing message for %s.", group)
            return
        except discord.HTTPException as exc:
            logger.error("Discord API error fetching standing message for %s: %s", group, exc)
            return

    try:
        if message:
            await message.edit(embeds=embeds)
        else:
            message = await channel.send(embeds=embeds)
            state[f"{group}_message_id"] = message.id
            save_state(STATE_FILE_PATH, state)
    except discord.Forbidden:
        logger.error("Missing permission to send/edit standing message for %s.", group)
    except discord.HTTPException as exc:
        logger.error("Discord API error sending/editing standing message for %s: %s", group, exc)


@bot.event
async def on_ready():
    logger.info("Logged in as %s (id: %s)", bot.user, bot.user.id)
    logger.info(
        "Watching channel %s, publishing to channel %s.",
        INTEGRATION_CHANNEL_ID,
        TRACKER_CHANNEL_ID,
    )


@bot.event
async def on_message(message: discord.Message):
    if message.channel.id != INTEGRATION_CHANNEL_ID:
        return
    if not message.embeds:
        return

    embed = message.embeds[0]
    if not embed.title:
        return

    group, slot = identify_slot(embed.title)
    if not group:
        logger.debug("Ignoring unrecognized embed title: %s", embed.title)
        return

    logger.info("Update received: %s / %s", group, slot)

    state[f"{group}_embeds"][slot] = embed.to_dict()
    save_state(STATE_FILE_PATH, state)

    tracker_channel = bot.get_channel(TRACKER_CHANNEL_ID)
    if tracker_channel is None:
        logger.error(
            "Tracker channel %s not found - check TRACKER_CHANNEL_ID and bot permissions.",
            TRACKER_CHANNEL_ID,
        )
        return

    await rebuild_and_send(tracker_channel, group)


@bot.event
async def on_error(event_name, *args, **kwargs):
    logger.exception("Unhandled exception in event: %s", event_name)


def main():
    logger.info("Starting Diablo Event Bot...")
    # log_handler=None: we've already configured our own handlers above, so this
    # stops discord.py from also installing its default ones (avoids duplicate log lines).
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
