import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Chess, Square } from 'chess.js';
import { Chessboard } from 'react-chessboard';
import ReactFlow, { Background, Controls, Edge, Node } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import './style.css';

type AnalysisLine = { multipv: number; score_cp_white: number | null; mate_in: number | null; mate_for: string | null; depth: number | null; best_move_uci: string | null; principal_variation: string[]; wdl?: { win: number; draw: number; loss: number } };
type Evaluation = AnalysisLine & { multipv_lines?: AnalysisLine[]; engine_version?: string; nodes?: number | null; time_ms?: number | null };
type Position = { id: number; full_fen: string; canonical_fen: string; evaluation?: Evaluation | null };
type MoveEdge = { id: number; parent_position_id: number; child_position_id: number; san: string; uci: string; games_count: number; white_wins: number; draws: number; black_wins: number };
type TreeChild = { edge: MoveEdge; position: Position; children: TreeChild[] };
type TreeResponse = { position: Position; children: TreeChild[] };

type TreeInstance = { instanceId: string; position: Position; parentInstanceId: string | null; incomingEdge?: MoveEdge; children: string[]; expanded: boolean };

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000';
const START_ID = 1;

function evaluationText(evaluation?: Pick<AnalysisLine, 'score_cp_white' | 'mate_in' | 'mate_for'> | null): string {
  if (!evaluation) return 'not analyzed';
  if (evaluation.mate_in) return `${evaluation.mate_for === 'white' ? 'M' : '-M'}${evaluation.mate_in}`;
  if (evaluation.score_cp_white == null) return 'n/a';
  return `${evaluation.score_cp_white >= 0 ? '+' : ''}${(evaluation.score_cp_white / 100).toFixed(2)}`;
}

function evalPercent(evaluation?: Evaluation | null): number {
  if (!evaluation || evaluation.score_cp_white == null) return 50;
  return Math.max(0, Math.min(100, 50 + evaluation.score_cp_white / 20));
}

function EvaluationPanel({ evaluation }: { evaluation?: Evaluation | null }) {
  const lines = evaluation?.multipv_lines?.length ? evaluation.multipv_lines : evaluation ? [evaluation] : [];
  return <section className="evaluation-panel">
    <div className="eval-bar" aria-label="White evaluation bar"><span style={{ width: `${evalPercent(evaluation)}%` }} /></div>
    <strong>{evaluationText(evaluation)}</strong>
    {evaluation?.depth ? <span>depth {evaluation.depth}</span> : null}
    {evaluation?.engine_version ? <span>{evaluation.engine_version}</span> : null}
    <ol>{lines.map((line, index) => <li key={`${line.multipv || index}-${line.best_move_uci || 'none'}`}>
      <b>#{line.multipv || index + 1}</b> {evaluationText(line)} <code>{line.principal_variation?.join(' ') || line.best_move_uci || 'no PV'}</code>
    </li>)}</ol>
  </section>;
}

function childInstanceId(parentId: string, edge: MoveEdge): string {
  return `${parentId}/${edge.id}`;
}

function flattenTree(tree: TreeResponse, parentId: string | null = null, incomingEdge?: MoveEdge, instances: Record<string, TreeInstance> = {}, instanceId = 'root'): Record<string, TreeInstance> {
  const childIds = tree.children.map((child) => childInstanceId(instanceId, child.edge));
  instances[instanceId] = { instanceId, position: tree.position, parentInstanceId: parentId, incomingEdge, children: childIds, expanded: true };
  tree.children.forEach((child) => {
    flattenTree({ position: child.position, children: child.children || [] }, instanceId, child.edge, instances, childInstanceId(instanceId, child.edge));
  });
  return instances;
}

function visibleFlow(instances: Record<string, TreeInstance>, selected: string): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];
  const visit = (id: string, depth: number, index: number) => {
    const item = instances[id];
    if (!item) return;
    nodes.push({
      id,
      position: { x: depth * 260, y: index * 115 },
      data: { label: `${item.incomingEdge?.san || 'Start'}\n${evaluationText(item.position.evaluation)}\n${item.incomingEdge?.games_count || 0} games` },
      className: id === selected ? 'selected-node' : '',
    });
    if (!item.expanded) return;
    item.children.forEach((childId, offset) => {
      const child = instances[childId];
      if (!child) return;
      edges.push({ id: `${id}-${childId}`, source: id, target: childId, label: child.incomingEdge?.san });
      visit(childId, depth + 1, index + offset - (item.children.length - 1) / 2);
    });
  };
  visit('root', 0, 0);
  return { nodes, edges };
}

function App() {
  const [instances, setInstances] = useState<Record<string, TreeInstance>>({});
  const [selectedId, setSelectedId] = useState('root');
  const [status, setStatus] = useState('Loading tree…');
  const selected = instances[selectedId];

  const loadTree = useCallback(async (positionId = START_ID) => {
    const response = await fetch(`${API}/api/tree/${positionId}?depth=2`);
    if (!response.ok) throw new Error(await response.text());
    const tree = await response.json() as TreeResponse;
    setInstances(flattenTree(tree));
    setSelectedId('root');
    setStatus('Ready');
  }, []);

  useEffect(() => { loadTree().catch((error) => setStatus(`Could not load tree: ${error}`)); }, [loadTree]);

  const flow = useMemo(() => visibleFlow(instances, selectedId), [instances, selectedId]);

  async function onPieceDrop(source: Square, target: Square) {
    if (!selected) return false;
    const game = new Chess(selected.position.full_fen);
    const move = game.move({ from: source, to: target, promotion: 'q' });
    if (!move) return false;
    const response = await fetch(`${API}/api/variations`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ parentPositionId: selected.position.id, moveUci: `${move.from}${move.to}${move.promotion || ''}` }) });
    if (!response.ok) return false;
    await loadTree(selected.position.id);
    return true;
  }

  async function analyzeSelected() {
    if (!selected) return;
    setStatus('Analyzing selected position…');
    const response = await fetch(`${API}/api/analysis`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ positionId: selected.position.id, nodeLimit: 1000000, multipv: 3 }) });
    if (!response.ok) {
      setStatus(`Analysis failed: ${await response.text()}`);
      return;
    }
    const evaluation = await response.json() as Evaluation;
    setInstances((current) => ({
      ...current,
      [selectedId]: {
        ...current[selectedId],
        position: { ...current[selectedId].position, evaluation },
      },
    }));
    setStatus('Analysis complete');
  }

  return <main>
    <header><h1>Opening Explorer</h1><span>{status}</span></header>
    <section className="workspace">
      <aside>
        <Chessboard position={selected?.position.full_fen || 'start'} onPieceDrop={onPieceDrop} boardWidth={420} />
        <button onClick={analyzeSelected} disabled={!selected}>Analyze selected node</button>
        <EvaluationPanel evaluation={selected?.position.evaluation} />
        <pre>{selected ? `${selected.incomingEdge?.san || 'Start'}\n${selected.position.full_fen}` : 'No position selected'}</pre>
      </aside>
      <div className="tree"><ReactFlow nodes={flow.nodes} edges={flow.edges} onNodeClick={(_, node) => setSelectedId(node.id)} fitView><Background /><Controls /></ReactFlow></div>
    </section>
  </main>;
}

createRoot(document.getElementById('root')!).render(<App />);
