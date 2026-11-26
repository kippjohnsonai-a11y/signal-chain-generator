"""``signal-chain-generator`` (or ``stompxl``) - start the local web app and open it in the browser.

``stompxl diagnose SLOT [SLOT ...]`` reads presets off the pedal and writes a
structural report, for comparing a pushed preset with one HX Edit saved.

``stompxl audit-models`` checks the model catalog against HX Edit's own model
table and lists parameters either side is missing.

``stompxl learn-nam-usb PRESET.hlx`` reads the Input and Send numbers of a
NAM-on-computer preset set up by hand in HX Edit, so the app's own NAM-on-computer
presets come out ready to play (see docs/nam.md).

``stompxl nam starter`` / ``stompxl nam full-rig`` / ``stompxl nam add LINK --slot N --name NAME`` /
``stompxl nam find WORDS`` download NAM captures from TONE3000, convert them for
NAM A2 Lite, and list them in Settings (sign in to TONE3000 in the app first).
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import webbrowser
from pathlib import Path

from . import APP_FULL_NAME


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "diagnose":
        diagnose(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "learn-nam-usb":
        learn_nam_usb(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "nam":
        nam(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "audit-models":
        from . import audit

        try:
            path, lines = audit.run()
        except Exception as exc:  # noqa: BLE001 - a missing HX Edit is the usual cause
            sys.exit(f"Error: {exc} (is HX Edit installed?)")
        print("\n".join(lines))
        print(f"\nFull report written to {path}")
        return

    command = Path(sys.argv[0]).name
    parser = argparse.ArgumentParser(
        prog=command if command and not command.endswith(".py") else "signal-chain-generator",
        description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    import uvicorn

    from . import access, lan
    from .server import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    url = f"http://{args.host}:{args.port}/"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"{APP_FULL_NAME} running at {url}  (Ctrl+C to stop)")
    app = create_app()
    if lan.enabled():
        lan.start(app)
        print(f"Phones on this Wi-Fi: paired phones can use port {lan.port()}")
    cf_port = access.cloudflare_port()
    if cf_port is None:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        return

    # A second port for the Cloudflare tunnel (Google sign-in for guests). It
    # serves the same app, but the access gate trusts only Cloudflare Access
    # tokens on it. Always loopback: only cloudflared on this Mac connects.
    import asyncio

    print(f"Guest sign-in port (Cloudflare tunnel): http://127.0.0.1:{cf_port}/")
    servers = [uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
               for host, port in ((args.host, args.port), ("127.0.0.1", cf_port))]

    async def serve_both() -> None:
        tasks = [asyncio.create_task(server.serve()) for server in servers]
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for server in servers:  # one stopping (Ctrl+C, launchd) stops both
            server.should_exit = True
        await asyncio.gather(*pending)

    asyncio.run(serve_both())


def learn_nam_usb(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="stompxl learn-nam-usb")
    parser.add_argument("preset", help="an .hlx exported from HX Edit: Input block on USB 5/6, "
                                       "path B's Output on Send L/R")
    args = parser.parse_args(argv)

    import json

    from . import config, xl

    try:
        with open(args.preset, encoding="utf-8") as handle:
            routing = xl.learn_usb_routing(json.load(handle))
    except (OSError, ValueError) as exc:
        sys.exit(f"Error: {exc}")
    config.write_settings({"nam_usb_routing": routing})
    print(f"Learnt: USB 5/6 input = {routing['input']}, Send output = {routing['send_output']}. "
          "New NAM-on-computer presets will use them.")


def diagnose(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="stompxl diagnose")
    parser.add_argument("slots", nargs="+",
                        help="presets to read: pedal labels like 29B, or preset names")
    args = parser.parse_args(argv)

    from . import diagnose as diag
    from . import pedal

    print("Reading from the pedal (quit HX Edit and stop the app first)...")
    try:
        path, lines = diag.run(args.slots)
    except (diag.SlotNotFound, pedal.PedalError) as exc:
        sys.exit(f"Error: {exc}")
    print("\nController assignments (what the expression pedal and snapshots control):")
    print("\n".join(lines))
    print(f"\nFull report written to {path}")


if __name__ == "__main__":
    main()


def nam(argv: list[str]) -> None:
    from . import nam2ir, namlib, tone3000

    parser = argparse.ArgumentParser(prog="stompxl nam", description="NAM captures from TONE3000.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("starter", help="the 20-capture starter library, IR slots 101-120")
    sub.add_parser("full-rig", help="the full-rig pack (cab included), IR slots 121 up")
    add = sub.add_parser("add", help="one capture from a TONE3000 tone link or number")
    add.add_argument("tone")
    add.add_argument("--slot", type=int, help="User IR slot (1-128); leave out for computer only")
    add.add_argument("--name", default="")
    add.add_argument("--model", default="", help="words from the model's name in the pack")
    add.add_argument("--description", default="")
    find = sub.add_parser("find", help="search TONE3000, most downloaded first")
    find.add_argument("words", nargs="+")
    args = parser.parse_args(argv)
    try:
        if args.command == "find":
            for tone in tone3000.Client().search(" ".join(args.words)):
                t = tone3000.describe(tone)
                print(f"{t['id']:>7}  {t['title']}  by {t['creator']}  "
                      f"{t['downloads']} downloads, {t['favorites']} favorites"
                      f"{', full rig' if t['full_rig'] else ''}")
            return
        entries = namlib.pack(args.command) if args.command in namlib.PACKS else [{
            "tone": args.tone, "slot": args.slot, "name": args.name,
            "model": args.model, "description": args.description}]
        result = namlib.add_many(entries, report=print)
    except (tone3000.Tone3000Error, nam2ir.ConvertError) as exc:
        sys.exit(f"Error: {exc}")
    print(f"\nAdded {len(result['added'])}, skipped {len(result['failed'])}.")
    print(f"Drag the .wav files in {result['ir_folder']} onto their slots in HX Edit "
          "(each name starts with its slot).")
