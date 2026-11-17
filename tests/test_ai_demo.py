import asyncio
import hashlib
import json
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bcproto.ai.agent import AIAgent


def score_pair(guess: str, secret: str) -> tuple[int, int]:
  """裁判標準算分器"""
  a = sum(1 for g, s in zip(guess, secret) if g == s)
  b = sum(1 for g in guess if g in secret) - a
  return a, b


def random_secret() -> str:
  """幫對手機器人隨機產生 4 位不重複秘密"""
  return "".join(random.sample("0123456789", 4))


class SingleGameJudge:

  def __init__(self, bot_secret: str):
    self.bot_secret = bot_secret
    self.ai_secret = None
    self.ai_commit = None

  async def handle_client(
      self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
  ):
    # 1. [C2] 接收 HELLO 並發放 player_id
    hello_line = await reader.readline()
    hello = json.loads(hello_line.decode("utf-8").strip())
    ai_name = hello.get("name", "AI")
    print(f"[裁判] 收到玩家連線：{ai_name} (team={hello.get('team')})")

    writer.write(
        b'{"type": "WELCOME", "protocol": "BC/1.0", "player_id": "p1"}\n'
    )
    await writer.drain()

    # 2. [C4] 廣播開局，接收 AI 的 COMMIT
    await asyncio.sleep(0.1)
    print(
        f"[裁判] 開局！p1 ({ai_name}) vs p2 (對手秘密: {self.bot_secret})\n"
        + "-" * 50
    )
    writer.write(
        b'{"type": "GAME_START", "game_id": "demo-001", "players": ["p1",'
        b' "p2"]}\n'
    )
    await writer.drain()

    commit_line = await reader.readline()
    commit_msg = json.loads(commit_line.decode("utf-8").strip())
    self.ai_commit = commit_msg.get("commit")

    # 3. 進入多輪攻防迴圈（最多 30 輪）
    turn_id = 1
    winner = None
    bot_candidates = [
        f"{a}{b}{c}{d}"
        for a in "0123456789"
        for b in "0123456789"
        for c in "0123456789"
        for d in "0123456789"
        if len({a, b, c, d}) == 4
    ]

    while turn_id <= 30 and not winner:
      if turn_id % 2 == 1:
        # --- 奇數輪：輪到你的 AI (p1) 猜 ---
        turn_msg = {
            "type": "TURN",
            "turn_id": turn_id,
            "guesser": "p1",
            "alive": ["p1", "p2"],
        }
        writer.write((json.dumps(turn_msg) + "\n").encode("utf-8"))
        await writer.drain()

        guess_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        ai_guess = json.loads(guess_line.decode("utf-8").strip()).get("guess")
        a, b = score_pair(ai_guess, self.bot_secret)
        print(f"第 {turn_id:02d} 輪 | {ai_name} 猜 '{ai_guess}' -> {a}A{b}B")

        res_msg = {
            "type": "TURN_RESULT",
            "turn_id": turn_id,
            "guesser": "p1",
            "guess": ai_guess,
            "skipped": False,
            "results": [{"player_id": "p2", "a": a, "b": b}],
            "eliminated": ["p2"] if a == 4 else [],
            "forfeited": [],
            "disqualified": [],
        }
        writer.write((json.dumps(res_msg) + "\n").encode("utf-8"))
        await writer.drain()

        if a == 4:
          winner = ai_name
          break
      else:
        # --- 偶數輪：輪到對手機器人 (p2) 猜 ---
        bot_guess = random.choice(bot_candidates)
        gm_msg = {
            "type": "GUESS_MADE",
            "turn_id": turn_id,
            "guesser": "p2",
            "guess": bot_guess,
            "responders": ["p1"],
        }
        writer.write((json.dumps(gm_msg) + "\n").encode("utf-8"))
        await writer.drain()

        fb_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        fb_msg = json.loads(fb_line.decode("utf-8").strip())
        a, b = fb_msg.get("a"), fb_msg.get("b")
        print(f"第 {turn_id:02d} 輪 | 對手 p2 猜 '{bot_guess}' -> {ai_name} 回報 {a}A{b}B")

        # 對手也簡單過濾一下自己的候選集，模擬真實對手壓力
        bot_candidates = [
            c for c in bot_candidates if score_pair(bot_guess, c) == (a, b)
        ]

        res_msg = {
            "type": "TURN_RESULT",
            "turn_id": turn_id,
            "guesser": "p2",
            "guess": bot_guess,
            "skipped": False,
            "results": [{"player_id": "p1", "a": a, "b": b}],
            "eliminated": ["p1"] if a == 4 else [],
            "forfeited": [],
            "disqualified": [],
        }
        writer.write((json.dumps(res_msg) + "\n").encode("utf-8"))
        await writer.drain()

        if a == 4:
          winner = "對手 p2"
          break

      turn_id += 1
      await asyncio.sleep(0.05)

    # 4. [C7] 終局揭示秘密並做 SHA-256 驗證
    print("-" * 50)
    writer.write(b'{"type": "REVEAL_REQUEST", "reason": "GAME_END"}\n')
    await writer.drain()

    rev_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    rev_msg = json.loads(rev_line.decode("utf-8").strip())
    rev_secret = rev_msg.get("secret", "")
    rev_salt = rev_msg.get("salt", "")

    check_hash = hashlib.sha256(
        f"{rev_secret}:{rev_salt}".encode("utf-8")
    ).hexdigest()
    hash_ok = check_hash == self.ai_commit
    if winner == ai_name and hash_ok:
      print(f"✅ AI 在限制回合內猜中，REVEAL commit 驗證成功：{rev_secret}")
    elif winner == "對手 p2":
      print("⚠️ 對手先猜中 AI；REVEAL 流程仍正常完成。")
    else:
      print("❌ AI 未在限制回合內猜中，或 REVEAL commit 驗證失敗。")

    writer.close()
    await writer.wait_closed()


async def main():
  bot_secret = random_secret()
  judge = SingleGameJudge(bot_secret)
  server = await asyncio.start_server(judge.handle_client, "127.0.0.1", 0)
  host, port = server.sockets[0].getsockname()[:2]
  print(f"[測試] 對手秘密：{bot_secret}；監聽 {host}:{port}")

  async with server:
    agent = AIAgent(host, port, name="G09-AI", team="G09")
    await agent.run()


if __name__ == "__main__":
  asyncio.run(main())
