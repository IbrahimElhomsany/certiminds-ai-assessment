/* The photo-real showroom on the shop's home page.
 *
 * The showroom is built and lit in Blender (tools/showroom/build_showroom.py), which renders one 360° picture per
 * standing spot plus images/showroom/spots.json (where each spot is, which spots you can walk to from it and where
 * each booth appears in the picture). Here we only show those pictures: drag to look around, tap a white circle on
 * the floor to walk there, tap a "+" to see the tile, or step through the views with ‹ › or the view list.
 *
 * Same interface as assets/showroom.js (the older live-3D version, still used if the pictures are missing).
 * three.js is MIT licensed (vendor/THREE-LICENSE.txt). */
import * as THREE from "./vendor/three.module.min.js";

const ORDER = ["s1", "s7", "s3", "s2", "s5", "s4", "s6"];          // the "views" you swipe through, inside the shop
const CSS = `
.sr-hs { position:absolute; left:0; top:0; z-index:2; transform:translate(-50%,-50%); border:0; padding:0; cursor:pointer; background:none; }
.sr-hs.walk { width:54px; height:54px; border-radius:50%; border:3px solid rgba(255,255,255,.95); background:rgba(255,255,255,.18);
  box-shadow:0 2px 12px rgba(0,0,0,.25); transition:transform .15s ease, background .15s ease; }
.sr-hs.walk:hover, .sr-hs.walk:focus-visible { background:rgba(255,255,255,.45); transform:translate(-50%,-50%) scale(1.1); }
.sr-hs.walk span, .sr-hs.tile span { position:absolute; left:50%; top:calc(100% + 6px); transform:translateX(-50%); white-space:nowrap; font-size:12px;
  background:rgba(17,17,17,.78); color:#fff; padding:4px 8px; border-radius:7px; opacity:0; transition:opacity .15s ease; pointer-events:none; }
.sr-hs:hover span, .sr-hs:focus-visible span { opacity:1; }
.sr-hs.tile { width:30px; height:30px; border-radius:50%; background:#fff; color:#111; font:600 20px/30px system-ui, sans-serif;
  box-shadow:0 0 0 6px rgba(255,255,255,.35), 0 3px 12px rgba(0,0,0,.3); animation:srPulse 2.4s ease-in-out infinite; }
.sr-hs.tile:hover { animation:none; transform:translate(-50%,-50%) scale(1.12); }
@keyframes srPulse { 0%,100% { box-shadow:0 0 0 4px rgba(255,255,255,.4), 0 3px 12px rgba(0,0,0,.3); } 50% { box-shadow:0 0 0 11px rgba(255,255,255,.12), 0 3px 12px rgba(0,0,0,.3); } }
.sr-views { position:absolute; left:16px; bottom:16px; z-index:3; display:flex; gap:6px; max-width:calc(100% - 140px); overflow-x:auto; scrollbar-width:none;
  scroll-snap-type:x mandatory; -webkit-overflow-scrolling:touch; }
.sr-views::-webkit-scrollbar { display:none; }
.sr-views button { flex:none; scroll-snap-align:start; border:0; border-radius:999px; padding:8px 13px; font-size:12.5px; cursor:pointer;
  background:rgba(255,255,255,.88); color:#111; box-shadow:0 2px 8px rgba(0,0,0,.15); }
.sr-views button.on { background:#111; color:#fff; }
.showroom:not(.inside) .sr-views, .showroom:not(.inside) .sr-hs { display:none; }
.stage:has(.pcard) .sr-views { display:none; }
@media (max-width: 760px) { .sr-views { left:10px; right:10px; bottom:62px; max-width:none; } }
@media (prefers-reduced-motion: reduce) { .sr-hs.tile { animation:none; } }`;

const VERT = `varying vec3 vDir; void main() { vDir = position; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
// three.js world: x right, y up, -z forward. Blender world (the pictures): x right, y forward, z up.
const FRAG = `
uniform sampler2D a; uniform sampler2D b; uniform float k; varying vec3 vDir;
const float PI = 3.14159265359;
vec2 eq(vec3 d) { vec3 n = normalize(d); float bx = n.x, by = -n.z, bz = n.y;
  return vec2(0.5 + atan(bx, by) / (2.0 * PI), 0.5 + atan(bz, length(vec2(bx, by))) / PI); }
