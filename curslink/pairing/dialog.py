"""The pairing request window, run as a short-lived helper process.

Tk wants the main thread, which the host and agent use for asyncio, so like
the switch animation (see :mod:`curslink.overlay`) the window is its own
process: ``curslink _pair-dialog NAME ROLE DEVICE_ID``. It prints the code
the user typed and exits 0, exits 1 if they declined, or 3 if there is no
display to show it on.
"""

from __future__ import annotations

import sys

EXIT_DECLINED = 1
EXIT_NO_DISPLAY = 3


def run(name: str, role: str, device_id: str) -> int:
    try:
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
    except Exception:
        return EXIT_NO_DISPLAY
    result: list[str] = []

    root.title("Curslink pairing request")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    frame = ttk.Frame(root, padding=18)
    frame.grid()
    ttk.Label(frame, text=f"{name} wants to pair", font=("TkDefaultFont", 13, "bold")).grid(
        column=0, row=0, columnspan=2, sticky="w"
    )
    ttk.Label(
        frame,
        text=(
            f"{name} ({role}, id {device_id}) is asking to share your keyboard and mouse.\n"
            f"Type the 6-digit code shown on {name} to pair, or decline if you didn't ask for this."
        ),
        wraplength=360,
        justify="left",
    ).grid(column=0, row=1, columnspan=2, sticky="w", pady=(6, 10))
    code = tk.StringVar()
    entry = ttk.Entry(frame, textvariable=code, width=12, font=("TkFixedFont", 18), justify="center")
    entry.grid(column=0, row=2, columnspan=2, pady=(0, 12))

    def pair(*_):
        digits = "".join(ch for ch in code.get() if ch.isdigit())
        if len(digits) == 6:
            result.append(digits)
            root.destroy()

    def decline(*_):
        root.destroy()

    ttk.Button(frame, text="Decline", command=decline).grid(column=0, row=3, sticky="w")
    ttk.Button(frame, text="Pair", command=pair).grid(column=1, row=3, sticky="e")
    root.bind("<Return>", pair)
    root.bind("<Escape>", decline)
    root.protocol("WM_DELETE_WINDOW", decline)
    root.after(50, lambda: (root.lift(), root.focus_force(), entry.focus_set()))
    root.mainloop()
    if not result:
        return EXIT_DECLINED
    print(result[0], flush=True)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: curslink _pair-dialog NAME ROLE DEVICE_ID", file=sys.stderr)
        return 2
    return run(*argv)
