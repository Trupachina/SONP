import asyncio
import json
import os
import random
import re
import sqlite3
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

APP_DIR = Path(__file__).parent.resolve()
DATA_DIR = APP_DIR / "data"
STATIC_DIR = APP_DIR / "static"
DB_PATH = APP_DIR / "sonp.sqlite3"
TASKS_PATH = DATA_DIR / "tasks.json"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

# ====================== DB ======================
def _has_column(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    cur.execute(f"PRAGMA table_info('{table}')")
    cols = {row[1] for row in cur.fetchall()}
    return column in cols

def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS rooms(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        created_at INTEGER,
        rounds INTEGER,
        status TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS players(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_code TEXT,
        player_id TEXT,
        name TEXT,
        score INTEGER DEFAULT 0
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS answers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_code TEXT,
        round_no INTEGER,
        question_id TEXT,
        category TEXT,
        player_id TEXT,
        player_name TEXT,
        answer_text TEXT,
        is_correct INTEGER,
        awarded INTEGER,
        time_spent_ms INTEGER
    )""")

    if not _has_column(cur, "players", "score"):
        cur.execute("ALTER TABLE players ADD COLUMN score INTEGER DEFAULT 0")

    if not _has_column(cur, "rooms", "status"):
        cur.execute("ALTER TABLE rooms ADD COLUMN status TEXT")

    if not _has_column(cur, "answers", "answer_choice"):
        cur.execute("ALTER TABLE answers ADD COLUMN answer_choice INTEGER")

    if not _has_column(cur, "answers", "time_spent_ms"):
        cur.execute("ALTER TABLE answers ADD COLUMN time_spent_ms INTEGER")

    con.commit()
    con.close()

def db_room_upsert(code: str, rounds: int, status: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO rooms(code, created_at, rounds, status) VALUES(?,?,?,?)",
        (code, int(time.time()), rounds, status)
    )
    cur.execute(
        "UPDATE rooms SET rounds=?, status=? WHERE code=?",
        (rounds, status, code)
    )
    con.commit()
    con.close()

def db_player_upsert(room_code: str, player_id: str, name: str, score: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO players(id, room_code, player_id, name, score)
        VALUES(
            COALESCE((SELECT id FROM players WHERE room_code=? AND player_id=?), NULL),
            ?,?,?,?
        )
    """, (room_code, player_id, room_code, player_id, name, score))
    con.commit()
    con.close()

def db_answer_add(room_code, round_no, qid, category, player_id, player_name,
                  text, choice, is_correct, awarded, time_spent_ms):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO answers(room_code, round_no, question_id, category, player_id, player_name,
                            answer_text, answer_choice, is_correct, awarded, time_spent_ms)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
    """, (room_code, round_no, qid, category, player_id, player_name,
          text, None if choice is None else int(choice), int(is_correct), awarded, time_spent_ms))
    con.commit()
    con.close()

def db_room_results(room_code: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT rounds, status FROM rooms WHERE code=?", (room_code,))
    row = cur.fetchone()
    rounds = row[0] if row else 0
    status = row[1] if row else "unknown"

    cur.execute("""
        SELECT player_id, name, score FROM players
        WHERE room_code=?
        ORDER BY score DESC, name ASC
    """, (room_code,))
    players = [{"playerId": r[0], "name": r[1], "score": r[2]} for r in cur.fetchall()]

    cur.execute("""
        SELECT round_no, question_id, category, player_id, player_name,
               answer_text, answer_choice, is_correct, awarded, time_spent_ms
        FROM answers
        WHERE room_code=?
        ORDER BY round_no, player_name
    """, (room_code,))
    answers = [{
        "round": r[0], "questionId": r[1], "category": r[2],
        "playerId": r[3], "playerName": r[4],
        "text": r[5], "choice": r[6],
        "isCorrect": bool(r[7]),
        "awarded": r[8], "timeMs": r[9]
    } for r in cur.fetchall()]
    con.close()
    return {"roomCode": room_code, "rounds": rounds, "status": status, "players": players, "answers": answers}

# ====================== TASKS ======================
def _normalize_answer(s: str) -> str:
    s = (s or "").strip()
    s = s.replace(",", ".")
    s = re.sub(r"\s+", " ", s)
    return s.lower()

def _is_correct_text(user_text: str, accepted: List[str]) -> bool:
    if not accepted:
        return False
    u = _normalize_answer(user_text)
    for a in accepted or []:
        if _normalize_answer(a) == u:
            return True
    try:
        au = _normalize_answer(accepted[0])
        return float(u) == float(au)
    except Exception:
        return False

def _check_robot_pair_to_target(ans_text: str) -> bool:
    if not ans_text:
        return False
    nums = re.findall(r"-?\d+", ans_text)
    if len(nums) < 2:
        return False
    try:
        a = int(nums[0])
        b = int(nums[1])
    except ValueError:
        return False
    if a <= 0 or b <= 0:
        return False
    return (a * b - 5) == 72

def _check_word_ladder_lisa_nora(ans_text: str) -> bool:
    if not ans_text:
        return False
    s = ans_text.strip()
    if not s:
        return False
    u = s.upper()
    if "ЛИСА" not in u or "НОРА" not in u:
        return False
    return True

def load_tasks_raw() -> dict:
    if not TASKS_PATH.exists():
        TASKS_PATH.write_text("{}", encoding="utf-8")
    try:
        with open(TASKS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def transform_tasks(raw: dict) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    id_counter = 1

    for category, arr in (raw or {}).items():
        if not isinstance(arr, list):
            continue
        out_list: List[dict] = []

        for q in arr:
            if not isinstance(q, dict):
                continue

            if "type" in q and "title" in q:
                qtype = q.get("type")
                if qtype not in ("mcq", "text"):
                    continue
                item = {
                    "id": q.get("id") or f"q{id_counter}",
                    "category": category,
                    "type": qtype,
                    "prompt": str(q.get("title", "")).strip()
                }
                id_counter += 1

                item["mode"] = q.get("mode", "base")
                if "subtype" in q:
                    item["subtype"] = q["subtype"]
                if "difficulty" in q:
                    item["difficulty"] = q["difficulty"]
                if "timeRef" in q:
                    item["timeRef"] = q["timeRef"]
                if "tags" in q:
                    item["tags"] = q["tags"]

                if qtype == "mcq":
                    opts = q.get("options") or []
                    if not opts or q.get("correctIndex") is None:
                        continue
                    item["options"] = [str(x) for x in opts]
                    item["correctIndex"] = int(q.get("correctIndex"))
                else:
                    acc = q.get("accept") or []
                    item["accept"] = [str(x) for x in acc]
                out_list.append(item)
                continue

            if "prompt" in q and "answers" in q:
                item = {
                    "id": q.get("id") or f"q{id_counter}",
                    "category": category,
                    "type": "text",
                    "prompt": str(q.get("prompt", "")).strip(),
                    "accept": [str(x) for x in (q.get("answers") or [])],
                    "mode": "base"
                }
                id_counter += 1
                out_list.append(item)
                continue

        if out_list:
            out[category] = out_list

    return out

RAW_TASKS = load_tasks_raw()
TASK_BANK = transform_tasks(RAW_TASKS)
CATEGORIES = list(TASK_BANK.keys())

def task_counts():
    total = 0
    per_cat = {}
    for k, v in TASK_BANK.items():
        c = len(v)
        per_cat[k] = c
        total += c
    return {"total": total, "categories": per_cat}

def _all_tasks():
    for cat in CATEGORIES:
        for q in TASK_BANK.get(cat, []):
            yield q

def _allowed_by_filter(q: dict, task_filter_mode: str) -> bool:
    mode = q.get("mode", "base")
    if task_filter_mode == "cards_only":
        return mode == "card"
    if task_filter_mode == "no_cards":
        return mode != "card"
    return True

def pick_question(used_ids: set,
                  desired_mode: Optional[str] = None,
                  task_filter_mode: str = "all") -> dict:
    """
    Выбор вопроса с учётом уже использованных id,
    желаемого режима (base/card) и глобального фильтра task_filter_mode.
    """
    candidates: List[dict] = []

    for cat in CATEGORIES:
        for q in TASK_BANK.get(cat, []):
            if q["id"] in used_ids:
                continue
            if not _allowed_by_filter(q, task_filter_mode):
                continue
            if desired_mode and q.get("mode", "base") != desired_mode:
                continue
            candidates.append(q)

    if not candidates and desired_mode is not None:
        for cat in CATEGORIES:
            for q in TASK_BANK.get(cat, []):
                if q["id"] in used_ids:
                    continue
                if not _allowed_by_filter(q, task_filter_mode):
                    continue
                candidates.append(q)

    if candidates:
        return random.choice(candidates)

    if not CATEGORIES:
        return {
            "id": "none",
            "category": "N/A",
            "type": "text",
            "prompt": "(Нет задач)",
            "accept": [""],
            "mode": "base"
        }

    cat = random.choice(CATEGORIES)
    return random.choice(TASK_BANK[cat])

# ====================== ROOMS ======================
def gen_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(alphabet) for _ in range(4))

@dataclass
class PlayerConn:
    ws: WebSocket
    id: str
    name: str
    score: int = 0
    answered: bool = False
    ans_text: str = ""
    ans_choice: Optional[int] = None
    ans_time_ms: int = 0

@dataclass
class Room:
    code: str
    rounds: int = 6
    admin: Optional[WebSocket] = None
    players: Dict[str, PlayerConn] = field(default_factory=dict)
    status: str = "lobby"
    current_round: int = 0
    current_question: Optional[dict] = None
    used_ids: set = field(default_factory=set)
    round_deadline: float = 0.0
    time_limit: int = 0
    timer_task: Optional[asyncio.Task] = None
    task_filter_mode: str = "all"   # all | cards_only | no_cards

    def snapshot_players(self):
        return [{"playerId": p.id, "name": p.name, "score": p.score} for p in self.players.values()]

ROOMS: Dict[str, Room] = {}
CLIENT_TO_ROOM: Dict[WebSocket, str] = {}

# ====================== FastAPI ======================
app = FastAPI(title="СОНП — локальный сервер")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.on_event("startup")
def on_startup():
    db_init()

@app.get("/", response_class=HTMLResponse)
def root():
    return FileResponse(APP_DIR / "index.html")

@app.get("/admin", response_class=HTMLResponse)
def admin_landing():
    return FileResponse(STATIC_DIR / "admin.html")

@app.get("/api/tasks")
def api_tasks():
    return JSONResponse(task_counts())

@app.post("/api/tasks/reload")
def api_tasks_reload():
    global RAW_TASKS, TASK_BANK, CATEGORIES
    RAW_TASKS = load_tasks_raw()
    TASK_BANK = transform_tasks(RAW_TASKS)
    CATEGORIES = list(TASK_BANK.keys())
    return JSONResponse({"ok": True, **task_counts()})

@app.get("/api/room/{code}/results")
def api_room_results(code: str):
    code = code.upper()
    if code not in ROOMS and not exists_in_db(code=code):
        return JSONResponse({"error": "room not found"}, status_code=404)
    return JSONResponse(db_room_results(code))

def exists_in_db(code: str) -> bool:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM rooms WHERE code=?", (code,))
    row = cur.fetchone()
    con.close()
    return bool(row)

@app.get("/api/export/{code}/player/{player_id}.csv")
def export_player_csv(code: str, player_id: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        SELECT round_no, question_id, category, answer_text, answer_choice, is_correct, awarded, time_spent_ms
        FROM answers WHERE room_code=? AND player_id=? ORDER BY round_no
    """, (code.upper(), player_id))
    rows = cur.fetchall()
    con.close()
    if not rows:
        return PlainTextResponse("Нет данных", status_code=404)
    header = "round,questionId,category,answerText,answerChoice,isCorrect,awarded,timeMs\n"

    def fmt(x: Any) -> str:
        s = "" if x is None else str(x)
        return s.replace(",", " ")

    body = "\n".join(",".join(map(fmt, r)) for r in rows)
    content = header + body + "\n"
    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{code.upper()}_{player_id}.csv"'}
    )

