#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pixeldrain per-file downloader (real Chrome automation)

Why this exists:
  Some proxies only allow a real browser, and opening a file URL directly only
  "plays" the media instead of downloading it.
  -> Drive a real Chrome instance and force each file to save to the download
     folder using the same-origin <a download> trick.
  Useful when a whole-folder zip is too big and you want per-file downloads.

This version:
  - Saves into a per-title subfolder
  - Per-file completion check + automatic retry on failure/stall (default 2)
  - Detects stalled downloads (.crdownload size not growing) and retries
  - Skips already-downloaded files (resume), aggregates success/failure counts

Requirements:
  - Google Chrome installed
  - pip install selenium      (Selenium 4.6+ manages the driver automatically)

Usage:
  python selenium_dl.py https://pixeldrain.com/d/XQWF8i8i -o D:\\save
  python selenium_dl.py https://pixeldrain.com/d/XQWF8i8i --limit 3     (test first 3)
  python selenium_dl.py https://pixeldrain.com/l/xxxxx --parallel 2
"""

import os
import sys
import time
import glob
import random
import argparse
from urllib.parse import quote

import pixeldrain_dl as pd  # reuse helpers from the main script in the same folder


def build_file_list(target, proxies):
    """Return (origin, title, [(href_path, filename, size), ...]).
    href_path is the proxy's API path (relative). e.g.:
      /api/filesystem/{id}/{name}   (file inside a folder)
      /api/file/{id}                (single file / gallery file)
    Being relative, it is resolved against whichever cdn origin (numbered node)
    is currently open."""
    parsed = pd.parse_target(target)
    if not parsed:
        raise SystemExit(f"Unrecognized link: {target}")
    kind, _id = parsed
    origin = proxies[0].rstrip("/")  # https://cdnNN.pixeldrain.eu.cc
    items = []

    if kind == "dir":
        qid = quote(_id, safe="/")          # may contain sub-path / spaces -> encode
        info = pd.http_json(f"{pd.PD_API}/filesystem/{qid}?stat")
        path = info.get("path") or []
        title = pd.sanitize(path[-1].get("name") if path else _id, _id)
        valid, single = pd._collect_dir_files(info)
        if single:
            name = pd.sanitize(path[-1].get("name"), _id)
            items.append((f"/api/filesystem/{qid}", name, path[-1].get("file_size")))
        else:
            def _walk(node_path, node_info):
                qp = quote(node_path, safe="/")
                v, _s = pd._collect_dir_files(node_info)
                for c in v:
                    nm = c.get("name") or ""
                    items.append((f"/api/filesystem/{qp}/{quote(nm, safe='')}",
                                  pd.sanitize(nm), c.get("file_size")))
                for d in pd._collect_dir_subdirs(node_info):
                    sub = d.get("name") or ""
                    sp = f"{node_path}/{sub}"
                    _walk(sp, pd.http_json(f"{pd.PD_API}/filesystem/{quote(sp, safe='/')}?stat"))
            _walk(_id, info)

    elif kind == "list":
        info = pd.http_json(f"{pd.PD_API}/list/{_id}")
        title = pd.sanitize(info.get("title"), _id)
        for f in (info.get("files") or []):
            fid = f.get("id")
            if fid:
                items.append((f"/api/file/{fid}", pd.sanitize(f.get("name"), fid), f.get("size")))

    else:  # single file
        try:
            fi = pd.http_json(f"{pd.PD_API}/file/{_id}/info")
            name = pd.sanitize(fi.get("name"), _id)
            size = fi.get("size")
        except Exception:
            name, size = _id, None
        items.append((f"/api/file/{_id}", name, size))
        title = name

    return origin, title, items


def start_chrome(download_dir):
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except Exception:
        print("\nselenium is not installed. Run in your terminal:")
        print("    pip install selenium")
        sys.exit(1)
    opts = Options()
    prefs = {
        "download.default_directory": os.path.abspath(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "profile.default_content_setting_values.automatic_downloads": 1,
        "plugins.always_open_pdf_externally": True,
    }
    opts.add_argument("--start-maximized")
    opts.add_argument("--incognito")
    opts.add_argument("--mute-audio")  # keep video silent while probing nodes
    opts.add_experimental_option("prefs", prefs)
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(options=opts)
    return driver


# Same-origin <a download> click -> force download
FORCE_DL_JS = """
const a = document.createElement('a');
a.href = arguments[0];
a.setAttribute('download', arguments[1] || '');
a.style.display = 'none';
document.body.appendChild(a);
a.click();
setTimeout(() => a.remove(), 1000);
return true;
"""


def active_downloads(download_dir):
    """Number of in-progress (.crdownload) downloads."""
    return len(glob.glob(os.path.join(download_dir, "*.crdownload")))


def _crdownload_for(download_dir, name):
    """Return the .crdownload path for this file (if any)."""
    direct = os.path.join(download_dir, name + ".crdownload")
    if os.path.exists(direct):
        return direct
    # Chrome may have renamed it, so also try a partial match.
    base, _ = os.path.splitext(name)
    for p in glob.glob(os.path.join(download_dir, "*.crdownload")):
        if base and base in os.path.basename(p):
            return p
    return None


def is_done(download_dir, name, expected_size=None):
    """Whether the file is already downloaded. If a file with the same name
    exists and is not in progress (.crdownload), it counts as present (size is
    not checked -- if it exists, it is skipped). Chrome only renames to the
    final name on completion, so final name present == complete/present."""
    path = os.path.join(download_dir, name)
    if not os.path.exists(path):
        return False
    if os.path.exists(path + ".crdownload"):
        return False
    return True


# Seconds of no progress before a file is considered stalled.
STALL_LIMIT = 60


def set_download_dir(driver, nodes, path):
    """Set each node tab's download folder to path."""
    for host, handle in nodes:
        try:
            driver.switch_to.window(handle)
            driver.execute_cdp_cmd("Page.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": os.path.abspath(path),
            })
        except Exception:
            pass


