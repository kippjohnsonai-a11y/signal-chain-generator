"use strict";

// The pedal plugged into the device this page runs on (a phone, a laptop),
// handed to the app on the Mac mini over a WebSocket.
//
// The page opens the pedal with WebUSB and claims interface 0, the
// vendor-specific control interface HX Edit uses (audio and MIDI are left
// alone). From then on it is a dumb pipe: the server sends "write N bytes",
// "read up to N bytes within T ms" or "clear halt", and the page answers. All
// of the protocol stays in Python (helixgen/device/usb.py), so a push from the
// phone runs the same tested code as a push from the Mac.
//
// The page must stay open (and the screen on) while the pedal is in use:
// a backgrounded tab loses its WebSocket.

const PedalRelay = (() => {
  const VENDOR_ID = 0x0e41;
  const IFACE = 0;
  const REMEMBER = "stompxl.relay";  // "on" once this device has been chosen
  const supported = !!navigator.usb && window.isSecureContext;

  let device = null;
  let ws = null;
  let inflight = null;  // the one transferIn in progress, kept across timeouts
  let listeners = [];

  const notify = () => listeners.forEach((fn) => { try { fn(status()); } catch (_) { /* ignore */ } });
  const remember = (on) => { try { on ? localStorage.setItem(REMEMBER, "on") : localStorage.removeItem(REMEMBER); } catch (_) { /* private mode */ } };
  const remembered = () => { try { return localStorage.getItem(REMEMBER) === "on"; } catch (_) { return false; } };

  function where() {
    const ua = navigator.userAgent;
    if (/Android/i.test(ua)) return /Mobile/i.test(ua) ? "Android phone" : "Android tablet";
    if (/CrOS/.test(ua)) return "Chromebook";
    if (/Macintosh|Mac OS X/.test(ua)) return "Mac";
    if (/Windows/.test(ua)) return "Windows PC";
    if (/Linux/.test(ua)) return "Linux computer";
    return "browser";
  }

  function status() {
    return { supported, active: !!(ws && ws.readyState === WebSocket.OPEN), device: device?.productName || "" };
  }

  const toB64 = (bytes) => { let s = ""; for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000)); return btoa(s); };
  const fromB64 = (text) => Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
  const TIMEOUT = Symbol("timeout");
  const after = (ms) => new Promise((resolve) => setTimeout(() => resolve(TIMEOUT), ms));

  // A read that times out is not cancelled (WebUSB can't): its transfer stays
  // pending and the next read waits on that same transfer, so a frame that
  // arrives late is handed to the next read instead of being lost.
  async function read(ep, size, timeout) {
    if (!inflight) inflight = device.transferIn(ep, Math.max(size, 512));
    let result;
    try {
      result = await Promise.race([inflight, after(timeout)]);
    } catch (err) {
      inflight = null;
      throw err;
    }
    if (result === TIMEOUT) return { ok: false, timeout: true, error: `read timed out after ${timeout} ms` };
    inflight = null;
    if (result.status !== "ok") return { ok: false, error: `read status ${result.status}` };
    return { ok: true, data: toB64(new Uint8Array(result.data.buffer, result.data.byteOffset, result.data.byteLength)) };
  }

  // A fresh start for each conversation the app opens with the pedal: releasing
  // and re-claiming the interface cancels a read still pending from the last
  // one (WebUSB has no other way to cancel a transfer). Without it a stale read
  // carries over into the next send, and on a Mac, where Chrome cancels that
  // read when the app clears the endpoint, the pedal's answers can go to a
  // transfer nobody is waiting on: the send then fails with a read timeout
  // even though the pedal is connected and the slot names listed fine.
  async function reclaim() {
    const stale = inflight;
    inflight = null;
    if (stale) stale.catch(() => {});  // its cancellation is expected
    await device.releaseInterface(IFACE).catch(() => {});
    await device.claimInterface(IFACE);
    return { ok: true };
  }

  // 512 on a high-speed link, 64 when the pedal came up at full speed (a hub,
  // an adapter): the app frames a preset write differently for that.
  function outPacketSize() {
    try {
      const iface = device.configuration.interfaces.find((i) => i.interfaceNumber === IFACE);
      const ep = iface.alternate.endpoints.find((e) => e.direction === "out" && e.endpointNumber === 1);
      return ep ? ep.packetSize : null;
    } catch (_) { return null; }
  }

  async function write(ep, data, timeout) {
    const bytes = fromB64(data);
    const result = await Promise.race([device.transferOut(ep, bytes), after(timeout)]);
    if (result === TIMEOUT) return { ok: false, timeout: true, error: `write timed out after ${timeout} ms` };
    if (result.status !== "ok") return { ok: false, error: `write status ${result.status}` };
    return { ok: true, written: result.bytesWritten };
  }

  async function handle(message) {
    if (!device) return { ok: false, error: "The pedal is not open on this device." };
    switch (message.op) {
      case "write": return write(message.ep, message.data, message.timeout ?? 2000);
      case "read": return read(message.ep, message.size ?? 512, message.timeout ?? 2000);
      case "reclaim": return reclaim();
      case "clear_halt":
        await device.clearHalt(message.direction === "in" ? "in" : "out", message.ep);
        return { ok: true };
      default: return { ok: false, error: `unknown op ${message.op}` };
    }
  }

  function openSocket() {
    return new Promise((resolve, reject) => {
      const url = `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/usb-relay`;
      const socket = new WebSocket(url);
      let attached = false;
      socket.onopen = () => socket.send(JSON.stringify({
        type: "hello", vendorId: device.vendorId, productId: device.productId,
        productName: device.productName, serialNumber: device.serialNumber, where: where(),
        packetSize: outPacketSize(),
      }));
      socket.onmessage = async (event) => {
        const message = JSON.parse(event.data);
        if (message.type === "attached") { attached = true; resolve(socket); return; }
        if (message.id === undefined) return;
        let reply;
        try { reply = await handle(message); } catch (err) { reply = { ok: false, error: `${err.name}: ${err.message}` }; }
        reply.id = message.id;
        if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(reply));
      };
      socket.onclose = (event) => {
        if (!attached) reject(new Error(event.code === 1008 ? "The app refused the connection (not signed in?)" : "Couldn't reach the app"));
        if (ws === socket) { ws = null; notify(); }
      };
    });
  }

  async function attach(chosen) {
    await detach({ forget: false });
    device = chosen;
    await device.open();
    if (device.configuration === null) await device.selectConfiguration(1);
    try {
      await device.claimInterface(IFACE);
    } catch (err) {
      await device.close().catch(() => {});
      device = null;
      throw new Error(`Couldn't take over the pedal's control connection (${err.message}). Something else is using it: HX Edit, or the app open in another tab or window. Close those, then retry.`);
    }
    ws = await openSocket();
    remember(true);
    notify();
  }

  async function detach({ forget = true } = {}) {
    const socket = ws;
    ws = null;
    if (socket) socket.close();
    inflight = null;
    if (device) {
      await device.releaseInterface(IFACE).catch(() => {});
      await device.close().catch(() => {});
      device = null;
    }
    if (forget) remember(false);
    notify();
  }

  // The button: asks the browser for the pedal (the picker needs a tap).
  async function connect() {
    if (!supported) throw new Error("This browser can't reach USB devices. Use Chrome or Edge, over the app's https address.");
    const chosen = await navigator.usb.requestDevice({ filters: [{ vendorId: VENDOR_ID }] });
    await attach(chosen);
  }

  // A device chosen before reconnects by itself when the page opens or the
  // pedal is plugged back in; no picker needed for a device already allowed.
  async function resume() {
    if (!supported || !remembered() || status().active) return;
    const [known] = (await navigator.usb.getDevices()).filter((d) => d.vendorId === VENDOR_ID);
    if (known) await attach(known).catch((err) => console.warn("Pedal relay:", err));
  }

  if (supported) {
    navigator.usb.addEventListener("connect", () => { resume(); });
    navigator.usb.addEventListener("disconnect", (event) => { if (event.device === device) detach({ forget: false }); });
  }

  return {
    supported, status, connect, resume,
    disconnect: () => detach({ forget: true }),
    onChange: (fn) => { listeners.push(fn); },
  };
})();
