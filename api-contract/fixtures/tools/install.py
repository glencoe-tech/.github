"""Synthetic generator-toolchain install for the api-contract self-test.

The "lockfile" sdk.lock pins the generator by its SHA-256. Installing records the hash
of the generator actually present, so a lockfile that does not pin it is rewritten (and the
lockfile check sees the drift), the way `npm install` rewrites a stale package-lock.json.
"""
import hashlib
import pathlib

digest = hashlib.sha256(pathlib.Path("tools/generate.py").read_bytes()).hexdigest()
pathlib.Path("sdk.lock").write_text(f"tools/generate.py sha256:{digest}\n")
