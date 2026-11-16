"""B10–B15：五階段流程的測試。**這個檔案由彭明山一個人改。**

這裡測的是整局流程，不是單一函式，所以用一個假的 `Conn` 把訊息餵進協定層的
`dispatch()`，再從假 `Conn` 收到的訊息反推伺服器做了什麼——跟真的客戶端
連進來走的是同一條路。

最重要的一道防護寫在 `FakeConn.send()` 裡：**伺服器送出的每一則訊息，都會
立刻丟進唐偉捷獨立寫的 `validate_server_message()` 驗一次。**兩個人各自讀
規範寫出來的東西要能互相驗過，那才真的代表對規範的理解一致——互通性是這個
專題的核心要求，這是最早能驗證它的地方。

時間處理上有個教訓：測試不要用「固定睡多久」去等流程前進，因為那會把階段的
時限一起消耗掉，於是承諾還沒送出去就先逾時了。這裡一律用 `until()` 等到
**某則訊息真的出現**為止，時限則開得很寬；只有真的要測逾時的那幾項才把對應
的時限壓到毫秒。

用 pytest 跑：

    pytest -q tests/test_game_flow.py

沒裝 pytest 的話直接執行這個檔案也可以。
"""

import asyncio
import os
import secrets
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bcproto.messages import validate_server_message
from bcproto.rules import ALL_NUMBERS, make_commit, score
from bcproto.server import Player, Server


# ---------------------------------------------------------------------------
# 測試替身
# ---------------------------------------------------------------------------

class FakeConn:
    """只實作遊戲引擎與協定層真正會用到的那幾個成員。

    真正的 `Conn`（唐偉捷的 A8）還要管傳送佇列與 writer task，那些跟流程無關。
    """

    def __init__(self, peer="203.0.113.7:50000"):
        self.sent = []
        self.errors = []
        self.player = None
        self._peer = peer
        self._closed = False

    @property
    def connected(self):
        return not self._closed

    @property
    def closed(self):
        return self._closed

    def label(self):
        return self.player.pid if self.player is not None else self._peer

    def send(self, msg):
        error = validate_server_message(msg)
        assert error is None, f"伺服器送出不合規範的 {msg.get('type')}：{error}"
        self.sent.append(msg)

    def error(self, code, message=None, **kwargs):
        self.errors.append(code)

    def close(self):
        self._closed = True

    # --- 測試用 ---
    def of(self, kind):
        return [m for m in self.sent if m["type"] == kind]

    def last(self, kind):
        found = self.of(kind)
        assert found, f"沒有收到任何 {kind}"
        return found[-1]

    def has(self, kind):
        return bool(self.of(kind))


class Client:
    """一個假客戶端：記住自己的秘密、鹽值與承諾值。"""

    def __init__(self, name, secret=None):
        self.name = name
        self.conn = FakeConn()
        self.secret = secret or ALL_NUMBERS[0]
        self.salt = secrets.token_hex(16)
        self.commit = make_commit(self.secret, self.salt)

    @property
    def pid(self):
        return self.conn.player.pid if self.conn.player else None


