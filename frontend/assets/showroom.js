/* The shop's showroom: a 3D tile showroom on the home page that customers can look around in.
 *
 *  create(canvas, products, { onPick, onHover }) -> { go(step), focus(sku), resize(), stop() }
 *
 *  Every display is made from the shop's own products (/api/shop/products), so a new tile appears in the
 *  showroom by itself. Tiles are laid on the boards at their real size (a 60 x 60 tile covers 60 x 60 cm of a
 *  1.2 m wide board), floor tiles also get a sample square on the floor in front of their board.
 *  Drag to look around, tap the floor to walk there, tap a display to see the product.
 *  three.js is MIT licensed (vendor/THREE-LICENSE.txt). */
import * as THREE from "./vendor/three.module.min.js";
import { RoomEnvironment } from "./vendor/RoomEnvironment.js";

const W = 13, D = 12, H = 3.8;              // the hall, metres
const EYE = 1.6;
const BOARD_W = 1.2, BOARD_H = 2.2, BOARD_Y = 0.75;   // a display board and the height of its lower edge
const PER_WALL = 6;

// ------------------------------------------------------------------ pictures drawn on a canvas
function loadImage(url) {
  return new Promise((ok, bad) => { const im = new Image(); im.crossOrigin = "anonymous"; im.onload = () => ok(im); im.onerror = bad; im.src = url; });
}
function size(p) {
  const m = /^(\d+)\s*x\s*(\d+)/.exec(p.size_cm || "");
  return m ? [+m[1], +m[2]] : [60, 60];
}
/** Tiles laid at real size over wcm x hcm, with grout. Planks take one band of the product photo each. */
function layTiles(im, wcm, hcm, tw, th, k = 4) {
  const c = document.createElement("canvas"); c.width = Math.round(wcm * k); c.height = Math.round(hcm * k);
  const x = c.getContext("2d");
  const long = Math.max(tw, th) / Math.min(tw, th);
  const iw = im.naturalWidth, sh = iw / long, bands = Math.max(1, Math.floor(im.naturalHeight / sh));
  let n = 0;
  for (let r = 0; r * th < hcm; r++) {
    const shift = long >= 2.5 ? (r % 3) * tw / 3 : 0;            // planks are laid staggered
    for (let col = -1; col * tw < wcm; col++) {
      const cx = (col * tw + shift + tw / 2) * k, cy = (r * th + th / 2) * k;
      x.save(); x.translate(cx, cy);
      if (long >= 2.5) {
        if (th > tw) x.rotate(Math.PI / 2);
        const L = Math.max(tw, th) * k, S = Math.min(tw, th) * k;
        x.drawImage(im, 0, ((n * 5 + r) % bands) * sh, iw, sh, -L / 2, -S / 2, L, S);
      } else {
        if ((n * 7 + r * 3) % 4 === 1) x.rotate(Math.PI);          // vary the tiles a little, as a real wall does
        x.drawImage(im, -tw * k / 2, -th * k / 2, tw * k, th * k);
      }
      x.restore(); n++;
    }
  }
  x.fillStyle = "rgba(205,200,190,.95)";
  const g = Math.max(1, 0.3 * k);
  for (let r = 0; r * th <= hcm; r++) {
    x.fillRect(0, r * th * k - g / 2, c.width, g);
    const shift = long >= 2.5 ? (r % 3) * tw / 3 : 0;
    for (let col = -1; col * tw <= wcm + tw; col++) x.fillRect((col * tw + shift) * k - g / 2, r * th * k, g, th * k);
  }
  return c;
}
function texture(canvas, repeat) {
  const t = new THREE.CanvasTexture(canvas); t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 8;
  if (repeat) { t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(...repeat); }
  return t;
}
function label(p) {
  const c = document.createElement("canvas"); c.width = 600; c.height = 150;
  const x = c.getContext("2d");
  x.fillStyle = "#fbfaf7"; x.fillRect(0, 0, 600, 150);
  x.fillStyle = "#111"; x.font = "600 44px system-ui, sans-serif"; x.fillText(p.name.length > 22 ? p.name.slice(0, 21) + "…" : p.name, 28, 60);
  x.fillStyle = "#6b7280"; x.font = "30px system-ui, sans-serif";
  const unit = p.unit === "m2" ? "m²" : p.unit || "set";
  x.fillText(`${p.size_cm ? p.size_cm.replace("x", " × ") + " cm · " : ""}QAR ${Math.round(p.price).toLocaleString("en")} / ${unit}`, 28, 112);
  return texture(c);
}
function sign(text) {
  const c = document.createElement("canvas"); c.width = 1024; c.height = 128;
  const x = c.getContext("2d");
  x.fillStyle = "#2b2b2b"; x.font = "500 64px system-ui, sans-serif"; x.textAlign = "center"; x.textBaseline = "middle";
  x.letterSpacing = "12px"; x.fillText(text.toUpperCase(), 512, 66);
  const t = texture(c);
  return new THREE.Mesh(new THREE.PlaneGeometry(4, 0.5), new THREE.MeshBasicMaterial({ map: t, transparent: true }));
}
const mat = (color, rough = 0.8, extra = {}) => new THREE.MeshStandardMaterial({ color, roughness: rough, metalness: 0, ...extra });

