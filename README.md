# Pixeldrain Downloader

A small command-line tool to download [Pixeldrain](https://pixeldrain.com) files,
galleries and filesystem folders from the terminal — no browser required.

It fetches file metadata from the official Pixeldrain API and streams the actual
bytes through a community CDN proxy (`*.pixeldrain.eu.cc`), with automatic node
discovery, failover and **resume** support for large files.

## Features

- **Single files** (`/u/{id}`), **galleries** (`/l/{id}`) and **filesystem folders** (`/d/{id}`)
- **Sub-paths** — `…/d/{id}/some/subfolder` downloads just that subfolder
- **Recursive folders** — walks into subdirectories and keeps the structure on disk
- **Smart CDN discovery** — probes DNS for live numbered proxy nodes and uses them
  in random order (no time wasted hammering dead hosts)
- **Resume** — interrupted downloads continue from the partial `.part` file via HTTP Range
- **Failover** — if one proxy node fails, the next one is tried automatically
- Cross-platform Python; optional real-browser fallback via Selenium

## Requirements

- Python 3.7+
- Recommended: [`curl_cffi`](https://github.com/lexiforest/curl_cffi) for Chrome TLS
  impersonation (the proxy sits behind Cloudflare). Without it the tool falls back to
  the standard library, which Cloudflare will usually block.

```bash
pip install -r requirements.txt
```

## Usage

```bash
python pixeldrain_dl.py <URL or file id> [options]
```

Examples:

```bash
# single file
python pixeldrain_dl.py https://pixeldrain.com/u/abcd1234

# gallery into a folder
python pixeldrain_dl.py https://pixeldrain.com/l/xxxxx -o ./downloads

# a filesystem folder (recurses into subfolders)
python pixeldrain_dl.py https://pixeldrain.com/d/yyyyy

# only a specific subfolder of a folder
python pixeldrain_dl.py "https://pixeldrain.com/d/yyyyy/subfolder"

# several at once
python pixeldrain_dl.py URL1 URL2 URL3
```

Run with no arguments for an interactive prompt (handy when double-clicking the
`.bat` launchers on Windows).

### Options

| Option | Description |
|---|---|
| `-o, --out <dir>` | Output directory (default: current directory) |
| `--zip` | Download a gallery as a single zip (folders fall back to per-file) |
| `--print` | Print the resolved download URLs without downloading |
| `--browser` | Open the URL in the default browser instead of downloading directly |
| `--refresh` | Force-refresh the proxy list |
| `--proxy <url>` | Use a specific proxy instead of the auto-discovered list |

### Windows launchers

- `download.bat` — run the direct downloader (drag a URL onto it, or run and paste)
- `open-in-browser.bat` — open in the browser instead (`--browser`)
- `download-selenium.bat` — Selenium-based per-file fallback (`selenium_dl.py`)

## How it works

1. Fetch the proxy entry point (`cdn.pixeldrain.eu.cc`) from the bypass list and cache it for 24h.
2. The bare entry point doesn't serve files, so the tool **discovers the live numbered
   nodes** (`cdnN.pixeldrain.eu.cc`) that actually exist in DNS and uses them in random order.
3. Read file names / listings / subfolders from the official Pixeldrain API.
4. Stream each file from `node + /api/filesystem/...` (or `/api/file/...`), resuming from
   `.part` on failure and failing over to another node.

## Notes & disclaimer

- The CDN proxy is operated by a third party; availability and speed depend on it.
- Pixeldrain free transfer limits still apply per region/time window.
- Use this only to download your own uploads or files you are authorized to download,
  and in accordance with Pixeldrain's terms of service.
- Provided "as is" without warranty. See [`LICENSE`](LICENSE).
