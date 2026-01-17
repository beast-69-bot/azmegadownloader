from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from typing import Iterable
from pathlib import Path

from mega import Mega
from mega.crypto import (
    a32_to_str,
    base64_to_a32,
    base64_url_decode,
    decrypt_attr,
    decrypt_key,
    get_chunks,
    str_to_a32,
)
from Crypto.Cipher import AES
from Crypto.Util import Counter
import requests


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
        _, _, frag = url.partition("#")
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


def _parse_public_link(url: str) -> tuple[str, str, str]:
    normalized = _normalize_mega_url(url)
    if "#F!" in normalized:
        frag = normalized.split("#", 1)[1]
        parts = frag.split("!")
        if len(parts) < 3:
            raise ValueError("MEGA folder link missing key")
        return "folder", parts[1], parts[2].split("?")[0].split("/")[0]
    if "#!" in normalized:
        frag = normalized.split("#!", 1)[1]
        parts = frag.split("!")
        if len(parts) < 2:
            raise ValueError("MEGA file link missing key")
        return "file", parts[0], parts[1].split("?")[0].split("/")[0]
    raise ValueError("Unsupported MEGA URL format")


def list_files_recursive(path: Path) -> list[str]:
    if path.is_file():
        return [str(path.resolve())]
    if not path.exists():
        return []
    files = [p.resolve() for p in path.rglob("*") if p.is_file()]
    return sorted(str(p) for p in files)


def safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _iter_candidate_public_keys(key_field: str) -> Iterable[tuple[str, str]]:
    for part in (key_field or "").split("/"):
        if ":" in part:
            owner, enc_key = part.split(":", 1)
            if enc_key:
                yield owner, enc_key


def _decrypt_public_nodes(
    mega: Mega, nodes: list[dict], folder_id: str, folder_key: str
) -> dict[str, dict]:
    shared_key = base64_to_a32(folder_key)
    result: dict[str, dict] = {}

    for node in nodes:
        if node.get("t") not in (0, 1):
            continue
        enc_key = None
        fallback = None
        for owner, candidate in _iter_candidate_public_keys(node.get("k", "")):
            if owner == folder_id:
                enc_key = candidate
                break
            if fallback is None:
                fallback = candidate
        if enc_key is None:
            enc_key = fallback
        if not enc_key:
            continue
        key = decrypt_key(str_to_a32(base64_url_decode(enc_key)), shared_key)
        if not key:
            continue
        if node["t"] == 0:
            if len(key) < 8:
                continue
            k = (key[0] ^ key[4], key[1] ^ key[5], key[2] ^ key[6], key[3] ^ key[7])
            node["iv"] = key[4:6] + (0, 0)
            node["meta_mac"] = key[6:8]
        else:
            if len(key) < 4:
                continue
            k = key
        node["key"] = key
        node["k"] = k
        attrs = decrypt_attr(base64_url_decode(node["a"]), k)
        if not attrs:
            continue
        node["a"] = attrs
        result[node["h"]] = node

    if result:
        return result

    # Fallback: use mega.py's internal processing for public shares.
    shared_keys = {"EXP": {node.get("h"): shared_key for node in nodes}}
    for node in nodes:
        if node.get("t") not in (0, 1):
            continue
        processed = mega._process_file(node, shared_keys)
        if processed.get("a"):
            result[node["h"]] = processed
    return result


def _build_public_paths(nodes: dict[str, dict]) -> list[tuple[dict, Path]]:
    items: list[tuple[dict, Path]] = []
    for node_id, node in nodes.items():
        if node.get("t") != 0:
            continue
        parts = [node["a"]["n"]]
        parent = node.get("p")
        while parent:
            parent_node = nodes.get(parent)
            if not parent_node or parent_node.get("t") != 1:
                break
            parts.append(parent_node["a"]["n"])
            parent = parent_node.get("p")
        rel_path = Path(*reversed(parts))
        items.append((node, rel_path))
    items.sort(key=lambda item: str(item[1]).lower())
    return items


def _fetch_public_folder_listing(mega: Mega, folder_id: str) -> dict:
    url = f"{mega.schema}://g.api.{mega.domain}/cs"
    params = {"id": mega.sequence_num, "n": folder_id}
    mega.sequence_num += 1
    payload = [{"a": "f", "c": 1, "r": 1, "ca": 1, "p": folder_id}]
    response = requests.post(
        url, params=params, data=json.dumps(payload), timeout=mega.timeout
    )
    try:
        data = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid MEGA API response: {exc}") from exc

    files = data[0] if isinstance(data, list) else data
    if isinstance(files, int):
        raise RuntimeError(f"MEGA API error: {files}")
    if not files or "f" not in files:
        raise RuntimeError("Failed to list MEGA folder contents")
    return files


