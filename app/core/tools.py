"""Resolution of external A/V tool binaries (ffmpeg / ffprobe).

One shared rule for every build target: an explicit AMM_FFMPEG / AMM_FFPROBE
environment variable wins (the Electron shell points these at the launchers in
resources/bundled-tools, assembled by prepare-build.sh — same mechanism as
AMM_MKVPROPEDIT / AMM_ATOMICPARSLEY); otherwise the bare command name resolves
on PATH exactly as before (Docker installs ffmpeg in the image; deb/rpm declare
it as a dependency). Core code never contains platform logic — the env vars are
set only by the packaging layer.
"""

import os


def ffmpeg_path() -> str:
    return os.environ.get("AMM_FFMPEG") or "ffmpeg"


def ffprobe_path() -> str:
    return os.environ.get("AMM_FFPROBE") or "ffprobe"
