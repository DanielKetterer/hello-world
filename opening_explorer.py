#!/usr/bin/env python3
"""FastAPI backend for a personal chess opening explorer.

Phase 1 imports PGN text into a transposition-aware position graph and exposes
subtrees for a move-tree UI. Phase 2 adds single-position Stockfish analysis for
selected nodes, with cached evaluations.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sqlite3
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import chess
import chess.engine
import chess.pgn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATABASE = Path(os.environ.get("OPENING_EXPLORER_DB", "opening_explorer.sqlite3"))
STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "stockfish")
ENGINE_NAME = "Stockfish"


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY,
    canonical_fen TEXT NOT NULL UNIQUE,
    full_fen TEXT NOT NULL,
    side_to_move TEXT NOT NULL,
    piece_placement TEXT NOT NULL,
    castling_rights TEXT NOT NULL,
    en_passant_square TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY,
    source TEXT,
    event TEXT,
    site TEXT,
    date TEXT,
    white TEXT,
    black TEXT,
    result TEXT,
    pgn TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS move_edges (
    id INTEGER PRIMARY KEY,
    parent_position_id INTEGER NOT NULL REFERENCES positions(id),
    child_position_id INTEGER NOT NULL REFERENCES positions(id),
    uci TEXT NOT NULL,
    san TEXT NOT NULL,
    games_count INTEGER NOT NULL DEFAULT 0,
    white_wins INTEGER NOT NULL DEFAULT 0,
    draws INTEGER NOT NULL DEFAULT 0,
    black_wins INTEGER NOT NULL DEFAULT 0,
    UNIQUE(parent_position_id, child_position_id, uci)
);
CREATE TABLE IF NOT EXISTS move_occurrences (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(id),
    ply INTEGER NOT NULL,
    edge_id INTEGER NOT NULL REFERENCES move_edges(id),
    clock_seconds REAL,
    player_rating INTEGER
);
CREATE TABLE IF NOT EXISTS position_analysis (
    id INTEGER PRIMARY KEY,
    position_id INTEGER NOT NULL REFERENCES positions(id),
    engine_name TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    network_hash TEXT NOT NULL,
    analysis_settings_hash TEXT NOT NULL,
    depth INTEGER,
    seldepth INTEGER,
    nodes INTEGER,
    time_ms INTEGER,
    score_cp_white INTEGER,
    mate_in INTEGER,
    mate_for TEXT,
    wdl_win INTEGER,
    wdl_draw INTEGER,
    wdl_loss INTEGER,
    best_move_uci TEXT,
    principal_variation TEXT NOT NULL DEFAULT '[]',
    completed_at TEXT NOT NULL,
    UNIQUE(position_id, engine_name, engine_version, network_hash, analysis_settings_hash)
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_fen(fen: str) -> str:
    return " ".join(fen.split()[:4])


@contextmanager
def connect(db_path: Path = DATABASE) -> Iterable[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path = DATABASE) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        get_or_create_position(conn, chess.STARTING_FEN)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def get_or_create_position(conn: sqlite3.Connection, fen: str) -> int:
    canonical = canonical_fen(fen)
    row = conn.execute("SELECT id FROM positions WHERE canonical_fen = ?", (canonical,)).fetchone()
    if row:
        return int(row["id"])
    board = chess.Board(fen)
    parts = fen.split()
    cur = conn.execute(
        """
        INSERT INTO positions (
            canonical_fen, full_fen, side_to_move, piece_placement,
            castling_rights, en_passant_square, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            canonical,
            fen,
            "white" if board.turn == chess.WHITE else "black",
            parts[0],
            parts[2],
            parts[3],
            utc_now(),
        ),
    )
    return int(cur.lastrowid)


def result_counts(result: str) -> tuple[int, int, int]:
    if result == "1-0":
        return 1, 0, 0
    if result == "0-1":
        return 0, 0, 1
    if result == "1/2-1/2":
        return 0, 1, 0
    return 0, 0, 0


def get_or_create_edge(
    conn: sqlite3.Connection,
    parent_id: int,
    child_id: int,
    uci: str,
    san: str,
) -> int:
    row = conn.execute(
        "SELECT id FROM move_edges WHERE parent_position_id = ? AND child_position_id = ? AND uci = ?",
        (parent_id, child_id, uci),
    ).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        """
        INSERT INTO move_edges (parent_position_id, child_position_id, uci, san)
        VALUES (?, ?, ?, ?)
        """,
        (parent_id, child_id, uci, san),
    )
    return int(cur.lastrowid)


