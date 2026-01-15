#!/usr/bin/env python3
"""
Minimal MEGA download + Telegram prep using Mega SDK with progress and concurrency.
Requires: megasdk (imported as "mega")
"""

import argparse
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from mega import MegaApi, MegaListener, MegaRequest

CHUNK_SIZE = 4 * 1024 * 1024


def is_folder_link(link):
    return "folder" in link or "/#F!" in link


def shorten(text, max_len=32):
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def format_bytes(num):
    if num is None or num < 0:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


class DownloadStats:
    def __init__(self, label):
        self.label = label
        self.name = ""
        self.bytes_done = 0
        self.total_bytes = 0
        self.speed = 0
        self.state = "pending"
        self.error = ""
        self._lock = threading.Lock()

    def set_name(self, name):
        with self._lock:
            self.name = name or self.name

    def set_state(self, state):
        with self._lock:
            self.state = state

    def set_error(self, error):
        with self._lock:
            self.error = str(error)
            self.state = "error"

    def update(self, bytes_done=None, total_bytes=None, speed=None):
        with self._lock:
            if bytes_done is not None:
                self.bytes_done = bytes_done
            if total_bytes is not None and total_bytes > 0:
                self.total_bytes = total_bytes
            if speed is not None:
                self.speed = speed

    def snapshot(self):
        with self._lock:
            return {
                "label": self.label,
                "name": self.name,
                "bytes_done": self.bytes_done,
                "total_bytes": self.total_bytes,
                "speed": self.speed,
                "state": self.state,
                "error": self.error,
            }


class MegaWaiter(MegaListener):
    def __init__(self, stats):
        super().__init__()
        self.req_event = threading.Event()
        self.transfer_event = threading.Event()
        self.error = None
        self.public_node = None
        self.root_node = None
        self.root_name = ""
        self.stats = stats

    def _err_to_str(self, error):
        if hasattr(error, "toString"):
            return error.toString()
        return str(error)

    def onRequestFinish(self, api, request, error):
        if str(error).lower() != "no error":
            self.error = self._err_to_str(error)
            self.req_event.set()
            return
        rtype = request.getType()
        if rtype == MegaRequest.TYPE_LOGIN:
            api.fetchNodes()
            return
        if rtype == MegaRequest.TYPE_GET_PUBLIC_NODE:
            self.public_node = request.getPublicMegaNode()
            if self.public_node:
                self.root_name = self.public_node.getName()
        elif rtype == MegaRequest.TYPE_FETCH_NODES:
            self.root_node = api.getRootNode()
            if self.root_node:
                self.root_name = self.root_node.getName()
        self.req_event.set()

    def onRequestTemporaryError(self, api, request, error):
        self.error = "RequestTempError: " + self._err_to_str(error)
        self.req_event.set()

    def onTransferUpdate(self, api, transfer):
        if not self.stats:
            return
        total = transfer.getTotalBytes()
        total = total if total and total > 0 else None
        self.stats.update(
            bytes_done=transfer.getTransferredBytes(),
            total_bytes=total,
            speed=transfer.getSpeed(),
        )
        if self.stats.snapshot()["state"] == "pending":
            self.stats.set_state("downloading")

    def onTransferFinish(self, api, transfer, error):
        if str(error).lower() != "no error":
            self.error = self._err_to_str(error)
            self.transfer_event.set()
            return
        if transfer.isFinished() and (
            transfer.isFolderTransfer()
            or transfer.getFileName() == self.root_name
        ):
            self.transfer_event.set()

    def onTransferTemporaryError(self, api, transfer, error):
        self.error = "TransferTempError: " + self._err_to_str(error)
        self.transfer_event.set()


def wait_or_raise(event, listener, stats, timeout=None):
    if not event.wait(timeout):
        msg = "Timed out waiting for Mega SDK"
        if stats:
            stats.set_error(msg)
        raise TimeoutError(msg)
    if listener.error:
        err = listener.error
        listener.error = None
        if stats:
            stats.set_error(err)
        raise RuntimeError(err)


def download_mega(link, out_dir, email=None, password=None, stats=None):
    api = MegaApi(None, None, None, "WZML-X-mini")
    listener = MegaWaiter(stats)
    api.addListener(listener)
    folder_api = None

    try:
        if email and password:
            if stats:
                stats.set_state("auth")
            listener.req_event.clear()
            listener.error = None
            api.login(email, password)
            wait_or_raise(listener.req_event, listener, stats)

        if stats:
            stats.set_state("resolving")

        if is_folder_link(link):
            folder_api = MegaApi(None, None, None, "WZML-X-mini")
            folder_api.addListener(listener)
            listener.req_event.clear()
            listener.error = None
            folder_api.loginToFolder(link)
            wait_or_raise(listener.req_event, listener, stats)
            if listener.root_node is None:
                raise RuntimeError("Folder node not found")
            node = folder_api.authorizeNode(listener.root_node)
        else:
            listener.req_event.clear()
            listener.error = None
            api.getPublicNode(link)
            wait_or_raise(listener.req_event, listener, stats)
            if listener.public_node is None:
                raise RuntimeError("Public node not found")
            node = listener.public_node

        if stats:
            stats.set_name(node.getName())
            stats.set_state("downloading")

        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        listener.transfer_event.clear()
        listener.error = None
        api.startDownload(node, str(out_path), node.getName(), None, False, None)
        wait_or_raise(listener.transfer_event, listener, stats)

        return out_path / node.getName()
    finally:
        try:
            api.logout()
        except Exception:
            pass
        if folder_api:
            try:
                folder_api.logout()
            except Exception:
                pass


