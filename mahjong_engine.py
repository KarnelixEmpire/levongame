"""
Luật mạt chược dùng cho bản web online.
Không phụ thuộc Discord. Mỗi quân bài là một chuỗi:
  m1..m9 = Vạn, p1..p9 = Đồng, s1..s9 = Sách, z1..z7 = Chữ (Đông, Nam, Tây, Bắc, Trung, Phát, Bạch)

Chế độ ghép đội: 2 đội A và B. Người thắng được chia tiền cho cả đội,
người ở đội đối phương trả tiền, đồng đội không trả tiền.
"""
import random
from collections import Counter
from typing import Dict, List, Optional

START_MONEY = 200_000
PAYOUT_PER_YAKU = 10_000
MIN_PLAYERS = 2
MAX_PLAYERS = 4
SUIT_ORDER = {"m": 0, "p": 1, "s": 2, "z": 3}


class GameError(Exception):
    pass


def sort_key(code: str):
    return (SUIT_ORDER[code[0]], int(code[1]))


def build_deck(num_players: int) -> List[str]:
    # Sanma (3 người): bỏ quân Vạn
    suits = ["p", "s"] if num_players == 3 else ["m", "p", "s"]
    deck = [f"{s}{v}" for s in suits for v in range(1, 10) for _ in range(4)]
    deck += [f"z{v}" for v in range(1, 8) for _ in range(4)]
    random.shuffle(deck)
    return deck


def _sets_ok(counts: Dict[str, int]) -> bool:
    """Kiểm tra các quân còn lại có tạo thành đủ bộ (3 giống nhau hoặc 3 liên tiếp cùng chất)."""
    keys = sorted(k for k, v in counts.items() if v > 0)
    if not keys:
        return True
    k = keys[0]
    suit, v = k[0], int(k[1])
    if counts[k] >= 3:
        counts[k] -= 3
        ok = _sets_ok(counts)
        counts[k] += 3
        if ok:
            return True
    if suit != "z" and v <= 7:
        k2, k3 = f"{suit}{v + 1}", f"{suit}{v + 2}"
        if counts.get(k2, 0) >= 1 and counts.get(k3, 0) >= 1:
            counts[k] -= 1
            counts[k2] -= 1
            counts[k3] -= 1
            ok = _sets_ok(counts)
            counts[k] += 1
            counts[k2] += 1
            counts[k3] += 1
            if ok:
                return True
    return False


def is_winning(tiles: List[str]) -> bool:
    """14 quân = 4 bộ + 1 đôi."""
    if len(tiles) != 14:
        return False
    counts = Counter(tiles)
    for k, c in counts.items():
        if c >= 2:
            trial = dict(counts)
            trial[k] -= 2
            if _sets_ok(trial):
                return True
    return False


def find_yaku(tiles: List[str]) -> List[str]:
    yaku = []
    counts = Counter(tiles)
    if sum(1 for c in counts.values() if c >= 3) >= 3:
        yaku.append("Toi (3 bộ ba)")
    if all(not t.startswith("z") for t in tiles):
        yaku.append("Tan Yao (không có Chữ)")
    yaku.append("Tsumo (tự rút)")
    return yaku


class Player:
    def __init__(self, pid: str, name: str):
        self.pid = pid
        self.name = name
        self.money = START_MONEY
        self.hand: List[str] = []
        self.discards: List[str] = []
        self.team: Optional[str] = None   # "A" | "B" | None (chơi lẻ)
        self.ws = None                    # kết nối WebSocket hiện tại, None nếu đã ngắt

    @property
    def online(self) -> bool:
        return self.ws is not None


