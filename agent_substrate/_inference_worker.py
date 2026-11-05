"""The provider side of the inference gateway's process boundary.

A STANDALONE SCRIPT, run by path under ``python -I`` with an empty
environment. It imports nothing of the package, so the gateway's process and
this one share no code, and nothing the gateway holds is reachable from here.
It is the only process that reads the provider credential: from a file under a
root the operator names, outside the repository, never from the environment.

PROTOCOL. One JSON object on stdin, ``{"request", "attempt", "prompt_b64"}``,
and one on stdout, ``{"status", "body_b64", "usage", "claimed", "error"}``.

MODES.

``live``
    Refuses. No provider has been chosen (owner decision OD-1), and this
    worker will not choose one. When one is chosen, its client goes here,
    behind this same protocol, and nowhere else.
``fixture``
    Replays a reply file. It requires the credential to be readable, so a
    test that passes through this mode proves the worker, and not the gateway,
    read it. A reply may ask to echo the credential, which is how the gateway's
    refusal of a leaking provider is tested.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import stat
import sys
import time

MAX_SECRET_BYTES = 64 * 1024
EXIT_BLOCKED = 3


def _read_secret(root: str, secret_id: str) -> str:
    if not secret_id or "/" in secret_id or secret_id in (".", ".."):
        raise ValueError("the secret id is not a file name")
    fd = os.open(os.path.join(root, secret_id),
                 os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_SECRET_BYTES:
            raise ValueError("the secret is not a bounded regular file")
        raw = os.read(fd, MAX_SECRET_BYTES + 1)
    finally:
        os.close(fd)
    return raw.decode("utf-8").strip()


def _reply(status: str, body: bytes = b"", usage=None, claimed=None,
           error: str = "") -> None:
    sys.stdout.write(json.dumps({
        "status": status, "body_b64": base64.b64encode(body).decode("ascii"),
        "usage": usage, "claimed": claimed, "error": error}))
    sys.stdout.flush()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=("live", "fixture"))
    ap.add_argument("--secret-root")
    ap.add_argument("--secret-id")
    ap.add_argument("--fixture")
    args = ap.parse_args(argv)
    msg = json.loads(sys.stdin.read())
    if args.mode == "live":
        _reply("ERROR", error="EXTERNALLY_BLOCKED: no provider has been "
               "chosen (owner decision OD-1); this worker makes no live call")
        return EXIT_BLOCKED
    if not (args.secret_root and args.secret_id and args.fixture):
        _reply("ERROR", error="fixture mode needs a secret root, a secret "
               "id and a fixture")
        return 2
    try:
        key = _read_secret(args.secret_root, args.secret_id)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        _reply("ERROR", error=f"the credential is not readable: "
               f"{type(exc).__name__}")
        return 2
    with open(args.fixture, encoding="utf-8") as fh:
        replies = json.load(fh)["replies"]
    rep = replies[(int(msg["attempt"]) - 1) % len(replies)]
    if rep.get("sleep_s"):
        time.sleep(float(rep["sleep_s"]))
    body = rep.get("body", "")
    if not isinstance(body, str):
        body = json.dumps(body, sort_keys=True, separators=(",", ":"))
    if rep.get("echo_credential"):
        body = body.replace("@@KEY@@", key)
    _reply(rep.get("status", "OK"), body.encode("utf-8"), rep.get("usage"),
           rep.get("claimed"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
