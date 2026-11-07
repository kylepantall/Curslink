"""curslink-helper.exe: ``service`` (started by Windows) or ``inject`` (started by the service)."""

import sys


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "service":
        from curslink.winhelper.service import run

        return run()
    if command == "inject":
        from curslink.winhelper.inject import main as inject

        return inject()
    print("curslink-helper: started by the Curslink input helper service, not by hand.\n"
          "Turn it on or off in Curslink's Settings.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
