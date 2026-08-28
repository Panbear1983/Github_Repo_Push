"""Install / remove / inspect the daily drift launchd job.

Kept inside ghrp on purpose: the dashboard must drive and reflect the real
scheduling mechanism rather than have automation happen out of band.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

LABEL = "com.panbear.ghrp-drift"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
DEFAULT_HOUR = 9
DEFAULT_MINUTE = 0


@dataclass
class RoutineStatus:
    installed: bool
    loaded: bool
    hour: Optional[int] = None
    minute: Optional[int] = None
    last_exit_code: Optional[str] = None

    def describe(self) -> str:
        if not self.installed:
            return "off (no launchd job installed)"
        when = f"{self.hour:02d}:{self.minute:02d}" if self.hour is not None else "??:??"
        state = "loaded" if self.loaded else "installed but NOT loaded"
        exit_note = ""
        if self.last_exit_code not in (None, "0"):
            exit_note = f", last exit {self.last_exit_code}"
        return f"on — daily at {when}, {state}{exit_note}"


def _runner_command(repo_root: Path, python: Optional[str] = None) -> list[str]:
    """The command launchd runs.

    The interpreter is pinned to an absolute path: launchd does not inherit a
    login shell's PATH, so a bare `python3` can resolve to something without the
    dependencies (or to nothing at all).
    """
    python = python or sys.executable or "/usr/bin/python3"
    return [
        "/bin/bash",
        "-c",
        f"cd {repo_root} && PYTHONPATH=src {python} -m github_repo_push.cli drift",
    ]


# launchd starts jobs with a bare PATH (/usr/bin:/bin:/usr/sbin:/sbin) and does
# NOT source a login shell. `gh` lives in Homebrew's prefix, so without this the
# sweep fails on every repo with "No such file or directory: 'gh'" — which is
# invisible when you test the command interactively.
JOB_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def build_plist(repo_root: Path, hour: int = DEFAULT_HOUR, minute: int = DEFAULT_MINUTE,
                python: Optional[str] = None) -> dict:
    log_dir = Path.home() / "Library" / "Logs"
    return {
        "Label": LABEL,
        "ProgramArguments": _runner_command(repo_root, python),
        "EnvironmentVariables": {"PATH": JOB_PATH, "HOME": str(Path.home())},
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": False,
        "StandardOutPath": str(log_dir / f"{LABEL}.out.log"),
        "StandardErrorPath": str(log_dir / f"{LABEL}.err.log"),
    }


def install(repo_root: Path, hour: int = DEFAULT_HOUR, minute: int = DEFAULT_MINUTE,
            plist_path: Path = PLIST_PATH) -> None:
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    with open(plist_path, "wb") as f:
        plistlib.dump(build_plist(repo_root, hour, minute), f)
    subprocess.run(["launchctl", "unload", str(plist_path)],
                   capture_output=True, check=False)
    subprocess.run(["launchctl", "load", str(plist_path)],
                   capture_output=True, check=False)


def uninstall(plist_path: Path = PLIST_PATH) -> bool:
    if not plist_path.exists():
        return False
    subprocess.run(["launchctl", "unload", str(plist_path)],
                   capture_output=True, check=False)
    plist_path.unlink()
    return True


def status(plist_path: Path = PLIST_PATH) -> RoutineStatus:
    if not plist_path.exists():
        return RoutineStatus(installed=False, loaded=False)

    hour = minute = None
    try:
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        interval = data.get("StartCalendarInterval") or {}
        hour, minute = interval.get("Hour"), interval.get("Minute")
    except Exception:
        pass

    listed = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=False)
    loaded, exit_code = False, None
    for line in listed.stdout.splitlines():
        if line.rstrip().endswith(LABEL):
            loaded = True
            parts = line.split()
            if len(parts) >= 2:
                exit_code = parts[1]
            break

    return RoutineStatus(installed=True, loaded=loaded, hour=hour, minute=minute,
                         last_exit_code=exit_code)
