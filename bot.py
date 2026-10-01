import logging
import logging.handlers
import os
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import tasks

from config import (
    DISCORD_TOKEN,
    INTEGRATION_CHANNEL_ID,
    TRACKER_CHANNEL_ID,
    ALERT_CHANNEL_ID,
    STATE_FILE_PATH,
)
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

# If a slot hasn't updated within this long, something upstream is likely
# broken - alert instead of waiting for someone to notice by eye.
#
# D4 slots changed meaning: they used to mean "a new event was reported via
# Wowhead's webhook" (so thresholds were ~3x each event's real-world
# cadence). Wowhead discontinued that webhook, so D4 now comes from polling
# diablo4.life's API directly (see poll_d4_api below) every
# D4_POLL_INTERVAL_MINUTES. A D4 slot's last_updated now means "last time we
# successfully reached that API," regardless of whether an event happened to
# be active - so the threshold here is really "how long can the API be
# unreachable before that's worth a page," not "how long since this event
# last fired." 30 minutes = 15 missed polls in a row, generous enough to
# ride out a brief redeploy or network blip without false alarms.
STALE_THRESHOLDS = {
    "d4": {
        "helltide": timedelta(minutes=30),
        "legion_event": timedelta(minutes=30),
        "world_boss": timedelta(minutes=30),
    },
    "d2r": {
        "terror_zone": timedelta(hours=3),
        "dclone": timedelta(days=7),
    },
}
STALENESS_CHECK_INTERVAL_MINUTES = 30

# How often to poll diablo4.life's public tracker API for D4 events.
# Legion Events are the shortest-lived (~25 min cycle), so this stays well
# under that to avoid missing one entirely between polls.
D4_POLL_INTERVAL_MINUTES = 2
D4_API_URL = "https://diablo4.life/api/trackers/list"

# Once an episode has been alerted, re-send a reminder every this-often while
# it's STILL stale, instead of going silent forever after the first alert.
# Without this, a source that never recovers (e.g. a webhook you removed and
# didn't replace) only ever pages once, then never again - which is exactly
# what happened to the D4 alerts.
STALE_REMINDER_INTERVAL = timedelta(days=1)


def embed_to_stored_dict(embed: discord.Embed) -> dict:
    """
    Converts an embed to a dict for storage, stripping the 'url' field.
    Wowhead reuses the same url across multiple D4 embeds (e.g. World Boss
    and Legion Event both point to the same event-timers page) - Discord's
    client silently merges embeds in one message that share an identical
    url into a single visual "gallery," dropping the others' visible
    content. Stripping url avoids that merge; titles just stop being
    clickable links, which is a fine tradeoff.
    """
    data = embed.to_dict()
    data.pop("url", None)
    return data


def identify_slot(embed: discord.Embed, author_name: str):
    """
    Maps an incoming channel message to (group, slot).

    Only D2R (followed-channel) events arrive this way anymore. D2R embeds
    have NO title - the identifying text is the crossposted message's
    author name instead, so we check that.

    D4 events used to arrive here too, via a Wowhead webhook that posted
    titled embeds into #event-integration - Wowhead discontinued that
    integration, so D4 is now sourced by polling an API directly instead
    (see poll_d4_api). Nothing posts D4 embeds into this channel anymore.
    """
    title = (embed.title or "").lower()
    author = (author_name or "").lower()

    if "#terror-zone" in author or "#terror-zone" in title:
        return "d2r", "terror_zone"
    if "#dclone-status" in author or "#dclone-status" in title:
        return "d2r", "dclone"
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
    # D4 is no longer sourced from this channel (see identify_slot) - only
    # D2R slots can still be backfilled from message history.
    all_slots = {("d2r", s) for s in D2R_SLOT_ORDER}

    async for message in integration_channel.history(limit=HISTORY_SCAN_LIMIT):
        if not message.embeds:
            continue
        embed = message.embeds[0]
        author_name = message.author.name if message.author else ""
        group, slot = identify_slot(embed, author_name)
        if not group or (group, slot) in found:
            continue  # history is newest-first, so first match per slot is the latest

        state[f"{group}_embeds"][slot] = embed_to_stored_dict(embed)
        state[f"{group}_last_updated"][slot] = datetime.now(timezone.utc).isoformat()
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
        placeholder = discord.Embed(
            title=SLOT_LABELS[slot],
            description="_Waiting for the next update..._",
            color=discord.Color.dark_grey(),
        )

        # Everything - reconstruction AND the truthiness check right after it -
        # is now inside this try/except. discord.Embed defines __len__, which
        # Python uses for truthiness when __bool__ isn't defined, so a plain
        # "if rebuilt:" check can itself raise for certain embed shapes - and
        # that was happening OUTSIDE the old try/except, aborting this whole
        # function silently with nothing logged. This guarantees one embed
        # (real or placeholder) always gets appended per slot, no matter what.
        try:
            if data:
                rebuilt = discord.Embed.from_dict(data)
                embeds.append(rebuilt if len(rebuilt) > 0 else placeholder)
            else:
                embeds.append(placeholder)
        except Exception:
            logger.exception(
                "Failed to build embed for %s / %s | raw data: %r",
                group, slot, data,
            )
            embeds.append(placeholder)


    header = GROUP_HEADERS[group]

    # Diagnostic: log exactly what we're about to send, so if Discord ever shows
    # something different than this, we know for certain it's not a bug in how
    # this list gets built.
    logger.info(
        "About to send %s message with %d embeds: %s",
        group, len(embeds), [e.title for e in embeds],
    )

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


