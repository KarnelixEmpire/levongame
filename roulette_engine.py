"""
Cò quay Nga cho bản web online. Tiền là tiền ảo, lưu trong từng bàn.
Luật: ổ có 6 phòng, chọn số viên đạn (1-5). Mỗi lượt bấm cò, ổ xoay một phòng.
Trúng đạn thì bị loại.

Chơi lẻ: người cuối cùng còn sống thắng.
Chơi theo đội: đội cuối cùng còn người sống thắng, các thành viên đội thắng chia tiền.
Mỗi người thua trả đúng tiền cược.
"""
import random
from typing import List, Optional

from mahjong_engine import GameError

CHAMBERS = 6
MIN_PLAYERS = 2
MAX_PLAYERS = 8
START_CHIPS = 200_000
MAX_WEB_BET = 200_000  # không cho cược quá số vốn ban đầu


class RPlayer:
    def __init__(self, pid: str, name: str):
        self.pid = pid
        self.name = name
        self.money = START_CHIPS
        self.alive = True
        self.team: Optional[str] = None
        self.ws = None

    @property
    def online(self) -> bool:
        return self.ws is not None


class RouletteRoom:
    kind = "russian"

    def __init__(self, code: str, host: str, bullets: int, bet: int, team_mode: bool = False):
        if not 1 <= bullets <= 5:
            raise GameError("Số viên đạn phải từ 1 đến 5.")
        if not 1000 <= bet <= MAX_WEB_BET:
            raise GameError(f"Tiền cược phải từ 1.000 đến {MAX_WEB_BET:,} VNĐ.")
        self.code = code
        self.host = host
        self.bullets = bullets
        self.bet = bet
        self.team_mode = team_mode
        self.players: List[RPlayer] = []
        self.state = "lobby"      # lobby | playing | ended
        self.turn = 0
        self.ptr = 0              # phòng đang dưới kim
        self.loaded: set = set()  # phòng có đạn (không gửi cho trình duyệt)
        self.spent: set = set()   # phòng đã bấm an toàn
        self.result: Optional[dict] = None
        self.log: List[str] = []

    # ---- tiện ích ----
    def player(self, pid: str) -> Optional[RPlayer]:
        return next((p for p in self.players if p.pid == pid), None)

    def current(self) -> Optional[RPlayer]:
        if self.state == "playing" and self.players:
            return self.players[self.turn]
        return None

    def _log(self, text: str) -> None:
        self.log.append(text)
        del self.log[:-40]

    def _reload(self) -> None:
        self.loaded = set(random.sample(range(CHAMBERS), self.bullets))
        self.spent = set()

    # ---- thành viên ----
    def add(self, pid: str, name: str) -> None:
        if self.state == "playing":
            raise GameError("Ván đang diễn ra, không thể vào lúc này.")
        if self.player(pid):
            return
        if len(self.players) >= MAX_PLAYERS:
            raise GameError(f"Bàn đã đủ {MAX_PLAYERS} người.")
        self.players.append(RPlayer(pid, name))
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
        for p in self.players:
            if p.money < self.bet:
                raise GameError(f"{p.name} không đủ {self.bet:,} VNĐ để cược.")
        for p in self.players:
            p.alive = True
        self.state = "playing"
        self.turn = 0
        self.ptr = 0
        self.result = None
        self._reload()
        self._log("Ván mới bắt đầu.")

    def _advance(self) -> None:
        n = len(self.players)
        i = (self.turn + 1) % n
        while not self.players[i].alive:
            i = (i + 1) % n
        self.turn = i

    def _winners_now(self) -> Optional[List[RPlayer]]:
        alive = [x for x in self.players if x.alive]
        if self.team_mode:
            if alive and len({x.team for x in alive}) == 1:
                return alive
            return None
        if len(alive) == 1:
            return alive
        return None

    def pull(self, pid: str) -> None:
        if self.state != "playing":
            raise GameError("Ván chưa bắt đầu hoặc đã kết thúc.")
        p = self.current()
        if p.pid != pid:
            raise GameError(f"Chưa đến lượt bạn. Đang chờ {p.name}.")

        if self.ptr in self.loaded:
            p.alive = False
            self._log(f"💥 {p.name} trúng đạn, bị loại.")
            self._reload()
            winners = self._winners_now()
            if winners:
                self.ptr = (self.ptr + 1) % CHAMBERS
                self._settle(winners)
                return
        else:
            self.spent.add(self.ptr)
            self._log(f"✅ {p.name}: Cạch, an toàn.")

        self.ptr = (self.ptr + 1) % CHAMBERS
        self._advance()

    def _settle(self, winners: List[RPlayer]) -> None:
        winner_ids = {w.pid for w in winners}
        gained, losers = 0, []
        for other in self.players:
            if other.pid in winner_ids:
                continue
            pay = min(self.bet, other.money)
            other.money -= pay
            gained += pay
            losers.append([other.name, pay])
        share = gained // len(winners)
        for w in winners:
            w.money += share
        winners[0].money += gained - share * len(winners)
        self.state = "ended"
        self.result = {
            "type": "win",
            "winners": [w.name for w in winners],
            "winner_pid": winners[0].pid,
            "gained": gained,
            "losers": losers,
        }
        names = ", ".join(w.name for w in winners)
        self._log(f"🏆 {names} thắng, nhận +{gained:,} VNĐ.")

    def end_early(self, pid: str) -> None:
        if pid != self.host:
            raise GameError("Chỉ chủ bàn mới kết thúc sớm được.")
        if self.state != "playing":
            raise GameError("Ván chưa bắt đầu.")
        self.state = "ended"
        self.result = {"type": "early"}
        self._log("Chủ bàn kết thúc sớm. Không ai mất tiền.")

    # ---- dữ liệu gửi cho trình duyệt ----
    def view(self, you: str) -> dict:
        cur = self.current()
        return {
            "code": self.code,
            "kind": self.kind,
            "team_mode": self.team_mode,
            "state": self.state,
            "host": self.host,
            "you": you,
            "turn": cur.pid if cur else None,
            "bullets": self.bullets,
            "bet": self.bet,
            "chambers": [
                {"spent": i in self.spent, "current": i == self.ptr}
                for i in range(CHAMBERS)
            ],
            "players": [
                {
                    "pid": p.pid,
                    "name": p.name,
                    "team": p.team,
                    "money": p.money,
                    "alive": p.alive,
                    "online": p.online,
                }
                for p in self.players
            ],
            "result": self.result,
            "log": self.log[-8:],
        }