def collect_files(root):
    root = Path(root)
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*") if p.is_file())


def split_file(path, max_bytes):
    parts = []
    part_index = 1
    with path.open("rb") as src:
        while True:
            part_path = path.with_suffix(path.suffix + f".part{part_index:03d}")
            written = 0
            with part_path.open("wb") as dst:
                while written < max_bytes:
                    chunk = src.read(min(CHUNK_SIZE, max_bytes - written))
                    if not chunk:
                        break
                    dst.write(chunk)
                    written += len(chunk)
            if written == 0:
                if part_path.exists():
                    part_path.unlink()
                break
            parts.append(part_path)
            part_index += 1
            if written < max_bytes:
                break
    if parts:
        path.unlink()
    return parts


def prepare_for_telegram(files, max_bytes):
    upload_queue = []
    for path in files:
        if path.stat().st_size <= max_bytes:
            upload_queue.append(path)
        else:
            upload_queue.extend(split_file(path, max_bytes))
    return upload_queue


def format_progress(snapshot):
    name = snapshot["name"] or "unknown"
    label = snapshot["label"]
    state = snapshot["state"]
    if state == "downloading":
        done = format_bytes(snapshot["bytes_done"])
        total = format_bytes(snapshot["total_bytes"]) if snapshot["total_bytes"] else "?"
        speed = format_bytes(snapshot["speed"]) + "/s"
        if snapshot["total_bytes"]:
            pct = (snapshot["bytes_done"] / snapshot["total_bytes"]) * 100
            pct_text = f"{pct:5.1f}%"
        else:
            pct_text = "  ??% "
        return f"[{label}] {shorten(name)} {pct_text} {done}/{total} {speed}"
    if state == "processing":
        return f"[{label}] {shorten(name)} processing"
    if state == "ready":
        return f"[{label}] {shorten(name)} ready"
    if state == "error":
        err = snapshot["error"] or "unknown error"
        return f"[{label}] {shorten(name)} error: {err}"
    return f"[{label}] {shorten(name)} {state}"


def progress_reporter(stats_list, interval, stop_event):
    while not stop_event.is_set():
        snapshots = [s.snapshot() for s in stats_list]
        if not snapshots:
            break
        line = " | ".join(format_progress(s) for s in snapshots)
        print(line, flush=True)
        if all(s["state"] in ("ready", "error") for s in snapshots):
            break
        time.sleep(interval)


def download_and_prepare(link, out_dir, email, password, max_bytes, stats):
    try:
        root = download_mega(link, out_dir, email, password, stats)
        if stats:
            stats.set_state("processing")
        files = collect_files(root)
        upload_queue = prepare_for_telegram(files, max_bytes)
        if stats:
            stats.set_state("ready")
        return link, upload_queue, None
    except Exception as exc:
        if stats:
            stats.set_error(exc)
        return link, [], str(exc)


def load_links(args):
    links = list(args.links or [])
    if args.links_file:
        with open(args.links_file, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                links.append(line)
    return links


def main():
    parser = argparse.ArgumentParser(
        description="Download MEGA links and prep for Telegram with progress"
    )
    parser.add_argument("links", nargs="*", help="MEGA file or folder links")
    parser.add_argument("--links-file", help="Text file with one link per line")
    parser.add_argument("--out", default="downloads", help="Download directory")
    parser.add_argument("--email", default=None, help="MEGA account email")
    parser.add_argument("--password", default=None, help="MEGA account password")
    parser.add_argument(
        "--tg-max-gb", type=int, default=2, help="Telegram limit in GiB"
    )
    parser.add_argument(
        "--concurrent", type=int, default=2, help="Max concurrent downloads"
    )
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=1.0,
        help="Progress update interval in seconds",
    )
    parser.add_argument("--no-progress", action="store_true")
    args = parser.parse_args()

    links = load_links(args)
    if not links:
        raise SystemExit("No MEGA links provided.")

    max_bytes = args.tg_max_gb * 1024**3
    stats_list = []
    results = []
    stop_event = threading.Event()

    with ThreadPoolExecutor(max_workers=max(1, args.concurrent)) as executor:
        futures = []
        for idx, link in enumerate(links, start=1):
            stats = DownloadStats(str(idx))
            stats_list.append(stats)
            futures.append(
                executor.submit(
                    download_and_prepare,
                    link,
                    args.out,
                    args.email,
                    args.password,
                    max_bytes,
                    stats,
                )
            )

        if not args.no_progress:
            reporter = threading.Thread(
                target=progress_reporter,
                args=(stats_list, args.progress_interval, stop_event),
                daemon=True,
            )
            reporter.start()

        for future in as_completed(futures):
            results.append(future.result())

        stop_event.set()
        if not args.no_progress:
            reporter.join()

    print("")
    print("Files ready for Telegram upload:")
    for link, files, error in results:
        if error:
            print(f"- {link} failed: {error}")
            continue
        print(f"- {link}")
        for path in files:
            print(str(path))


if __name__ == "__main__":
    main()