def _discord_relative_timestamp(epoch_ms) -> str:
    """Converts a millisecond epoch timestamp to Discord's auto-formatting
    relative-time tag, e.g. <t:1234567890:R> renders as 'in 12 minutes' or
    '2 hours ago', and updates live in the Discord client with no bot
    involvement."""
    try:
        return f"<t:{int(epoch_ms) // 1000}:R>"
    except (TypeError, ValueError):
        return "unknown"


def build_d4_embed(label: str, payload: dict | None) -> discord.Embed:
    """
    Builds a display embed for one D4 slot from the diablo4.life tracker
    API response. The API returns {} for a slot with nothing currently
    active/scheduled - that's valid, successful data, not a failure (see
    poll_d4_api for why that still counts as "fresh" for staleness).
    """
    if not payload:
        return discord.Embed(
            title=label,
            description="_No active or upcoming event reported right now._",
            color=discord.Color.dark_grey(),
        )

    lines = []
    name = payload.get("name")
    time_ms = payload.get("time")
    if name:
        lines.append(f"**{name}**")
    if time_ms:
        lines.append(f"Spawns {_discord_relative_timestamp(time_ms)}")
    if not lines:
        # Unrecognized shape from the API - surface it in Discord so it's
        # debuggable without needing to go dig through flyctl logs.
        lines.append(f"_Unrecognized API data: {payload}_")

    return discord.Embed(title=label, description="\n".join(lines), color=discord.Color.red())


