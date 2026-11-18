"""外部ツール（FFmpeg）の場所を探す。

順序: 環境変数（ATHFORM_FFPROBE / ATHFORM_FFMPEG）→ PATH → winget の既定のインストール先（Windows）。
winget で入れた直後は、実行中のシェルの PATH に反映されていないことがあるため、最後の候補を見る。
"""

from __future__ import annotations

import glob
import os
import shutil


def find_tool(name: str) -> str | None:
    env = os.environ.get(f"ATHFORM_{name.upper()}")
    if env and os.path.isfile(env):
        return env
    found = shutil.which(name)
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        pattern = os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "ffmpeg-*", "bin", f"{name}.exe")
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return None