void main() { vec2 uv = eq(vDir); gl_FragColor = vec4(mix(texture2D(a, uv).rgb, texture2D(b, uv).rgb, k), 1.0); }`;

const dirOf = (u, v) => {                           // picture position -> direction (three.js world)
  const yaw = (u - 0.5) * 2 * Math.PI, pitch = (0.5 - v) * Math.PI;
  return new THREE.Vector3(Math.sin(yaw) * Math.cos(pitch), Math.sin(pitch), -Math.cos(yaw) * Math.cos(pitch));
};
const ease = t => t < .5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
const angDiff = (a, b) => { let d = (b - a) % (2 * Math.PI); if (d > Math.PI) d -= 2 * Math.PI; if (d < -Math.PI) d += 2 * Math.PI; return d; };

export async function create(canvas, products, { onPick = () => {}, onHover = () => {}, onEntered = () => {} } = {}) {
  const data = await fetch("/images/showroom/spots.json").then(r => { if (!r.ok) throw new Error("no showroom pictures"); return r.json(); });
  const box = canvas.parentElement, sec = box.closest(".showroom") || box;
  if (!document.getElementById("sr360css")) { const st = document.createElement("style"); st.id = "sr360css"; st.textContent = CSS; document.head.append(st); }
  const spots = Object.fromEntries(data.spots.map(s => [s.id, s]));
  const booths = Object.fromEntries(data.booths.map(b => [b.id, b]));
  const bySku = sku => products.find(p => p.sku === sku);
  const small = matchMedia("(max-width: 900px)").matches;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: false });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(75, 1, 0.1, 100);
  const loader = new THREE.TextureLoader(), cache = {};
  const tex = url => cache[url] || (cache[url] = new Promise((ok, bad) => loader.load(url, t => {
    t.colorSpace = THREE.NoColorSpace; t.minFilter = THREE.LinearFilter; t.generateMipmaps = false; t.wrapS = THREE.RepeatWrapping; ok(t);
  }, undefined, bad)));
  const picture = s => tex(s === "closed" ? data.closed : small ? spots[s].small : spots[s].image);
  const first = await picture("closed").catch(() => picture("s1"));
  const uni = { a: { value: first }, b: { value: first }, k: { value: 0 } };
  scene.add(new THREE.Mesh(new THREE.SphereGeometry(10, 64, 32), new THREE.ShaderMaterial({ uniforms: uni, vertexShader: VERT, fragmentShader: FRAG, side: THREE.BackSide, depthWrite: false })));

  // ---- where we look: yaw/pitch as in the pictures (u = 0.5 is straight ahead into the shop's depth)
  const view = { u: (spots.s0 || spots.s1).look_u, pitch: -0.04, fov: 75 };
  let cur = "s0", busy = false, alive = true, entered = false, dirty = true, hover = null;
  const aim = () => { const d = dirOf(view.u, 0.5 - view.pitch / Math.PI); camera.lookAt(d); camera.fov = view.fov; camera.updateProjectionMatrix(); };

  // ---- hotspots (DOM buttons placed over the picture each frame)
  const layer = document.createElement("div"); box.append(layer);
  let marks = [];
  const label = (b) => (bySku(b.sku) || {}).name || b.title || "Tile";
  function buildMarks() {
    layer.innerHTML = ""; marks = [];
    if (!entered || busy) return;
    const s = spots[cur];
    for (const l of s.links) {
      if (l.to === "s0") continue;
      const el = document.createElement("button"); el.type = "button"; el.className = "sr-hs walk";
      el.setAttribute("aria-label", "Walk to " + spots[l.to].name); el.innerHTML = `<span>${spots[l.to].name}</span>`;
      el.onclick = () => walk(l.to, false); layer.append(el); marks.push({ el, d: dirOf(l.u, Math.max(l.v, 0.62)) });
    }
    for (const h of s.booths) {
      const b = booths[h.id], picks = [[b.sku, 0]].concat(b.set ? [[b.set, 0.07]] : []).concat(b.floor && b.floor !== b.sku ? [[b.floor, 0.14]] : []);
      for (const [sku, dv] of picks) {
        const p = bySku(sku); if (!p) continue;
        const el = document.createElement("button"); el.type = "button"; el.className = "sr-hs tile"; el.textContent = "+";
        el.setAttribute("aria-label", p.name); el.title = p.name;
        el.onmouseenter = () => onHover(p); el.onmouseleave = () => onHover(null);
        el.onclick = () => { onHover(null); onPick(p); };
        layer.append(el); marks.push({ el, d: dirOf(h.u, h.v + dv - 0.04) });
      }
    }
    dirty = true;
  }
  const v3 = new THREE.Vector3();
  function placeMarks() {
    const w = canvas.clientWidth, h = canvas.clientHeight, fwd = new THREE.Vector3(); camera.getWorldDirection(fwd);
    for (const m of marks) {
      if (m.d.dot(fwd) < 0.2) { m.el.style.display = "none"; continue; }
      v3.copy(m.d).project(camera);
      const x = (v3.x + 1) / 2 * w, y = (1 - v3.y) / 2 * h;
      const off = x < -30 || x > w + 30 || y < -30 || y > h + 30;
      m.el.style.display = off ? "none" : ""; if (!off) m.el.style.transform = `translate(${x}px, ${y}px) translate(-50%, -50%)`;
    }
  }

  // ---- the view list ("swipe left or right" between views)
  const strip = document.createElement("div"); strip.className = "sr-views"; strip.setAttribute("aria-label", "Showroom views");
  strip.innerHTML = ORDER.filter(id => spots[id]).map(id => `<button type="button" data-v="${id}">${spots[id].name}</button>`).join("");
  strip.onclick = e => { const b = e.target.closest("[data-v]"); if (b) walk(b.dataset.v, true); };
  box.append(strip);
  const markStrip = () => strip.querySelectorAll("button").forEach(b => {
    const on = b.dataset.v === cur; b.classList.toggle("on", on); if (on) b.scrollIntoView({ block: "nearest", inline: "center", behavior: reduced ? "auto" : "smooth" });
  });

  // ---- moving between spots: turn toward it, then "step forward" (zoom in while the next picture fades in)
  const anim = (ms, step) => new Promise(done => {
    if (reduced) ms = Math.min(ms, 250);
    const t0 = performance.now();
    const f = now => { if (!alive) return done(); const t = Math.min(1, (now - t0) / ms); step(t); dirty = true; t < 1 ? requestAnimationFrame(f) : done(); };
    requestAnimationFrame(f);
  });
  async function walk(to, face) {
    if (busy || !spots[to] || (to === cur && entered)) return;
    busy = true; buildMarks(); onPick(null);
    const from = spots[cur], dest = spots[to], link = from.links.find(l => l.to === to);
    const next = picture(to);
    const u0 = view.u, p0 = view.pitch;
    if (link && !reduced) {                                          // turn to face where we're going
      const du = angDiff(u0 * 2 * Math.PI, link.u * 2 * Math.PI) / (2 * Math.PI);
      await anim(Math.min(700, 250 + Math.abs(du) * 1400), t => { const e = ease(t); view.u = u0 + du * e; view.pitch = p0 + (-0.08 - p0) * e; });
    }
    uni.b.value = await next; uni.k.value = 0;
    const u1 = view.u, endU = face ? dest.look_u : u1, du2 = angDiff(u1 * 2 * Math.PI, endU * 2 * Math.PI) / (2 * Math.PI), p1 = view.pitch;
    await anim(link ? 900 : 1100, t => {
      view.fov = 75 - 24 * Math.sin(Math.PI * t);
      uni.k.value = Math.min(1, Math.max(0, (t - 0.25) / 0.45));
      view.u = u1 + du2 * ease(t); view.pitch = p1 + (-0.04 - p1) * ease(t);
    });
    uni.a.value = uni.b.value; uni.k.value = 0; view.fov = 75;
    cur = to; busy = false; markStrip(); buildMarks();
    ORDER.forEach(id => spots[id] && picture(id).catch(() => {}));   // fetch the rest quietly
  }

  // ---- entrance: closed doors -> doors slide open -> walk in
  let introRun = 0;
  async function enter() {
    const run = ++introRun;
    entered = false; busy = true; buildMarks(); sec.classList?.remove?.("inside");
    cur = "s0"; view.u = spots.s0.look_u; view.pitch = -0.02; view.fov = 75;
    uni.a.value = await picture("closed").catch(() => picture("s0")); uni.k.value = 0; dirty = true;
    const opened = picture("s0"), inside = picture("s1");
    await new Promise(r => setTimeout(r, reduced ? 0 : 1100)); if (run !== introRun) return;
    uni.b.value = await opened;
    await anim(1500, t => { uni.k.value = ease(t); view.fov = 75 - 6 * ease(t); }); if (run !== introRun) return;
    uni.a.value = uni.b.value; uni.k.value = 0;
    await new Promise(r => setTimeout(r, reduced ? 0 : 500)); if (run !== introRun) return;
    uni.b.value = await inside;
    const f0 = view.fov;
    await anim(1600, t => { view.fov = f0 - 26 * ease(Math.min(1, t * 1.4)) + 26 * ease(Math.max(0, t - 0.55) / 0.45); uni.k.value = Math.min(1, Math.max(0, (t - 0.3) / 0.4)); });
    if (run !== introRun) return;
    uni.a.value = uni.b.value; uni.k.value = 0; view.fov = 75; view.u = spots.s1.look_u;
    finishEnter();
  }
  function finishEnter() { cur = "s1"; entered = true; busy = false; markStrip(); buildMarks(); dirty = true; onEntered(); }
  async function skip() {
    introRun++; uni.a.value = await picture("s1"); uni.k.value = 0; view.u = spots.s1.look_u; view.pitch = -0.04; view.fov = 75; finishEnter();
  }

  // ---- drag to look around, wheel/trackpad sideways swipe for the next view, arrow keys
  let drag = null;
  canvas.addEventListener("pointerdown", e => { drag = { x: e.clientX, y: e.clientY, u: view.u, p: view.pitch }; canvas.setPointerCapture(e.pointerId); canvas.style.cursor = "grabbing"; });
  canvas.addEventListener("pointermove", e => {
    if (!drag || busy) return;
    const k = (camera.fov * Math.PI / 180) / canvas.clientHeight;
    view.u = drag.u - (e.clientX - drag.x) * k / (2 * Math.PI);
    view.pitch = Math.max(-1.2, Math.min(1.2, drag.p + (e.clientY - drag.y) * k)); dirty = true;
  });
  const up = () => { drag = null; canvas.style.cursor = ""; };
  canvas.addEventListener("pointerup", up); canvas.addEventListener("pointercancel", up);
  let wheelAt = 0;
  canvas.addEventListener("wheel", e => {
    if (!entered || Math.abs(e.deltaX) < Math.abs(e.deltaY) * 1.5 || Math.abs(e.deltaX) < 25) return;
    e.preventDefault(); const now = performance.now(); if (now - wheelAt < 1200) return; wheelAt = now; go(e.deltaX > 0 ? 1 : -1);
  }, { passive: false });
  canvas.tabIndex = 0;
  canvas.addEventListener("keydown", e => {
    if (!entered) return;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); go(e.key === "ArrowRight" ? 1 : -1); }
  });

  function go(dir) {
    if (!entered) return skip();
    const ids = ORDER.filter(id => spots[id]), i = ids.indexOf(cur);
    walk(ids[((i < 0 ? 0 : i + dir) + ids.length) % ids.length], true);
  }

  function resize() {
    const w = canvas.clientWidth || 1, h = canvas.clientHeight || 1;
    renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix(); dirty = true;
  }
  const ro = new ResizeObserver(resize); ro.observe(canvas); resize();
  let visible = true;
  const io = new IntersectionObserver(es => { visible = es[0].isIntersecting; dirty = true; }); io.observe(canvas);
  (function loop() {
    if (!alive) return;
    if (dirty && visible && !document.hidden) { aim(); renderer.render(scene, camera); placeMarks(); dirty = false; }
    requestAnimationFrame(loop);
  })();
  window.__sr360 = { walk, go, skip, get cur() { return cur; }, view };

  enter();
  return {
    go, skip, enter, resize, displays: data.booths,
    focus: () => {}, overview: () => { onPick(null); },
    stop() { alive = false; introRun++; ro.disconnect(); io.disconnect(); layer.remove(); strip.remove(); renderer.dispose(); },
  };
}