def download_multi(driver, download_dir, items, nodes, per_node=2, retries=2):
    """Download files spread across multiple node tabs concurrently. Returns
    (success, failure). If one node is throttled, others keep going so overall
    throughput is maintained. Tracks each file's completion/stall/no-start and
    retries on another node when it fails."""
    total = len(items)
    queue = []
    ok = fail = 0
    finished = 0

    # Skip already-downloaded files.
    for href, name, size in items:
        if is_done(download_dir, name, size):
            print(f"  already present, skipping: {name}")
            ok += 1
            finished += 1
        else:
            queue.append((href, name, size))

    attempts = {}                                  # name -> attempt count
    actives = {h: {} for h, _ in nodes}            # host -> {name: meta}
    handles = {h: handle for h, handle in nodes}

    def total_active():
        return sum(len(a) for a in actives.values())

    def begin_on(host, href, name, size):
        cr = _crdownload_for(download_dir, name)
        if cr:
            try:
                os.remove(cr)
            except OSError:
                pass
        try:
            driver.switch_to.window(handles[host])
            driver.execute_script(FORCE_DL_JS, href, name)
        except Exception as e:
            print(f"  click failed ({name}@{host}): {e}")
        actives[host][name] = {"href": href, "size": size, "start": time.time(),
                               "last_size": -1, "last_change": time.time()}
        attempts[name] = attempts.get(name, 0) + 1

    while queue or total_active():
        # Fill each node's free slots (round-robin).
        for host, _ in nodes:
            while len(actives[host]) < per_node and queue:
                href, name, size = queue.pop(0)
                print(f"  start [{host}]: {name}  ({pd.human(size)})")
                begin_on(host, href, name, size)
                time.sleep(0.5)

        now = time.time()
        for host, _ in nodes:
            for name in list(actives[host].keys()):
                a = actives[host][name]
                size = a["size"]
                if is_done(download_dir, name, size):
                    del actives[host][name]
                    ok += 1
                    finished += 1
                    print(f"\n  done [{finished}/{total}]: {name}")
                    continue
                cr = _crdownload_for(download_dir, name)
                if cr and os.path.exists(cr):
                    cur = os.path.getsize(cr)
                    if cur != a["last_size"]:
                        a["last_size"] = cur
                        a["last_change"] = now
                    elif now - a["last_change"] > STALL_LIMIT:
                        del actives[host][name]
                        if attempts[name] <= retries:
                            print(f"\n  stalled, retry {attempts[name]}/{retries}: {name}")
                            queue.append((a["href"], name, size))
                        else:
                            fail += 1
                            finished += 1
                            print(f"\n  !! failed (stalled): {name}")
                else:
                    if now - a["start"] > STALL_LIMIT:
                        del actives[host][name]
                        if attempts[name] <= retries:
                            print(f"\n  no start, retry {attempts[name]}/{retries}: {name}")
                            queue.append((a["href"], name, size))
                        else:
                            fail += 1
                            finished += 1
                            print(f"\n  !! failed (no start): {name}")

        act = total_active()
        if act:
            sys.stdout.write(f"\r  active {act} (nodes {len(nodes)})  queued {len(queue)}  "
                             f"done {finished}/{total}   ")
            sys.stdout.flush()
        time.sleep(1)

    sys.stdout.write("\n")
    return ok, fail


