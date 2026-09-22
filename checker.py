import os
import sys
import time
import json
import shutil
import signal
import subprocess
from pathlib import Path
from urllib.parse import quote

import requests
import yaml

RUNTIME = Path(os.environ.get("RUNTIME_DIR", "/work/runtime"))
RUNTIME.mkdir(parents=True, exist_ok=True)

SUB_URL = os.environ["SUB_URL"]
TEST_URLS = [u.strip() for u in os.environ.get("TEST_URLS", "http://connectivitycheck.platform.hicloud.com/generate_204").split(",") if u.strip()]
TIMEOUT = int(os.environ.get("TEST_TIMEOUT_MS", "8000"))
MAX_DELAY = int(os.environ.get("MAX_DELAY_MS", "0"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "1800"))
MIHOMO_API = os.environ.get("MIHOMO_API", "http://mihomo-check:9090")
MIHOMO_SECRET = os.environ.get("MIHOMO_SECRET", "")
OUTPUT_FILE = Path(os.environ.get("OUTPUT_FILE", "/work/output/filtered.yaml"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "/work/output/last_result.json"))
STARTUP_WAIT = int(os.environ.get("MIHOMO_STARTUP_WAIT", "20"))
LOOP = os.environ.get("LOOP", "true").lower() == "true"
VERIFY_TLS = os.environ.get("VERIFY_TLS", "true").lower() == "true"
DOWNLOAD_HEADERS = os.environ.get("DOWNLOAD_HEADERS", "")
KEEP_PATTERNS = [s.strip() for s in os.environ.get("KEEP_NAME_REGEX", "").split("||") if s.strip()]

OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
if MIHOMO_SECRET:
    SESSION.headers.update({"Authorization": f"Bearer {MIHOMO_SECRET}"})


def log(msg: str):
    print(time.strftime("[%F %T]"), msg, flush=True)


def make_download_headers():
    headers = {}
    raw = DOWNLOAD_HEADERS.strip()
    if not raw:
        return headers
    for part in raw.split("||"):
        if not part.strip() or "=" not in part:
            continue
        k, v = part.split("=", 1)
        headers[k.strip()] = v.strip()
    return headers


def fetch_subscription() -> dict:
    headers = make_download_headers()
    log(f"下载订阅: {SUB_URL}")
    r = requests.get(SUB_URL, headers=headers, timeout=60, verify=VERIFY_TLS)
    r.raise_for_status()
    text = r.text
    try:
        data = yaml.safe_load(text)
    except Exception as e:
        raise RuntimeError(f"订阅不是合法 YAML: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
        raise RuntimeError("订阅内容里没有 proxies: 列表。请让 Sub-Store 输出 Clash/Mihomo YAML，而不是 URI/Base64。")
    return data


def build_runtime_config(proxies: list):
    cfg = {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "Rule",
        "log-level": "warning",
        "ipv6": True,
        "unified-delay": True,
        "tcp-concurrent": True,
        "external-controller": "0.0.0.0:9090",
        "secret": MIHOMO_SECRET,
        "proxies": proxies,
        "proxy-groups": [
            {"name": "节点选择", "type": "select", "proxies": [p.get("name", f"node-{i}") for i, p in enumerate(proxies)] + ["DIRECT"]}
        ],
        "rules": ["MATCH,DIRECT"],
        "dns": {
            "enable": True,
            "ipv6": True,
            "default-nameserver": ["223.5.5.5", "1.1.1.1"],
            "nameserver": ["223.5.5.5", "1.1.1.1"],
            "proxy-server-nameserver": ["223.5.5.5", "1.1.1.1"],
        },
    }
    with open(RUNTIME / "config.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def api_get(path: str, **kwargs):
    url = MIHOMO_API.rstrip("/") + path
    return SESSION.get(url, timeout=kwargs.pop("timeout", 15), **kwargs)


def wait_api_ready():
    last_err = None
    for _ in range(STARTUP_WAIT):
        try:
            r = api_get("/version", timeout=3)
            if r.ok:
                return
        except Exception as e:
            last_err = e
        time.sleep(1)
    raise RuntimeError(f"mihomo API 未就绪: {last_err}")


def test_proxy(name: str):
    encoded = quote(name, safe="")
    best = None
    hit_url = None
    errors = []
    for url in TEST_URLS:
        try:
            r = api_get(f"/proxies/{encoded}/delay", params={"url": url, "timeout": TIMEOUT}, timeout=(TIMEOUT / 1000) + 5)
            if not r.ok:
                errors.append(f"{url} -> HTTP {r.status_code} {r.text[:200]}")
                continue
            data = r.json()
            delay = int(data.get("delay", 0) or 0)
            if delay > 0 and (MAX_DELAY <= 0 or delay <= MAX_DELAY):
                if best is None or delay < best:
                    best = delay
                    hit_url = url
        except Exception as e:
            errors.append(f"{url} -> {e}")
    return best, hit_url, errors


def should_force_keep(name: str) -> bool:
    if not KEEP_PATTERNS:
        return False
    import re
    for pattern in KEEP_PATTERNS:
        try:
            if re.search(pattern, name):
                return True
        except re.error:
            pass
    return False


def run_once():
    raw = fetch_subscription()
    proxies = raw["proxies"]
    if not proxies:
        raise RuntimeError("订阅里 proxies 为空")

    names = []
    seen = set()
    deduped = []
    for i, p in enumerate(proxies):
        name = str(p.get("name") or f"node-{i}")
        base = name
        n = 2
        while name in seen:
            name = f"{base}#{n}"
            n += 1
        if name != p.get("name"):
            p = dict(p)
            p["name"] = name
        seen.add(name)
        names.append(name)
        deduped.append(p)

    build_runtime_config(deduped)
    wait_api_ready()

    keep = []
    details = []

    for p in deduped:
        name = p["name"]
        if should_force_keep(name):
            keep.append(p)
            details.append({"name": name, "kept": True, "forced": True, "delay": None, "url": None, "errors": []})
            log(f"[KEEP][FORCED] {name}")
            continue

        delay, hit_url, errors = test_proxy(name)
        if delay is not None:
            keep.append(p)
            details.append({"name": name, "kept": True, "forced": False, "delay": delay, "url": hit_url, "errors": []})
            log(f"[KEEP] {name} delay={delay} url={hit_url}")
        else:
            details.append({"name": name, "kept": False, "forced": False, "delay": None, "url": None, "errors": errors})
            log(f"[DROP] {name} errors={'; '.join(errors[:2])}")

    output = {"proxies": keep}
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(output, f, allow_unicode=True, sort_keys=False)

    state = {
        "time": time.strftime("%F %T"),
        "source_url": SUB_URL,
        "test_urls": TEST_URLS,
        "timeout_ms": TIMEOUT,
        "max_delay_ms": MAX_DELAY,
        "total": len(deduped),
        "kept": len(keep),
        "dropped": len(deduped) - len(keep),
        "details": details,
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    latest_link = OUTPUT_FILE.parent / "latest.yaml"
    if latest_link.exists() or latest_link.is_symlink():
        latest_link.unlink()
    latest_link.symlink_to(OUTPUT_FILE.name)

    log(f"完成: 保留 {len(keep)}/{len(deduped)}，输出 {OUTPUT_FILE}")


def main():
    while True:
        try:
            run_once()
        except Exception as e:
            log(f"运行失败: {e}")
        if not LOOP:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
