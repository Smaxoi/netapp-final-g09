"""B8／B9／B16 的單元測試。

測試資料與期望值全部取自規範本身：§11.2 的 `LOBBY` 欄位、§11.3 的廣播對象、
§5.6 的日誌欄位。`broadcast_lobby()` 產出的訊息還會丟進協定層的
`validate_server_message()` 再驗一次——兩個人各自依規範寫的東西能互相驗過，
才真的代表對規範的理解一致。

這裡用一個十行的假連線取代協定層的 `Conn`（A8），所以不必等唐偉捷寫完
就能測遊戲引擎。
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bcproto.messages import validate_server_message
from bcproto.rules import ALL_NUMBERS
from bcproto.server import Player, Server
from bcproto.server.game import MIN_PLAYERS


# ----------------------------------------------------------------------
# 測試替身
# ----------------------------------------------------------------------

class FakeConn:
    """假連線。遊戲引擎只用到 send / close / closed / label 四個介面。"""

    def __init__(self, name="127.0.0.1:1234"):
        self.sent = []
        self.closed = False
        self._label = name

    def send(self, msg):
        if not self.closed:
            self.sent.append(msg)

    def close(self):
        self.closed = True

    def label(self):
        return self._label


def make_args(**overrides):
    """§5.2 規定的預設值；名稱與預設值不得更改。"""
    args = dict(host="0.0.0.0", port=5040, players=3, bots=0,
                guess_timeout=60.0, feedback_timeout=60.0,
                commit_timeout=30.0, reveal_timeout=15.0,
                max_turns=120, max_skips=3, games=1, log_dir="logs")
    args.update(overrides)
    return SimpleNamespace(**args)


def make_server(**overrides):
    return Server(make_args(**overrides))


def add_player(srv, name, kind="human", status="ALIVE", conn=None):
    pid = f"p{srv.next_id}"
    srv.next_id += 1
    player = Player(pid, name, "G09", kind, conn if conn is not None else FakeConn())
    player.status = status
    srv.players[pid] = player
    return player


# ----------------------------------------------------------------------
# B8：狀態欄位
# ----------------------------------------------------------------------

def test_initial_state_matches_contract():
    srv = make_server()
    assert srv.phase == "LOBBY"
    assert srv.turn_id == 0
    assert srv.guesser is None
    assert srv.guess_open is False
    assert srv.cur_guess is None
    assert srv.fb_open is False
    assert srv.responders == []
    assert srv.feedback == {}
    assert srv.reveal_wait == set()
    assert srv.players == {}
    assert srv.next_id == 1            # player_id 每局自 p1 起編（§9）
    assert srv.conns == set()
    assert srv.game_id is None
    assert srv.logf is None
    assert srv.manual_start is False
    assert isinstance(srv.changed, asyncio.Event)
    assert not srv.changed.is_set()


def test_player_starts_with_all_5040_candidates():
    p = Player("p1", "彭明山", "G09", "human", FakeConn())
    assert p.commit is None and p.reveal is None
    assert p.status == "LOBBY" and p.out_turn is None
    assert p.records == []
    assert p.skips == 0
    assert len(p.cands) == 5040          # §2.1：四位相異數字共 5040 個
    assert p.cands == list(ALL_NUMBERS)


def test_each_player_owns_a_separate_candidate_list():
    """候選集合是逐玩家刪減的，不能共用同一個 list。"""
    a = Player("p1", "甲", "G09", "human", FakeConn())
    b = Player("p2", "乙", "G09", "ai", FakeConn())
    a.cands.remove("0123")
    assert len(a.cands) == 5039
    assert len(b.cands) == 5040


# ----------------------------------------------------------------------
# B8：廣播對象（§11.3）
# ----------------------------------------------------------------------

def test_audience_includes_players_already_out_but_still_connected():
    """被猜中、棄權、取消資格的人仍在線上就還要收廣播——他們要看到終局名次。"""
    srv = make_server()
    alive = add_player(srv, "甲", status="ALIVE")
    guessed = add_player(srv, "乙", status="GUESSED")
    forfeit = add_player(srv, "丙", status="FORFEIT")
    disqualified = add_player(srv, "丁", status="DQ")

    srv.broadcast({"type": "PING"})
    for p in (alive, guessed, forfeit, disqualified):
        assert p.conn.sent == [{"type": "PING"}], p.pid


def test_audience_excludes_players_removed_during_commit_phase():
    """承諾階段未承諾而被移出本局的人，不屬於廣播對象。"""
    srv = make_server()
    staying = add_player(srv, "甲", status="ALIVE")
    removed = add_player(srv, "乙", status="REMOVED")

    srv.broadcast({"type": "PING"})
    assert staying.conn.sent == [{"type": "PING"}]
    assert removed.conn.sent == []


def test_audience_excludes_closed_connections():
    srv = make_server()
    online = add_player(srv, "甲")
    offline = add_player(srv, "乙")
    offline.conn.close()

    srv.broadcast({"type": "PING"})
    assert online.conn.sent == [{"type": "PING"}]
    assert offline.conn.sent == []


# ----------------------------------------------------------------------
# B8：LOBBY 訊息（§11.2）
# ----------------------------------------------------------------------

def test_lobby_message_passes_the_protocol_layer_validator():
    srv = make_server(players=3)
    add_player(srv, "甲", kind="human")
    add_player(srv, "乙", kind="ai")
    add_player(srv, "丙", kind="ai")

    srv.broadcast_lobby()
    msg = srv.players["p1"].conn.sent[-1]

    assert msg["type"] == "LOBBY"
    assert msg["min_players"] == MIN_PLAYERS == 2   # 固定為 2
    assert msg["max_players"] == 3                   # 即 --players
    # 兩個人各自依規範寫的東西要能互相驗過
    assert validate_server_message(msg) is None


def test_lobby_list_follows_join_order():
    """大廳清單依加入順序（輪轉順序是對局中才用的）。"""
    srv = make_server(players=3)
    for name in ("甲", "乙", "丙"):
        add_player(srv, name)

    srv.broadcast_lobby()
    msg = srv.players["p1"].conn.sent[-1]
    assert [entry["id"] for entry in msg["players"]] == ["p1", "p2", "p3"]
    assert [entry["name"] for entry in msg["players"]] == ["甲", "乙", "丙"]


def test_lobby_list_omits_removed_players():
    srv = make_server(players=3)
    add_player(srv, "甲")
    add_player(srv, "乙", status="REMOVED")

    srv.broadcast_lobby()
    msg = srv.players["p1"].conn.sent[-1]
    assert [entry["id"] for entry in msg["players"]] == ["p1"]
    assert validate_server_message(msg) is None


def test_close_all_conns_covers_connections_without_hello():
    """局末要關閉本局所有連線，包含還沒送 HELLO 的（§5.3-6）。"""
    srv = make_server()
    anonymous = FakeConn("10.0.0.9:51000")
    srv.conns.add(anonymous)
    player = add_player(srv, "甲")
    srv.conns.add(player.conn)

    srv.close_all_conns()
    assert anonymous.closed and player.conn.closed


# ----------------------------------------------------------------------
# B9：wait_until
# ----------------------------------------------------------------------

def test_wait_until_returns_at_once_when_already_true():
    srv = make_server()
    assert asyncio.run(srv.wait_until(lambda: True, 5)) is True


def test_wait_until_times_out_and_reports_false():
    """逾時要回 False，而且真的等過——不能立刻就放棄。

    下界留了寬裕的餘裕：asyncio 允許計時器提早一個時鐘解析度觸發，
    而 Windows 的解析度約 15.6 ms，所以不能拿 0.2 去斷言「至少等了 0.2 秒」。
    """
    srv = make_server()

    async def scenario():
        loop = asyncio.get_running_loop()
        start = loop.time()
        result = await srv.wait_until(lambda: False, 0.2)
        return result, loop.time() - start

    result, elapsed = asyncio.run(scenario())
    assert result is False
    assert elapsed >= 0.15


def test_wait_until_wakes_up_on_changed_set():
    """協定層填答 + 敲鐘，遊戲迴圈要醒過來。"""
    srv = make_server()

    async def scenario():
        async def protocol_layer():
            await asyncio.sleep(0.02)
            srv.cur_guess = "0524"       # A 填答
            srv.guess_open = False
            srv.changed.set()            # A 敲鐘

        asyncio.create_task(protocol_layer())
        return await srv.wait_until(lambda: srv.cur_guess is not None, 5)

    assert asyncio.run(scenario()) is True
    assert srv.cur_guess == "0524"


def test_stale_signal_does_not_fake_a_result():
    """上一回合留下的訊號不能讓這回合誤判條件成立。"""
    srv = make_server()
    srv.changed.set()                     # 殘留的舊訊號
    assert asyncio.run(srv.wait_until(lambda: False, 0.05)) is False


def test_signal_arriving_during_clear_is_not_lost():
    """clear() 把剛到的訊號清掉時，必須靠重新檢查 cond() 補回來。

    這是整個 wait_until 最容易寫錯的地方：漏了 clear() 後的那次檢查，
    這一局就會卡到逾時，而且不會報任何錯。
    """
    srv = make_server()
    state = {"filled": False}
    probes = {"n": 0}

    def cond():
        probes["n"] += 1
        # 模擬「第一次檢查時還沒填，緊接著協定層就填好並敲鐘」：
        # 條件在 clear() 之前才變成真，訊號隨即被 clear() 吃掉。
        if probes["n"] == 1:
            state["filled"] = True
            srv.changed.set()
            return False
        return state["filled"]

    async def scenario():
        loop = asyncio.get_running_loop()
        start = loop.time()
        result = await srv.wait_until(cond, 5)
        return result, loop.time() - start

    result, elapsed = asyncio.run(scenario())
    assert result is True
    assert elapsed < 1          # 若訊號遺失，這裡會等滿 5 秒


# ----------------------------------------------------------------------
# B16：§5.6 日誌
# ----------------------------------------------------------------------

def _read_log(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_log_record_has_exactly_the_four_required_fields():
    with tempfile.TemporaryDirectory() as tmp:
        srv = make_server(log_dir=tmp)
        game_id = srv.new_game_id()
        srv.open_log(game_id)
        srv.log("recv", "10.0.0.9:51000", {"type": "HELLO", "name": "甲"})
        srv.log("send", "p1", {"type": "WELCOME", "player_id": "p1"})

        name = os.listdir(tmp)[0]
        records = _read_log(os.path.join(tmp, name))
        # Windows 不允許刪除還開著的檔案，離開 with 之前要先關掉
        srv.close_log()

    assert len(records) == 2
    for record in records:
        # 欄位名稱與結構必須完全一致（§5.6）
        assert set(record) == {"ts", "dir", "peer", "msg"}
        assert isinstance(record["ts"], (int, float))
        assert record["dir"] in {"send", "recv"}

    # 完成 HELLO 前 peer 寫 IP:埠號，之後寫 player_id
    assert records[0]["dir"] == "recv"
    assert records[0]["peer"] == "10.0.0.9:51000"
    assert records[0]["msg"] == {"type": "HELLO", "name": "甲"}
    assert records[1]["peer"] == "p1"


def test_log_filename_contains_game_id_and_one_file_per_game():
    with tempfile.TemporaryDirectory() as tmp:
        srv = make_server(log_dir=tmp)
        first = srv.new_game_id()
        srv.open_log(first)
        srv.log("send", "p1", {"type": "PING"})

        second = srv.new_game_id()
        srv.open_log(second)              # 下一局：換檔，不是接著寫
        srv.log("send", "p1", {"type": "PING"})
        srv.close_log()

        names = sorted(os.listdir(tmp))

    assert len(names) == 2                     # 一局恰好一個檔
    assert any(first in name for name in names)
    assert any(second in name for name in names)
    assert all(name.endswith(".jsonl") for name in names)


def test_unparseable_input_is_logged_as_raw():
    with tempfile.TemporaryDirectory() as tmp:
        srv = make_server(log_dir=tmp)
        srv.open_log(srv.new_game_id())
        srv.log_raw("recv", "10.0.0.9:51000", "not json at all")
        srv.close_log()

        records = _read_log(os.path.join(tmp, os.listdir(tmp)[0]))

    assert records[0]["msg"] == {"raw": "not json at all"}
    assert set(records[0]) == {"ts", "dir", "peer", "msg"}


def test_each_record_is_flushed_immediately():
    """助教會在對局進行中讀日誌，所以不能等關檔才落盤（§5.6）。"""
    with tempfile.TemporaryDirectory() as tmp:
        srv = make_server(log_dir=tmp)
        srv.open_log(srv.new_game_id())
        srv.log("send", "p1", {"type": "PING"})

        # 還沒 close_log，檔案就該讀得到內容
        records = _read_log(os.path.join(tmp, os.listdir(tmp)[0]))
        assert len(records) == 1
        srv.close_log()


def test_log_before_open_is_dropped_instead_of_crashing():
    """大廳還沒開、logf 是 None 時寫日誌不能炸掉伺服器。"""
    srv = make_server()
    srv.log("send", "p1", {"type": "PING"})      # 不應拋出例外
    srv.log_raw("recv", "1.2.3.4:5", "garbage")


def test_log_is_utf8_so_chinese_names_survive():
    with tempfile.TemporaryDirectory() as tmp:
        srv = make_server(log_dir=tmp)
        srv.open_log(srv.new_game_id())
        srv.log("recv", "p1", {"type": "CHAT", "text": "幾A幾B"})
        srv.close_log()

        path = os.path.join(tmp, os.listdir(tmp)[0])
        with open(path, encoding="utf-8") as handle:
            assert "幾A幾B" in handle.read()
        assert _read_log(path)[0]["msg"]["text"] == "幾A幾B"


def test_new_game_id_matches_the_field_format():
    from bcproto.validators import valid_game_id
    srv = make_server()
    ids = {srv.new_game_id() for _ in range(100)}
    assert len(ids) == 100                       # 不重複
    assert all(valid_game_id(value) for value in ids)


if __name__ == "__main__":
    passed = failed = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                passed += 1
            except Exception as exc:
                failed += 1
                print(f"FAIL {name}: {exc!r}")
    print(f"{passed} 項通過，{failed} 項失敗")
    sys.exit(1 if failed else 0)
