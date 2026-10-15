"""Synthetic SDK generator for the api-contract self-test.

Writes sdk/operations.txt: one line per operationId in openapi.yaml, sorted. Deterministic, so a
clean regeneration reproduces the committed output byte for byte.
"""
import pathlib
import re

ids = sorted(set(re.findall(r"operationId:\s*(\S+)", pathlib.Path("openapi.yaml").read_text())))
out = pathlib.Path("sdk")
out.mkdir(exist_ok=True)
(out / "operations.txt").write_text("".join(f"{i}\n" for i in ids))
