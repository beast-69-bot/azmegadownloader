from __future__ import annotations

import asyncio
import re
from pathlib import Path

from mega import Mega


def is_mega_url(url: str) -> bool:
    if not url:
        return False
    return "mega.nz" in url or "mega.co.nz" in url


def is_folder_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    return "/folder/" in lower or "#f!" in lower


def _normalize_mega_url(url: str) -> str:
    if not url:
        return url
    if "#F!" in url or "#!" in url:
        base, _, frag = url.partition("#")
        if frag.startswith("F!"):
            parts = frag.split("!", 2)
            if len(parts) >= 3:
                folder_id = parts[1]
                key = parts[2].split("?")[0].split("/")[0]
                return f"https://mega.nz/#F!{folder_id}!{key}"
        if frag.startswith("!"):
            frag = frag[1:]
            parts = frag.split("!", 1)
            if len(parts) == 2:
                file_id, key = parts
                key = key.split("?")[0].split("/")[0]
                return f"https://mega.nz/#!{file_id}!{key}"
        return url

    lower = url.lower()
    if "/folder/" in lower:
        match = re.search(r"/folder/([^?#/]+)#([^/?]+)", url, re.IGNORECASE)
        if not match:
            raise ValueError("MEGA folder link missing key")
        folder_id, key = match.group(1), match.group(2)
        return f"https://mega.nz/#F!{folder_id}!{key}"
    if "/file/" in lower:
        match = re.search(r"/file/([^?#/]+)#([^/?]+)", url, re.IGNORECASE)
        if not match:
            raise ValueError("MEGA file link missing key")
        file_id, key = match.group(1), match.group(2)
        return f"https://mega.nz/#!{file_id}!{key}"
    return url


def list_files_recursive(path: Path) -> list[str]:
    if path.is_file():
        return [str(path.resolve())]
    if not path.exists():
        return []
    files = [p.resolve() for p in path.rglob("*") if p.is_file()]
    return sorted(str(p) for p in files)


def safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


async def download_mega_url(url: str, dest_dir: str) -> list[str]:
    if not is_mega_url(url):
        raise ValueError("Invalid MEGA URL")

    dest_path = Path(dest_dir).resolve()
    safe_mkdir(dest_path)

    url = _normalize_mega_url(url)

    mega = Mega()
    try:
        mega.login()
        await asyncio.to_thread(mega.download_url, url, str(dest_path))
    except Exception as exc:
        raise RuntimeError(f"MEGA download failed: {exc}") from exc

    files = list_files_recursive(dest_path)
    if not files:
        raise RuntimeError("No files downloaded from MEGA link")
    return files
