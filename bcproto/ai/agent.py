import asyncio
import sys

# 匯入放在bcproto/wire.py的A1(encode)跟A2(decode_line)
try:
    from bcproto.wire import decode_line, encode
except ImportError:
    from wire import decode_line, encode

class AIAgent:
    def __init__(self, host: str, port: int, name: str = "G09-AI"):
        self.host = host
        self.port = port
        self.name = name
        self.writer: asyncio.StreamWriter | None = None

    async def send_msg(self, msg: dict):
        """將 dict 封裝為 JSON Lines 並透過 TCP 發送 (使用 A1 encode)"""
        if not self.writer:
            return
        raw_bytes = encode(msg)
        self.writer.write(raw_bytes)
        await self.writer.drain()

    async def dispatch(self, msg: dict):
        """C1 的訊息分派中心：收到合法的 dict 訊息後印出（後續 C2~C7 會在此擴充）"""
        mtype = msg.get("type")
        print(f"[C1 接收] 收到訊息類型: {mtype} | 內容: {msg}")

    async def run(self):
        """C1 核心：建立連線與 StreamReader 讀取迴圈"""
        print(f"[C1] 嘗試連線至 {self.host}:{self.port} ...")
        try:
            reader, writer = await asyncio.open_connection(self.host, self.port)
            self.writer = writer
            print(f"[C1] 連線成功！進入封包監聽迴圈...")
        except Exception as e:
            print(f"[C1 錯誤] 無法連線至伺服器: {e}")
            return

        try:
            while True:
                line = await reader.readline()
                if not line:
                    print("[C1] 伺服器已關閉連線。")
                    break
                # 用A2(decode_line)解析收到的訊息
                msg, err = decode_line(line)
                if err:
                    print(f"[C1 警告] 收到無法解析的訊息，錯誤碼: {err}")
                    continue
                if msg is None:
                    # 忽略空行
                    continue
                # 把dict丟給dispatch
                await self.dispatch(msg)
        except asyncio.CancelledError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            print("[C1] 連線已安全關閉")


