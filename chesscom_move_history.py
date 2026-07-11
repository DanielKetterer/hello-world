#!/usr/bin/env python3
"""
Export a Chess.com player's complete public move history to one CSV file.

The program:
1. Gets every available monthly game archive from the Chess.com PubAPI.
2. Reads the PGN for every completed game.
3. Expands each game into one CSV row per half-move (ply).

Install:
    python -m pip install requests python-chess

Run:
    python chesscom_move_history.py USERNAME

Examples:
    python chesscom_move_history.py hikaru
    python chesscom_move_history.py hikaru --output hikaru_moves.csv
    python chesscom_move_history.py hikaru --contact you@example.com

Chess.com PubAPI:
    https://api.chess.com/pub/player/{username}/games/archives
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import chess
    import chess.pgn
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError as exc:
    missing = getattr(exc, "name", "a required package")
    raise SystemExit(
        f"Missing dependency: {missing}\n"
        "Install dependencies with:\n"
        "  python -m pip install requests python-chess"
    ) from exc


API_ROOT = "https://api.chess.com/pub"
CLOCK_RE = re.compile(r"\[%clk\s+([0-9:.]+)\]")
EMT_RE = re.compile(r"\[%emt\s+([0-9:.]+)\]")


CSV_FIELDS = [
    "game_id",
    "game_url",
    "game_end_time_utc",
    "game_date",
    "time_class",
    "time_control",
    "rated",
    "rules",
    "white_username",
    "black_username",
    "player_color",
    "ply",
    "move_number",
    "color",
    "san",
    "uci",
    "from_square",
    "to_square",
    "piece",
    "captured_piece",
    "is_capture",
    "is_en_passant",
    "is_castling",
    "promotion",
    "gives_check",
    "gives_checkmate",
    "clock_seconds",
    "elapsed_move_seconds",
    "fen_before",
    "fen_after",
]


def unix_to_utc(value: Any) -> str:
    """Convert a Unix timestamp to an ISO-8601 UTC string."""
    if value in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def clock_text_to_seconds(value: str | None) -> float | None:
    """Convert H:MM:SS, M:SS, or seconds text to seconds."""
    if not value:
        return None

    try:
        parts = [float(part) for part in value.split(":")]
    except ValueError:
        return None

    if len(parts) == 3:
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    if len(parts) == 1:
        return parts[0]
    return None


def annotation_seconds(node: chess.pgn.ChildNode, kind: str) -> float | None:
    """
    Read clock/elapsed-time annotations from a PGN move comment.

    Newer python-chess versions provide node.clock() and node.emt().
    Regex fallbacks preserve compatibility with older versions.
    """
    method = getattr(node, kind, None)
    if callable(method):
        try:
            result = method()
            if result is not None:
                return float(result)
        except (TypeError, ValueError):
            pass

    comment = node.comment or ""
    pattern = CLOCK_RE if kind == "clock" else EMT_RE
    match = pattern.search(comment)
    return clock_text_to_seconds(match.group(1)) if match else None


class ChessComClient:
    """Small serial Chess.com PubAPI client with retries and a descriptive UA."""

    def __init__(
        self,
        username: str,
        *,
        contact: str | None = None,
        delay: float = 0.15,
    ) -> None:
        self.username = username
        self.delay = max(0.0, delay)
        self.session = requests.Session()

        retry = Retry(
            total=7,
            connect=7,
            read=7,
            status=7,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)

        user_agent = f"ChessComMoveHistory/1.0 (username: {username}"
        if contact:
            user_agent += f"; contact: {contact}"
        user_agent += ")"

        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            }
        )

    def get_json(self, url: str) -> dict[str, Any]:
        logging.debug("GET %s", url)
        response = self.session.get(url, timeout=(15, 120))

        if self.delay:
            time.sleep(self.delay)

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = response.text[:500].strip()
            raise RuntimeError(
                f"Chess.com returned HTTP {response.status_code} for {url}"
                + (f": {detail}" if detail else "")
            ) from exc

        try:
            data = response.json()
        except requests.JSONDecodeError as exc:
            raise RuntimeError(f"Chess.com returned invalid JSON for {url}") from exc

        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected API response type for {url}")
        return data


def player_color(game_data: dict[str, Any], username: str) -> str:
    target = username.casefold()
    white = str((game_data.get("white") or {}).get("username", "")).casefold()
    black = str((game_data.get("black") or {}).get("username", "")).casefold()

    if white == target:
        return "white"
    if black == target:
        return "black"
    return ""


def captured_piece_name(board: chess.Board, move: chess.Move) -> str:
    if not board.is_capture(move):
        return ""

    if board.is_en_passant(move):
        return "pawn"

    captured = board.piece_at(move.to_square)
    return chess.piece_name(captured.piece_type) if captured else ""


def parse_game_rows(
    game_data: dict[str, Any],
    username: str,
) -> Iterator[dict[str, Any]]:
    """Turn one Chess.com game record into one row per legal mainline move."""
    pgn_text = game_data.get("pgn")
    if not isinstance(pgn_text, str) or not pgn_text.strip():
        return

    parsed = chess.pgn.read_game(io.StringIO(pgn_text))
    if parsed is None:
        return

    headers = parsed.headers
    board = parsed.board()

    game_id = str(
        game_data.get("uuid")
        or game_data.get("url")
        or headers.get("Link")
        or ""
    )
    game_url = str(game_data.get("url") or headers.get("Link") or "")
    end_time_utc = unix_to_utc(game_data.get("end_time"))
    game_date = headers.get("UTCDate") or headers.get("Date") or ""
    requested_player_color = player_color(game_data, username)

    white_username = str(
        (game_data.get("white") or {}).get("username")
        or headers.get("White")
        or ""
    )
    black_username = str(
        (game_data.get("black") or {}).get("username")
        or headers.get("Black")
        or ""
    )

    for ply, node in enumerate(parsed.mainline(), start=1):
        move = node.move
        if move is None:
            continue

        color = "white" if board.turn == chess.WHITE else "black"
        move_number = board.fullmove_number
        piece = board.piece_at(move.from_square)

        # These values must be computed before pushing the move.
        san = board.san(move)
        uci = board.uci(move)
        fen_before = board.fen()
        capture = board.is_capture(move)
        en_passant = board.is_en_passant(move)
        castling = board.is_castling(move)
        captured_piece = captured_piece_name(board, move)
        gives_check = board.gives_check(move)

        board.push(move)

        yield {
            "game_id": game_id,
            "game_url": game_url,
            "game_end_time_utc": end_time_utc,
            "game_date": game_date,
            "time_class": game_data.get("time_class", ""),
            "time_control": game_data.get("time_control", ""),
            "rated": game_data.get("rated", ""),
            "rules": game_data.get("rules", ""),
            "white_username": white_username,
            "black_username": black_username,
            "player_color": requested_player_color,
            "ply": ply,
            "move_number": move_number,
            "color": color,
            "san": san,
            "uci": uci,
            "from_square": chess.square_name(move.from_square),
            "to_square": chess.square_name(move.to_square),
            "piece": chess.piece_name(piece.piece_type) if piece else "",
            "captured_piece": captured_piece,
            "is_capture": capture,
            "is_en_passant": en_passant,
            "is_castling": castling,
            "promotion": (
                chess.piece_name(move.promotion) if move.promotion else ""
            ),
            "gives_check": gives_check,
            "gives_checkmate": board.is_checkmate(),
            "clock_seconds": annotation_seconds(node, "clock"),
            "elapsed_move_seconds": annotation_seconds(node, "emt"),
            "fen_before": fen_before,
            "fen_after": board.fen(),
        }


def export_move_history(
    username: str,
    output: Path,
    *,
    contact: str | None,
    delay: float,
) -> tuple[int, int, int]:
    """
    Download all public completed games and atomically write the move CSV.

    Returns:
        (archives_processed, games_processed, move_rows_written)
    """
    client = ChessComClient(username, contact=contact, delay=delay)
    archives_url = f"{API_ROOT}/player/{username}/games/archives"
    archives_data = client.get_json(archives_url)
    archives = archives_data.get("archives", [])

    if not isinstance(archives, list):
        raise RuntimeError("The archives endpoint did not return an archive list.")
    if not archives:
        raise RuntimeError(
            f"No completed-game archives were found for Chess.com user {username!r}."
        )

    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".partial")

    games_processed = 0
    rows_written = 0
    skipped_games = 0
    seen_games: set[str] = set()

    try:
        with temporary.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.DictWriter(
                csv_file,
                fieldnames=CSV_FIELDS,
                extrasaction="ignore",
            )
            writer.writeheader()

            for archive_number, archive_url in enumerate(archives, start=1):
                logging.info(
                    "Archive %d/%d: %s",
                    archive_number,
                    len(archives),
                    archive_url,
                )
                archive_data = client.get_json(str(archive_url))
                games = archive_data.get("games", [])

                if not isinstance(games, list):
                    logging.warning("Skipping malformed archive: %s", archive_url)
                    continue

                for game_data in games:
                    if not isinstance(game_data, dict):
                        continue

                    identity = str(
                        game_data.get("uuid")
                        or game_data.get("url")
                        or game_data.get("pgn")
                        or ""
                    )
                    if identity and identity in seen_games:
                        continue
                    if identity:
                        seen_games.add(identity)

                    try:
                        game_rows = list(parse_game_rows(game_data, username))
                    except Exception as exc:
                        skipped_games += 1
                        logging.warning(
                            "Could not parse game %s: %s",
                            game_data.get("url", identity or "<unknown>"),
                            exc,
                        )
                        continue

                    if not game_rows:
                        skipped_games += 1
                        continue

                    writer.writerows(game_rows)
                    games_processed += 1
                    rows_written += len(game_rows)

        temporary.replace(output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    if skipped_games:
        logging.warning("Skipped %d unparseable or empty games.", skipped_games)

    return len(archives), games_processed, rows_written


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download every public completed Chess.com game for a player and "
            "write one CSV row per half-move."
        )
    )
    parser.add_argument("username", help="Chess.com username")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output CSV path (default: USERNAME_move_history.csv)",
    )
    parser.add_argument(
        "--contact",
        help=(
            "Optional email or URL placed in the User-Agent, as recommended "
            "by Chess.com for API clients."
        ),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.15,
        help="Seconds between serial API requests (default: 0.15)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed request logging",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    username = args.username.strip()
    if not username:
        print("Username cannot be empty.", file=sys.stderr)
        return 2

    output = args.output or Path(f"{username}_move_history.csv")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    try:
        archive_count, game_count, move_count = export_move_history(
            username,
            output,
            contact=args.contact,
            delay=args.delay,
        )
    except KeyboardInterrupt:
        print("\nExport cancelled; no partial CSV was retained.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1

    print(f"Saved: {output.expanduser().resolve()}")
    print(f"Monthly archives: {archive_count:,}")
    print(f"Games: {game_count:,}")
    print(f"Move rows (plies): {move_count:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
