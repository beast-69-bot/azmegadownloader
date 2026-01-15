from __future__ import annotations

import asyncio
from pathlib import Path

from .config import MEGA_EMAIL, MEGA_PASSWORD
from .utils import is_mega_folder

try:
    from mega import MegaApi, MegaListener, MegaRequest, MegaTransfer

    SDK_AVAILABLE = True
except Exception:
    MegaApi = None
    MegaListener = object
    MegaRequest = None
    MegaTransfer = None
    SDK_AVAILABLE = False

try:
    from mega import Mega as MegaPy

    MEGAPY_AVAILABLE = True
except Exception:
    MegaPy = None
    MEGAPY_AVAILABLE = False


class AsyncMega:
    def __init__(self):
        self.api = None
        self.folder_api = None
        self._event = asyncio.Event()

    async def run(self, func, *args, **kwargs):
        self._event.clear()
        await asyncio.to_thread(func, *args, **kwargs)
        await self._event.wait()

    async def logout(self):
        await self.run(self.api.logout)
        if self.folder_api:
            await self.run(self.folder_api.logout)

    def mark_done(self):
        self._event.set()

    def __getattr__(self, name):
        attr = getattr(self.api, name)
        if callable(attr):

            async def wrapper(*args, **kwargs):
                return await self.run(attr, *args, **kwargs)

            return wrapper
        return attr


class MegaDownloadListener(MegaListener):
    _NO_EVENT_ON = (MegaRequest.TYPE_LOGIN, MegaRequest.TYPE_FETCH_NODES)

    def __init__(self, loop, async_api, total_size=0, progress_cb=None):
        self._loop = loop
        self._async_api = async_api
        self._progress_cb = progress_cb
        self._total = total_size
        self.public_node = None
        self.node = None
        self.error = None
        self._name = ""
        self._bytes = 0
        self._speed = 0
        super().__init__()

    @property
    def downloaded_bytes(self):
        return self._bytes

    @property
    def speed(self):
        return self._speed

    def onRequestFinish(self, api, request, error):
        if str(error).lower() != "no error":
            self.error = error.copy()
            self._async_api.mark_done()
            return

        req_type = request.getType()
        if req_type == MegaRequest.TYPE_LOGIN:
            api.fetchNodes()
        elif req_type == MegaRequest.TYPE_GET_PUBLIC_NODE:
            self.public_node = request.getPublicMegaNode()
            self._name = self.public_node.getName()
        elif req_type == MegaRequest.TYPE_FETCH_NODES:
            self.node = api.getRootNode()
            self._name = self.node.getName()

        if req_type not in self._NO_EVENT_ON or (
            self.node and "cloud drive" not in self._name.lower()
        ):
            self._async_api.mark_done()

    def onRequestTemporaryError(self, api, request, error):
        self.error = error.toString()
        self._async_api.mark_done()

    def onTransferUpdate(self, api: MegaApi, transfer: MegaTransfer):
        self._speed = transfer.getSpeed()
        self._bytes = transfer.getTransferredBytes()
        if self._progress_cb:
            self._loop.call_soon_threadsafe(
                asyncio.create_task,
                self._progress_cb(self._bytes, self._speed, self._total),
            )

    def onTransferFinish(self, api: MegaApi, transfer: MegaTransfer, error):
        if transfer.isFinished() and (
            transfer.isFolderTransfer() or transfer.getFileName() == self._name
        ):
            self._async_api.mark_done()

    def onTransferTemporaryError(self, api, transfer, error):
        self.error = error.toString()
        self._async_api.mark_done()


async def download_mega(link: str, dest: Path, progress_cb=None) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    if SDK_AVAILABLE:
        loop = asyncio.get_running_loop()
        async_api = AsyncMega()
        async_api.api = MegaApi(None, None, None, "MegaLeech")
        listener = MegaDownloadListener(loop, async_api, 0, progress_cb)
        async_api.api.addListener(listener)

        if MEGA_EMAIL and MEGA_PASSWORD:
            await async_api.login(MEGA_EMAIL, MEGA_PASSWORD)

        if is_mega_folder(link):
            async_api.folder_api = MegaApi(None, None, None, "MegaLeech")
            async_api.folder_api.addListener(listener)
            await async_api.run(async_api.folder_api.loginToFolder, link)
            node = await asyncio.to_thread(
                async_api.folder_api.authorizeNode, listener.node
            )
        else:
            await async_api.getPublicNode(link)
            node = listener.public_node

        if listener.error:
            raise RuntimeError(str(listener.error))

        size = await asyncio.to_thread(async_api.api.getSize, node)
        listener._total = int(size or 0)
        await async_api.startDownload(node, str(dest), "", None, False, None)
        await async_api.logout()
        return dest

    if not MEGAPY_AVAILABLE:
        raise RuntimeError("Mega SDK or mega.py is required")

    if is_mega_folder(link):
        raise RuntimeError("MEGA folder links require Mega SDK")

    mega = MegaPy()
    if MEGA_EMAIL and MEGA_PASSWORD:
        mega.login(MEGA_EMAIL, MEGA_PASSWORD)
    else:
        mega.login()

    info = mega.get_public_url_info(link) or {}
    name = info.get("name", "mega_download")
    size = int(info.get("size", 0) or 0)
    target_path = dest / name

    async def _poll_progress(task):
        last = 0
        while not task.done():
            if target_path.exists():
                cur = target_path.stat().st_size
                speed = max(cur - last, 0)
                last = cur
                if progress_cb:
                    await progress_cb(cur, speed, size)
            await asyncio.sleep(1)

    download_task = asyncio.to_thread(mega.download_url, link, str(dest), name)
    poll_task = asyncio.create_task(_poll_progress(download_task))
    try:
        await download_task
    finally:
        poll_task.cancel()
    return dest
