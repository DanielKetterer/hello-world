# Personal Chess Opening Explorer

This repository contains a two-phase implementation of a personal chess opening
explorer.

## Phase 1: PGN import, position graph, and move-tree UI

The backend stores openings as a transposition-aware directed graph of chess
positions. Positions are keyed by the first four FEN fields so equivalent
positions reached by different move orders share one canonical record. Move
edges store SAN/UCI notation, game counts, and result totals. A FastAPI endpoint
returns an expandable tree-shaped projection of that graph for the browser.

The frontend is a React + TypeScript Vite app. It renders a draggable
`react-chessboard` next to an expandable React Flow move tree. Clicking a tree
node selects the authoritative position, and dragging a legal move on the board
creates or selects a variation through the backend.

## Phase 2: selected-position Stockfish analysis

The backend exposes `/api/analysis` for the currently selected position. It uses
`python-chess` to run a native UCI Stockfish process, stores the result in the
`position_analysis` table, and returns centipawn or mate scores from White's
point of view with the principal variation. The request can use node, depth, or
time limits and can ask for MultiPV lines. Cached analysis is keyed by position,
engine identity, settings, and MultiPV value. The frontend displays the returned
evaluation bar, depth, engine version, and candidate principal variations beside
the board.

## Setup

```bash
python -m pip install -r requirements.txt
python opening_explorer.py --db opening_explorer.sqlite3
uvicorn opening_explorer:app --reload
```

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Set `STOCKFISH_PATH` if the Stockfish binary is not on your `PATH`:

```bash
STOCKFISH_PATH=/path/to/stockfish uvicorn opening_explorer:app --reload
```

## API highlights

- `POST /api/import/pgn` imports PGN text into the position graph.
- `GET /api/tree/{position_id}?depth=2` returns a tree projection for the UI.
- `POST /api/variations` validates and adds a manual board move.
- `GET /api/engine` reports whether the configured Stockfish binary is available.
- `POST /api/analysis` analyzes a selected position with Stockfish. It accepts `nodeLimit`, `depth`, `timeMs`, and `multipv`.

The older `chesscom_move_history.py` exporter remains available for downloading
opponent-filtered Chess.com move histories that can be converted into PGN import
workflows.
