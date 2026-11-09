"""Clipboard sync: copy on one device, paste on another.

Text, images and files copied on any linked device are sent to the others,
so Ctrl+C (or Cmd+C) on one computer and Ctrl+V on the next just works.

* The overlay helper (a Qt process every device already runs, see
  :mod:`curslink.overlay`) watches this device's clipboard and reads and
  writes it (:mod:`curslink.clipboard.helper`). It prints ``clip {...}``
  when something new was copied here and takes ``clipset {...}`` to put
  what another device copied onto this one's clipboard.
* The service sends what was copied over the paired link as ``clip`` and
  ``clip_data`` frames (:mod:`curslink.clipboard.sync`). The host is the
  hub: what an agent copies goes to the host's clipboard and on to every
  other agent.

Password managers mark what they copy as private (macOS's concealed type,
Windows' "exclude from monitoring", KDE's password hint); that is never
sent. Anything bigger than ``clipboard_max_mb`` stays on its own device.
"""
