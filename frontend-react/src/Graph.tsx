import { useEffect, useRef, useState } from 'react';
import { TransformComponent, TransformWrapper, type ReactZoomPanPinchRef } from 'react-zoom-pan-pinch';
import { Maximize2, Minus, Plus } from 'lucide-react';
import mermaid from 'mermaid';

export type GraphNode = { id: string; title: string; subtitle: string; status: string; detail: Record<string, any> };
export type GraphEdge = { source: string; target: string };
const stateNames: Record<string, string> = { pending: '等待执行', running: '正在执行', succeeded: '执行完成', failed: '执行失败', canceled: '已取消', planned: '流程说明' };
mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', htmlLabels: false, theme: 'base', fontFamily: 'Microsoft YaHei, sans-serif', themeVariables: { fontSize: '12px', primaryColor: '#ffffff', primaryTextColor: '#315c65', primaryBorderColor: '#abcbd0', lineColor: '#8bb7be' }, flowchart: { useMaxWidth: false, padding: 9, rankSpacing: 18, nodeSpacing: 24, curve: 'basis' } });
let counter = 0;

function definition(nodes: GraphNode[], edges: GraphEdge[]) {
  const encode = (text: string) => text.replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('\n', ' ');
  const names = new Map(nodes.map((node, index) => [node.id, `n${index}`]));
  const lines = ['flowchart TD'];
  nodes.forEach((node, index) => {
    const style = ['succeeded', 'running', 'failed'].includes(node.status) ? node.status : 'pending';
    lines.push(`n${index}["${encode(node.title)}<br/>${encode(node.subtitle.length > 38 ? `${node.subtitle.slice(0, 38)}…` : node.subtitle)}<br/>${stateNames[node.status] || node.status}"]:::${style}`, `class n${index} process${index}`);
  });
  edges.forEach(edge => lines.push(`${names.get(edge.source)} --> ${names.get(edge.target)}`));
  lines.push('classDef pending fill:#fff,stroke:#d4e0e2,color:#7b969b', 'classDef running fill:#edf8fa,stroke:#4f9aa5,stroke-width:2px,color:#2e6f78', 'classDef succeeded fill:#fff,stroke:#98c5c4,color:#366e73', 'classDef failed fill:#fff2f1,stroke:#cb9692,color:#9c625d');
  return lines.join('\n');
}

export default function Graph({ nodes, edges, onSelect }: { nodes: GraphNode[]; edges: GraphEdge[]; onSelect: (node: GraphNode) => void }) {
  const host = useRef<HTMLDivElement>(null);
  const vector = useRef<HTMLDivElement>(null);
  const controls = useRef<ReactZoomPanPinchRef>(null);
  const snapshot = useRef(nodes);
  snapshot.current = nodes;
  const source = definition(nodes, edges);
  const [diagram, setDiagram] = useState<{ svg: string; width: number; height: number; nodes: GraphNode[] } | null>(null);
  const [error, setError] = useState('');
  const fit = () => {
    if (diagram && host.current) controls.current?.centerView(Math.min(host.current.clientWidth * 0.9 / diagram.width, host.current.clientHeight * 0.85 / diagram.height, 1), 100);
  };
  useEffect(() => {
    let active = true;
    const current = snapshot.current;
    const timer = window.setTimeout(() => {
      mermaid.render(`trip-flow-${++counter}`, source).then(({ svg }) => {
        if (!active) return;
        const root = new DOMParser().parseFromString(svg, 'text/html').querySelector('svg');
        if (!root) throw new Error('未生成 SVG 流程图');
        setDiagram({ svg, width: root.viewBox.baseVal.width, height: root.viewBox.baseVal.height, nodes: current });
        setError('');
      }).catch(reason => { if (active) { setDiagram(null); setError(reason.message); } });
    }, 60);
    return () => { active = false; window.clearTimeout(timer); };
  }, [source]);
  useEffect(() => {
    if (!diagram || !host.current) return;
    diagram.nodes.forEach((node, index) => {
      const element = vector.current?.querySelector(`.process${index}`);
      element?.setAttribute('data-node-id', node.id);
      element?.setAttribute('role', 'button');
      element?.setAttribute('tabindex', '0');
      element?.setAttribute('aria-label', `${node.title}，${stateNames[node.status] || node.status}`);
    });
    const observer = new ResizeObserver(fit);
    observer.observe(host.current);
    const frame = requestAnimationFrame(fit);
    return () => { observer.disconnect(); cancelAnimationFrame(frame); };
  }, [diagram]);
  function select(target: EventTarget | null) {
    const element = target instanceof Element ? target.closest('[data-node-id]') : null;
    const node = diagram?.nodes.find(item => item.id === element?.getAttribute('data-node-id'));
    if (node) onSelect(snapshot.current.find(item => item.id === node.id) || node);
  }
  return <div className="graph-host" ref={host} style={{ overflow: 'clip' }}>
    {error ? <p className="error">{error}</p> : <TransformWrapper ref={controls} minScale={0.02} maxScale={5} limitToBounds={false} panning={{ excluded: ['node'] }} doubleClick={{ disabled: true }}>
      <TransformComponent wrapperStyle={{ width: '100%', height: '100%', overflow: 'clip' }}><div ref={vector} className="diagram" style={{ width: diagram?.width, height: diagram?.height }} onClick={event => select(event.target)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(event.target); } }} dangerouslySetInnerHTML={{ __html: diagram?.svg || '' }}/></TransformComponent>
    </TransformWrapper>}
    <div className="graph-tools"><button aria-label="放大流程图" onClick={() => controls.current?.zoomIn()}><Plus size={16}/></button><button aria-label="缩小流程图" onClick={() => controls.current?.zoomOut()}><Minus size={16}/></button><button aria-label="适应全部节点" onClick={fit}><Maximize2 size={15}/></button></div>
    <span className="graph-caption">Mermaid · 点击节点查看详情 · 拖动浏览</span>
  </div>;
}
