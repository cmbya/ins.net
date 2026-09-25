"""Prepare writable Docker mounts, then run the app as its unprivileged UID."""

import os
import sys
from pathlib import Path


DEFAULT_UID = 10001
DEFAULT_GID = 10001


def _chown_data_tree(data_root, uid, gid):
    """Reassign only application data, leaving a legacy media tree untouched."""
    paths = [data_root, data_root / "insnet.sqlite3", data_root / "insnet.sqlite3-wal",
             data_root / "insnet.sqlite3-shm", data_root / "accounts", data_root / "tmp"]
    for path in paths:
        if path.is_dir():
            for current, directories, files in os.walk(path):
                if Path(current) == data_root:
                    directories[:] = [name for name in directories if name != "media"]
                os.chown(current, uid, gid)
                for name in files:
                    try:
                        os.chown(Path(current) / name, uid, gid, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
        elif path.exists():
            os.chown(path, uid, gid, follow_symlinks=False)


def _check_archive_access(archive_root):
    probe = archive_root / f".insnet-write-test-{os.getpid()}"
    try:
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        probe.unlink()
    except OSError as exc:
        probe.unlink(missing_ok=True)
        raise SystemExit(
            f"无法写入归档目录 {archive_root}：{exc}. "
            "请在飞牛上授予目录写入权限，或把 Compose 的 INS_UID/INS_GID "
            "设置为该目录所有者的数字 UID/GID。"
        ) from exc


def main():
    data_root = Path(os.environ.get("INS_DATA", "/data")).resolve()
    archive_root = Path(os.environ.get("INS_ARCHIVE_ROOT", "/archive")).resolve()
    try:
        uid = int(os.environ.get("INS_UID", str(DEFAULT_UID)))
        gid = int(os.environ.get("INS_GID", str(DEFAULT_GID)))
    except ValueError as exc:
        raise SystemExit("INS_UID 和 INS_GID 必须是数字") from exc
    if uid <= 0 or gid <= 0:
        raise SystemExit("INS_UID 和 INS_GID 必须是大于 0 的数字")
    data_root.mkdir(parents=True, exist_ok=True)
    archive_root.mkdir(parents=True, exist_ok=True)

    if os.geteuid() == 0:
        _chown_data_tree(data_root, uid, gid)
        os.chmod(data_root, 0o700)
        os.setgroups([gid])
        os.setgid(gid)
        os.setuid(uid)
    elif os.geteuid() != uid or os.getegid() != gid:
        raise SystemExit("容器以非 root 用户运行，请让 INS_UID/INS_GID 与容器用户一致")

    _check_archive_access(archive_root)

    os.execv(sys.executable, [sys.executable, "-m", "insnet.web"])


if __name__ == "__main__":
    main()