async def poll_d4_api() -> bool:
    """
    Fetches current D4 event data from diablo4.life's public tracker API
    and updates state for all three D4 slots. Returns True on success.

    This replaces the old Wowhead-webhook-via-Discord ingestion for D4
    events, which stopped working once Wowhead discontinued that
    integration. This API needs no key/login and is polled directly, so
    D4 tracking no longer depends on any third party continuing to post
    into this Discord server on our behalf.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(D4_API_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except Exception as exc:
        logger.error("D4 API poll failed: %s", exc)
        return False

    now_iso = datetime.now(timezone.utc).isoformat()
    mapping = {
        "helltide": data.get("helltide"),
        "world_boss": data.get("worldBoss") or data.get("nextWorldBoss"),
        "legion_event": data.get("zoneEvent"),
    }

    for slot, payload in mapping.items():
        state["d4_embeds"][slot] = embed_to_stored_dict(build_d4_embed(SLOT_LABELS[slot], payload))
        state["d4_last_updated"][slot] = now_iso

    save_state(STATE_FILE_PATH, state)
    logger.info("D4 API poll succeeded.")
    return True


@tasks.loop(minutes=D4_POLL_INTERVAL_MINUTES)
async def poll_d4_api_loop():
    if await poll_d4_api():
        tracker_channel = bot.get_channel(TRACKER_CHANNEL_ID)
        if tracker_channel is not None:
            await rebuild_and_send(tracker_channel, "d4")


@poll_d4_api_loop.error
async def poll_d4_api_loop_error(error: Exception):
    logger.exception("poll_d4_api_loop task crashed: %s", error)
    if not poll_d4_api_loop.is_running():
        poll_d4_api_loop.restart()


@tasks.loop(minutes=STALENESS_CHECK_INTERVAL_MINUTES)
async def check_staleness():
    """
    Compares each tracker's last-updated time against its expected cadence.
    If something's gone quiet for way longer than normal, that's a strong
    signal the upstream source (Wowhead's webhook, the D2R followed channel)
    has broken - post an alert, then a reminder every STALE_REMINDER_INTERVAL
    for as long as it's still stale (instead of alerting once and going
    permanently silent), and clear it automatically once fresh data
    comes back in.
    """
    logger.info("Running staleness check...")

    alert_channel = bot.get_channel(ALERT_CHANNEL_ID)
    if alert_channel is None:
        logger.error("Alert channel %s not found - check ALERT_CHANNEL_ID and permissions.", ALERT_CHANNEL_ID)
        return

    now = datetime.now(timezone.utc)
    # alerted: key -> ISO timestamp of the last alert sent for that episode.
    # (Migrates transparently from the old list-of-keys format, where every
    # existing entry is treated as "alerted just now" so reminders start
    # counting fresh instead of firing immediately on the next check.)
    raw_alerted = state.get("stale_alerted", {})
    if isinstance(raw_alerted, list):
        alerted = {key: now.isoformat() for key in raw_alerted}
    else:
        alerted = dict(raw_alerted)
    changed = False

    for group, thresholds in STALE_THRESHOLDS.items():
        for slot, threshold in thresholds.items():
            key = f"{group}/{slot}"
            last_str = state.get(f"{group}_last_updated", {}).get(slot)
            if not last_str:
                logger.info("Staleness check: %s has no data yet - skipping.", key)
                continue  # never received data for this slot yet - nothing to compare

            last_dt = datetime.fromisoformat(last_str)
            age = now - last_dt
            logger.info("Staleness check: %s last updated %s ago (threshold %s)", key, age, threshold)

            if age > threshold:
                last_alert_str = alerted.get(key)
                due = (
                    last_alert_str is None
                    or (now - datetime.fromisoformat(last_alert_str)) >= STALE_REMINDER_INTERVAL
                )
                if due:
                    verb = "still hasn't" if last_alert_str else "hasn't"
                    await alert_channel.send(
                        f"⚠️ **{SLOT_LABELS[slot]}** ({group.upper()}) {verb} updated in "
                        f"{age.days}d {age.seconds // 3600}h - the source webhook/feed may be down. "
                        f"Check `#event-integration`."
                    )
                    alerted[key] = now.isoformat()
                    changed = True
                    logger.warning("Staleness alert sent for %s (age: %s)", key, age)
            elif key in alerted:
                del alerted[key]
                changed = True
                logger.info("Staleness cleared for %s - fresh data received.", key)

    if changed:
        state["stale_alerted"] = alerted
        save_state(STATE_FILE_PATH, state)


@check_staleness.error
async def check_staleness_error(error: Exception):
    # tasks.loop silently stops forever on an unhandled exception with no
    # log anywhere by default - this is what would have been hiding a bug
    # like that. Log it in full, then restart the loop so a single bad tick
    # doesn't permanently kill monitoring.
    logger.exception("check_staleness task crashed: %s", error)
    if not check_staleness.is_running():
        check_staleness.restart()


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

    # Immediate first D4 fetch so the tracker isn't blank/stale-looking
    # until the first poll_d4_api_loop tick (up to D4_POLL_INTERVAL_MINUTES
    # away otherwise).
    await poll_d4_api()
    await rebuild_and_send(tracker_channel, "d4")

    if not check_staleness.is_running():
        check_staleness.start()
        logger.info("Staleness watchdog started (checks every %d min).", STALENESS_CHECK_INTERVAL_MINUTES)
    else:
        logger.info("Staleness watchdog already running.")

    if not poll_d4_api_loop.is_running():
        poll_d4_api_loop.start()
        logger.info("D4 API poller started (every %d min).", D4_POLL_INTERVAL_MINUTES)
    else:
        logger.info("D4 API poller already running.")


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

    state[f"{group}_embeds"][slot] = embed_to_stored_dict(embed)
    state[f"{group}_last_updated"][slot] = datetime.now(timezone.utc).isoformat()
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
