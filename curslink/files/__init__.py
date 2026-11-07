"""Dragging files to another device.

* :mod:`curslink.files.meta` describes what is being dragged (name, kind,
  size) for the placeholder that follows the pointer on the other device.
* :mod:`curslink.files.transfer` sends the files over the paired, encrypted
  link and saves them on the other side.
* :mod:`curslink.files.handoff` notices a file drag leaving the host (over an
  edge, or carried there by a look) and drives the two above.
"""
