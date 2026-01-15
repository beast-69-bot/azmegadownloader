from __future__ import annotations

import asyncio
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
