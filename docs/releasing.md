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

### The Mac app (optional)

Each release can also attach a signed, notarized `Signal-Chain-Generator-<version>.dmg`.
The app inside is a small launcher (a copy of uv and a script) that runs that
exact PyPI release, so it needs no Python, Homebrew or Terminal. It needs an
Apple Developer account:

1. At developer.apple.com, create a **Developer ID Application** certificate,
   install it in Keychain Access, and export it as a `.p12` with a password.
2. In App Store Connect, under **Users and Access > Integrations**, create a
   **Team Key** with Developer access; keep the `.p8`, the Key ID and the
   Issuer ID.
3. In this repository's **Settings > Environments**, add an environment named
   `macos-release` (with yourself as required reviewer) and these secrets:

   | Secret | Value |
   | --- | --- |
   | `MACOS_CERT_P12` | the `.p12`, base64-encoded: `base64 -i cert.p12 \| pbcopy` |
   | `MACOS_CERT_PASSWORD` | the `.p12`'s password |
   | `ASC_KEY_P8` | the whole text of the `.p8` file |
   | `ASC_KEY_ID` | the key's ID |
   | `ASC_ISSUER_ID` | the issuer ID |

Without them the PyPI release still goes out; only the app job fails.
To build an unsigned app on your own Mac: `bash scripts/build-mac-app.sh 0.2.0`.

## Each release

1. Set `version` in `pyproject.toml` (for example `0.2.0`) and merge it.
2. Tag that commit and push the tag: `git tag v0.2.0 && git push origin v0.2.0`.
3. The **Release** workflow runs the tests, checks that the tag matches the
   version, builds, and publishes to PyPI, then builds the Mac app and
   attaches it to the GitHub release.

A version on PyPI can't be replaced, only followed by a newer one.

## Names

The vendored HelixGen lives at `stompxl.helixgen`, not as a top-level
`helixgen` package, because `helixgen` on PyPI is a different project. The
`helixgen` command is still installed for pedal backups; if someone also
installs that other project with pip in the same environment, whichever was
installed last owns the command. `uvx` and `pipx` keep each app in its own
environment, so neither is affected.