// ------------------------------------------------------------------ which product goes where
export function arrange(products) {
  const tiles = products.filter(p => p.kind === "tile" && p.image);
  const wall = tiles.filter(p => (p.surfaces || []).includes("wall"));
  const outdoor = tiles.filter(p => (p.locations || []).includes("outdoor") && !wall.includes(p));
  const floor = tiles.filter(p => !wall.includes(p) && !outdoor.includes(p));
  const sets = products.filter(p => p.kind === "set" && p.image);
  return [
    { wall: "left", title: "Floor tiles", items: floor.slice(0, PER_WALL) },
    { wall: "back", title: "Wall tiles", items: wall.slice(0, PER_WALL) },
    { wall: "right", title: "Outdoor & bathroom", items: [...outdoor.slice(0, Math.max(3, PER_WALL - sets.length)), ...sets].slice(0, PER_WALL) },
  ];
}

// ------------------------------------------------------------------ the viewer
export async function create(canvas, products, { onPick, onHover } = {}) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 0.92;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  const scene = new THREE.Scene(); scene.background = new THREE.Color(0xe9e5de);
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.45;
  const camera = new THREE.PerspectiveCamera(62, 1, 0.05, 60);
  let yaw = 0, pitch = -0.04, dirty = true, alive = true, idle = true, glide = null;
  camera.position.set(0.4, EYE, D / 2 - 1.4);

  // ---- the hall
  const byId = Object.fromEntries(products.map(p => [p.sku, p]));
  const floorTile = byId.CN006 || products.find(p => p.kind === "tile" && size(p)[0] >= 60);
  const floorMat = mat(0xd9d4cb, 0.32);
  if (floorTile) loadImage(floorTile.image).then(im => {
    const [tw, th] = size(floorTile);
    floorMat.map = texture(layTiles(im, tw * 2, th * 2, tw, th, 2), [W * 100 / (tw * 2), D * 100 / (th * 2)]);
    floorMat.color.set(0xffffff); floorMat.needsUpdate = true; dirty = true;
  }).catch(() => {});
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(W, D), floorMat);
  floor.rotation.x = -Math.PI / 2; floor.userData.floor = true; scene.add(floor);
  const ceil = new THREE.Mesh(new THREE.PlaneGeometry(W, D), mat(0x3a3836, 0.9)); ceil.rotation.x = Math.PI / 2; ceil.position.y = H; scene.add(ceil);
  const paint = mat(0xf4f1ec, 0.95);
  const walls = {
    back: { pos: [0, 0, -D / 2], rot: 0, len: W },
    front: { pos: [0, 0, D / 2], rot: Math.PI, len: W },
    left: { pos: [-W / 2, 0, 0], rot: Math.PI / 2, len: D },
    right: { pos: [W / 2, 0, 0], rot: -Math.PI / 2, len: D },
  };
  for (const w of Object.values(walls)) {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w.len, H), paint);
    m.position.set(w.pos[0], H / 2, w.pos[2]); m.rotation.y = w.rot; scene.add(m);
    const sk = new THREE.Mesh(new THREE.BoxGeometry(w.len, 0.1, 0.02), mat(0x2b2b2b, 0.6));
    const n = new THREE.Vector3(0, 0, 1).applyAxisAngle(new THREE.Vector3(0, 1, 0), w.rot);
    sk.position.set(w.pos[0] + n.x * 0.01, 0.05, w.pos[2] + n.z * 0.01); sk.rotation.y = w.rot; scene.add(sk);
  }
  // light: a soft fill from above plus track lights along each display wall
  scene.add(new THREE.HemisphereLight(0xfffaf2, 0x8a8278, 0.9));
  const key = new THREE.DirectionalLight(0xfff3e2, 0.6); key.position.set(2, 8, 6); scene.add(key);
  const trackMat = mat(0x1d1d1d, 0.5, { metalness: 0.4 });
  for (const name of ["left", "back", "right"]) {
    const w = walls[name];
    const n = new THREE.Vector3(0, 0, 1).applyAxisAngle(new THREE.Vector3(0, 1, 0), w.rot);
    const rail = new THREE.Mesh(new THREE.BoxGeometry(w.len - 1, 0.04, 0.06), trackMat);
    rail.position.set(w.pos[0] + n.x * 1.2, H - 0.05, w.pos[2] + n.z * 1.2); rail.rotation.y = w.rot; scene.add(rail);
    for (const t of [-0.3, 0, 0.3]) {
      const along = new THREE.Vector3(1, 0, 0).applyAxisAngle(new THREE.Vector3(0, 1, 0), w.rot).multiplyScalar(t * w.len);
      const l = new THREE.PointLight(0xffe9cc, 4.5, 8, 1.6);
      l.position.set(w.pos[0] + n.x * 1.6 + along.x, H - 0.4, w.pos[2] + n.z * 1.6 + along.z); scene.add(l);
    }
  }
  // a bench and two plants in the middle, so the hall feels like a place
  const oak = mat(0xb68a5c, 0.55);
  const bench = new THREE.Mesh(new THREE.BoxGeometry(2.4, 0.08, 0.7), oak); bench.position.set(0, 0.45, 0.6); scene.add(bench);
  for (const dx of [-1, 1]) { const leg = new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.41, 0.6), mat(0x1d1d1d, 0.5)); leg.position.set(dx, 0.205, 0.6); scene.add(leg); }
  for (const [x, z] of [[-W / 2 + 0.7, D / 2 - 0.7], [W / 2 - 0.7, D / 2 - 0.7], [-W / 2 + 0.7, -D / 2 + 0.7], [W / 2 - 0.7, -D / 2 + 0.7]]) {
    const pot = new THREE.Mesh(new THREE.CylinderGeometry(0.28, 0.22, 0.6, 24), mat(0xe8e2d8, 0.6)); pot.position.set(x, 0.3, z); scene.add(pot);
    const green = mat(0x4f6b3a, 0.9);
    for (let i = 0; i < 7; i++) {
      const leaf = new THREE.Mesh(new THREE.SphereGeometry(0.28, 12, 10), green);
      const a = i * 0.9;
      leaf.scale.set(0.6, 1.4, 0.6); leaf.position.set(x + Math.cos(a) * 0.18, 1.0 + (i % 3) * 0.28, z + Math.sin(a) * 0.18); scene.add(leaf);
    }
  }

  // ---- the displays
  const displays = [];                                  // { sku, mesh, frame, stand: Vector3, look: Vector3 }
  const pickables = [floor];
  const frameMat = () => mat(0x262626, 0.45, { metalness: 0.3 });
  for (const group of arrange(products)) {
    const w = walls[group.wall], items = group.items;
    if (!items.length) continue;
    const n = new THREE.Vector3(0, 0, 1).applyAxisAngle(new THREE.Vector3(0, 1, 0), w.rot);
    const t = new THREE.Vector3(1, 0, 0).applyAxisAngle(new THREE.Vector3(0, 1, 0), w.rot);
    const step = Math.min(2.1, (w.len - 2.2) / items.length);
    const s = sign(group.title);
    s.position.set(w.pos[0] + n.x * 0.02, H - 0.55, w.pos[2] + n.z * 0.02); s.rotation.y = w.rot; scene.add(s);
    items.forEach((p, i) => {
      const off = (i - (items.length - 1) / 2) * step;
      const c = new THREE.Vector3(w.pos[0] + t.x * off, 0, w.pos[2] + t.z * off);
      const isSet = p.kind === "set";
      const bh = isSet ? 1.2 : BOARD_H, by = isSet ? 1.15 : BOARD_Y;
      const frame = new THREE.Mesh(new THREE.BoxGeometry(BOARD_W + 0.08, bh + 0.08, 0.05), frameMat());
      frame.position.set(c.x + n.x * 0.03, by + bh / 2, c.z + n.z * 0.03); frame.rotation.y = w.rot; scene.add(frame);
      const panelMat = mat(isSet ? 0xffffff : 0xdedad3, isSet ? 0.9 : 0.35);
      const panel = new THREE.Mesh(new THREE.PlaneGeometry(BOARD_W, bh), panelMat);
      panel.position.set(c.x + n.x * 0.058, by + bh / 2, c.z + n.z * 0.058); panel.rotation.y = w.rot; scene.add(panel);
      const plate = new THREE.Mesh(new THREE.PlaneGeometry(1.0, 0.25), new THREE.MeshBasicMaterial({ map: label(p) }));
      plate.position.set(c.x + n.x * 0.02, by - 0.24, c.z + n.z * 0.02); plate.rotation.y = w.rot; scene.add(plate);
      loadImage(p.image).then(im => {
        if (isSet) {
          const cv = document.createElement("canvas"); cv.width = cv.height = 800;
          const x = cv.getContext("2d"); x.fillStyle = "#fff"; x.fillRect(0, 0, 800, 800); x.drawImage(im, 40, 40, 720, 720);
          panelMat.map = texture(cv);
        } else {
          const [tw, th] = size(p);
          panelMat.map = texture(layTiles(im, BOARD_W * 100, BOARD_H * 100, tw, th));
        }
        panelMat.color.set(0xffffff); panelMat.needsUpdate = true; dirty = true;
      }).catch(() => {});
      if (isSet) {                                      // a white plinth under each bathroom set
        const pl = new THREE.Mesh(new THREE.BoxGeometry(1.0, 0.8, 0.55), mat(0xf7f6f3, 0.5));
        pl.position.set(c.x + n.x * 0.33, 0.4, c.z + n.z * 0.33); pl.rotation.y = w.rot; scene.add(pl);
      } else if (group.wall !== "back") {
        // floor tiles: a 1.2 m sample square on the floor in front of the board
        const pad = new THREE.Mesh(new THREE.BoxGeometry(BOARD_W, 0.012, BOARD_W), mat(0xdedad3, 0.3));
        pad.position.set(c.x + n.x * 0.75, 0.006, c.z + n.z * 0.75); pad.rotation.y = w.rot; scene.add(pad);
        panel.userData.pad = pad;
        loadImage(p.image).then(im => {
          const [tw, th] = size(p);
          const m = mat(0xffffff, 0.3, { map: texture(layTiles(im, BOARD_W * 100, BOARD_W * 100, tw, th)) });
          pad.material = [mat(0x8f8a82, 0.5), mat(0x8f8a82, 0.5), m, m, mat(0x8f8a82, 0.5), mat(0x8f8a82, 0.5)];
          pad.userData.sku = p.sku; pickables.push(pad); dirty = true;
        }).catch(() => {});
      }
      const d = { sku: p.sku, product: p, panel, frame, look: new THREE.Vector3(c.x + n.x * 0.06, 1.55, c.z + n.z * 0.06),
                  stand: new THREE.Vector3(c.x + n.x * 3.0, EYE, c.z + n.z * 3.0) };
      panel.userData.sku = frame.userData.sku = plate.userData.sku = p.sku;
      pickables.push(panel, frame, plate); displays.push(d);
    });
  }

  // ---- looking around
  function aim() { camera.rotation.set(pitch, yaw, 0, "YXZ"); dirty = true; }
  function lookAt(target, from = camera.position) {
    const d = target.clone().sub(from);
    return [Math.atan2(-d.x, -d.z), Math.atan2(d.y, Math.hypot(d.x, d.z))];
  }
  function moveTo(pos, look, ms = 900) {
    const [y1, p1] = look ? lookAt(look, pos) : [yaw, pitch];
    let dy = y1 - yaw; while (dy > Math.PI) dy -= 2 * Math.PI; while (dy < -Math.PI) dy += 2 * Math.PI;
    glide = { t0: performance.now(), ms, from: camera.position.clone(), to: pos.clone(), y0: yaw, dy, p0: pitch, p1 };
    idle = false;
  }
  const clampIn = v => { v.x = Math.max(-W / 2 + 0.9, Math.min(W / 2 - 0.9, v.x)); v.z = Math.max(-D / 2 + 0.9, Math.min(D / 2 - 0.9, v.z)); v.y = EYE; return v; };

  const ray = new THREE.Raycaster(), ndc = new THREE.Vector2();
  function hit(e) {
    const r = canvas.getBoundingClientRect();
    ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, camera);
    return ray.intersectObjects(pickables, false)[0];
  }
  let hover = null;
  function setHover(sku) {
    if (sku === hover) return;
    for (const d of displays) d.frame.material.color.set(d.sku === sku ? 0x3d6fd8 : 0x262626);
    hover = sku; canvas.style.cursor = sku ? "pointer" : "grab"; dirty = true;
    if (onHover) onHover(sku ? displays.find(d => d.sku === sku).product : null);
  }
  let down = null;
  canvas.addEventListener("pointerdown", e => { down = { x: e.clientX, y: e.clientY, yaw, pitch, moved: false }; idle = false; glide = null; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener("pointermove", e => {
    if (down) {
      const dx = e.clientX - down.x, dy = e.clientY - down.y;
      if (Math.hypot(dx, dy) > 5) down.moved = true;
      if (down.moved) {
        yaw = down.yaw + dx * 0.0045; pitch = Math.max(-0.7, Math.min(0.5, down.pitch + dy * 0.0035)); aim();
        canvas.style.cursor = "grabbing";
      }
    } else if (e.pointerType === "mouse") { const h = hit(e); setHover(h && h.object.userData.sku || null); }
  });
  canvas.addEventListener("pointerup", e => {
    const was = down; down = null; canvas.style.cursor = hover ? "pointer" : "grab";
    if (!was || was.moved) return;
    const h = hit(e); if (!h) return;
    if (h.object.userData.sku) return focus(h.object.userData.sku);
    if (h.object.userData.floor) {                      // walk to where the floor was tapped
      const to = clampIn(h.point.clone());
      moveTo(to, null, Math.min(1400, 300 + h.distance * 120));
      if (onPick) onPick(null);
    }
  });
  canvas.addEventListener("pointerleave", () => setHover(null));
  canvas.addEventListener("wheel", e => {                // scroll inside the showroom walks forward or back
    if (!e.ctrlKey && Math.abs(e.deltaY) < 2) return;
    e.preventDefault(); idle = false; glide = null;
    const f = new THREE.Vector3(-Math.sin(yaw), 0, -Math.cos(yaw)).multiplyScalar(-e.deltaY * 0.004);
    clampIn(camera.position.add(f)); dirty = true;
  }, { passive: false });

  let current = -1;
  function focus(sku) {
    const i = displays.findIndex(d => d.sku === sku); if (i < 0) return;
    current = i; const d = displays[i];
    moveTo(clampIn(d.stand.clone()), d.look);
    if (onPick) onPick(d.product);
  }
  function go(step) {
    if (!displays.length) return;
    current = (current + step + displays.length) % displays.length;
    focus(displays[current].sku);
  }

  // ---- drawing (only when something changed, and not while the showroom is off screen)
  let visible = true;
  const io = new IntersectionObserver(es => { visible = es[0].isIntersecting; }, { threshold: 0.05 });
  io.observe(canvas);
  function resize() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false); camera.aspect = w / h;
    camera.fov = w / h < 1 ? 75 : 62; camera.updateProjectionMatrix(); dirty = true;
  }
  const ro = new ResizeObserver(resize); ro.observe(canvas);
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const start = performance.now();
  [yaw, pitch] = lookAt(new THREE.Vector3(-2.2, 1.45, -D / 2));
  const yaw0 = yaw;
  aim(); resize();
  function loop(now) {
    if (!alive) return;
    requestAnimationFrame(loop);
    if (!visible || document.hidden) return;
    if (glide) {
      const k = Math.min(1, (now - glide.t0) / glide.ms), e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      camera.position.lerpVectors(glide.from, glide.to, e);
      yaw = glide.y0 + glide.dy * e; pitch = glide.p0 + (glide.p1 - glide.p0) * e; aim();
      if (k >= 1) glide = null;
    } else if (idle && !reduce) {                        // a slow look along the displays until the visitor takes over
      yaw = yaw0 - 0.55 + 0.55 * Math.cos((now - start) / 7000); aim();
    }
    if (dirty) { renderer.render(scene, camera); dirty = false; }
  }
  requestAnimationFrame(loop);
  const home = { pos: camera.position.clone(), yaw, pitch };
  function overview() {
    current = -1;
    const look = camera.position.clone().add(new THREE.Vector3(-Math.sin(home.yaw), Math.tan(home.pitch), -Math.cos(home.yaw)));
    moveTo(home.pos, look.sub(camera.position).add(home.pos));
  }
  return {
    go, focus, resize, overview, displays: displays.map(d => d.sku),
    stop() { alive = false; io.disconnect(); ro.disconnect(); renderer.dispose(); pmrem.dispose(); },
  };
}
