#!/usr/bin/env python3
"""Copy frozen agent-books weeks (+ .sha256) into the webhook-served directory and write
its index.json. Idempotent; never deletes. Files land 0644 iq:iq so the webhook (user iq)
can serve them.

    publish_weeks.py <src_dir> <dst_dir>
"""
import grp
import hashlib
import json
import os
import pwd
import sys


def main(src: str, dst: str) -> int:
    os.makedirs(dst, exist_ok=True)
    try:
        uid, gid = pwd.getpwnam("iq").pw_uid, grp.getgrnam("iq").gr_gid
    except KeyError:
        uid = gid = -1
    names = sorted(n for n in os.listdir(src)
                   if n.startswith("agent-books-week-") and n.endswith(".json"))
    index = []
    for n in names:
        with open(os.path.join(src, n), "rb") as fh:
            data = fh.read()
        sha = hashlib.sha256(data).hexdigest()
        for name, body in ((n, data), (n + ".sha256", (sha + "\n").encode())):
            out = os.path.join(dst, name)
            if os.path.exists(out):
                with open(out, "rb") as fh:
                    if fh.read() == body:
                        continue
            tmp = out + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(body)
            os.chmod(tmp, 0o644)
            if uid >= 0:
                os.chown(tmp, uid, gid)
            os.replace(tmp, out)
        index.append({"week": n[len("agent-books-week-"):-len(".json")], "file": n,
                      "sha256": sha})
    tmp = os.path.join(dst, "index.json.tmp")
    with open(tmp, "w") as fh:
        json.dump({"weeks": index}, fh, sort_keys=True, indent=1)
    os.chmod(tmp, 0o644)
    if uid >= 0:
        os.chown(tmp, uid, gid)
        os.chown(dst, uid, gid)
    os.replace(tmp, os.path.join(dst, "index.json"))
    print(f"published {len(index)} week(s) to {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
