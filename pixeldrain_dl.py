#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pixeldrain Local Downloader (proxy bypass)

How it works:
  Fetches the gamedrive.org proxy list (proxy.json), the same way the
  Tampermonkey userscript does, and downloads Pixeldrain files through the
  proxy. File names / listings and other metadata come from the official
  Pixeldrain API.

Supported link forms:
  - https://pixeldrain.com/u/{id}     single file
  - https://pixeldrain.com/l/{id}     gallery (list / album)
  - https://pixeldrain.com/d/{id}     folder (filesystem bucket)
  - the .net / .dev / pixeldra.in domains are also accepted
  - a bare file id is treated as a single file

Examples:
  python pixeldrain_dl.py https://pixeldrain.com/u/abcd1234
  python pixeldrain_dl.py https://pixeldrain.com/l/xxxxx -o C:\\Downloads
  python pixeldrain_dl.py https://pixeldrain.com/d/yyyyy --zip
  python pixeldrain_dl.py            (run with no args for interactive input)

Recommended: pip install curl_cffi   (Chrome TLS impersonation -> passes the
       proxy's Cloudflare check). Falls back to the standard library if it is
       not installed. Python 3.7+
"""

import os
import re
import sys
import json
import time
import socket
import random
import argparse
import urllib.request
import urllib.error
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse

# Use curl_cffi (Chrome TLS impersonation) if available -- needed when the proxy
# inspects the TLS fingerprint (Cloudflare).
try:
    from curl_cffi import requests as _cffi
    _IMPERSONATE = "chrome"
    HAVE_CFFI = True
except Exception:
    _cffi = None
    HAVE_CFFI = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROXY_JSON_URL = "https://pixeldrain-bypass.gamedrive.org/api/proxy.json"
PD_API = "https://pixeldrain.com/api"
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pd_proxy_cache.json")
CACHE_TTL = 24 * 60 * 60  # 24 hours
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PD_HOSTS = ("pixeldrain.com", "pixeldrain.net", "pixeldrain.dev", "pixeldra.in")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _request(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def http_json(url, timeout=30):
    if HAVE_CFFI:
        r = _cffi.get(url, impersonate=_IMPERSONATE, timeout=timeout,
                      headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        return r.json()
    with _request(url, timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# Proxy list
# ---------------------------------------------------------------------------
def normalize_proxy(entry):
    if not entry or not isinstance(entry, str):
        return None
    entry = entry.strip()
    if not entry:
        return None
    if not re.match(r"^https?://", entry, re.I):
        entry = "https://" + entry
    return entry if entry.endswith("/") else entry + "/"


def _load_proxy_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("ts", 0) < CACHE_TTL:
            lst = data.get("proxies") or []
            if lst:
                return lst
    except Exception:
        pass
    return None


def _save_proxy_cache(proxies):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "proxies": proxies}, f)
    except Exception:
        pass


def load_proxies(force_refresh=False):
    if not force_refresh:
        cached = _load_proxy_cache()
        if cached:
            return cached
    try:
        data = http_json(PROXY_JSON_URL)
    except Exception as e:
        cached = _load_proxy_cache()
        if cached:
            print("  (failed to refresh proxy list, using cache)")
            return cached
        raise SystemExit(f"Could not fetch the proxy list: {e}")

    raw = []
    if isinstance(data, dict) and isinstance(data.get("proxies"), list):
        raw = data["proxies"]
    elif isinstance(data, dict) and isinstance(data.get("proxy"), str):
        raw = [data["proxy"]]
    elif isinstance(data, list):
        raw = data

    proxies = [p for p in (normalize_proxy(x) for x in raw) if p]
    if not proxies:
        raise SystemExit("No usable proxies found.")
    _save_proxy_cache(proxies)
    return proxies


# ---------------------------------------------------------------------------
# CDN node discovery
#   proxy.json only returns a single un-numbered entry point (cdn.<base>). That
#   entry point does NOT serve files (it resets the connection); only the
#   numbered nodes (cdnNN.<base>) actually work. On top of that, only a subset
#   of the numbered nodes exist in DNS (e.g. 10..50), so blindly hitting
#   cdn1..cdnN in order wastes time on hosts that don't resolve.
#   -> Probe DNS and use only the numbered nodes that actually exist.
# ---------------------------------------------------------------------------
CDN_PROBE_RANGE = range(1, 81)  # consider cdn1 .. cdn80 as candidates


def _host_resolves(host):
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return True
    except OSError:
        return False


def discover_cdn_nodes(entry_host, probe=CDN_PROBE_RANGE, max_workers=40):
    """From a 'cdn.<base>' entry point, return the numbered nodes (cdnN.<base>)
    that actually resolve in DNS."""
    parts = entry_host.split(".", 1)
    base = parts[1] if len(parts) == 2 else entry_host
    cands = [f"cdn{i}.{base}" for i in probe]
    live = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for host, ok in zip(cands, ex.map(_host_resolves, cands)):
            if ok:
                live.append(host)
    return live


def expand_proxies(proxies):
    """Expand an un-numbered cdn entry point into the live numbered nodes
    (shuffled). If no numbered node is found, keep the original entry point
    (safety net for offline use)."""
    out, seen = [], set()
    for p in proxies:
        host = urlparse(p).netloc
        if host.split(".")[0] == "cdn":          # 'cdn.<base>' entry point
            nodes = discover_cdn_nodes(host)
            if nodes:
                random.shuffle(nodes)
                for h in nodes:
                    u = f"https://{h}/"
                    if u not in seen:
                        seen.add(u)
                        out.append(u)
                continue
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out or proxies


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------
def parse_target(s):
    """Extract (kind, id) from the input string. kind = file|list|dir"""
    s = s.strip().strip('"').strip("'")
    if not s:
        return None

    # Full URL.
    #   /d/ (folder) keeps the sub-path. e.g. /d/ZVapKWEh/AV -> "ZVapKWEh/AV"
    #   /u/, /l/ use the id only.
    m = re.search(r"pixeldra(?:in\.(?:com|net|dev)|\.in)/(u|l|d)/([^?#]+)", s, re.I)
    if m:
        kind = {"u": "file", "l": "list", "d": "dir"}[m.group(1).lower()]
        val = m.group(2).strip("/")
        if kind != "dir":
            val = val.split("/")[0]
        return kind, val

    # /api/file/{id}
    m = re.search(r"/api/file/(\w+)", s)
    if m:
        return "file", m.group(1)

    # A bare alphanumeric id -> treat as a single file.
    if re.fullmatch(r"[A-Za-z0-9]+", s):
        return "file", s

    return None


# ---------------------------------------------------------------------------
# File name handling
# ---------------------------------------------------------------------------
_WIN_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name, fallback="download"):
    name = (name or "").strip()
    name = _WIN_INVALID.sub("_", name)
    name = name.rstrip(". ")
    return name or fallback


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while True:
        cand = f"{base} ({i}){ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


def human(n):
    if n is None or n < 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}PB"


# ---------------------------------------------------------------------------
# Download (proxy failover + progress)
# ---------------------------------------------------------------------------
def _download_once(url, dest, expected_size=None):
    tmp = dest + ".part"
    # Resume: if a partial .part already exists, continue from there via Range.
    existing = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    if expected_size and existing >= expected_size > 0:
        os.replace(tmp, dest)           # already complete
        return existing

    start = time.time()
    headers = {"User-Agent": USER_AGENT}
    if existing > 0:
        headers["Range"] = f"bytes={existing}-"
    downloaded = existing

    if HAVE_CFFI:
        # curl_cffi's streaming response is not a context manager, so close()
        # it explicitly.
        resp = _cffi.get(url, impersonate=_IMPERSONATE, stream=True, timeout=120,
                         headers=headers)
        try:
            resp.raise_for_status()
            resuming = resp.status_code == 206     # server accepted the resume
            if not resuming:
                downloaded = 0
            total = expected_size
            clen = resp.headers.get("Content-Length")
            if clen and str(clen).isdigit():
                total = downloaded + int(clen) if resuming else int(clen)
            with open(tmp, "ab" if resuming else "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 128):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    _print_progress(downloaded, total, start)
        finally:
            resp.close()
    else:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            resuming = getattr(resp, "status", 200) == 206
            if not resuming:
                downloaded = 0
            total = expected_size
            clen = resp.headers.get("Content-Length")
            if clen and str(clen).isdigit():
                total = downloaded + int(clen) if resuming else int(clen)
            with open(tmp, "ab" if resuming else "wb") as f:
                while True:
                    chunk = resp.read(1024 * 128)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    _print_progress(downloaded, total, start)

    sys.stdout.write("\n")
    os.replace(tmp, dest)
    return downloaded


def _print_progress(done, total, start):
    elapsed = max(time.time() - start, 1e-6)
    speed = done / elapsed
    if total:
        pct = done / total * 100
        bar_len = 28
        filled = int(bar_len * done / total)
        bar = "#" * filled + "-" * (bar_len - filled)
        sys.stdout.write(f"\r  [{bar}] {pct:5.1f}%  {human(done)}/{human(total)}  {human(speed)}/s   ")
    else:
        sys.stdout.write(f"\r  {human(done)}  {human(speed)}/s   ")
    sys.stdout.flush()


PRINT_ONLY = False  # --print: only print the resolved URL, do not download


def download(suffix, dest, proxies, expected_size=None):
    """Try each proxy in turn, appending suffix. Returns True on success."""
    if PRINT_ONLY:
        # Only print the resolved URL (using the first proxy); no download.
        print(f"  {proxies[0] + suffix}    [{os.path.basename(dest)}  {human(expected_size)}]")
        return True
    dest = unique_path(dest)
    order = proxies[:]
    random.shuffle(order)
    last_err = None
    for i, proxy in enumerate(order, 1):
        url = proxy + suffix
        host = urlparse(proxy).netloc
        print(f"  -> {os.path.basename(dest)}  (node {host})")
        try:
            _download_once(url, dest, expected_size)
            return True
        except KeyboardInterrupt:
            raise
        except Exception as e:
            last_err = e
            print(f"     failed: {e}  resuming on next node...")
            # Keep the .part file -> the next node / next run resumes from it.
    print(f"  !! all nodes failed: {last_err}  (.part kept: run again to resume)")
    return False


# ---------------------------------------------------------------------------
# Per-kind handlers
# ---------------------------------------------------------------------------
def handle_file(fid, outdir, proxies):
    try:
        info = http_json(f"{PD_API}/file/{fid}/info")
        name = sanitize(info.get("name"), fid)
        size = info.get("size")
    except Exception:
        name, size = fid, None
    print(f"\n[file] {name}  ({human(size)})")
    ok = download(f"api/file/{fid}", os.path.join(outdir, name), proxies, size)
    return 1 if ok else 0, 0 if ok else 1


def handle_list(lid, outdir, proxies, as_zip=False):
    info = http_json(f"{PD_API}/list/{lid}")
    title = sanitize(info.get("title"), lid)
    files = info.get("files") or []
    print(f"\n[gallery] {title}  ({len(files)} files)")

    if as_zip:
        dest = os.path.join(outdir, title + ".zip")
        ok = download(f"api/list/{lid}/zip", dest, proxies)
        return (1, 0) if ok else (0, 1)

    folder = unique_path(os.path.join(outdir, title))
    if not PRINT_ONLY:
        os.makedirs(folder, exist_ok=True)
    succ = fail = 0
    for f in files:
        fid = f.get("id")
        if not fid:
            continue
        name = sanitize(f.get("name"), fid)
        if download(f"api/file/{fid}", os.path.join(folder, name), proxies, f.get("size")):
            succ += 1
        else:
            fail += 1
    return succ, fail


def _collect_dir_files(node):
    """From a filesystem stat result, return (list of file children, is_single)."""
    children = node.get("children") or []
    valid = [c for c in children
             if c.get("type") == "file" and not str(c.get("name", "")).endswith(".search_index.gz")]
    # No children and the last path entry is a file -> single-file bucket.
    path = node.get("path") or []
    single = False
    if not valid and path:
        last = path[-1]
        if last.get("type") == "file":
            single = True
    return valid, single


def _collect_dir_subdirs(node):
    """From a filesystem stat result, return the list of subdirectory children."""
    children = node.get("children") or []
    return [c for c in children if c.get("type") == "dir"]


def handle_dir(did, outdir, proxies, as_zip=False):
    # did is 'bucket' or 'bucket/sub/path' (may contain spaces) -> URL-encode.
    qdid = quote(did, safe="/")
    info = http_json(f"{PD_API}/filesystem/{qdid}?stat")
    path = info.get("path") or []
    dirname = sanitize(path[-1].get("name") if path else did, did)
    valid, single = _collect_dir_files(info)
    subdirs = _collect_dir_subdirs(info)

    if single:
        name = sanitize(path[-1].get("name"), did)
        size = path[-1].get("file_size")
        print(f"\n[folder/single file] {name}  ({human(size)})")
        ok = download(f"api/filesystem/{qdid}", os.path.join(outdir, name), proxies, size)
        return (1, 0) if ok else (0, 1)

    if as_zip:
        # The current cdn proxy (pixeldrain.eu.cc) has no whole-folder ZIP
        # endpoint (filesystem folders have none) -> download per file instead.
        print("  (note: folder ZIP is not supported by the current proxy -> downloading per file)")

    print(f"\n[folder] {dirname}  ({len(valid)} files, {len(subdirs)} subfolders)")
    folder = unique_path(os.path.join(outdir, dirname))
    if not PRINT_ONLY:
        os.makedirs(folder, exist_ok=True)
    succ = fail = 0
    for c in valid:
        name = c.get("name") or ""
        encoded = quote(name, safe="")
        suffix = f"api/filesystem/{qdid}/{encoded}"
        if download(suffix, os.path.join(folder, sanitize(name)), proxies, c.get("file_size")):
            succ += 1
        else:
            fail += 1
    # Recurse into subfolders (keeping the folder structure on disk).
    for d in subdirs:
        sub = d.get("name") or ""
        s2, f2 = handle_dir(f"{did}/{sub}", folder, proxies, as_zip)
        succ += s2
        fail += f2
    return succ, fail


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def open_browser(target, proxies):
    """For when the proxy only allows a real browser: open the resolved URL in
    the default browser and let Chrome download it directly. Galleries are
    fetched as a single zip."""
    parsed = parse_target(target)
    if not parsed:
        print(f"Unrecognized link: {target}")
        return 0, 1
    kind, _id = parsed
    proxy = proxies[0]

    if kind == "dir":
        # No whole-folder ZIP, so open the official folder page (pick files there).
        url = f"https://pixeldrain.com/d/{_id}"
        label = "folder page (for per-file download, run without --browser)"
    elif kind == "list":
        url = proxy + f"api/list/{_id}/zip"   # whole gallery -> single zip
        label = "whole gallery ZIP"
    else:
        url = proxy + f"api/file/{_id}?download"  # force single-file download
        label = "single file"

    print(f"\n[open in browser] {label}")
    print(f"  {url}")
    try:
        webbrowser.open(url)
        print("  -> the download should start in your default browser.")
        if kind == "file":
            print("  (if it only plays, use the 'download' item in the player's menu)")
        return 1, 0
    except Exception as e:
        print(f"  Failed to open browser: {e}")
        print(f"  Copy this URL into your browser's address bar:\n  {url}")
        return 0, 1


def run(target, outdir, as_zip, proxies):
    parsed = parse_target(target)
    if not parsed:
        print(f"Unrecognized link: {target}")
        return 0, 1
    kind, _id = parsed
    if not PRINT_ONLY:
        os.makedirs(outdir, exist_ok=True)
    if kind == "file":
        return handle_file(_id, outdir, proxies)
    if kind == "list":
        return handle_list(_id, outdir, proxies, as_zip)
    if kind == "dir":
        return handle_dir(_id, outdir, proxies, as_zip)
    return 0, 1


def main():
    ap = argparse.ArgumentParser(
        description="Pixeldrain local downloader (proxy bypass)")
    ap.add_argument("urls", nargs="*", help="Pixeldrain URL or file id (multiple allowed)")
    ap.add_argument("-o", "--out", default=os.getcwd(), help="output directory (default: current)")
    ap.add_argument("--zip", action="store_true",
                    help="download a gallery/folder as a single ZIP")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="print the resolved download URLs without downloading")
    ap.add_argument("--browser", action="store_true",
                    help="open the resolved URL in the default browser instead of "
                         "downloading directly (use when the proxy only allows a real "
                         "browser; galleries become a single zip)")
    ap.add_argument("--refresh", action="store_true", help="force-refresh the proxy list")
    ap.add_argument("--proxy", help="use a specific proxy instead of the list (e.g. https://my.proxy/)")
    # Force UTF-8 output so non-ASCII names don't break on legacy consoles.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = ap.parse_args()

    global PRINT_ONLY
    PRINT_ONLY = args.print_only

    print("Pixeldrain Local Downloader")
    if HAVE_CFFI:
        print("  engine: curl_cffi (Chrome TLS impersonation) [OK]")
    else:
        print("  engine: urllib (stdlib) -- if the proxy blocks you, run 'pip install curl_cffi'")
    if args.proxy:
        proxies = [normalize_proxy(args.proxy)]
        print(f"  proxy (explicit): {proxies[0]}")
    else:
        print("  fetching proxy list...")
        proxies = load_proxies(force_refresh=args.refresh)
        print(f"  {len(proxies)} proxy entry point(s)")
    # Expand the un-numbered cdn entry point into the live numbered nodes
    # (the ones that can actually serve downloads).
    print("  discovering cdn nodes...")
    proxies = expand_proxies(proxies)
    print(f"  {len(proxies)} usable cdn node(s)")

    urls = args.urls
    if not urls:
        # Interactive mode (for double-click launches).
        print("\nEnter Pixeldrain URL(s) (one per line, empty line to start):")
        lines = []
        try:
            while True:
                line = input("> ").strip()
                if not line:
                    break
                lines.append(line)
        except EOFError:
            pass
        urls = lines

    if not urls:
        print("No URL provided. Exiting.")
        return

    if args.browser:
        print("  mode: open in browser (Chrome downloads directly)")

    total_ok = total_fail = 0
    for u in urls:
        try:
            if args.browser:
                ok, fail = open_browser(u, proxies)
            else:
                ok, fail = run(u, args.out, args.zip, proxies)
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
            break
        except urllib.error.HTTPError as e:
            print(f"  HTTP error: {e}")
            ok, fail = 0, 1
        except Exception as e:
            print(f"  error: {e}")
            ok, fail = 0, 1
        total_ok += ok
        total_fail += fail

    print(f"\nDone: {total_ok} succeeded, {total_fail} failed")
    if args.browser:
        print("Downloads run in the browser; files go to your browser's download folder.")
    else:
        print(f"Saved to: {os.path.abspath(args.out)}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
    # Keep the window open when double-clicked with no arguments.
    if sys.stdin and sys.stdin.isatty() and len(sys.argv) <= 1:
        try:
            input("\nPress Enter to exit...")
        except EOFError:
            pass
