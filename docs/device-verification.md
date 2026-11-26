# First push to the HX Stomp XL: verification checklist

HelixGen's USB protocol is verified on the HX Stomp. The XL uses the same protocol family and is already in its device table (USB product id `0x4253`), but nobody has confirmed a write to one yet. These steps check the parts most likely to differ, on a slot you don't mind losing.

## 0. Back up
- In **HX Edit**: File → Backup (a full `.hxb` backup you can restore from).
- Optional: `uv run helixgen backup --output ~/stompxl-backup` (raw per-slot copies).
- The app also saves each slot's old preset to `~/Library/Application Support/StompXL Tone Studio/backups/` before every push. These raw copies can't be imported into HX Edit; the `.hxb` backup is the one to restore from.

## 1. Can the app see the pedal?
- Quit HX Edit, connect the XL, start the app. The top bar should show "HX Stomp XL (serial ...) (untested model)".
- If it says "USB unavailable": run `brew install libusb` and restart the app.

## 2. File import (no USB write)
- Generate a preset, click **Download .hlx**, and drag the file into HX Edit's preset list.
- Check that it imports without a device-mismatch warning, that all 4 snapshots appear with the right names, and that the wah's Pedal parameter shows **EXP 1** as its controller.
  - If the controller shows something else (or nothing), the Stomp-family EXP numbering differs from Helix's: tell Claude Code and it'll adjust `CONTROLLER_EXP1/2` in `stompxl/xl.py`.

## 3. First USB push
- HX Edit must be **installed but closed**.
- Pick a slot that holds a preset you don't need (e.g. a factory preset in bank 32), then **Send to pedal**.
- On the pedal, check:
  - [ ] the preset name and block order match the app
  - [ ] all 4 snapshots switch, with the right blocks on or off
  - [ ] per-snapshot values change (e.g. amp drive between INTRO and CHORUS)
  - [ ] footswitch labels match the app (looper on FS6, then wah, drive, delay, boost on FS5, FS2, FS3; FS1/FS4 left free for snapshot up/down)
  - [ ] the expression pedal sweeps the wah, volume or Pitch Wham **while that block is on** (it may be on only in some snapshots). HX Edit shows it under the block's Controller Assign as Position → EXP 1.

## 4. Report back
If everything checks out, change `VERIFIED_PRODUCT_IDS` in `stompxl/helixgen/device/usb.py` to include `0x4253`, and consider sending a device report upstream to [HelixGen](https://github.com/ORogers/HelixGen/issues/new/choose).

## Footswitch numbering and snapshot diagnostics

The XL's assignable switches are FS1-FS3 (top row) and FS4-FS6 (bottom row); MODE and TAP/TUNER aren't assignable. Switch *n* is position *n* of the preset's footswitch table, confirmed on the pedal for FS4-FS6. To re-check it, or to investigate HX Edit's "failed to recall snapshot data" message:

1. In HX Edit, make a preset with a few blocks, assign one block to each switch you care about (e.g. a delay on switch 6), give it 4 snapshots, and save it to a spare slot. Quit HX Edit and stop the app.
2. Run `cd ~/stompxl && uv run stompxl diagnose <pushed preset> <HX Edit preset>`, naming each by its pedal label (e.g. `29B`) or preset name.
3. Send the report (`~/Library/Application Support/StompXL Tone Studio/diagnose.json`) to Claude Code. If the mapping differs, it goes in `settings.json` (same folder) as `footswitch_map`, e.g. `{"6": 9}` for "switch 6 is position 9".
