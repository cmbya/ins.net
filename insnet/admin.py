"""Management API helpers for dashboard, records, authorization and settings."""
from pathlib import Path

from .engine import USERNAME


def _safe_subdir(value, archive_root):
    value = str(value or "").strip().strip("/")
    path = Path(value)
    if (len(value) > 180 or path.is_absolute() or "\\" in value or "\x00" in value
            or any(part in ("..", ".") for part in path.parts)):
        raise ValueError("保存目录必须是 /archive 内的相对路径")
    (Path(archive_root) / path).resolve().relative_to(Path(archive_root).resolve())
    return value


def _account(db, account_id):
    account = db.account(str(account_id or ""))
    if not account:
        raise ValueError("账号不存在")
    return account


def get(server, path, query):
    db = server.db
    q = {key: values[0] for key, values in query.items() if values}
    if path == "/api/dashboard":
        return db.dashboard(q.get("account", ""))
    if path == "/api/records":
        return db.records(q)
    if path == "/api/logs":
        return db.system_logs(q)
    if path == "/api/admin/accounts":
        return db.admin_accounts()
    if path == "/api/config":
        return {**db.admin_config(), "archive_root": str(server.archive_root),
                "media_subdir": db.setting("media_subdir", "Instagram")}
    return None


def post(server, path, value):
    db = server.db
    if path == "/api/config":
        try:
            config = {key: int(value[key]) for key in (
                "creator_interval", "creator_max", "saved_interval", "saved_max",
                "saved_recent", "saved_enabled", "scheduler_enabled", "log_days")}
        except (KeyError, TypeError, ValueError):
            raise ValueError("系统配置缺少有效参数")
        if not (30 <= config["creator_interval"] <= 10080
                and 1 <= config["creator_max"] <= 200
                and 30 <= config["saved_interval"] <= 10080
                and 1 <= config["saved_max"] <= 200
                and config["saved_recent"] in (0, 1)
                and config["saved_enabled"] in (0, 1)
                and config["scheduler_enabled"] in (0, 1)
                and 1 <= config["log_days"] <= 365):
            raise ValueError("同步周期需为 30–10080 分钟，上限需为 1–200 条，日志保留需为 1–365 天")
        subdir = _safe_subdir(value.get("media_subdir", "Instagram"), server.archive_root)
        db.save_admin_config(config)
        db.set_setting("media_subdir", subdir)
        return {"ok": True}

    if path == "/api/records/delete":
        account = _account(db, value.get("account"))
        username = str(value.get("username", "")).strip().lower().lstrip("@") or None
        if username and not USERNAME.fullmatch(username):
            raise ValueError("博主用户名无效")
        ids = value.get("ids")
        if ids is not None and (not isinstance(ids, list) or not ids or len(ids) > 100):
            raise ValueError("请选择 1 到 100 条作品记录")
        with server.coordinator.lock:
            if account["id"] in server.coordinator.active:
                raise ValueError("该账号有任务正在运行，请等待完成后再删除记录")
            count = db.hide_records(account["id"], username=username,
                                    ids=[int(i) for i in ids] if ids is not None else None)
        return {"ok": True, "count": count, "files_deleted": 0}

    if path == "/api/records/restore":
        account = _account(db, value.get("account"))
        ids = value.get("ids")
        if not isinstance(ids, list) or not ids or len(ids) > 100:
            raise ValueError("请选择 1 到 100 条作品记录")
        with server.coordinator.lock:
            if account["id"] in server.coordinator.active:
                raise ValueError("该账号有任务正在运行，请等待完成后再恢复记录")
            count = db.hide_records(account["id"], ids=[int(i) for i in ids], restore=True)
        return {"ok": True, "count": count}

    if path == "/api/creator/delete":
        account = _account(db, value.get("account"))
        username = str(value.get("username", "")).strip().lower().lstrip("@")
        mode = value.get("mode")
        if not USERNAME.fullmatch(username) or mode not in ("list", "records"):
            raise ValueError("移除参数无效")
        if mode == "list":
            with server.coordinator.lock:
                if account["id"] in server.coordinator.active:
                    raise ValueError("该账号有任务正在运行，请等待完成后再移除博主")
                result = db.delete_creator(account["id"], username, delete_archive=False)
            if result is None:
                raise ValueError("博主不在监控列表")
            return {"ok": True, "removed_records": 0, "files_deleted": 0}
        count = db.hide_records(account["id"], username=username)
        return {"ok": True, "removed_records": count, "files_deleted": 0}

    if path == "/api/admin/account":
        account = _account(db, value.get("account"))
        fields = {}
        if "label" in value:
            label = str(value["label"]).strip()
            if not label or len(label) > 60:
                raise ValueError("账号备注为 1 到 60 个字符")
            fields["label"] = label
        if "enabled" in value:
            if not isinstance(value["enabled"], bool):
                raise ValueError("授权开关无效")
            fields["enabled"] = int(value["enabled"])
        for key in ("creator_subdir", "saved_subdir"):
            if key in value:
                fields[key] = _safe_subdir(value[key], server.archive_root)
        if not fields:
            raise ValueError("没有可保存的修改")
        with db.connect() as conn:
            conn.execute("UPDATE accounts SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                         (*fields.values(), account["id"]))
        return {"ok": True}

    if path == "/api/admin/account/remove-cookie":
        account = _account(db, value.get("account"))
        with server.coordinator.lock:
            if account["id"] in server.coordinator.active:
                raise ValueError("该账号有任务正在运行，请结束后再移除授权")
            cookie = Path(account["cookie_path"]).resolve()
            safe_root = (db.root / "accounts").resolve()
            with db.connect() as conn:
                conn.execute("UPDATE accounts SET enabled=0,cookie_status='removed',cookie_path='' WHERE id=?",
                             (account["id"],))
            if account["cookie_path"] and cookie.is_relative_to(safe_root):
                cookie.unlink(missing_ok=True)
        return {"ok": True}

    if path == "/api/account/check":
        account = _account(db, value.get("account"))
        return {"run_id": server.coordinator.start(account["id"], "check")}

    return None