def make_args(log_dir, **overrides):
    """§5.2 的選項。時限預設開得很寬，只有測逾時的項目才自己壓短。"""
    args = SimpleNamespace(
        host="0.0.0.0", port=5040, players=2, bots=0,
        guess_timeout=5.0, feedback_timeout=5.0,
        commit_timeout=5.0, reveal_timeout=5.0,
        max_turns=120, max_skips=3, games=1, log_dir=log_dir,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


async def until(predicate, limit=800):
    """等到條件成立為止，最多 `limit` 次事件迴圈切換。

    比「睡固定時間」好的地方在於：條件一成立就馬上往下走，不會白白吃掉
    階段的時限。
    """
    for _ in range(limit):
        if predicate():
            return True
        await asyncio.sleep(0.001)
    return predicate()


async def join(srv, client):
    """送 HELLO 讓玩家進大廳，走的是協定層真正的 dispatch。"""
    srv.conns.add(client.conn)
    srv.dispatch(client.conn, {
        "type": "HELLO", "protocol": "BC/1.0",
        "name": client.name, "team": "G09", "kind": "ai",
    })
    assert await until(lambda: client.conn.has("WELCOME")), "沒收到 WELCOME"


def find(clients, pid):
    for c in clients:
        if c.pid == pid:
            return c
    raise AssertionError(f"找不到 {pid}")


async def stop(srv, task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    srv.close_log()


# ---------------------------------------------------------------------------
# 完整一局
# ---------------------------------------------------------------------------

async def _play_two_player_game(log_dir):
    """兩人局：先喊數的人直接喊中對手的秘密，一回合結束。"""
    srv = Server(make_args(log_dir, players=2))
    task = asyncio.create_task(srv.run_game())
    clients = [Client("甲", "0275"), Client("乙", "1836")]

    for c in clients:
        await join(srv, c)
    assert await until(lambda: clients[0].conn.has("GAME_START")), "沒開局"
    start = clients[0].conn.last("GAME_START")

    for c in clients:
        srv.dispatch(c.conn, {"type": "COMMIT", "commit": c.commit})
    assert await until(lambda: clients[0].conn.has("TURN")), "沒進入回合"

    turn = clients[0].conn.last("TURN")
    guesser = find(clients, turn["guesser"])
    target = find(clients, [p for p in turn["alive"] if p != turn["guesser"]][0])

    srv.dispatch(guesser.conn, {"type": "GUESS", "turn_id": turn["turn_id"],
                                "guess": target.secret})
    assert await until(lambda: guesser.conn.has("GUESS_MADE"))

    a, b = score(target.secret, target.secret)
    srv.dispatch(target.conn, {"type": "FEEDBACK", "turn_id": turn["turn_id"],
                               "a": a, "b": b})

    # 被猜中的人先揭示（第二道防線）。
    assert await until(lambda: target.conn.has("REVEAL_REQUEST")), "沒要求淘汰揭示"
    srv.dispatch(target.conn, {"type": "REVEAL", "secret": target.secret,
                               "salt": target.salt})

    # 終局時存活者也要揭示（第三道防線）。
    assert await until(lambda: guesser.conn.has("REVEAL_REQUEST")), "沒要求終局揭示"
    srv.dispatch(guesser.conn, {"type": "REVEAL", "secret": guesser.secret,
                                "salt": guesser.salt})

    await asyncio.wait_for(task, 3)
    return srv, clients, start, turn


def test_two_player_game_runs_to_game_over():
    async def main():
        with tempfile.TemporaryDirectory() as log_dir:
            srv, clients, _, turn = await _play_two_player_game(log_dir)
            over = clients[0].conn.last("GAME_OVER")
            assert over["reason"] == "LAST_PLAYER"
            assert over["turns"] == 1
            assert srv.phase == "OVER"

            ranks = {row["player"]: row for row in over["ranking"]}
            assert len(ranks) == 2
            winner = ranks[turn["guesser"]]
            assert winner["rank"] == 1
            assert winner["status"] == "SURVIVOR"
            assert winner["out_turn"] is None
            loser = [r for r in ranks.values() if r["rank"] == 2][0]
            assert loser["status"] == "GUESSED" and loser["out_turn"] == 1
    asyncio.run(main())


def test_whole_game_passes_the_protocol_layer_validator():
    """FakeConn.send() 每一則都驗過了，這裡確認該出現的訊息真的都出現。"""
    async def main():
        with tempfile.TemporaryDirectory() as log_dir:
            _, clients, _, _ = await _play_two_player_game(log_dir)
            kinds = {m["type"] for c in clients for m in c.conn.sent}
            assert {"WELCOME", "LOBBY", "GAME_START", "COMMITS", "TURN",
                    "GUESS_MADE", "TURN_RESULT", "REVEAL_REQUEST",
                    "REVEALED", "GAME_OVER"} <= kinds
    asyncio.run(main())


def test_game_start_order_matches_the_player_list():
    """§11.2：`order` 必須與 `players` 的排列完全一致。"""
    async def main():
        with tempfile.TemporaryDirectory() as log_dir:
            _, _, start, _ = await _play_two_player_game(log_dir)
            assert start["order"] == [p["id"] for p in start["players"]]
            assert start["rules"]["max_turns"] == 120
            assert start["rules"]["guess_timeout"] == 5.0
    asyncio.run(main())


def test_one_game_writes_exactly_one_log_file():
    async def main():
        with tempfile.TemporaryDirectory() as log_dir:
            srv, _, _, _ = await _play_two_player_game(log_dir)
            files = os.listdir(log_dir)
            assert len(files) == 1, "一局必須恰好一個日誌檔（§5.6）"
            assert srv.game_id in files[0]
    asyncio.run(main())


def test_all_connections_are_closed_when_the_game_ends():
    """§5.3-6：局末關閉本局所有連線。"""
    async def main():
        with tempfile.TemporaryDirectory() as log_dir:
            _, clients, _, _ = await _play_two_player_game(log_dir)
            assert all(c.conn.closed for c in clients)
    asyncio.run(main())


# ---------------------------------------------------------------------------
# 大廳與承諾
# ---------------------------------------------------------------------------

def test_lobby_is_broadcast_when_someone_joins():
    async def main():
        with tempfile.TemporaryDirectory() as log_dir:
            srv = Server(make_args(log_dir, players=3))
            task = asyncio.create_task(srv.run_game())
            first = Client("甲")
            await join(srv, first)
            await join(srv, Client("乙"))

            lobby = first.conn.last("LOBBY")
            assert [p["name"] for p in lobby["players"]] == ["甲", "乙"]
            assert lobby["min_players"] == 2      # §11.2 固定是 2
            assert lobby["max_players"] == 3      # 這個才是 --players
            assert not first.conn.has("GAME_START"), "人還沒到齊就開局了"
            await stop(srv, task)
    asyncio.run(main())


def test_manual_start_opens_the_game_with_two_players():
    """莊家在主控台輸入 `start`：至少 2 人就能提早開局（§5.3）。"""
    async def main():
        with tempfile.TemporaryDirectory() as log_dir:
            srv = Server(make_args(log_dir, players=6))
            task = asyncio.create_task(srv.run_game())
            clients = [Client("甲"), Client("乙")]
            for c in clients:
                await join(srv, c)
            assert not clients[0].conn.has("GAME_START")

            srv.manual_start = True
            srv.changed.set()
            assert await until(lambda: clients[0].conn.has("GAME_START"))
            await stop(srv, task)
    asyncio.run(main())


def test_player_who_never_commits_is_removed_and_game_aborts():
    """兩人局只有一人承諾 → 剩不到 2 人 → ABORTED（§11.2）。"""
    async def main():
        with tempfile.TemporaryDirectory() as log_dir:
            srv = Server(make_args(log_dir, players=2, commit_timeout=0.05))
            task = asyncio.create_task(srv.run_game())
            good, silent = Client("甲"), Client("乙")
            await join(srv, good)
            await join(srv, silent)
            assert await until(lambda: good.conn.has("GAME_START"))

            srv.dispatch(good.conn, {"type": "COMMIT", "commit": good.commit})
            await asyncio.wait_for(task, 3)

            over = good.conn.last("GAME_OVER")
            assert over["reason"] == "ABORTED"
            assert over["turns"] == 0 and over["ranking"] == []
            assert silent.conn.player.status == "REMOVED"
            assert not any(p is silent.conn.player for p in srv.audience())
    asyncio.run(main())


def test_commits_message_lists_every_player_still_in():
    async def main():
        with tempfile.TemporaryDirectory() as log_dir:
            srv = Server(make_args(log_dir, players=2))
            task = asyncio.create_task(srv.run_game())
            clients = [Client("甲"), Client("乙")]
            for c in clients:
                await join(srv, c)
            assert await until(lambda: clients[0].conn.has("GAME_START"))
            for c in clients:
                srv.dispatch(c.conn, {"type": "COMMIT", "commit": c.commit})
            assert await until(lambda: clients[0].conn.has("COMMITS"))

            commits = clients[0].conn.last("COMMITS")
            assert set(commits["order"]) == set(commits["commits"])
            assert commits["commits"][clients[0].pid] == clients[0].commit
            await stop(srv, task)
    asyncio.run(main())


# ---------------------------------------------------------------------------
# 回合
# ---------------------------------------------------------------------------

async def _reach_first_turn(srv, clients):
    for c in clients:
        await join(srv, c)
    assert await until(lambda: clients[0].conn.has("GAME_START"))
    for c in clients:
        srv.dispatch(c.conn, {"type": "COMMIT", "commit": c.commit})
    assert await until(lambda: clients[0].conn.has("TURN")), "沒進入回合"
    return clients[0].conn.last("TURN")


def test_a_turn_with_no_guess_is_marked_skipped():
    """§11.3：`skipped` 與 `guess is None` 同真同假，且 `results` 必空。"""
    async def main():
        with tempfile.TemporaryDirectory() as log_dir:
            srv = Server(make_args(log_dir, players=2,
                                   guess_timeout=0.02, max_skips=99))
            task = asyncio.create_task(srv.run_game())
            clients = [Client("甲"), Client("乙")]
            await _reach_first_turn(srv, clients)
            assert await until(lambda: clients[0].conn.has("TURN_RESULT"))

            result = clients[0].conn.of("TURN_RESULT")[0]
            assert result["skipped"] is True
            assert result["guess"] is None
            assert result["results"] == []
            assert result["forfeited"] == []      # max_skips 還沒到
            await stop(srv, task)
    asyncio.run(main())


def test_guesser_forfeits_after_max_skips():
    async def main():
        with tempfile.TemporaryDirectory() as log_dir:
            srv = Server(make_args(log_dir, players=2,
                                   guess_timeout=0.02, max_skips=1))
            task = asyncio.create_task(srv.run_game())
            clients = [Client("甲"), Client("乙")]
            turn = await _reach_first_turn(srv, clients)
            assert await until(lambda: clients[0].conn.has("TURN_RESULT"))

            result = clients[0].conn.of("TURN_RESULT")[0]
            assert result["forfeited"] == [turn["guesser"]]
            assert srv.players[turn["guesser"]].status == "FORFEIT"
            assert srv.players[turn["guesser"]].out_turn == 1
            await stop(srv, task)
    asyncio.run(main())


def test_no_feedback_is_public_before_everyone_has_answered():
    """第一道防線的前提（§3、§14）：收齊之前不得洩漏任何一筆回報。

    三人局，只有一位回報者先回答。此時 `TURN_RESULT` 還不能出現——
    否則最後一位就能看著別人的答案編自己的。
    """
    async def main():
        with tempfile.TemporaryDirectory() as log_dir:
            srv = Server(make_args(log_dir, players=3))
            task = asyncio.create_task(srv.run_game())
            clients = [Client("甲", "0275"), Client("乙", "1836"), Client("丙", "9420")]
            turn = await _reach_first_turn(srv, clients)

            guesser = find(clients, turn["guesser"])
            srv.dispatch(guesser.conn, {"type": "GUESS", "turn_id": turn["turn_id"],
                                        "guess": "0123"})
            assert await until(lambda: guesser.conn.has("GUESS_MADE"))

            made = guesser.conn.last("GUESS_MADE")
            assert turn["guesser"] not in made["responders"]
            assert len(made["responders"]) == 2

            first = find(clients, made["responders"][0])
            a, b = score("0123", first.secret)
            srv.dispatch(first.conn, {"type": "FEEDBACK", "turn_id": turn["turn_id"],
                                      "a": a, "b": b})
            for _ in range(30):
                await asyncio.sleep(0.001)
            assert not clients[0].conn.has("TURN_RESULT"), "還沒收齊就公開了回報"

            # 第二個人回答之後才一次公開兩筆。
            second = find(clients, made["responders"][1])
            a2, b2 = score("0123", second.secret)
            srv.dispatch(second.conn, {"type": "FEEDBACK", "turn_id": turn["turn_id"],
                                       "a": a2, "b": b2})
            assert await until(lambda: clients[0].conn.has("TURN_RESULT"))
            result = clients[0].conn.last("TURN_RESULT")
            assert len(result["results"]) == 2
            await stop(srv, task)
    asyncio.run(main())


def test_responder_who_stays_silent_forfeits():
    async def main():
        with tempfile.TemporaryDirectory() as log_dir:
            srv = Server(make_args(log_dir, players=2, feedback_timeout=0.02))
            task = asyncio.create_task(srv.run_game())
            clients = [Client("甲", "0275"), Client("乙", "1836")]
            turn = await _reach_first_turn(srv, clients)
            guesser = find(clients, turn["guesser"])
            srv.dispatch(guesser.conn, {"type": "GUESS", "turn_id": turn["turn_id"],
                                        "guess": "0123"})
            assert await until(lambda: clients[0].conn.has("TURN_RESULT"))

            result = clients[0].conn.of("TURN_RESULT")[0]
            assert len(result["forfeited"]) == 1
            assert result["results"] == []
            assert result["skipped"] is False     # 有喊數，只是沒人回報
            await stop(srv, task)
    asyncio.run(main())


# ---------------------------------------------------------------------------
# B13 一致性檢查、B14 揭示（直接測函式，不必跑滿一局）
# ---------------------------------------------------------------------------

def _bare_server(log_dir, **kw):
    srv = Server(make_args(log_dir, **kw))
    srv.game_id = srv.new_game_id()
    return srv


def _add(srv, pid, name="甲"):
    conn = FakeConn()
    player = Player(pid, name, "G09", "ai", conn)
    conn.player = player
    srv.players[pid] = player
    player.status = "ALIVE"
    return player, conn


def test_empty_candidate_set_disqualifies_on_the_spot():
    """候選集合變空 = 沒有任何秘密能同時滿足他講過的每一句話。"""
    with tempfile.TemporaryDirectory() as log_dir:
        srv = _bare_server(log_dir)
        player, _ = _add(srv, "p1")
        player.cands = ["0275"]          # 只剩一個候選
        srv.turn_id = 7
        srv.feedback = {"p1": (0, 0)}    # 0275 對 0275 不可能是 0A0B

        eliminated, forfeited, dq = srv.settle_turn("0275", ["p1"])
        assert dq == ["p1"] and not eliminated and not forfeited
        assert player.status == "DQ"
        assert player.out_turn == 7


def test_four_a_zero_b_eliminates_the_responder():
    with tempfile.TemporaryDirectory() as log_dir:
        srv = _bare_server(log_dir)
        player, _ = _add(srv, "p1")
        srv.turn_id = 3
        srv.feedback = {"p1": (4, 0)}

        eliminated, _, dq = srv.settle_turn("0275", ["p1"])
        assert eliminated == ["p1"] and not dq
        assert player.status == "GUESSED" and player.out_turn == 3


def test_candidates_shrink_with_every_report():
    """每位回報者的候選集合是逐回合刪減的，不是只看最後一筆。"""
    with tempfile.TemporaryDirectory() as log_dir:
        srv = _bare_server(log_dir)
        player, _ = _add(srv, "p1")
        assert len(player.cands) == 5040

        srv.turn_id = 1
        srv.feedback = {"p1": score("0123", "0275")}
        srv.settle_turn("0123", ["p1"])
        after_one = len(player.cands)
        assert 0 < after_one < 5040

        srv.turn_id = 2
        srv.feedback = {"p1": score("4567", "0275")}
        srv.settle_turn("4567", ["p1"])
        assert 0 < len(player.cands) < after_one
        assert "0275" in player.cands
        assert len(player.records) == 2


def test_reveal_that_does_not_match_the_commit_is_disqualified():
    """第二道防線：承諾值對不上 → BAD_COMMIT → 取消資格（§14.2）。"""
    async def main():
        with tempfile.TemporaryDirectory() as log_dir:
            srv = _bare_server(log_dir, reveal_timeout=0.01)
            player, conn = _add(srv, "p1")
            player.commit = make_commit("0275", "a" * 32)
            player.reveal = ("1836", "b" * 32)    # 換了一組，對不上
            srv.turn_id = 5

            await srv.collect_reveals([player], "GAME_END")

            revealed = conn.last("REVEALED")
            assert revealed["verdict"] == "BAD_COMMIT"
            assert revealed["bad_turns"] == []
            assert player.status == "DQ"
            assert player.out_turn == 5, "DQ 的 out_turn 不能是 null（§11.3）"
    asyncio.run(main())


def test_reveal_that_contradicts_past_reports_lists_the_bad_turns():
    """第三道防線：用揭示的秘密重算每一筆歷史回報（§14.2）。"""
    async def main():
        with tempfile.TemporaryDirectory() as log_dir:
            srv = _bare_server(log_dir, reveal_timeout=0.01)
            player, conn = _add(srv, "p1")
            player.commit = make_commit("0275", "a" * 32)
            player.reveal = ("0275", "a" * 32)
            player.records = [
                (1, "0123", *score("0123", "0275")),   # 誠實
                (2, "4567", 3, 0),                     # 編的
            ]
            srv.turn_id = 2

            await srv.collect_reveals([player], "GAME_END")

            revealed = conn.last("REVEALED")
            assert revealed["verdict"] == "INCONSISTENT"
            assert revealed["bad_turns"] == [2]
            assert player.status == "DQ"
    asyncio.run(main())


def test_missing_reveal_is_no_reveal():
    async def main():
        with tempfile.TemporaryDirectory() as log_dir:
            srv = _bare_server(log_dir, reveal_timeout=0.01)
            player, conn = _add(srv, "p1")
            player.commit = make_commit("0275", "a" * 32)
            srv.turn_id = 2

            await srv.collect_reveals([player], "ELIMINATED")

            revealed = conn.last("REVEALED")
            assert revealed["verdict"] == "NO_REVEAL"
            assert revealed["secret"] is None and revealed["salt"] is None
            assert conn.last("REVEAL_REQUEST")["reason"] == "ELIMINATED"
    asyncio.run(main())


def test_reveal_request_goes_only_to_the_person_concerned():
    """`REVEAL_REQUEST` 不是廣播——只有當事人收得到，判定結果才公開。"""
    async def main():
        with tempfile.TemporaryDirectory() as log_dir:
            srv = _bare_server(log_dir, reveal_timeout=0.01)
            first, conn1 = _add(srv, "p1", "甲")
            _, conn2 = _add(srv, "p2", "乙")
            first.commit = make_commit("0275", "a" * 32)
            srv.turn_id = 1

            await srv.collect_reveals([first], "ELIMINATED")

            assert conn1.has("REVEAL_REQUEST")
            assert not conn2.has("REVEAL_REQUEST")
            assert conn2.has("REVEALED")
    asyncio.run(main())


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"  [PASS] {name}")
            except Exception as exc:
                failed += 1
                print(f"  [FAIL] {name}: {exc!r}")
    print(f"{passed} passed, {failed} failed")
