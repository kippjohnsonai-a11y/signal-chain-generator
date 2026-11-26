# Releasing

Releases go to PyPI as `signal-chain-generator`, so anyone can run the app
without cloning the repository:

```bash
brew install uv
uvx signal-chain-generator          # or: pipx install signal-chain-generator
```

## One-time setup (the maintainer)

1. Make an account on [pypi.org](https://pypi.org) and turn on two-factor sign-in.
2. Under **Your projects > Publishing**, add a *pending* trusted publisher:
   project `signal-chain-generator`, this repository's owner and name,
   workflow `release.yml`, environment `pypi`.
3. In this repository's **Settings > Environments**, add an environment
   named `pypi` (optionally requiring your approval for each run).

No PyPI token is ever stored: GitHub proves to PyPI which workflow is asking.

## Each release

1. Set `version` in `pyproject.toml` (for example `0.2.0`) and merge it.
2. Tag that commit and push the tag: `git tag v0.2.0 && git push origin v0.2.0`.
3. The **Release** workflow runs the tests, checks that the tag matches the
   version, builds, and publishes.

A version on PyPI can't be replaced, only followed by a newer one.

## Names

The vendored HelixGen lives at `stompxl.helixgen`, not as a top-level
`helixgen` package, because `helixgen` on PyPI is a different project. The
`helixgen` command is still installed for pedal backups; if someone also
installs that other project with pip in the same environment, whichever was
installed last owns the command. `uvx` and `pipx` keep each app in its own
environment, so neither is affected.
