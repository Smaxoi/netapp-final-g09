import asyncio
import sys
import threading


class AsyncInput:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.loop = asyncio.get_running_loop()

        self.thread = threading.Thread(
            target=self._read_stdin, daemon=True, name="StdinReaderThread"
        )
        self.thread.start()

    def _read_stdin(self):

        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                clean_text = line.rstrip("\r\n")

                self.loop.call_soon_threadsafe(self.queue.put_nowait, clean_text)
            except Exception:
                break
        
    async def get_line(self, timeout: float | None = None) -> str:

        if timeout is not None:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        return await self.queue.get()
