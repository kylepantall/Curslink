"""Pairing devices so they share a per-pair key instead of a typed-in secret.

* :mod:`.store` keeps the link key for every paired device.
* :mod:`.handshake` is the code-confirmed key agreement, over any stream.
* :mod:`.discovery` finds Curslink devices on the LAN with mDNS.
* :mod:`.service` is the listener that receives pairing requests.
"""
