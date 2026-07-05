import os


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


DISCORD_TOKEN = _require_env("DISCORD_TOKEN")
INTEGRATION_CHANNEL_ID = int(_require_env("INTEGRATION_CHANNEL_ID"))
TRACKER_CHANNEL_ID = int(_require_env("TRACKER_CHANNEL_ID"))

# Lives on the Fly.io persistent volume so it survives restarts/redeploys.
STATE_FILE_PATH = os.getenv("STATE_FILE_PATH", "/data/state.json")
