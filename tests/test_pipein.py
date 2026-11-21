import subprocess
import sys
import threading
import time

from curslink import pipein


def test_lines_come_whole_from_pieces_and_end_when_the_pipe_closes():
    pieces = [b"ho", b"me\nlook {\"A\"", b": 1}\n", b"tail"]
    slept = []

    def peek():
        if not pieces:
            return None
        if len(slept) < 2:  # nothing waiting at first
            return 0
        return len(pieces[0])

    got = list(pipein.split(peek, lambda n: pieces.pop(0)[:n], sleep=slept.append))
    assert got == ["home\n", 'look {"A": 1}\n', "tail"]
    assert len(slept) == 2


def test_a_child_reads_its_commands_and_stops_when_stdin_closes():
    # On Windows this goes by PeekNamedPipe (no read left waiting on the pipe);
    # elsewhere it's the plain read.
    code = ("import sys\nfrom curslink import pipein\n"
            "for line in pipein.lines():\n    print('got', line.strip(), flush=True)\nprint('closed', flush=True)\n")
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    out: list[str] = []
    reader = threading.Thread(target=lambda: out.extend(proc.stdout), daemon=True)
    reader.start()
    proc.stdin.write("home\n")
    proc.stdin.flush()
    deadline = time.monotonic() + 10
    while not out and time.monotonic() < deadline:
        time.sleep(0.02)
    assert out and out[0].strip() == "got home"  # (before the pipe closed)
    proc.stdin.write("gaze pause\n")
    proc.stdin.close()
    assert proc.wait(timeout=10) == 0
    reader.join(timeout=5)
    assert [line.strip() for line in out] == ["got home", "got gaze pause", "closed"]


def test_a_dll_style_look_at_stdin_does_not_wait_for_a_command():
    # What hung: with a blocking read pending on a Windows pipe, asking what
    # kind of file stdin is (as a DLL's C runtime does when it loads) waited
    # for the read. With pipein it answers at once.
    code = ("import os, sys, threading, time\nfrom curslink import pipein\n"
            "threading.Thread(target=lambda: [None for _ in pipein.lines()], daemon=True).start()\n"
            "time.sleep(0.3)\nt = time.monotonic()\nos.fstat(0)\n"
            "print(round(time.monotonic() - t, 2), flush=True)\n")
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline()
        assert line and float(line) < 1.0
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def test_windows_lists_cameras_in_the_order_they_are_opened():
    from curslink.gaze import cameras

    if sys.platform == "win32":
        names = cameras.media_foundation_names()  # (a CI machine has none: it's the calls that are checked)
        assert isinstance(names, list) and all(isinstance(n, str) for n in names)
    assert cameras.infrared("USB2.0 IR UVC WebCam") and not cameras.infrared("USB2.0 FHD UVC WebCam")
    assert not cameras.infrared("Mirror Cam") and cameras.infrared("Integrated Infrared Camera")
    found = [cameras.Camera(0, "USB2.0 FHD UVC WebCam"), cameras.Camera(1, "USB2.0 IR UVC WebCam")]
    # Saved as "0, IR" from the old list: number 0 is what was opened, and it's the colour one.
    assert cameras.renamed(0, found) == (0, "USB2.0 FHD UVC WebCam")
    # The infrared one saved by number: the colour one instead.
    assert cameras.renamed(1, found) == (0, "USB2.0 FHD UVC WebCam")
    assert cameras.renamed(0, [cameras.Camera(0, "USB2.0 IR UVC WebCam")]) == (0, "USB2.0 IR UVC WebCam")
    assert cameras.renamed(3, []) == (3, "")
    assert cameras._kind("USB2.0 IR UVC WebCam") == "Infrared"
