// Help for people new to the app: the "How to use this app" walkthrough
// (#help-dialog in index.html) and the tips placed around the app.
//
// Any element with data-help opens the walkthrough; data-help="send" opens it
// at the section with id "help-send". A tip is an element with data-tip="<id>":
// it shows until its "Got it" (data-tip-close) is tapped, and More > Show tips
// again brings every tip back. Which tips were closed is kept in this browser
// only; if storage is blocked the tips simply show every time.

const HELP_TIPS_KEY = "scg.tips.closed";
const HELP_SEEN_KEY = "scg.help.seen";

function helpClosedTips() {
  try { return new Set(JSON.parse(localStorage.getItem(HELP_TIPS_KEY) || "[]")); } catch (_) { return new Set(); }
}

function helpCloseTip(id) {
  const closed = helpClosedTips();
  closed.add(id);
  try { localStorage.setItem(HELP_TIPS_KEY, JSON.stringify([...closed])); } catch (_) { /* shows again next time */ }
}

// The markup for a tip rendered from JavaScript ("" once it's been closed).
// `html` is trusted markup written in this file or app.js, never user text.
function helpTip(id, html, section = "") {
  if (helpClosedTips().has(id)) return "";
  return `<div class="tip" data-tip="${id}" role="note">
    <div class="tip-body"><b>Tip</b> ${html}</div>
    <div class="tip-actions">${section ? `<button type="button" class="ghost small" data-help="${section}">Show me how</button>` : ""}<button type="button" class="ghost small" data-tip-close>Got it</button></div>
  </div>`;
}

// Tips written into index.html start hidden; show the ones not yet closed.
function helpApplyTips(root = document) {
  const closed = helpClosedTips();
  for (const el of root.querySelectorAll("[data-tip]")) el.hidden = closed.has(el.dataset.tip);
}

function helpSeen() {
  try { return !!localStorage.getItem(HELP_SEEN_KEY); } catch (_) { return false; }
}

function openHelp(section = "") {
  const dialog = document.getElementById("help-dialog");
  if (!dialog) return;
  try { localStorage.setItem(HELP_SEEN_KEY, "1"); } catch (_) { /* fine */ }
  // Opened from another dialog (setup, Send to pedal): the guide goes on top.
  if (!dialog.open) dialog.showModal();
  const target = section && document.getElementById("help-" + section);
  if (target) target.scrollIntoView({ block: "start" });
  else dialog.scrollTop = 0;
}

document.addEventListener("click", (e) => {
  const open = e.target.closest("[data-help]");
  if (open) {
    e.preventDefault();
    openHelp(open.dataset.help);
    return;
  }
  const close = e.target.closest("[data-tip-close]");
  if (close) {
    const tip = close.closest("[data-tip]");
    if (tip) { helpCloseTip(tip.dataset.tip); tip.hidden = true; }
    return;
  }
  if (e.target.closest("#help-tips-reset")) {
    try { localStorage.removeItem(HELP_TIPS_KEY); } catch (_) { /* fine */ }
    helpApplyTips();
    if (typeof toast === "function") toast("Tips are back. You'll see them on each screen again.");
  }
});

helpApplyTips();