@app.get("/api/export/{code}/room.csv")
def export_room_csv(code: str):
    data = db_room_results(code.upper())
    header = "playerId,name,score\n"
    body = "\n".join(f'{p["playerId"]},{p["name"]},{p["score"]}' for p in data["players"])
    content = header + body + "\n"
    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{code.upper()}_summary.csv"'}
    )

# ====================== WebSocket ======================
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            t = msg.get("type")

            if t == "admin_create_room":
                code = (msg.get("preferredCode") or gen_code()).upper()
                if code in ROOMS:
                    code = gen_code()

                tfm = msg.get("taskFilterMode", "all")
                if tfm not in ("all", "cards_only", "no_cards"):
                    tfm = "all"

                room = Room(code=code, rounds=int(msg.get("rounds", 6)), task_filter_mode=tfm)
                ROOMS[code] = room
                room.admin = ws
                CLIENT_TO_ROOM[ws] = code
                db_room_upsert(code, room.rounds, "lobby")
                await ws.send_json({
                    "type": "room_created",
                    "roomCode": code,
                    "taskFilterMode": room.task_filter_mode
                })
                continue

            if t == "admin_attach":
                code = msg["roomCode"].upper()
                room = ROOMS.get(code)
                if not room:
                    await ws.send_json({"type": "error", "message": "Комната не найдена"})
                    continue
                room.admin = ws
                CLIENT_TO_ROOM[ws] = code
                await ws.send_json({
                    "type": "room_attached",
                    "roomCode": code,
                    "players": room.snapshot_players(),
                    "status": room.status,
                    "taskFilterMode": room.task_filter_mode
                })
                continue

            if t == "join":
                code = msg["roomCode"].upper()
                name = str(msg.get("playerName", "Игрок")).strip()[:32]
                room = ROOMS.get(code)
                if not room:
                    await ws.send_json({"type": "error", "message": "Комната не найдена"})
                    continue
                if room.status != "lobby":
                    await ws.send_json({"type": "error", "message": "Игра уже идёт"})
                    continue
                if len(room.players) >= 10:
                    await ws.send_json({"type": "error", "message": "Комната заполнена"})
                    continue
                pid = msg.get("playerId") or ("p_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8)))
                pc = PlayerConn(ws=ws, id=pid, name=name)
                room.players[pid] = pc
                CLIENT_TO_ROOM[ws] = code
                db_player_upsert(room.code, pid, name, 0)
                await ws.send_json({"type": "joined", "roomCode": code, "playerId": pid, "players": room.snapshot_players()})
                await broadcast(code, {"type": "players", "players": room.snapshot_players()})
                continue

            if t == "admin_start":
                code = msg["roomCode"].upper()
                room = ROOMS.get(code)
                if not room or room.admin is not ws:
                    await ws.send_json({"type": "error", "message": "Нет прав/комната не найдена"})
                    continue
                if len(room.players) == 0:
                    await ws.send_json({"type": "error", "message": "Нет игроков"})
                    continue
                room.status = "running"
                db_room_upsert(code, room.rounds, "running")
                await broadcast(code, {"type": "game_started", "rounds": room.rounds})
                asyncio.create_task(run_rounds(room))
                continue

            if t == "answer":
                code = msg["roomCode"].upper()
                room = ROOMS.get(code)
                if not room:
                    continue
                pid = msg.get("playerId")
                pc = room.players.get(pid)
                if not pc or room.status != "running" or room.current_question is None:
                    continue
                if pc.answered:
                    continue
                now = time.time()
                if now > room.round_deadline:
                    continue

                q = room.current_question
                if q["type"] == "mcq":
                    ch = msg.get("choice")
                    try:
                        pc.ans_choice = int(ch) if ch is not None else None
                    except Exception:
                        pc.ans_choice = None
                    pc.ans_text = ""
                else:
                    pc.ans_text = str(msg.get("text", ""))[:300]
                    pc.ans_choice = None

                spent_ms = int((now - (room.round_deadline - room.time_limit)) * 1000)
                pc.ans_time_ms = max(0, spent_ms)
                pc.answered = True

                if all(p.answered for p in room.players.values()):
                    if room.timer_task and not room.timer_task.done():
                        room.timer_task.cancel()
                    await finish_round(room)
                continue

            if t == "admin_end":
                code = msg["roomCode"].upper()
                room = ROOMS.get(code)
                if room and room.admin is ws:
                    room.status = "finished"
                    db_room_upsert(code, room.rounds, "finished")
                    await broadcast(code, {"type": "final", "scores": room.snapshot_players()})
                continue

    except WebSocketDisconnect:
        pass
    finally:
        code = CLIENT_TO_ROOM.get(ws)
        if code:
            room = ROOMS.get(code)
            if room:
                if room.admin is ws:
                    room.admin = None
                drop_pid = None
                for pid, pc in list(room.players.items()):
                    if pc.ws is ws:
                        drop_pid = pid
                        break
                if drop_pid:
                    del room.players[drop_pid]
                    await safe_broadcast(code, {"type": "players", "players": room.snapshot_players()})
            CLIENT_TO_ROOM.pop(ws, None)

# ====================== game loop ======================
async def run_rounds(room: Room):
    room.used_ids = set()

    tfm = room.task_filter_mode or "all"

    has_card = any(
        q.get("mode", "base") == "card" and _allowed_by_filter(q, tfm)
        for q in _all_tasks()
    )

    for r in range(1, room.rounds + 1):
        if room.status != "running":
            break
        room.current_round = r

        if tfm == "cards_only":
            desired_mode = "card"
        elif tfm == "no_cards":
            desired_mode = "base"
        else:
            if has_card and r % 3 == 0:
                desired_mode = "card"
            else:
                desired_mode = "base"

        q = pick_question(room.used_ids, desired_mode=desired_mode, task_filter_mode=tfm)
        room.used_ids.add(q["id"])
        room.current_question = q

        for p in room.players.values():
            p.answered = False
            p.ans_text = ""
            p.ans_choice = None
            p.ans_time_ms = 0

        tl = int(q.get("timeRef") or random.randint(40, 60))
        room.time_limit = tl
        room.round_deadline = time.time() + tl

        payload = {
            "type": "question",
            "round": r,
            "totalRounds": room.rounds,
            "category": q["category"],
            "questionId": q["id"],
            "timeLimit": tl,
            "qtype": q["type"],
            "prompt": q["prompt"],
            "mode": q.get("mode", "base"),
            "subtype": q.get("subtype")
        }
        if q["type"] == "mcq":
            payload["options"] = q.get("options", [])

        await broadcast(room.code, payload)

        async def timer():
            try:
                await asyncio.sleep(tl)
                await finish_round(room)
            except asyncio.CancelledError:
                pass

        room.timer_task = asyncio.create_task(timer())

        while room.current_question is not None and room.status == "running":
            await asyncio.sleep(0.1)

    if room.status == "running":
        room.status = "finished"
        db_room_upsert(room.code, room.rounds, "finished")
    await broadcast(room.code, {"type": "final", "scores": room.snapshot_players()})

async def finish_round(room: Room):
    if room.current_question is None:
        return
    q = room.current_question
    mode = q.get("mode", "base")
    subtype = q.get("subtype")
    results = []

    for p in room.players.values():
        if mode == "card" and subtype == "robot_pair_to_target":
            ok = _check_robot_pair_to_target(p.ans_text)
        elif mode == "card" and subtype == "word_ladder_lisa_nora":
            ok = _check_word_ladder_lisa_nora(p.ans_text)
        else:
            if q["type"] == "mcq":
                ok = (p.ans_choice is not None) and (int(p.ans_choice) == int(q.get("correctIndex", -1)))
            else:
                ok = _is_correct_text(p.ans_text, q.get("accept", []))

        awarded = 1 if ok else 0
        p.score += awarded

        db_player_upsert(room.code, p.id, p.name, p.score)
        db_answer_add(
            room.code, room.current_round, q["id"], q["category"], p.id, p.name,
            p.ans_text, p.ans_choice, ok, awarded, p.ans_time_ms
        )
        results.append({
            "playerId": p.id,
            "name": p.name,
            "choice": p.ans_choice,
            "text": p.ans_text,
            "isCorrect": ok,
            "awarded": awarded,
            "timeMs": p.ans_time_ms,
            "score": p.score
        })

    reveal = {
        "type": "reveal",
        "round": room.current_round,
        "questionId": q["id"],
        "category": q["category"],
        "qtype": q["type"],
        "prompt": q["prompt"],
        "mode": mode,
        "subtype": subtype,
        "results": results,
        "scores": room.snapshot_players()
    }
    if q["type"] == "mcq":
        reveal["options"] = q.get("options", [])
        reveal["correctIndex"] = q.get("correctIndex")
        if q.get("options"):
            ci = q.get("correctIndex", -1)
            if 0 <= ci < len(q["options"]):
                reveal["correctText"] = q["options"][ci]
            else:
                reveal["correctText"] = None
        else:
            reveal["correctText"] = None
    else:
        acc = q.get("accept", [])
        reveal["accepted"] = acc
        reveal["correctText"] = acc[0] if acc else ""

    await broadcast(room.code, reveal)
    room.current_question = None

async def broadcast(room_code: str, payload: dict):
    await safe_broadcast(room_code, payload)

async def safe_broadcast(room_code: str, payload: dict):
    room = ROOMS.get(room_code)
    if not room:
        return
    dead = []
    if room.admin:
        try:
            await room.admin.send_json(payload)
        except Exception:
            dead.append(room.admin)
    for pc in list(room.players.values()):
        try:
            await pc.ws.send_json(payload)
        except Exception:
            dead.append(pc.ws)
    for ws in dead:
        CLIENT_TO_ROOM.pop(ws, None)

@app.get("/healthz")
def health():
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )
