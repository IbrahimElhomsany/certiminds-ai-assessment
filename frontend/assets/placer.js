/* "Place products": true-size 3D products drawn into a room photo (the visualiser page).
 *
 * How it works, in plain words:
 *  1. Every photo room already knows a real rectangle on its floor (4 corners in the photo and its size in cm).
 *     From that rectangle we work out the camera that took the photo: how far it was from the floor,
 *     which way it looked and its zoom (focal length). The same maths as the tiles (visualiser.js).
 *  2. A three.js camera is set up exactly like that one, so a 3D model placed on the floor comes out at its
 *     real size and in the right perspective: a 172 cm bath looks 172 cm long next to the room's tiles.
 *  3. The models are our own (bathroom products) or free CC0 furniture from Poly Haven, made into small
 *     .glb files by tools/products/build_products.py. Light comes from above with a soft shadow on the floor.
 *  4. Wall products (WC, mirror, vanity, shower set) snap to the wall the room's wall rectangle stands on,
 *     with their back on the wall, and slide along it.
 * The models are drawn on a see-through canvas over the tiled photo; Download and Share combine the two.
 */
import * as THREE from "./vendor/three.module.min.js";
import { GLTFLoader } from "./vendor/GLTFLoader.js";
import { RoomEnvironment } from "./vendor/RoomEnvironment.js";

const NV = window.NovaVisualiser;
const loader = new GLTFLoader();
const models = new Map();                       // url -> Promise<{ scene, box }>, each file loaded once

function loadModel(url) {
  if (!models.has(url)) {
    models.set(url, new Promise((ok, fail) => loader.load(url, g => {
      g.scene.traverse(o => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = false; } });
      ok({ scene: g.scene, box: new THREE.Box3().setFromObject(g.scene) });
    }, undefined, fail)));
  }
  return models.get(url);
}

/** The camera that took the photo, from the floor rectangle. Returns null when the rectangle can't give one. */
export function cameraFromFloor(plane, sx, W, H) {
  const [w, d] = plane.size;
  const Hm = NV.homography([[0, 0], [w, 0], [w, d], [0, d]], plane.quad.map(([x, y]) => [x * sx, y * sx]));   // cm -> px
  const cx = W / 2, cy = H / 2, col = j => [Hm[j] - cx * Hm[6 + j], Hm[3 + j] - cy * Hm[6 + j], Hm[6 + j]];
  const h1 = col(0), h2 = col(1), h3 = col(2), big = Math.max(W, H);
  // focal length: the floor's two directions are at right angles, and both are measured in cm (same length scale)
  const ok = f2 => Number.isFinite(f2) && f2 > (0.45 * big) ** 2 && f2 < (2.6 * big) ** 2;
  const fa = -(h1[0] * h2[0] + h1[1] * h2[1]) / (h1[2] * h2[2]);
  const fb = (h1[0] ** 2 + h1[1] ** 2 - h2[0] ** 2 - h2[1] ** 2) / (h2[2] ** 2 - h1[2] ** 2);
  const f = ok(fa) && ok(fb) ? Math.sqrt(Math.sqrt(fa * fb)) : ok(fa) ? Math.sqrt(fa) : ok(fb) ? Math.sqrt(fb) : 0.8 * big;
  const k = h => new THREE.Vector3(h[0] / f, h[1] / f, h[2]);
  let r1 = k(h1), r2 = k(h2), t = k(h3);
  const lam = 2 / (r1.length() + r2.length());
  r1.multiplyScalar(lam); r2.multiplyScalar(lam); t.multiplyScalar(lam);
  if (t.z < 0) { r1.negate(); r2.negate(); t.negate(); }        // the floor is in front of the camera
  r1.normalize(); r2.sub(r1.clone().multiplyScalar(r1.dot(r2))).normalize();
  const r3 = new THREE.Vector3().crossVectors(r1, r2);           // points down into the floor
  // world (three.js, metres, y up) -> floor plan (cm): X = 100x, Y = 100z, Z(down) = -100y
  // camera (OpenCV: x right, y down, z ahead) = R * plan + t;  three.js camera = (x, -y, -z)
  const R = new THREE.Matrix3().set(r1.x, r2.x, r3.x, r1.y, r2.y, r3.y, r1.z, r2.z, r3.z);
  const A = R.clone().multiply(new THREE.Matrix3().set(1, 0, 0, 0, 0, 1, 0, -1, 0));
  const e = A.elements;                                          // column-major
  const V = new THREE.Matrix4().set(
    e[0], e[3], e[6], t.x / 100,
    -e[1], -e[4], -e[7], -t.y / 100,
    -e[2], -e[5], -e[8], -t.z / 100,
    0, 0, 0, 1);
  const cam = new THREE.PerspectiveCamera(2 * Math.atan(H / 2 / f) * 180 / Math.PI, W / H, 0.05, 200);
  cam.matrixAutoUpdate = false;
  cam.matrixWorldInverse.copy(V); cam.matrixWorld.copy(V).invert();
  cam.matrixWorld.decompose(cam.position, cam.quaternion, cam.scale);
  cam.updateProjectionMatrix();
  if (cam.position.y < 0.3 || cam.position.y > 4) return null;   // not a sensible camera height: don't guess
  return cam;
}

