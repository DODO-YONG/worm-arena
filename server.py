#!/usr/bin/env python3
"""꿈틀 아레나 멀티플레이 서버.

파이썬 표준 라이브러리만 사용한다 (pip 설치 불필요).
- 같은 포트에서 웹 페이지(public/index.html)와 WebSocket(/ws)을 함께 제공한다.
- 게임 진행(이동, 먹이, 충돌, 봇)은 전부 서버가 계산하고,
  브라우저는 방향/부스트 입력만 보내고 받은 상태를 그린다.

실행:  python server.py            (기본 포트 8080)
       python server.py 9000       (포트 지정)
"""
import asyncio
import base64
import colorsys
import hashlib
import json
import math
import random
import socket
import struct
import sys
import time
import traceback
from pathlib import Path

PORT = 8080
PUBLIC = Path(__file__).resolve().parent / "public"
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/solo.html": "solo.html"}

WORLD_R = 3000
BOT_COUNT = 16
FOOD_TARGET = 1100
FOOD_MAX = 4000
TICK_HZ = 30
SEND_EVERY = 2          # 30Hz 계산, 15Hz 전송
LB_EVERY = 15           # 0.5초마다 순위/미니맵
BASE_SPEED = 175
BOOST_SPEED = 360
START_SCORE = 14
CELL = 100
TAU = math.tau

PALETTES = [
    ("#ff5d73", "#ffd166"), ("#06d6a0", "#118ab2"), ("#8338ec", "#ff006e"),
    ("#ffbe0b", "#fb5607"), ("#3a86ff", "#8ecae6"), ("#80ed99", "#38a3a5"),
    ("#f15bb5", "#fee440"), ("#e0e1dd", "#778da9"), ("#ff9f1c", "#2ec4b6"),
]
BOT_NAMES = ["꼬물이", "지렁왕", "느림보", "번개", "초코", "말랑이", "쭉쭉이", "구불구불", "밤톨", "새싹",
             "두부", "라면", "호떡", "감자", "먹보", "꿈틀대장", "고구마", "미역", "도토리", "젤리",
             "통통이", "모찌", "바람", "은하수", "콩떡", "방울", "솜사탕", "단무지"]
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ang_diff(a, b):
    d = (b - a) % TAU
    if d > math.pi:
        d -= TAU
    return d


def clamp(v, a, b):
    return a if v < a else b if v > b else v