def import_pgn_text(conn: sqlite3.Connection, pgn_text: str, source: str = "upload") -> dict[str, int]:
    games = positions = edges = occurrences = skipped = 0
    stream = io.StringIO(pgn_text)
    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        try:
            board = game.board()
            headers = game.headers
            cur = conn.execute(
                """
                INSERT INTO games (source, event, site, date, white, black, result, pgn, imported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    headers.get("Event", ""),
                    headers.get("Site", ""),
                    headers.get("Date", ""),
                    headers.get("White", ""),
                    headers.get("Black", ""),
                    headers.get("Result", ""),
                    str(game),
                    utc_now(),
                ),
            )
            game_id = int(cur.lastrowid)
            parent_id = get_or_create_position(conn, board.fen())
            white_wins, draws, black_wins = result_counts(headers.get("Result", ""))
            for ply, move in enumerate(game.mainline_moves(), start=1):
                san = board.san(move)
                uci = move.uci()
                board.push(move)
                child_id = get_or_create_position(conn, board.fen())
                edge_id = get_or_create_edge(conn, parent_id, child_id, uci, san)
                conn.execute(
                    """
                    UPDATE move_edges
                    SET games_count = games_count + 1,
                        white_wins = white_wins + ?,
                        draws = draws + ?,
                        black_wins = black_wins + ?
                    WHERE id = ?
                    """,
                    (white_wins, draws, black_wins, edge_id),
                )
                conn.execute(
                    "INSERT INTO move_occurrences (game_id, ply, edge_id) VALUES (?, ?, ?)",
                    (game_id, ply, edge_id),
                )
                parent_id = child_id
                occurrences += 1
            games += 1
        except Exception:
            skipped += 1
    positions = conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"]
    edges = conn.execute("SELECT COUNT(*) AS n FROM move_edges").fetchone()["n"]
    return {"games": games, "positions": positions, "edges": edges, "occurrences": occurrences, "skipped": skipped}


def fetch_position(conn: sqlite3.Connection, position_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Position not found")
    data = dict(row)
    data["evaluation"] = latest_analysis(conn, position_id)
    return data


def latest_analysis(conn: sqlite3.Connection, position_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM position_analysis
        WHERE position_id = ?
        ORDER BY completed_at DESC
        LIMIT 1
        """,
        (position_id,),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["principal_variation"] = json.loads(data["principal_variation"] or "[]")
    return data


def fetch_tree(conn: sqlite3.Connection, position_id: int, depth: int) -> dict[str, Any]:
    position = fetch_position(conn, position_id)
    if depth <= 0:
        return {"position": position, "children": []}
    rows = conn.execute(
        """
        SELECT * FROM move_edges
        WHERE parent_position_id = ?
        ORDER BY games_count DESC, san ASC
        """,
        (position_id,),
    ).fetchall()
    children = []
    for edge in rows:
        children.append(
            {
                "edge": dict(edge),
                "position": fetch_position(conn, int(edge["child_position_id"])),
                "children": fetch_tree(conn, int(edge["child_position_id"]), depth - 1)["children"],
            }
        )
    return {"position": position, "children": children}


def settings_hash(limit: dict[str, int | None], multipv: int) -> str:
    payload = json.dumps({"limit": limit, "multipv": multipv}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def stockfish_version(path: str = STOCKFISH_PATH) -> str:
    try:
        result = subprocess.run([path], input="uci\nquit\n", text=True, capture_output=True, timeout=5, check=False)
        for line in result.stdout.splitlines():
            if line.startswith("id name "):
                return line.removeprefix("id name ")
    except Exception:
        pass
    return "unknown"


def analyze_position(conn: sqlite3.Connection, position_id: int, nodes: int, multipv: int) -> dict[str, Any]:
    position = fetch_position(conn, position_id)
    fen = position["full_fen"]
    version = stockfish_version()
    limit_payload = {"nodes": nodes, "depth": None, "time_ms": None}
    ahash = settings_hash(limit_payload, multipv)
    cached = conn.execute(
        """
        SELECT * FROM position_analysis
        WHERE position_id = ? AND engine_name = ? AND engine_version = ?
          AND network_hash = ? AND analysis_settings_hash = ?
        """,
        (position_id, ENGINE_NAME, version, "default", ahash),
    ).fetchone()
    if cached:
        data = dict(cached)
        data["principal_variation"] = json.loads(data["principal_variation"] or "[]")
        return data

    board = chess.Board(fen)
    try:
        with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as engine:
            result = engine.analyse(board, chess.engine.Limit(nodes=nodes), multipv=multipv)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=f"Stockfish not found at {STOCKFISH_PATH!r}") from exc

    line = result[0] if isinstance(result, list) else result
    pov = line["score"].pov(chess.WHITE)
    mate = pov.mate()
    score_cp = pov.score(mate_score=None) if mate is None else None
    pv = [move.uci() for move in line.get("pv", [])]
    wdl = pov.wdl().white()
    cur = conn.execute(
        """
        INSERT INTO position_analysis (
            position_id, engine_name, engine_version, network_hash, analysis_settings_hash,
            depth, seldepth, nodes, time_ms, score_cp_white, mate_in, mate_for,
            wdl_win, wdl_draw, wdl_loss, best_move_uci, principal_variation, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            position_id,
            ENGINE_NAME,
            version,
            "default",
            ahash,
            line.get("depth"),
            line.get("seldepth"),
            line.get("nodes"),
            line.get("time"),
            score_cp,
            abs(mate) if mate is not None else None,
            "white" if mate and mate > 0 else "black" if mate and mate < 0 else None,
            wdl.wins,
            wdl.draws,
            wdl.losses,
            pv[0] if pv else None,
            json.dumps(pv),
            utc_now(),
        ),
    )
    saved = conn.execute("SELECT * FROM position_analysis WHERE id = ?", (cur.lastrowid,)).fetchone()
    data = dict(saved)
    data["principal_variation"] = json.loads(data["principal_variation"] or "[]")
    return data


class ImportRequest(BaseModel):
    pgn: str = Field(min_length=1)
    source: str = "upload"


class VariationRequest(BaseModel):
    parentPositionId: int
    moveUci: str


class AnalysisRequest(BaseModel):
    positionId: int
    nodeLimit: int = Field(default=500_000, ge=1, le=50_000_000)
    multipv: int = Field(default=1, ge=1, le=10)


app = FastAPI(title="Opening Explorer", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/import/pgn")
def import_pgn(request: ImportRequest) -> dict[str, int]:
    with connect() as conn:
        return import_pgn_text(conn, request.pgn, request.source)


@app.get("/api/tree/{position_id}")
def tree(position_id: int, depth: int = 2) -> dict[str, Any]:
    with connect() as conn:
        return fetch_tree(conn, position_id, max(0, min(depth, 6)))


@app.post("/api/variations")
def create_variation(request: VariationRequest) -> dict[str, Any]:
    with connect() as conn:
        parent = fetch_position(conn, request.parentPositionId)
        board = chess.Board(parent["full_fen"])
        move = chess.Move.from_uci(request.moveUci)
        if move not in board.legal_moves:
            raise HTTPException(status_code=400, detail="Illegal move")
        san = board.san(move)
        board.push(move)
        child_id = get_or_create_position(conn, board.fen())
        edge_id = get_or_create_edge(conn, request.parentPositionId, child_id, move.uci(), san)
        edge = conn.execute("SELECT * FROM move_edges WHERE id = ?", (edge_id,)).fetchone()
        return {"edge": dict(edge), "position": fetch_position(conn, child_id)}


@app.post("/api/analysis")
def analysis(request: AnalysisRequest) -> dict[str, Any]:
    with connect() as conn:
        return analyze_position(conn, request.positionId, request.nodeLimit, request.multipv)


def cli() -> int:
    parser = argparse.ArgumentParser(description="Import PGN into the opening explorer database.")
    parser.add_argument("pgn", type=Path, nargs="?", help="PGN file to import")
    parser.add_argument("--db", type=Path, default=DATABASE)
    args = parser.parse_args()
    init_db(args.db)
    if args.pgn:
        with connect(args.db) as conn:
            print(import_pgn_text(conn, args.pgn.read_text(encoding="utf-8"), str(args.pgn)))
    else:
        print(f"Initialized {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
