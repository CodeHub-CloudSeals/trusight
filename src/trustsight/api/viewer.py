"""Minimal three.js viewer for the derived spatial view.

three.js is served from this application rather than a public CDN: a client
network that blocks external script hosts would otherwise show an empty
viewer, and a demo is exactly where that happens.

Labelled "Spatial Interpretation / Completeness View", never a BIM model.
Every placement is instantiated, so six piles appear as six cylinders
rather than one element node. Unresolved reinforcement is not drawn.
"""
from __future__ import annotations

VIEWER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>TrustSight — Spatial Interpretation</title>
<style>
  body { margin:0; font-family: system-ui, sans-serif; background:#0E1E38; color:#fff; }
  #hud { position:absolute; top:12px; left:12px; background:rgba(14,30,56,.9);
         padding:12px 16px; border:1px solid #00B0D8; border-radius:8px; max-width:380px; }
  h1 { font-size:15px; margin:0 0 6px; color:#00B0D8; letter-spacing:.04em; }
  .note { font-size:12px; color:#A9DCEC; margin:2px 0; }
  .finding { font-size:12px; color:#FFC20E; margin:3px 0; }
  #sel { position:absolute; bottom:12px; left:12px; background:rgba(14,30,56,.92);
         padding:10px 14px; border:1px solid #35435A; border-radius:8px;
         font-size:12px; max-width:420px; display:none; }
  code { color:#A9DCEC; }
  #back { position:absolute; top:12px; right:12px; font-size:11px; color:#A9DCEC;
          text-decoration:none; border:1px solid #35435A; border-radius:8px;
          padding:6px 10px; background:rgba(14,30,56,.9); }
  #back:hover { border-color:#00B0D8; color:#00B0D8; }
</style>
</head>
<body>
<a id="back" href="/" target="_top" style="display:none">← Dashboard</a>
<div id="hud">
  <h1>SPATIAL INTERPRETATION — COMPLETENESS VIEW</h1>
  <div class="note">Derived from validated graph data. Not a BIM model.</div>
  <div id="stats" class="note"></div>
  <div id="findings"></div>
</div>
<div id="sel"></div>
<script type="importmap">
{"imports":{"three":"/assets/vendor/three.module.js"}}
</script>
<script type="module">
import * as THREE from 'three';

if (window.self === window.top) document.getElementById('back').style.display = 'block';

/* The run is in the path. Reading it from a query string meant the viewer
   opened blank whenever the URL was copied, shared or bookmarked without
   the string — exactly what happens when someone sends a colleague a link. */
const runId = __RUN_ID__ || new URLSearchParams(location.search).get('run');
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0E1E38);
const camera = new THREE.PerspectiveCamera(50, innerWidth/innerHeight, 1, 200000);
const renderer = new THREE.WebGLRenderer({antialias:true});
renderer.setSize(innerWidth, innerHeight);
document.body.appendChild(renderer.domElement);
scene.add(new THREE.HemisphereLight(0xbfe6ff, 0x14203a, 1.15));
const dir = new THREE.DirectionalLight(0xffffff, .7); dir.position.set(1,2,1); scene.add(dir);

const OK = 0x00B0D8, WARN = 0xFFC20E, BAD = 0xE06C4F;
const picks = [];
const raycaster = new THREE.Raycaster(), mouse = new THREE.Vector2();

function mesh(node, p) {
  const c = node.state === 'ok' ? OK : (node.state === 'conflicted' ? BAD : WARN);
  const mat = new THREE.MeshStandardMaterial({
    color:c, transparent:true, opacity: node.state === 'ok' ? .55 : .35,
    roughness:.6, metalness:.1,
  });
  let geo;
  const q = node.params || {};
  if (node.primitive === 'cylinder') {
    geo = new THREE.CylinderGeometry((q.diameter||600)/2, (q.diameter||600)/2, q.length||5000, 28);
  } else {
    geo = new THREE.BoxGeometry(q.width||q.diameter||1000, q.thickness||q.height||q.length||1000, q.depth||1000);
  }
  const m = new THREE.Mesh(geo, mat);
  m.position.set(p.x||0, -(q.length||2000)/2, p.y||0);
  m.userData = node;
  scene.add(m);
  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(geo), new THREE.LineBasicMaterial({color:c, opacity:.8, transparent:true}));
  edges.position.copy(m.position); scene.add(edges);
  picks.push(m);
  return m;
}

function bars(node, p) {
  for (const bar of node.bars || []) {
    const pts = bar.points.map(([x,y]) => new THREE.Vector3((p.x||0)+x*0.25, -y*0.25, (p.y||0)));
    const line = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({color:0xFFC20E}));
    scene.add(line);
  }
}

fetch(`/runs/${runId}/scene`).then(r => r.json()).then(data => {
  let placements = 0;
  for (const node of data.nodes) {
    const ps = node.placements && node.placements.length ? node.placements : [{x:0,y:0}];
    for (const p of ps) { mesh(node, p); bars(node, p); placements++; }
  }
  document.getElementById('stats').textContent =
    `${data.nodes.length} element(s), ${placements} placement(s) instantiated`;
  const f = document.getElementById('findings');
  for (const finding of data.findings || []) {
    const d = document.createElement('div'); d.className='finding'; d.textContent='⚠ '+finding; f.appendChild(d);
  }
  const box = new THREE.Box3().setFromObject(scene);
  const size = box.getSize(new THREE.Vector3()).length() || 20000;
  const c = box.getCenter(new THREE.Vector3());
  camera.position.set(c.x + size*0.6, c.y + size*0.4, c.z + size*0.8);
  camera.lookAt(c);
});

renderer.domElement.addEventListener('click', e => {
  mouse.x = (e.clientX/innerWidth)*2-1; mouse.y = -(e.clientY/innerHeight)*2+1;
  raycaster.setFromCamera(mouse, camera);
  const hit = raycaster.intersectObjects(picks)[0];
  const panel = document.getElementById('sel');
  if (!hit) { panel.style.display='none'; return; }
  const n = hit.object.userData;
  panel.style.display='block';
  panel.innerHTML = `<b>${n.element_key}</b><br>` +
    `type <code>${n.element_type}</code> · state <code>${n.state}</code><br>` +
    `params <code>${JSON.stringify(n.params)}</code><br>` +
    `bars rendered: ${(n.bars||[]).length}` +
    ((n.issues||[]).length ? `<br><span style="color:#FFC20E">${n.issues.join('<br>')}</span>` : '');
});

let t = 0;
(function loop(){
  requestAnimationFrame(loop);
  t += 0.0016;
  const box = new THREE.Box3().setFromObject(scene);
  const c = box.getCenter(new THREE.Vector3());
  const r = (box.getSize(new THREE.Vector3()).length() || 20000) * 0.75;
  camera.position.x = c.x + Math.cos(t)*r; camera.position.z = c.z + Math.sin(t)*r;
  camera.lookAt(c);
  renderer.render(scene, camera);
})();
addEventListener('resize', () => {
  camera.aspect = innerWidth/innerHeight; camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});
</script>
</body>
</html>
"""
