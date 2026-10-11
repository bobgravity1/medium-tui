"""Copy plain text using the OS clipboard without a shell or temporary files."""

import shutil
import subprocess
import sys


def copy_text(text: str) -> bool:
    if sys.platform == "darwin":
        command = ["/usr/bin/pbcopy"]
    elif sys.platform == "win32":
        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$input | Set-Clipboard",
        ]
    elif shutil.which("wl-copy"):
        command = ["wl-copy"]
    elif shutil.which("xclip"):
        command = ["xclip", "-selection", "clipboard"]
    else:
        return False
    try:
        subprocess.run(
            command,
            input=text,
            encoding="utf-8",
            check=True,
            timeout=2,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True
