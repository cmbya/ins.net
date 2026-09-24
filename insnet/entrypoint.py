"""Prepare writable Docker mounts, then run the app as its unprivileged UID."""

import os
import sys
from pathlib import Path


APP_UID = 10001
APP_GID = 10001


def main():
    data_root = Path(os.environ.get("INS_DATA", "/data")).resolve()
    archive_root = Path(os.environ.get("INS_ARCHIVE_ROOT", "/archive")).resolve()
    for path in (data_root, archive_root):
        path.mkdir(parents=True, exist_ok=True)

    if os.geteuid() == 0:
        for path in (data_root, archive_root):
            os.chown(path, APP_UID, APP_GID)
            os.chmod(path, 0o755)
        os.setgroups([])
        os.setgid(APP_GID)
        os.setuid(APP_UID)

    os.execv(sys.executable, [sys.executable, "-m", "insnet.web"])


if __name__ == "__main__":
    main()