def _download_public_node(mega: Mega, folder_id: str, node: dict, dest_dir: Path, filename: str) -> Path:
    url = f"{mega.schema}://g.api.{mega.domain}/cs"
    params = {"id": mega.sequence_num, "n": folder_id}
    mega.sequence_num += 1
    payload = [{"a": "g", "g": 1, "n": node["h"]}]
    response = requests.post(
        url, params=params, data=json.dumps(payload), timeout=mega.timeout
    )
    try:
        data = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid MEGA API response: {exc}") from exc

    file_data = data[0] if isinstance(data, list) else data
    if isinstance(file_data, int):
        raise RuntimeError(f"MEGA API error: {file_data}")
    if "g" not in file_data:
        raise RuntimeError("MEGA file not accessible")

    file_url = file_data["g"]
    file_size = file_data["s"]
    k = node["k"]
    iv = node["iv"]
    meta_mac = node["meta_mac"]

    last_error = None
    for attempt in range(3):
        try:
            input_file = requests.get(file_url, stream=True, timeout=mega.timeout).raw
            break
        except requests.exceptions.RequestException as exc:
            last_error = exc
            time.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError(f"MEGA download failed: {last_error}")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(mode="w+b", prefix="megapy_", delete=False) as temp_output_file:
        k_str = a32_to_str(k)
        counter = Counter.new(128, initial_value=((iv[0] << 32) + iv[1]) << 64)
        aes = AES.new(k_str, AES.MODE_CTR, counter=counter)

        mac_str = "\0" * 16
        mac_encryptor = AES.new(k_str, AES.MODE_CBC, mac_str.encode("utf8"))
        iv_str = a32_to_str([iv[0], iv[1], iv[0], iv[1]])

        for _chunk_start, chunk_size in get_chunks(file_size):
            chunk = input_file.read(chunk_size)
            chunk = aes.decrypt(chunk)
            temp_output_file.write(chunk)

            encryptor = AES.new(k_str, AES.MODE_CBC, iv_str)
            i = 0
            for i in range(0, len(chunk) - 16, 16):
                block = chunk[i : i + 16]
                encryptor.encrypt(block)

            if file_size > 16:
                i += 16
            else:
                i = 0

            block = chunk[i : i + 16]
            if len(block) % 16:
                block += b"\0" * (16 - (len(block) % 16))
            mac_str = mac_encryptor.encrypt(encryptor.encrypt(block))

        file_mac = str_to_a32(mac_str)
        if (file_mac[0] ^ file_mac[1], file_mac[2] ^ file_mac[3]) != meta_mac:
            raise ValueError("Mismatched mac")

        output_path = dest_dir / filename
        shutil.move(temp_output_file.name, output_path)
        return output_path


async def _download_public_folder(mega: Mega, folder_id: str, folder_key: str, dest_path: Path) -> list[str]:
    files = _fetch_public_folder_listing(mega, folder_id)

    nodes = _decrypt_public_nodes(mega, files.get("f", []), folder_id, folder_key)
    if not nodes:
        raise RuntimeError("No files found in MEGA folder")

    download_items = _build_public_paths(nodes)
    if not download_items:
        raise RuntimeError("No files found in MEGA folder")

    downloaded: list[str] = []
    for node, rel_path in download_items:
        target_dir = dest_path / rel_path.parent
        safe_mkdir(target_dir)
        output_path = await asyncio.to_thread(
            _download_public_node, mega, folder_id, node, target_dir, rel_path.name
        )
        downloaded.append(str(Path(output_path).resolve()))
    return downloaded


async def get_mega_total_size(url: str) -> int:
    if not is_mega_url(url):
        raise ValueError("Invalid MEGA URL")

    mega = Mega()
    mega.login()
    link_type, handle, key = _parse_public_link(url)
    if link_type == "file":
        normalized = _normalize_mega_url(url)
        info = await asyncio.to_thread(mega.get_public_url_info, normalized)
        if not info:
            return 0
        return int(info.get("size", 0) or 0)

    files = _fetch_public_folder_listing(mega, handle)
    total = 0
    for node in files.get("f", []):
        if node.get("t") == 0:
            total += int(node.get("s", 0) or 0)
    return total


async def download_mega_url(url: str, dest_dir: str) -> list[str]:
    if not is_mega_url(url):
        raise ValueError("Invalid MEGA URL")

    dest_path = Path(dest_dir).resolve()
    safe_mkdir(dest_path)

    mega = Mega()
    try:
        mega.login()
        link_type, handle, key = _parse_public_link(url)
        if link_type == "folder":
            return await _download_public_folder(mega, handle, key, dest_path)
        url = _normalize_mega_url(url)
        await asyncio.to_thread(mega.download_url, url, str(dest_path))
    except Exception as exc:
        raise RuntimeError(f"MEGA download failed: {exc}") from exc

    files = list_files_recursive(dest_path)
    if not files:
        raise RuntimeError("No files downloaded from MEGA link")
    return files