# --------------------------------------------------------------------------
# 게임 객체
# --------------------------------------------------------------------------
class Worm:
    def __init__(self, wid, x, y, ang, name, pal, score, client=None):
        self.id = wid
        self.name = name
        self.pal = pal
        self.client = client
        self.score = score
        self.angle = ang
        self.target = ang
        self.boost = False
        self.boosting = False
        self.alive = True
        self.kills = 0
        self.drop_t = 0.0
        self.think_t = 0.0
        self.wander_t = 0
        self.aggr = random.random() * 0.7
        self.r = self.radius()
        sp = self.spacing()
        self.segs = [[x - math.cos(ang) * i * sp, y - math.sin(ang) * i * sp] for i in range(self.target_len())]
        self.bbox = (x, y, x, y)
        self.flat = []

    def radius(self):
        return min(28.0, 7 + math.sqrt(max(self.score, 0)) * 0.5)

    def spacing(self):
        return self.r * 0.6

    def target_len(self):
        return min(900, int(20 + self.score * 0.35))

    def update(self, dt, game):
        mt = max(2.2, 5.4 - self.r * 0.1) * dt
        self.angle = (self.angle + clamp(ang_diff(self.angle, self.target), -mt, mt)) % TAU

        self.boosting = self.boost and self.score > 10
        speed = BOOST_SPEED if self.boosting else BASE_SPEED
        if self.boosting:
            self.score -= 9 * dt
            self.drop_t += dt
            if self.drop_t > 0.1:
                self.drop_t = 0
                t = self.segs[-1]
                game.add_food(t[0] + random.uniform(-4, 4), t[1] + random.uniform(-4, 4), 0.75, 1000 + self.pal * 2)

        segs = self.segs
        h = segs[0]
        h[0] += math.cos(self.angle) * speed * dt
        h[1] += math.sin(self.angle) * speed * dt
        sp = self.spacing()
        it = iter(segs)
        prev = next(it)
        for b in it:
            dx = prev[0] - b[0]
            dy = prev[1] - b[1]
            dd = math.hypot(dx, dy)
            if dd > sp:
                k = (dd - sp) / dd
                b[0] += dx * k
                b[1] += dy * k
            prev = b
        tl = self.target_len()
        while len(segs) < tl:
            t = segs[-1]
            segs.append([t[0], t[1]])
        if len(segs) > tl:
            del segs[tl:]
        self.r = self.radius()

    # 봇 AI ------------------------------------------------------------
    def think(self, game):
        h = self.segs[0]
        r = self.r
        ca, sa = math.cos(self.angle), math.sin(self.angle)
        self.boost = False

        # 1) 경계 회피
        if math.hypot(h[0], h[1]) > WORLD_R - 220 - r * 3:
            self.target = math.atan2(-h[1], -h[0]) + random.uniform(-0.4, 0.4)
            return

        # 2) 다른 지렁이 몸 회피
        look = 110 + r * 4
        look2 = look * look
        ax = ay = threat = 0.0
        for lst in game.query(game.seg_grid, h[0] - look, h[1] - look, h[0] + look, h[1] + look):
            for (o, s) in lst:
                if o is self or not o.alive:
                    continue
                dx = s[0] - h[0]
                dy = s[1] - h[1]
                d2 = dx * dx + dy * dy
                if d2 > look2 or d2 < 1:
                    continue
                d = math.sqrt(d2)
                dot = (dx * ca + dy * sa) / d
                if dot < -0.3:
                    continue
                w = (1 - d / look) * (1 + dot)
                ax -= dx / d * w
                ay -= dy / d * w
                if w > threat:
                    threat = w
        if threat > 0.15:
            self.target = math.atan2(ay + sa * 0.2, ax + ca * 0.2)
            if threat > 1.1 and self.score > 30 and random.random() < 0.5:
                self.boost = True
            return

        # 3) 사냥: 자기보다 작은 지렁이 앞을 가로막기
        if self.score > 60 and random.random() < self.aggr * 0.5:
            prey, best = None, 600 * 600
            for o in game.worms.values():
                if o is self or not o.alive or o.score > self.score * 1.2:
                    continue
                oh = o.segs[0]
                d2 = (oh[0] - h[0]) ** 2 + (oh[1] - h[1]) ** 2
                if d2 < best:
                    best, prey = d2, o
            if prey:
                ph = prey.segs[0]
                lead = 120 + prey.r * 3
                tx = ph[0] + math.cos(prey.angle) * lead
                ty = ph[1] + math.sin(prey.angle) * lead
                self.target = math.atan2(ty - h[1], tx - h[0])
                if self.score > 40 and random.random() < 0.4:
                    self.boost = True
                return

        # 4) 먹이 찾기
        best_f, bs, fr = None, 0.0, 420
        for lst in game.query(game.food_grid, h[0] - fr, h[1] - fr, h[0] + fr, h[1] + fr):
            for f in lst:
                if f[2] <= 0:
                    continue
                dx = f[0] - h[0]
                dy = f[1] - h[1]
                d = math.hypot(dx, dy) + 20
                dot = (dx * ca + dy * sa) / d
                if dot < 0 and d < 140:
                    continue
                sc = f[2] / d * (1.4 + dot)
                if sc > bs:
                    bs, best_f = sc, f
        if best_f:
            self.target = math.atan2(best_f[1] - h[1], best_f[0] - h[0])
            return

        # 5) 배회
        self.wander_t -= 1
        if self.wander_t <= 0:
            self.target = self.angle + random.uniform(-1.2, 1.2)
            self.wander_t = random.randint(5, 15)


class Game:
    def __init__(self):
        self.worms = {}
        self.foods = []          # [x, y, value, radius, color]
        self.clients = set()
        self.time = 0.0
        self.next_id = 1
        self.respawn = []
        self.seg_grid = {}
        self.food_grid = {}
        for _ in range(BOT_COUNT):
            self.spawn_bot()
        for _ in range(FOOD_TARGET):
            self.spawn_ambient_food()
        self.build_seg_grid()
        self.build_food_grid()

    # ---- 헬퍼 ----
    def new_id(self):
        self.next_id += 1
        return self.next_id

    def add_food(self, x, y, v, color=None):
        if color is None:
            color = random.randrange(360)
        self.foods.append([x, y, v, 3 + min(8.0, v * 0.9), color])

    def spawn_ambient_food(self):
        a = random.random() * TAU
        d = math.sqrt(random.random()) * (WORLD_R - 40)
        self.add_food(math.cos(a) * d, math.sin(a) * d, random.uniform(1, 2.6))

    def safe_spawn(self, min_dist):
        best_p, best_d = None, -1
        for _ in range(30):
            a = random.random() * TAU
            d = math.sqrt(random.random()) * WORLD_R * 0.7
            x, y = math.cos(a) * d, math.sin(a) * d
            nearest = float("inf")
            for w in self.worms.values():
                for s in w.segs[::4]:
                    dd = math.hypot(s[0] - x, s[1] - y)
                    if dd < nearest:
                        nearest = dd
            if nearest > min_dist:
                return x, y
            if nearest > best_d:
                best_d, best_p = nearest, (x, y)
        return best_p

    def spawn_bot(self):
        x, y = self.safe_spawn(350)
        score = random.uniform(150, 400) if random.random() < 0.15 else random.uniform(10, 60)
        ang = math.atan2(-y, -x) + random.uniform(-1, 1)
        w = Worm(self.new_id(), x, y, ang, random.choice(BOT_NAMES), random.randrange(len(PALETTES)), score)
        self.worms[w.id] = w

    @staticmethod
    def query(grid, x0, y0, x1, y1):
        cx0, cy0 = int(x0 // CELL), int(y0 // CELL)
        cx1, cy1 = int(x1 // CELL), int(y1 // CELL)
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                lst = grid.get((cx, cy))
                if lst:
                    yield lst

    def build_seg_grid(self):
        grid = {}
        for w in self.worms.values():
            minx = miny = float("inf")
            maxx = maxy = float("-inf")
            for s in w.segs:
                x, y = s
                grid.setdefault((int(x // CELL), int(y // CELL)), []).append((w, s))
                if x < minx: minx = x
                if x > maxx: maxx = x
                if y < miny: miny = y
                if y > maxy: maxy = y
            w.bbox = (minx - w.r, miny - w.r, maxx + w.r, maxy + w.r)
        self.seg_grid = grid

    def build_food_grid(self):
        grid = {}
        for f in self.foods:
            grid.setdefault((int(f[0] // CELL), int(f[1] // CELL)), []).append(f)
        self.food_grid = grid

    # ---- 진행 ----
    def eat(self, w, dt):
        h = w.segs[0]
        r = w.r
        reach = r * 2 + 30
        for lst in self.query(self.food_grid, h[0] - reach, h[1] - reach, h[0] + reach, h[1] + reach):
            for f in lst:
                if f[2] <= 0:
                    continue
                dx = f[0] - h[0]
                dy = f[1] - h[1]
                d = math.hypot(dx, dy)
                if d < r + f[3] + 2:
                    w.score += f[2]
                    f[2] = -1
                elif 0 < d < reach:
                    pull = min(d, 220 * dt)
                    f[0] -= dx / d * pull
                    f[1] -= dy / d * pull

    def kill(self, w, killer):
        if not w.alive:
            return
        w.alive = False
        r = w.r
        n = len(w.segs)
        v = max(0.8, w.score * 0.7 / math.ceil(n / 2))
        for i in range(0, n, 2):
            s = w.segs[i]
            self.add_food(s[0] + random.uniform(-r, r) * 0.6, s[1] + random.uniform(-r, r) * 0.6, v,
                          1000 + w.pal * 2 + ((i // 2) % 2))
        self.worms.pop(w.id, None)
        if killer:
            killer.kills += 1
            if killer.client:
                killer.client.send_json({"t": "kill", "name": w.name})
        if w.client:
            w.client.on_death(w, killer)
        else:
            self.respawn.append(self.time + random.uniform(1.5, 4))

    def check_deaths(self):
        for a in list(self.worms.values()):
            if not a.alive:
                continue
            h = a.segs[0]
            ra = a.r
            if math.hypot(h[0], h[1]) > WORLD_R - ra:
                self.kill(a, None)
                continue
            m = ra + 30
            hit = None
            for lst in self.query(self.seg_grid, h[0] - m, h[1] - m, h[0] + m, h[1] + m):
                for (b, s) in lst:
                    if b is a or not b.alive:
                        continue
                    lim = (ra + b.r) * 0.85
                    dx = s[0] - h[0]
                    dy = s[1] - h[1]
                    if dx * dx + dy * dy < lim * lim:
                        hit = b
                        break
                if hit:
                    break
            if hit:
                self.kill(a, hit)

    def step(self, dt):
        self.time += dt
        for w in list(self.worms.values()):
            if w.client is None:
                w.think_t -= dt
                if w.think_t <= 0:
                    w.think(self)
                    w.think_t = random.uniform(0.08, 0.18)
        for w in list(self.worms.values()):
            w.update(dt, self)
            self.eat(w, dt)
        self.build_seg_grid()
        self.check_deaths()

        due = [t for t in self.respawn if t <= self.time]
        if due:
            self.respawn = [t for t in self.respawn if t > self.time]
            for _ in due:
                self.spawn_bot()

        self.foods = [f for f in self.foods if f[2] > 0]
        for _ in range(25):
            if len(self.foods) >= FOOD_TARGET:
                break
            self.spawn_ambient_food()
        if len(self.foods) > FOOD_MAX:
            del self.foods[: len(self.foods) - FOOD_MAX]
        self.build_food_grid()

    # ---- 네트워크 ----
    def top_worm(self):
        return max(self.worms.values(), key=lambda w: w.score, default=None)

    def broadcast(self):
        if not self.clients:
            return
        for w in self.worms.values():
            w.flat = [round(v) for s in w.segs for v in s]
        top = self.top_worm()
        tm = round(self.time, 3)
        for c in list(self.clients):
            cx, cy = c.view_center(top)
            hw, hh = c.hw + 200, c.hh + 200
            x0, y0, x1, y1 = cx - hw, cy - hh, cx + hw, cy + hh
            ws = []
            for w in self.worms.values():
                b = w.bbox
                if b[2] < x0 or b[0] > x1 or b[3] < y0 or b[1] > y1:
                    continue
                ws.append([w.id, w.pal, w.name, round(w.r * 10), round(w.angle * 100), 1 if w.boosting else 0, w.flat])
            fs = []
            for lst in self.query(self.food_grid, x0, y0, x1, y1):
                for f in lst:
                    if f[2] > 0:
                        fs.extend((round(f[0]), round(f[1]), round(f[3] * 10), f[4]))
            me = c.worm.id if c.worm and c.worm.alive else 0
            c.send_json({"t": "s", "tm": tm, "me": me, "c": [round(cx), round(cy)], "w": ws, "f": fs})

    def send_leaderboard(self):
        if not self.clients:
            return
        ranked = sorted(self.worms.values(), key=lambda w: w.score, reverse=True)
        heads = []
        for w in ranked:
            h = w.segs[0]
            heads.extend((round(h[0]), round(h[1]), round(w.r)))
        players = sum(1 for c in self.clients if c.worm and c.worm.alive)
        for c in list(self.clients):
            me = c.worm if c.worm and c.worm.alive else None
            msg = {
                "t": "lb",
                "top": [[w.name, int(w.score), 1 if w is me else 0] for w in ranked[:10]],
                "n": len(ranked),
                "heads": heads,
                "online": len(self.clients),
                "players": players,
            }
            if me:
                msg["rank"] = ranked.index(me) + 1
                msg["score"] = int(me.score)
                msg["kills"] = me.kills
            c.send_json(msg)

    def on_message(self, c, msg):
        t = msg.get("t")
        if t == "join":
            if c.worm and c.worm.alive:
                return
            name = str(msg.get("name") or "").strip()[:12] or "손님"
            try:
                pal = int(msg.get("pal", 0)) % len(PALETTES)
            except (TypeError, ValueError):
                pal = 0
            x, y = self.safe_spawn(500)
            w = Worm(self.new_id(), x, y, math.atan2(-y, -x), name, pal, START_SCORE, client=c)
            self.worms[w.id] = w
            c.worm = w
            c.send_json({"t": "joined", "id": w.id})
            print(f"[입장] {name}  (접속 {len(self.clients)}명)")
        elif t == "in":
            try:
                c.hw = clamp(float(msg.get("vw", c.hw)), 200, 2600)
                c.hh = clamp(float(msg.get("vh", c.hh)), 200, 2600)
                a = msg.get("a")
                if c.worm and c.worm.alive:
                    if a is not None and math.isfinite(float(a)):
                        c.worm.target = float(a)
                    c.worm.boost = bool(msg.get("b"))
            except (TypeError, ValueError):
                pass

    def on_disconnect(self, c):
        self.clients.discard(c)
        if c.worm and c.worm.alive:
            c.worm.client = None
            self.kill(c.worm, None)
            self.respawn.pop()  # 사람 자리에 봇이 생기지 않도록
        print(f"[퇴장] 접속 {len(self.clients)}명")

    async def run(self):
        dt = 1 / TICK_HZ
        nxt = time.perf_counter()
        tick = 0
        while True:
            try:
                self.step(dt)
                tick += 1
                if tick % SEND_EVERY == 0:
                    self.broadcast()
                if tick % LB_EVERY == 0:
                    self.send_leaderboard()
            except Exception:
                traceback.print_exc()
            nxt += dt
            delay = nxt - time.perf_counter()
            if delay < -0.25:
                nxt = time.perf_counter()
                delay = 0
            await asyncio.sleep(max(0.0, delay))


class Client:
    def __init__(self, game, writer):
        self.game = game
        self.writer = writer
        self.worm = None
        self.hw = 900
        self.hh = 600
        self.cx = 0.0
        self.cy = 0.0
        self.hold_until = 0.0
        self.closed = False

    def view_center(self, top):
        if self.worm and self.worm.alive:
            h = self.worm.segs[0]
            self.cx, self.cy = h[0], h[1]
        elif self.game.time >= self.hold_until and top:
            h = top.segs[0]
            self.cx, self.cy = h[0], h[1]
        return self.cx, self.cy

    def on_death(self, w, killer):
        h = w.segs[0]
        self.cx, self.cy = h[0], h[1]
        self.hold_until = self.game.time + 4
        self.send_json({"t": "dead", "score": int(w.score), "kills": w.kills, "by": killer.name if killer else None})
        print(f"[탈락] {w.name}  길이 {int(w.score)}")

    def send_json(self, obj):
        if self.closed:
            return
        transport = self.writer.transport
        if transport.is_closing() or transport.get_write_buffer_size() > 2_000_000:
            return  # 너무 느린 클라이언트는 이번 프레임을 건너뜀
        data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
        self.writer.write(ws_frame(data))


# --------------------------------------------------------------------------
# 최소 HTTP + WebSocket 구현
# --------------------------------------------------------------------------
def ws_frame(payload, opcode=1):
    n = len(payload)
    if n < 126:
        hdr = bytes([0x80 | opcode, n])
    elif n < 65536:
        hdr = bytes([0x80 | opcode, 126]) + struct.pack("!H", n)
    else:
        hdr = bytes([0x80 | opcode, 127]) + struct.pack("!Q", n)
    return hdr + payload


async def read_frame(reader):
    b1, b2 = await reader.readexactly(2)
    op = b1 & 0x0F
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack("!H", await reader.readexactly(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", await reader.readexactly(8))[0]
    if n > 65536:
        raise ValueError("frame too large")
    mask = await reader.readexactly(4) if b2 & 0x80 else None
    data = await reader.readexactly(n)
    if mask:
        data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
    return op, data


GAME = None


async def handle(reader, writer):
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        lines = head.decode("latin-1").split("\r\n")
        _method, path, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        path = path.split("?", 1)[0]
        if headers.get("upgrade", "").lower() == "websocket" and path == "/ws":
            await ws_session(reader, writer, headers)
        else:
            await serve_static(writer, path)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, asyncio.LimitOverrunError, ConnectionError, ValueError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def serve_static(writer, path):
    name = STATIC_FILES.get(path)
    file = PUBLIC / name if name else None
    if file and file.is_file():
        body = file.read_bytes()
        status = "200 OK"
        ctype = "text/html; charset=utf-8"
    else:
        body = "찾을 수 없는 페이지입니다.".encode()
        status = "404 Not Found"
        ctype = "text/plain; charset=utf-8"
    writer.write(
        f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
        f"Cache-Control: no-cache\r\nConnection: close\r\n\r\n".encode() + body
    )
    await writer.drain()


async def ws_session(reader, writer, headers):
    key = headers.get("sec-websocket-key")
    if not key:
        return
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    writer.write(
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
    )
    sock = writer.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    c = Client(GAME, writer)
    GAME.clients.add(c)
    print(f"[접속] {writer.get_extra_info('peername')}  (접속 {len(GAME.clients)}명)")
    try:
        while True:
            op, data = await read_frame(reader)
            if op == 8:
                break
            if op == 9:
                writer.write(ws_frame(data, 10))
                continue
            if op != 1:
                continue
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if isinstance(msg, dict):
                GAME.on_message(c, msg)
    except (asyncio.IncompleteReadError, ConnectionError, ValueError):
        pass
    finally:
        c.closed = True
        GAME.on_disconnect(c)


def lan_addresses():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # 실제로 패킷을 보내지 않음, 경로 확인용
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith(("127.", "169.254.")):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


async def main():
    global GAME
    GAME = Game()
    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    print("=" * 46)
    print(" 꿈틀 아레나 서버 실행 중  (끄려면 Ctrl+C)")
    print(f"  이 컴퓨터     : http://localhost:{PORT}")
    for ip in lan_addresses():
        print(f"  같은 네트워크 : http://{ip}:{PORT}")
    print("=" * 46)
    asyncio.create_task(GAME.run())
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        PORT = int(sys.argv[1])
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("서버를 종료합니다.")
