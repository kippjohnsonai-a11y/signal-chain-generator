"""Enumerate USB devices through the bundled libusb, then free them.

A crash in libusb_unref_device on Windows showed up only when pyusb's
device objects were garbage-collected, so this lists every device, reads
the Line 6 rows the way the app does, drops them and forces a collection.
"""

import gc
import sys

from stompxl.helixgen.device import usb as hx_usb

usb = hx_usb._require_pyusb()
import libusb_package  # noqa: E402

print("libusb:", libusb_package.get_library_path())
for _ in range(3):
    devices = list(usb.core.find(find_all=True))
    print(len(devices), "devices")
    print(hx_usb.find_devices())
    del devices
    gc.collect()
print("ok")
sys.stdout.flush()
