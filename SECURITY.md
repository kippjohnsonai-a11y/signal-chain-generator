# Security

Please report security problems privately, not in a public issue: use GitHub's
**Report a vulnerability** button on the repository's Security tab.

Worth reporting: anything that lets someone reach a hub they weren't let into,
read another person's library or keys, or make the app write to a pedal without
the owner asking. Bugs in how presets sound are ordinary issues.

## How the app treats secrets

- Your Anthropic and TONE3000 keys are stored only in `settings.json` in your
  data folder, readable by your user account only, and are sent only to
  Anthropic and TONE3000.
- The app listens on `127.0.0.1` unless you choose otherwise. To reach it from
  other devices, put it behind Tailscale (see `docs/mac-mini.md`) or Cloudflare
  Access (see `docs/guests.md`); don't expose the port to the internet directly.
- Diagnostic reports redact keys, tokens and email addresses before they leave
  your computer.
