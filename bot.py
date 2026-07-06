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
intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)

state = load_state(STATE_FILE_PATH)

D2R_SLOT_ORDER = ["dclone", "terror_zone"]
D4_SLOT_ORDER = ["world_boss", "helltide", "legion_event"]

GROUP_HEADERS = {
    "d2r": "**Diablo II Resurrected Events**",
    "d4": "**Diablo IV Events**",
}

SLOT_LABELS = {
    "terror_zone": "Terror Zone",
    "dclone": "Diablo Clone",
    "helltide": "Helltide",
    "legion_event": "Legion Event",
    "world_boss": "World Boss",
}

# How many recent messages to scan on startup to backfill anything missed
# while the bot was offline (deploys, crashes, restarts).
HISTORY_SCAN_LIMIT = 200


def identify_slot(embed: discord.Embed, author_name: str):
    """
    Maps an incoming message to (group, slot).

    D4 (Wowhead) embeds carry their own title, so we match on embed.title.
    D2R (followed-channel) embeds have NO title - the identifying text is
    the crossposted message's author name instead, so we check that too.
    """
    title = (embed.title or "").lower()
    author = (author_name or "").lower()

    if "#terror-zone" in author or "#terror-zone" in title:
        return "d2r", "terror_zone"
    if "#dclone-status" in author or "#dclone-status" in title:
        return "d2r", "dclone"
    if "helltide" in title:
        return "d4", "helltide"
    if "legion event" in title:
        return "d4", "legion_event"
    if "world boss" in title:
        return "d4", "world_boss"
    return None, None


async def catch_up_on_missed_events(integration_channel: discord.TextChannel) -> None:
    """
    Scans recent message history in the integration channel and backfills
    any slot that's missing from state. Handles the case where a message
    arrived while the bot was offline (deploy, crash, restart) and would
    otherwise be permanently missed, since Discord doesn't replay gateway
    events for time the bot was disconnected.
    """
    found = set()
    all_slots = {("d2r", s) for s in D2R_SLOT_ORDER} | {("d4", s) for s in D4_SLOT_ORDER}

    async for message in integration_channel.history(limit=HISTORY_SCAN_LIMIT):
        if not message.embeds:
            continue
        embed = message.embeds[0]
        author_name = message.author.name if message.author else ""
        group, slot = identify_slot(embed, author_name)
        if not group or (group, slot) in found:
            continue  # history is newest-first, so first match per slot is the latest

        state[f"{group}_embeds"][slot] = embed.to_dict()
        found.add((group, slot))
        logger.info("Catch-up: backfilled %s / %s from channel history", group, slot)

        if found == all_slots:
            break

    save_state(STATE_FILE_PATH, state)


async def rebuild_and_send(channel: discord.TextChannel, group: str) -> None:
    """
    Rebuilds the full embed list (plus header text) for a group from stored
    state and either edits the existing standing message or creates a new
    one if it's missing. Slots with no data yet get a placeholder embed
    instead of being omitted, so the message always shows the full expected
    layout.
    """
    slot_order = D2R_SLOT_ORDER if group == "d2r" else D4_SLOT_ORDER
    embeds_dict = state[f"{group}_embeds"]

    embeds = []
    for slot in slot_order:
        data = embeds_dict.get(slot)
        rebuilt = None
        if data:
            try:
                rebuilt = discord.Embed.from_dict(data)
            except Exception as exc:
                # Don't let one bad embed silently kill the whole update - fall
                # back to a placeholder and log exactly what went wrong so it's
                # diagnosable instead of the event just quietly disappearing.
                logger.error(
                    "Failed to rebuild embed for %s / %s: %s | raw data: %r",
                    group, slot, exc, data,
                )

        if rebuilt:
            embeds.append(rebuilt)
        else:
            embeds.append(
                discord.Embed(
                    title=SLOT_LABELS[slot],
                    description="_Waiting for the next update..._",
                    color=discord.Color.dark_grey(),
                )
            )


    header = GROUP_HEADERS[group]
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
            await message.edit(content=header, embeds=embeds)
        else:
            message = await channel.send(content=header, embeds=embeds)
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

    integration_channel = bot.get_channel(INTEGRATION_CHANNEL_ID)
    tracker_channel = bot.get_channel(TRACKER_CHANNEL_ID)

    if integration_channel is None or tracker_channel is None:
        logger.error("Could not resolve integration/tracker channel - check IDs and permissions.")
        return

    await catch_up_on_missed_events(integration_channel)
    await rebuild_and_send(tracker_channel, "d2r")
    await rebuild_and_send(tracker_channel, "d4")


@bot.event
async def on_message(message: discord.Message):
    if message.channel.id != INTEGRATION_CHANNEL_ID:
        return
    if not message.embeds:
        return

    embed = message.embeds[0]
    author_name = message.author.name if message.author else ""

    logger.info(
        "Incoming message - author: %r, embed.title: %r",
        author_name,
        embed.title,
    )

    group, slot = identify_slot(embed, author_name)
    if not group:
        logger.info("No match for this message - ignored.")
        return

    logger.info("Matched: %s / %s", group, slot)

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
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