// a soft dark patch under each floor product, so it sits on the floor even where the light's shadow is faint
const contactTex = (() => {
  const c = Object.assign(document.createElement("canvas"), { width: 128, height: 128 }), x = c.getContext("2d");
  const g = x.createRadialGradient(64, 64, 8, 64, 64, 64);
  g.addColorStop(0, "rgba(0,0,0,.55)"); g.addColorStop(0.6, "rgba(0,0,0,.25)"); g.addColorStop(1, "rgba(0,0,0,0)");
  x.fillStyle = g; x.fillRect(0, 0, 128, 128);
  return new THREE.CanvasTexture(c);
})();

export function create(canvas, { onSelect = () => {}, onChange = () => {}, blocked = () => false } = {}) {
  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(1);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  const scene = new THREE.Scene();
  const pmrem = new THREE.PMREMGenerator(renderer);
  const studioEnv = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environment = studioEnv; scene.environmentIntensity = 0.6;
  let roomEnv = null;
  /* Reflections from the room photo itself (wrapped around as if it were all around the product), so a mirror,
     chrome or a glossy bath picks up the room's own colours instead of a grey studio. Only an approximation. */
  function useRoomLight(url) {
    new THREE.TextureLoader().load(url, t => {
      t.mapping = THREE.EquirectangularReflectionMapping; t.colorSpace = THREE.SRGBColorSpace;
      if (roomEnv) roomEnv.dispose();
      roomEnv = pmrem.fromEquirectangular(t).texture; t.dispose();
      scene.environment = roomEnv; scene.environmentIntensity = 0.9; render();
    });
  }
  const hemi = new THREE.HemisphereLight(0xffffff, 0x8f8476, 0.9); scene.add(hemi);
  const sun = new THREE.DirectionalLight(0xfff4e6, 1.5);
  sun.castShadow = true; sun.shadow.mapSize.set(2048, 2048); sun.shadow.radius = 6; sun.shadow.bias = -0.0004;
  Object.assign(sun.shadow.camera, { left: -7, right: 7, top: 7, bottom: -7, near: 0.5, far: 30 });
  scene.add(sun, sun.target);
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(60, 60), new THREE.ShadowMaterial({ opacity: 0.28 }));
  ground.rotation.x = -Math.PI / 2; ground.receiveShadow = true; scene.add(ground);
  const group = new THREE.Group(), wallGroup = new THREE.Group(); scene.add(group, wallGroup);
  const ray = new THREE.Raycaster();

  let cam = null, W = 0, H = 0, scale = 1, walls = [], items = [], sel = null, roomFloor = null;

  /** room: the visualiser room; size: { width, height, scale } from NovaVisualiser.setRoom. Returns true if products can go in. */
  function setRoom(room, size, floorScale = 1) {
    clear(false);
    W = size.width; H = size.height; scale = size.scale; cam = null; walls = []; roomFloor = null; wallGroup.clear();
    canvas.width = W; canvas.height = H; renderer.setSize(W, H, false);
    const floor = (room.surfaces && room.surfaces.floor || [])[0];
    // floorScale: the customer's "Room size" correction for their own photo (the tiles use the same one)
    if (floor && floor.quad && floor.size) cam = cameraFromFloor({ ...floor, size: floor.size.map(v => v * floorScale) }, size.scale, W, H);
    if (!cam) { render(); return false; }
    roomFloor = floor; const sx = scale;
    scene.environment = studioEnv; scene.environmentIntensity = 0.6;
    if (room.photo) useRoomLight(room.photo);
    // the walls: the bottom edge of each wall rectangle, found on the floor
    for (const pl of (room.surfaces.wall || [])) {
      if (!pl.quad) continue;
      const a = floorAt(pl.quad[3][0] * sx / W, pl.quad[3][1] * sx / H), b = floorAt(pl.quad[2][0] * sx / W, pl.quad[2][1] * sx / H);
      if (!a || !b || a.distanceTo(b) < 0.6 || a.distanceTo(cam.position) > 25 || b.distanceTo(cam.position) > 25) continue;
      const u = b.clone().sub(a).normalize(), n = new THREE.Vector3(-u.z, 0, u.x);
      if (n.dot(cam.position.clone().sub(a)) < 0) n.negate();
      const w = { a, b, u, n, len: a.distanceTo(b) };
      // the part of the wall's foot that is in the picture (wall rectangles often reach past the photo's edges)
      const seen = [];
      for (let i = 0; i <= 40; i++) {
        const s = w.len * i / 40, q = a.clone().add(u.clone().multiplyScalar(s)).project(cam);
        if (Math.abs(q.x) < 0.92 && q.y > -0.95 && q.y < 0.95 && q.z < 1) seen.push(s);
      }
      if (!seen.length) continue;
      w.from = seen[0]; w.to = seen[seen.length - 1];
      // an invisible wall: it catches the shadows of wall products and hides anything pushed behind it
      const m = new THREE.Mesh(new THREE.PlaneGeometry(w.len + 2, 3.2), new THREE.ShadowMaterial({ opacity: 0.22 }));
      m.position.copy(a).add(u.clone().multiplyScalar(w.len / 2)).setY(1.6).add(n.clone().multiplyScalar(-0.002));
      m.rotation.y = Math.atan2(n.x, n.z); m.receiveShadow = true; wallGroup.add(m);
      walls.push(w);
    }
    // light from above and a little behind the camera, so shadows fall away from the viewer
    const ahead = new THREE.Vector3(0, 0, -1).applyQuaternion(cam.quaternion).setY(0).normalize();
    const target = cam.position.clone().setY(0).add(ahead.clone().multiplyScalar(4));
    const right = new THREE.Vector3(-ahead.z, 0, ahead.x);
    sun.target.position.copy(target);
    sun.position.copy(target).add(ahead.clone().multiplyScalar(-2.5)).add(right.multiplyScalar(1.5)).setY(6);
    ground.position.set(target.x, 0, target.z);
    render();
    return true;
  }

  /** Where on the floor (y = 0) is the photo point (u, v), both 0..1? null if that is above the horizon. */
  function floorAt(u, v) {
    if (!cam) return null;
    ray.setFromCamera(new THREE.Vector2(u * 2 - 1, 1 - v * 2), cam);
    const p = ray.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0, 1, 0), 0), new THREE.Vector3());
    return p && p.distanceTo(cam.position) < 40 ? p : null;
  }

  /** Where a new floor product goes: the middle of the floor seen in the photo, between 2.2 and 6 m from the
   *  camera, and next to the products already there rather than inside them (r = half its footprint, metres). */
  function startSpot(r) {
    let u = 0.5, v = 0.75;
    if (roomFloor && roomFloor.area) {
      const pts = roomFloor.area, n = pts.length;
      u = pts.reduce((a, p) => a + p[0], 0) / n * scale / W; v = pts.reduce((a, p) => a + p[1], 0) / n * scale / H;
    }
    const c = floorAt(u, Math.min(0.95, v)) || floorAt(0.5, 0.85);
    if (!c) return null;
    const base = cam.position.clone().setY(0), flat = c.clone().sub(base);
    const centre = base.clone().add(flat.setLength(Math.max(2.2, Math.min(6, flat.length()))));
    const side = new THREE.Vector3(-flat.z, 0, flat.x).normalize(), back = flat.clone().normalize();
    const free = p => items.every(o => o.wall != null || o.obj.position.distanceTo(p) >= r + Math.hypot(o.size.x, o.size.z) / 2 * 0.8);
    for (let ring = 0; ring < 6; ring++) {               // look left and right, then a row further back
      for (const k of ring ? [ring, -ring] : [0]) {
        for (const row of [0, 1, 2]) {
          const p = centre.clone().add(side.clone().multiplyScalar(k * (r + 0.3))).add(back.clone().multiplyScalar(row * (r + 0.4)));
          const q = p.clone().project(cam);
          if (Math.abs(q.x) < 0.9 && Math.abs(q.y) < 0.9 && free(p)) return p;
        }
      }
    }
    return centre;
  }
  function nearestWall(p) {
    let best = null;
    for (const [i, w] of walls.entries()) {
      const s = Math.max(w.from, Math.min(w.to, p.clone().sub(w.a).dot(w.u)));
      const q = w.a.clone().add(w.u.clone().multiplyScalar(s)), d = q.distanceTo(p);
      if (!best || d < best.d) best = { i, s, d };
    }
    return best;
  }
  function onWall(it, s) {
    const w = walls[it.wall]; it.s = Math.max(-0.5, Math.min(w.len + 0.5, s));
    it.obj.position.copy(w.a).add(w.u.clone().multiplyScalar(it.s));
    it.obj.rotation.y = Math.atan2(w.n.x, w.n.z);
  }
  /** A new wall product goes next to the ones already on that wall, not inside them. */
  function freeSpotOnWall(it) {
    const w = walls[it.wall], others = items.filter(o => o.wall === it.wall);
    const free = s => others.every(o => Math.abs(o.s - s) >= (o.size.x + it.size.x) / 2 + 0.05);
    if (free(it.s)) return;
    const tries = others.flatMap(o => [o.s + (o.size.x + it.size.x) / 2 + 0.06, o.s - (o.size.x + it.size.x) / 2 - 0.06])
      .filter(s => s > w.from - 0.2 && s < w.to + 0.2 && free(s)).sort((a, b) => Math.abs(a - it.s) - Math.abs(b - it.s));
    if (tries.length) onWall(it, tries[0]);
  }
  function faceCamera(obj) {
    const d = cam.position.clone().sub(obj.position);
    obj.rotation.y = Math.round(Math.atan2(d.x, d.z) / (Math.PI / 12)) * (Math.PI / 12);   // 15° steps
  }

  async function build(product, state) {
    const m = await loadModel(product.model);
    const obj = new THREE.Group(), model = m.scene.clone(true);
    obj.add(model);
    const size = m.box.getSize(new THREE.Vector3()), center = m.box.getCenter(new THREE.Vector3());
    if (product.mount !== "wall" || (product.lift_cm || 0) < 20) {                   // contact shadow
      const c = new THREE.Mesh(new THREE.PlaneGeometry(size.x * 1.15, size.z * 1.15),
        new THREE.MeshBasicMaterial({ map: contactTex, transparent: true, depthWrite: false, opacity: product.mount === "wall" ? 0.5 : 0.8 }));
      c.rotation.x = -Math.PI / 2; c.position.set(center.x, 0.003, center.z); obj.add(c);
    }
    const wire = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(size.x, size.y, size.z)),
      new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.95, depthTest: false }));
    wire.position.copy(center); wire.visible = false; wire.renderOrder = 10; obj.add(wire);
    const it = { product, obj, wire, size, wall: null, s: 0 };
    obj.userData.item = it;
    if (state) {
      obj.position.set(state.x, 0, state.z); obj.rotation.y = state.rot || 0;
      if (state.wall != null && walls[state.wall]) { it.wall = state.wall; onWall(it, state.s || 0); }
    }
    return it;
  }

  async function add(product) {
    if (!cam) return null;
    const it = await build(product);
    const p = startSpot(Math.hypot(it.size.x, it.size.z) / 2 * 0.8); if (!p) return null;
    it.obj.position.copy(p);
    if (product.mount === "wall" && walls.length) {      // the middle of the longest wall in view
      const i = walls.reduce((bi, w, j) => (w.to - w.from > walls[bi].to - walls[bi].from ? j : bi), 0);
      it.wall = i; onWall(it, (walls[i].from + walls[i].to) / 2); freeSpotOnWall(it);
    }
    else faceCamera(it.obj);
    items.push(it); group.add(it.obj); select(it); changed();
    return it;
  }
  async function duplicate() {
    if (!sel) return;
    const it = await build(sel.product, state(sel)), step = sel.size.x + 0.1;      // next to it, along its width
    if (it.wall != null) onWall(it, it.s + step);
    else it.obj.position.add(new THREE.Vector3(step, 0, 0).applyAxisAngle(new THREE.Vector3(0, 1, 0), sel.obj.rotation.y));
    items.push(it); group.add(it.obj); select(it); changed();
  }
  function remove() {
    if (!sel) return;
    group.remove(sel.obj); items = items.filter(i => i !== sel); select(null); changed();
  }
  function rotate(deg) {
    if (!sel || sel.wall != null) return;
    sel.obj.rotation.y += deg * Math.PI / 180; changed();
  }
  /** Take a wall product off its wall (to stand it anywhere and turn it), or put it back on the nearest wall. */
  function toggleWall() {
    if (!sel || sel.product.mount !== "wall" || !walls.length) return;
    if (sel.wall != null) { sel.wall = null; faceCamera(sel.obj); }
    else { const w = nearestWall(sel.obj.position); sel.wall = w.i; onWall(sel, w.s); }
    select(sel); changed();
  }
  function select(it) {
    if (sel) sel.wire.visible = false;
    sel = it; if (sel) sel.wire.visible = true;
    render(); onSelect(sel ? { product: sel.product, onWall: sel.wall != null, canSnap: sel.product.mount === "wall" && walls.length > 0 } : null);
  }
  const state = it => ({ id: it.product.id, x: +it.obj.position.x.toFixed(3), z: +it.obj.position.z.toFixed(3),
                         rot: +it.obj.rotation.y.toFixed(4), wall: it.wall, s: +it.s.toFixed(3) });
  function changed() { render(); onChange(items.map(state)); }

  /** Put back saved products: [{ id, x, z, rot, wall, s }] with the product list to look them up. */
  async function restore(list, byId) {
    clear(false);
    const built = await Promise.all((list || []).filter(s => byId[s.id]).map(s => build(byId[s.id], s).catch(() => null)));
    for (const it of built) if (it) { items.push(it); group.add(it.obj); }
    render();
  }
  function clear(notify = true) {
    for (const it of items) group.remove(it.obj);
    items = []; sel = null; onSelect(null); render();
    if (notify) onChange([]);
  }

  function render() {
    if (!W) return;
    renderer.render(scene, cam || new THREE.PerspectiveCamera());
  }

  // ---------------------------------------------------------------- dragging
  let drag = null, swallowClick = false;
  const uv = e => { const b = canvas.getBoundingClientRect(); return [(e.clientX - b.left) / b.width, (e.clientY - b.top) / b.height]; };
  function hitItem(u, v) {
    if (!cam || !items.length) return null;
    ray.setFromCamera(new THREE.Vector2(u * 2 - 1, 1 - v * 2), cam);
    const hit = ray.intersectObjects(items.map(i => i.obj.children[0]), true)[0];
    if (!hit) return null;
    let o = hit.object; while (o && !o.userData.item) o = o.parent;
    return o ? o.userData.item : null;
  }
  function wallHit(it, u, v) {
    const w = walls[it.wall];
    ray.setFromCamera(new THREE.Vector2(u * 2 - 1, 1 - v * 2), cam);
    const p = ray.ray.intersectPlane(new THREE.Plane().setFromNormalAndCoplanarPoint(w.n, w.a), new THREE.Vector3());
    return p ? p.sub(w.a).dot(w.u) : null;
  }
  function attach(stage) {
    const inBar = e => e.target.closest && e.target.closest(".psel");         // the product's own buttons
    const down = e => {
      swallowClick = false;
      if (blocked() || !cam || inBar(e)) return;
      const [u, v] = uv(e), it = hitItem(u, v);
      swallowClick = !!it || !!sel;
      if (!it) { if (sel) select(null); return; }
      e.stopPropagation(); e.preventDefault();
      if (it !== sel) select(it);
      const start = it.wall != null ? wallHit(it, u, v) : floorAt(u, v);
      if (start == null) return;
      drag = { it, id: e.pointerId, start, from: it.wall != null ? it.s : it.obj.position.clone(), moved: false };
      try { stage.setPointerCapture(e.pointerId); } catch {}
    };
    const move = e => {
      if (!drag && e.pointerType !== "touch" && !blocked() && cam && items.length) {   // a hand over a product: "grab"
        const [u, v] = uv(e), over = !inBar(e) && !!hitItem(u, v);
        stage.classList.toggle("onprod", over);
        if (over) e.stopPropagation();
      }
      if (!drag || e.pointerId !== drag.id) return;
      e.stopPropagation(); e.preventDefault();
      const [u, v] = uv(e), it = drag.it;
      if (it.wall != null) { const s = wallHit(it, u, v); if (s != null) onWall(it, drag.from + s - drag.start); }
      else {
        const p = floorAt(u, Math.min(v, 0.995)); if (!p) return;
        const next = drag.from.clone().add(p.sub(drag.start));
        if (next.distanceTo(cam.position) > 15) return;
        it.obj.position.copy(next);
      }
      drag.moved = true; render();
    };
    const up = e => {
      if (!drag || e.pointerId !== drag.id) return;
      e.stopPropagation();
      if (drag.moved) changed();
      drag = null;
    };
    stage.addEventListener("pointerdown", down, true);
    stage.addEventListener("pointermove", move, true);
    stage.addEventListener("pointerup", up, true);
    stage.addEventListener("pointercancel", up, true);
    stage.addEventListener("click", e => { if (swallowClick) { e.stopPropagation(); swallowClick = false; } }, true);
    // on phones a finger on a product moves the product, not the page
    stage.addEventListener("touchstart", e => {
      if (blocked() || !cam || e.touches.length !== 1 || inBar(e)) return;
      const b = canvas.getBoundingClientRect(), t = e.touches[0];
      if (hitItem((t.clientX - b.left) / b.width, (t.clientY - b.top) / b.height)) e.preventDefault();
    }, { passive: false, capture: true });
  }

  return {
    setRoom, add, duplicate, remove, rotate, toggleWall, restore, clear, render, attach,
    select: () => select(null),
    get ready() { return !!cam; },
    get count() { return items.length; },
    get selected() { return sel ? sel.product : null; },
    /** Draw the products over a 2D canvas that already holds the tiled photo (same size). */
    drawOver(ctx) { if (items.length) { const s = sel; if (s) s.wire.visible = false; render(); ctx.drawImage(canvas, 0, 0, ctx.canvas.width, ctx.canvas.height); if (s) { s.wire.visible = true; render(); } } },
  };
}
