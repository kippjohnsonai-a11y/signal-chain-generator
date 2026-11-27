# Contributing

Thanks for helping. Bug reports, presets that came out wrong, and pull requests
are all welcome.

## Running it from source

```bash
brew install uv
git clone <this repository> && cd <its folder>
uv run signal-chain-generator          # or the older name: uv run stompxl
```

## Before you open a pull request

```bash
uv run --extra dev pytest -q             # all tests pass
uv run --extra dev ruff check stompxl    # lint is clean
```

- Keep one change per pull request, with a short note on what it changes for a player.
- Never commit Line 6's files (`Helix.sym`, `amp.models`), presets pulled off a
  pedal, NAM captures, your library, or any key. `.gitignore` blocks the usual ones.
- Pedal-protocol changes need a note on which pedal and firmware you tested on.
  Firmware, flash and DFU operations are out of scope and won't be merged.
- How presets are built lives in `stompxl/knowledge/` as plain Markdown; changes
  there are a good first contribution.