class Room:
    kind = "mahjong"

    def __init__(self, code: str, host: str, team_mode: bool = False):
        self.code = code
        self.host = host
        self.team_mode = team_mode
        self.players: List[Player] = []
        self.state = "lobby"      # lobby | playing | ended
        self.wall: List[str] = []
        self.turn = 0
        self.phase = "draw"       # draw | discard
        self.result: Optional[dict] = None
        self.log: List[str] = []

    # ---- tiện ích ----
    def player(self, pid: str) -> Optional[Player]:
        return next((p for p in self.players if p.pid == pid), None)

    def current(self) -> Optional[Player]:
        if self.state == "playing" and self.players:
            return self.players[self.turn]
        return None

    def _log(self, text: str) -> None:
        self.log.append(text)
        del self.log[:-40]

    # ---- thành viên ----
    def add(self, pid: str, name: str) -> None:
        if self.state == "playing":
            raise GameError("Ván đang diễn ra, không thể vào lúc này.")
        if self.player(pid):
            return
        if len(self.players) >= MAX_PLAYERS:
            raise GameError(f"Bàn đã đủ {MAX_PLAYERS} người.")
        self.players.append(Player(pid, name))
        self._log(f"{name} đã vào bàn.")

    def set_team(self, pid: str, team: Optional[str]) -> None:
        if self.state == "playing":
            raise GameError("Không đổi đội khi ván đang diễn ra.")
        if not self.team_mode:
            raise GameError("Bàn này không chơi theo đội.")
        if team not in ("A", "B"):
            raise GameError("Đội phải là A hoặc B.")
        p = self.player(pid)
        if not p:
            raise GameError("Bạn chưa vào bàn.")
        if p.team != team:
            p.team = team
            self._log(f"{p.name} vào Đội {team}.")

    def remove(self, pid: str) -> None:
        if self.state == "playing":
            raise GameError("Ván đang diễn ra, không thể rời lúc này.")
        p = self.player(pid)
        if p:
            self.players.remove(p)
            self._log(f"{p.name} đã rời bàn.")
            if self.host == pid and self.players:
                self.host = self.players[0].pid

    # ---- ván chơi ----
    def start(self, pid: str) -> None:
        if pid != self.host:
            raise GameError("Chỉ chủ bàn mới bắt đầu được.")
        if self.state == "playing":
            raise GameError("Ván đã bắt đầu rồi.")
        if len(self.players) < MIN_PLAYERS:
            raise GameError(f"Cần ít nhất {MIN_PLAYERS} người để bắt đầu.")
        if self.team_mode:
            teams = {p.team for p in self.players}
            if None in teams:
                raise GameError("Mọi người phải chọn đội A hoặc B trước khi bắt đầu.")
            if teams != {"A", "B"}:
                raise GameError("Cần có người ở cả Đội A và Đội B.")
        self.wall = build_deck(len(self.players))
        for p in self.players:
            p.hand = [self.wall.pop() for _ in range(13)]
            p.hand.sort(key=sort_key)
            p.discards = []
        self.state = "playing"
        self.turn = 0
        self.phase = "draw"
        self.result = None
        self._log("Ván mới bắt đầu.")

    def draw(self, pid: str) -> None:
        if self.state != "playing":
            raise GameError("Ván chưa bắt đầu hoặc đã kết thúc.")
        p = self.current()
        if p.pid != pid:
            raise GameError(f"Chưa đến lượt bạn. Đang chờ {p.name}.")
        if self.phase != "draw":
            raise GameError("Bạn cần đánh một quân trước.")
        if not self.wall:
            self.state = "ended"
            self.result = {"type": "draw"}
            self._log("Hết quân trên tường, ván hòa.")
            return
        p.hand.append(self.wall.pop())
        p.hand.sort(key=sort_key)
        self.phase = "discard"
        self._log(f"{p.name} đã rút bài.")
        if is_winning(p.hand):
            self._settle(p)

    def discard(self, pid: str, idx: int) -> None:
        if self.state != "playing":
            raise GameError("Ván chưa bắt đầu hoặc đã kết thúc.")
        p = self.current()
        if p.pid != pid:
            raise GameError("Chưa đến lượt bạn.")
        if self.phase != "discard":
            raise GameError("Bạn cần rút bài trước.")
        if not 0 <= idx < len(p.hand):
            raise GameError("Quân bài không hợp lệ.")
        tile = p.hand.pop(idx)
        p.discards.append(tile)
        self._log(f"{p.name} đánh {tile}.")
        self.turn = (self.turn + 1) % len(self.players)
        self.phase = "draw"

    def _winners_of(self, winner: Player) -> List[Player]:
        if winner.team is None:
            return [winner]
        return [p for p in self.players if p.team == winner.team]

    def _settle(self, winner: Player) -> None:
        yaku = find_yaku(winner.hand)
        per_loser = PAYOUT_PER_YAKU * len(yaku)
        gained, losers = 0, []
        for other in self.players:
            if other is winner:
                continue
            if winner.team is not None and other.team == winner.team:
                continue  # đồng đội không trả tiền
            pay = min(per_loser, other.money)
            other.money -= pay
            gained += pay
            losers.append([other.name, pay])
        winners = self._winners_of(winner)
        share = gained // len(winners)
        for w in winners:
            w.money += share
        winners[0].money += gained - share * len(winners)  # phần dư (nếu có) cho người thắng
        self.state = "ended"
        self.result = {
            "type": "win",
            "winners": [w.name for w in winners],
            "winner_pid": winner.pid,
            "yaku": yaku,
            "gained": gained,
            "losers": losers,
        }
        names = ", ".join(w.name for w in winners)
        self._log(f"{names} TSUMO! Nhận +{gained:,} VNĐ.")

    # ---- dữ liệu gửi cho trình duyệt ----
    def view(self, you: str) -> dict:
        cur = self.current()
        me = self.player(you)
        return {
            "code": self.code,
            "kind": self.kind,
            "team_mode": self.team_mode,
            "state": self.state,
            "host": self.host,
            "you": you,
            "turn": cur.pid if cur else None,
            "phase": self.phase,
            "wall": len(self.wall),
            "players": [
                {
                    "pid": p.pid,
                    "name": p.name,
                    "team": p.team,
                    "money": p.money,
                    "discards": p.discards,
                    "count": len(p.hand),
                    "online": p.online,
                }
                for p in self.players
            ],
            "hand": me.hand if me else [],
            "result": self.result,
            "log": self.log[-8:],
        }