_PAGE_STATE_JS = r"""
const errEl = document.querySelector(
    '#main-frame-error, #main-message, .neterror, .error-code, #error-information-popup');
const title = (document.title || '');
const bodyText = document.body ? document.body.innerText.slice(0, 300) : '';
const cf = /just a moment|attention required|checking your browser/i
            .test(title + ' ' + bodyText);
return {
    host: location.hostname,
    err: !!errEl,
    cf: cf,
    ready: document.readyState,
    ct: document.contentType || '',
    bodyLen: document.body ? document.body.innerText.length : 0,
};
"""


def _page_state(driver):
    try:
        return driver.execute_script(_PAGE_STATE_JS)
    except Exception:
        return None


def quiet_page(driver):
    """Keep the tab (preserve same-origin) but stop any playing video/audio and
    cut the stream. Closing the tab would lose the same-origin <a download>
    context, so just stop playback instead of closing."""
    try:
        driver.execute_script("""
            document.querySelectorAll('video,audio').forEach(function(m){
                try{
                    m.pause();
                    m.muted = true;
                    m.removeAttribute('src');
                    while (m.firstChild) m.removeChild(m.firstChild);
                    m.load();   // abort the in-progress network stream
                }catch(e){}
            });
        """)
    except Exception:
        pass


def node_loads(driver, url, base, max_wait=20):
    """Navigate to url and check whether the page really loaded. Returns the
    actual hostname on success, else None.
    - Chrome 'connection failed' error pages fail immediately (try next node).
    - While Cloudflare is checking, keep waiting."""
    try:
        driver.get(url)
    except Exception:
        pass
    deadline = time.time() + max_wait
    while time.time() < deadline:
        st = _page_state(driver)
        if st:
            host = st.get("host") or ""
            if base in host:
                if st.get("err"):
                    return None  # connection-failed page -> next node now
                if st.get("cf"):
                    time.sleep(2)
                    continue     # wait for Cloudflare to pass
                ct = st.get("ct") or ""
                media = ct.startswith(("video", "image", "audio", "application"))
                if st.get("ready") == "complete" and (media or st.get("bodyLen", 0) > 0):
                    return host
        time.sleep(1)
    return None


def _hint_to_host(hint, base):
    """Normalize 'cdn47' or a full URL into a hostname."""
    from urllib.parse import urlparse
    nh = (hint or "").strip()
    if not nh:
        return None
    if "://" in nh or "/" in nh:
        nh = urlparse(nh if "://" in nh else "https://" + nh).netloc
    if nh and "." not in nh:        # given a bare number like 'cdn47'
        nh = f"{nh}.{base}"
    return nh or None


def candidate_hosts(origin, node_hints, max_nodes=80):
    """List of cdn host candidates to try (hints first, then live numbered
    nodes). Only numbered nodes that actually resolve in DNS are used, in
    random order (so the browser doesn't waste timeouts on hosts that don't
    exist)."""
    from urllib.parse import urlparse
    host = urlparse(origin).netloc                          # cdn.pixeldrain.eu.cc
    base = host.split(".", 1)[1] if "." in host else host   # pixeldrain.eu.cc
    cands = []
    for hint in (node_hints or []):
        h = _hint_to_host(hint, base)
        if h:
            cands.append(h)
    if host.split(".")[0] == "cdn":     # un-numbered entry point -> auto-discover live nodes
        live = pd.discover_cdn_nodes(host, probe=range(1, max_nodes + 1))
        random.shuffle(live)
        cands += live
    else:
        cands.append(host)
    # Dedupe (preserve order).
    seen, out = set(), []
    for h in cands:
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out, base


def find_working_nodes(driver, origin, first_href, want, node_hints, max_wait=12):
    """Find `want` working cdn nodes (testing one at a time in the current tab).
    Each node is returned as (host, window_handle) -- a separate tab per node."""
    cands, base = candidate_hosts(origin, node_hints)
    nodes = []          # (host, handle)
    first_handle = driver.current_window_handle
    for h in cands:
        if len(nodes) >= want:
            break
        # Test the first node in the starting tab, the rest in new tabs.
        if nodes:
            driver.switch_to.new_window("tab")
        else:
            driver.switch_to.window(first_handle)
        print(f"  checking node: {h} ...")
        got = node_loads(driver, f"https://{h}{first_href}", base, max_wait=max_wait)
        if got:
            quiet_page(driver)
            print(f"   -> using ({got})")
            nodes.append((got, driver.current_window_handle))
        else:
            # Close the failed tab if it isn't the starting tab.
            if driver.current_window_handle != first_handle:
                driver.close()
                driver.switch_to.window(first_handle)
    return nodes, base


