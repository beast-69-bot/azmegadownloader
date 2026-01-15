from __future__ import annotations

from pathlib import Path


def is_mega_link(url: str) -> bool:
    if not url:
        return False
    return "mega.nz" in url or "mega.co.nz" in url


def is_mega_folder(url: str) -> bool:
    if not url:
        return False
    return "/folder/" in url or "#F!" in url


def iter_files(root: Path):
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def safe_link_from_text(text: str) -> str:
    if not text:
        return ""
    return text.strip().split()[0]
