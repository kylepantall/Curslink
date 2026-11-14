"""The Windows input helper: control admin apps, UAC prompts and the lock screen.

Windows won't let a normal program inject input into a program running as
administrator (UIPI), and UAC prompts and the lock screen are on a separate,
secure desktop that only a SYSTEM process can reach. So, when it's switched
on in Settings (one admin prompt), Curslink installs a small service:

* ``curslink-helper.exe service`` runs as LocalSystem (:mod:`.service`). It
  starts, and keeps running, one injector in the session at the console.
* ``curslink-helper.exe inject`` (:mod:`.inject`) runs as SYSTEM in that
  session. It listens on a named pipe only that session's user can open,
  and injects what it's sent, always on whichever desktop has the input
  (the normal one, or the secure one while UAC or the lock screen is up).
* The agent (:class:`.client.HelperSink`) sends its input there when the
  helper is running, and injects it itself when it isn't.
* The host (:class:`.client.HelperWatch`, wire v2) asks it to listen too:
  this PC's own input hooks hear nothing over an app that runs as
  administrator, so the pointer couldn't leave the screen there and keys
  meant for another computer went to that app. The helper's hooks (SYSTEM)
  hear everything; it reports what they hear on a second pipe, and while
  input is on another computer keeps it from this PC.

The helper is a separate small executable copied into Program Files, so a
program running as the user can't replace what the service runs. Turning
the setting off stops the service, deletes it and removes the copy.

What it costs: anything running as this user can send input through the
pipe, as Curslink does, including to UAC prompts. That's the same trade-off
Synergy and similar tools make, and why it's off unless switched on.
"""

from __future__ import annotations

SERVICE_NAME = "CurslinkHelper"
DISPLAY_NAME = "Curslink input helper"
EXE_NAME = "curslink-helper.exe"
INSTALL_FOLDER = "Curslink Helper"   # under %ProgramFiles%
PIPE_PREFIX = r"\\.\pipe\curslink-input-"
WIRE_VERSION = 2   # 2: "watch", and the events pipe


def pipe_name(session: int) -> str:
    return f"{PIPE_PREFIX}{session}"


def events_pipe_name(session: int) -> str:
    """Where the helper reports what its hooks hear (wire v2)."""
    return f"{PIPE_PREFIX}{session}-events"