def main():
    ap = argparse.ArgumentParser(description="Pixeldrain per-file downloader (Chrome automation)")
    ap.add_argument("urls", nargs="*", help="Pixeldrain URL")
    ap.add_argument("-o", "--out", default=os.getcwd(),
                    help="output folder (default: current; a per-title subfolder is created)")
    ap.add_argument("--limit", type=int, default=0, help="download only the first N files (for testing)")
    ap.add_argument("--nodes", type=int, default=3,
                    help="number of cdn nodes (tabs) to use concurrently (default 3; avoids per-node throttle)")
    ap.add_argument("--parallel", type=int, default=2,
                    help="concurrent downloads per node (default 2). total = nodes x parallel")
    ap.add_argument("--retries", type=int, default=2, help="retries per file (default 2)")
    ap.add_argument("--flat", action="store_true",
                    help="save directly into -o without creating a per-title subfolder")
    ap.add_argument("--proxy", help="use a specific proxy")
    ap.add_argument("--node", help="specify a working cdn node (e.g. cdn47)")
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print("Pixeldrain Selenium Downloader (Chrome automation) -- per-file download")
    proxies = [pd.normalize_proxy(args.proxy)] if args.proxy else pd.load_proxies()
    print(f"  proxy: {proxies[0]}")

    urls = args.urls
    if not urls:
        print("\nEnter Pixeldrain URL(s) (empty line to start):")
        urls = []
        try:
            while True:
                line = input("> ").strip()
                if not line:
                    break
                urls.append(line)
        except EOFError:
            pass
    if not urls:
        print("No URL. Exiting.")
        return

    # Collect the file list for every URL (keep groups per URL).
    groups = []  # (title, items)
    origin = None
    for u in urls:
        origin, title, items = build_file_list(u, proxies)
        print(f"\n[{title}] {len(items)} files")
        groups.append((title, items))

    base_out = os.path.abspath(args.out)

    # Working-node hints (e.g. cdn47). Comma-separated for several. Empty = auto.
    node_hints = []
    if args.node:
        node_hints = [h.strip() for h in args.node.split(",") if h.strip()]
    elif not args.proxy:
        try:
            raw = input(
                "\nIf you know a working cdn node, enter it (e.g. cdn47, or cdn47,cdn12 "
                "for several); press Enter to auto-discover: ").strip()
        except EOFError:
            raw = ""
        node_hints = [h.strip() for h in raw.split(",") if h.strip()]

    # A temporary initial download folder -> start Chrome.
    os.makedirs(base_out, exist_ok=True)
    driver = start_chrome(base_out)

    # Open several node tabs to spread downloads (avoids per-node throttling).
    all_items = [it for _, items in groups for it in items]

    total_ok = total_fail = 0
    try:
        print(f"\nFinding {args.nodes} proxy node(s)... "
              "(if Chrome shows a Cloudflare check, wait for it to pass)")
        nodes, base = find_working_nodes(driver, origin, all_items[0][0],
                                         args.nodes, node_hints)
        if not nodes:
            print("\n!! Could not find a working node.")
            print("   In Chrome's address bar, enter a working URL")
            print("   (e.g. https://cdn47.pixeldrain.eu.cc/api/filesystem/...), confirm the page")
            print("   loads, then press Enter.")
            input("   (press Enter to continue using the currently open page) ")
            quiet_page(driver)
            nodes = [(driver.execute_script("return location.hostname;") or "current-tab",
                      driver.current_window_handle)]
        print(f"  using nodes: {', '.join(h for h, _ in nodes)}")

        for title, items in groups:
            # Reuse an existing title folder if present (for resume).
            download_dir = base_out if args.flat else os.path.join(base_out, title)
            os.makedirs(download_dir, exist_ok=True)
            set_download_dir(driver, nodes, download_dir)

            sub_items = items[:args.limit] if args.limit > 0 else items
            total = len(sub_items)
            print(f"\n===== [{title}] {total} files -> {download_dir} "
                  f"(nodes {len(nodes)} x {args.parallel}) =====")

            # href is relative (/api/...) -> resolved against each tab's node origin (same-origin).
            ok, fail = download_multi(driver, download_dir, sub_items, nodes,
                                      per_node=args.parallel, retries=args.retries)
            total_ok += ok
            total_fail += fail

        print(f"\nAll done: {total_ok} succeeded, {total_fail} failed")
    finally:
        try:
            input("\nPress Enter to close Chrome... ")
        except EOFError:
            pass
        try:
            driver.quit()
        except Exception:
            pass

    print(f"Saved to: {base_out}")


if __name__ == "__main__":
    main()
