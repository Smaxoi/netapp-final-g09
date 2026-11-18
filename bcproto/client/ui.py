import asyncio
import sys
import threading

from ..validators import valid_feedback, valid_number


def parse_secret_input(text: str) -> str:
    """C15：解析並檢查玩家輸入的四位秘密數字。"""
    value = text.strip()
    if not valid_number(value):
        raise ValueError("請輸入四位不重複的 ASCII 數字，例如 0427")
    return value


def parse_feedback_input(text: str) -> tuple[int, int]:
    """C15：解析 ``a b`` 或 ``a,b`` 格式的回報輸入。"""
    fields = text.replace(",", " ").split()
    if len(fields) != 2:
        raise ValueError("請輸入兩個數字，例如 1 2")
    try:
        a, b = (int(value) for value in fields)
    except ValueError as error:
        raise ValueError("A 與 B 必須是整數") from error
    if not valid_feedback(a, b):
        raise ValueError("A/B 回報不符合規則")
    return a, b


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


async def confirm_feedback(input_reader: AsyncInput, turn_id: int, guess: str,
                           draft: dict) -> bool:
    """顯示預填回報，要求玩家確認後才送出。"""
    print(f"回合 {turn_id}，對方喊數 {guess}，預填回報 {draft['a']}A{draft['b']}B")
    answer = (await input_reader.get_line()).strip().lower()
    return answer in {"y", "yes", "是", "確認"}
