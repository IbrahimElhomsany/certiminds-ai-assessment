/* The shop's showroom: a 3D tile showroom on the home page that customers can look around in.
 *
 *  create(canvas, products, { onPick, onHover, onEntered }) -> { go(step), focus(sku), enter(), skip(), overview(), resize(), stop() }
 *
 *  The visit starts outside, on the paved square in front of the shop: the glass doors slide open and the
 *  visitor walks in (about 6 seconds; a tap skips it, and it is skipped for people who ask for less motion).
 *  Inside: drag to look, tap the floor or use the arrow keys / WASD to walk, tap a display for the product.
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
const DOOR_W = 2.6, DOOR_H = 2.7, WIN_X = 4.1, WIN_W = 3.4, WIN_Y = 0.45, WIN_H = 2.45;   // the shop front
const OUT = D / 2 + 7.5;                    // where the visit starts, metres in front of the doors' wall (z)

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
export async function create(canvas, products, { onPick, onHover, onEntered } = {}) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 0.92;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  const scene = new THREE.Scene(); scene.background = new THREE.Color(0xe9e5de);
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.45;
  const camera = new THREE.PerspectiveCamera(62, 1, 0.05, 60);
  let yaw = 0, pitch = -0.04, dirty = true, alive = true, idle = true, glide = null, intro = null;
  camera.position.set(-2.6, EYE, OUT);

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
  for (const w of [walls.back, walls.left, walls.right]) {
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
  const green = mat(0x4f6b3a, 0.9), potMat = mat(0xe8e2d8, 0.6);
  function plant(x, z, k = 1) {
    const pot = new THREE.Mesh(new THREE.CylinderGeometry(0.28 * k, 0.22 * k, 0.6 * k, 24), potMat); pot.position.set(x, 0.3 * k, z); scene.add(pot);
    for (let i = 0; i < 7; i++) {
      const leaf = new THREE.Mesh(new THREE.SphereGeometry(0.28 * k, 12, 10), green);
      const a = i * 0.9;
      leaf.scale.set(0.6, 1.4, 0.6); leaf.position.set(x + Math.cos(a) * 0.18 * k, (1.0 + (i % 3) * 0.28) * k, z + Math.sin(a) * 0.18 * k); scene.add(leaf);
    }
  }
  for (const [x, z] of [[-W / 2 + 0.7, -D / 2 + 0.7], [W / 2 - 0.7, -D / 2 + 0.7]]) plant(x, z);

  // ---- the shop front: glass doors and two shop windows, a sign, and the paved square outside
  const holes = [[0, 0, DOOR_W, DOOR_H], [-WIN_X, WIN_Y, WIN_W, WIN_H], [WIN_X, WIN_Y, WIN_W, WIN_H]];
  const wallWithHoles = (width, height, m) => {
    const sh = new THREE.Shape(); sh.moveTo(-width / 2, 0); sh.lineTo(width / 2, 0); sh.lineTo(width / 2, height); sh.lineTo(-width / 2, height); sh.closePath();
    for (const [cx, y0, w, h] of holes) { const ph = new THREE.Path(); ph.moveTo(cx - w / 2, y0); ph.lineTo(cx - w / 2, y0 + h); ph.lineTo(cx + w / 2, y0 + h); ph.lineTo(cx + w / 2, y0); ph.closePath(); sh.holes.push(ph); }
    return new THREE.Mesh(new THREE.ShapeGeometry(sh), m);
  };
  const inside = wallWithHoles(W, H, paint); inside.rotation.y = Math.PI; inside.position.set(0, 0, D / 2); scene.add(inside);
  const FZ = D / 2 + 0.18;                            // the outside face of the front wall
  const facade = wallWithHoles(W + 8, H + 1.6, mat(0x2f2d2b, 0.7)); facade.position.set(0, 0, FZ); scene.add(facade);
  const alu = mat(0x1a1a1a, 0.35, { metalness: 0.6 });
  for (const [cx, y0, w, h] of holes) {               // aluminium frames lining each opening
    for (const [x, y, fw, fh] of [[cx - w / 2, y0 + h / 2, 0.06, h], [cx + w / 2, y0 + h / 2, 0.06, h], [cx, y0 + h, w + 0.06, 0.06], ...(y0 > 0 ? [[cx, y0, w + 0.06, 0.06]] : [])]) {
      const f = new THREE.Mesh(new THREE.BoxGeometry(fw, fh, 0.2), alu); f.position.set(x, y, D / 2 + 0.09); scene.add(f);
    }
  }
  const glass = new THREE.MeshPhysicalMaterial({ color: 0xd6e6ea, roughness: 0.04, metalness: 0, transparent: true, opacity: 0.2,
                                                 side: THREE.DoubleSide, depthWrite: false, envMapIntensity: 1.2 });
  for (const x of [-WIN_X, WIN_X]) {
    const g = new THREE.Mesh(new THREE.PlaneGeometry(WIN_W, WIN_H), glass); g.position.set(x, WIN_Y + WIN_H / 2, D / 2 + 0.1); scene.add(g);
  }
  const doors = [-1, 1].map(side => {                 // two sliding glass doors with a long steel handle each
    const g = new THREE.Group();
    const pane = new THREE.Mesh(new THREE.PlaneGeometry(DOOR_W / 2, DOOR_H - 0.04), glass); pane.position.y = DOOR_H / 2; g.add(pane);
    for (const [x, y, fw, fh] of [[-DOOR_W / 4, DOOR_H / 2, 0.05, DOOR_H], [DOOR_W / 4, DOOR_H / 2, 0.05, DOOR_H], [0, 0.03, DOOR_W / 2, 0.06], [0, DOOR_H - 0.03, DOOR_W / 2, 0.06]]) {
      const f = new THREE.Mesh(new THREE.BoxGeometry(fw, fh, 0.05), alu); f.position.set(x, y, 0); g.add(f);
    }
    const handle = new THREE.Mesh(new THREE.CylinderGeometry(0.015, 0.015, 1.0, 10), mat(0xc9c9c9, 0.25, { metalness: 0.9 }));
    handle.position.set(-side * (DOOR_W / 4 - 0.12), 1.1, 0.06); g.add(handle);
    g.position.set(side * DOOR_W / 4, 0, D / 2 + 0.26); scene.add(g);
    return { g, side };
  });
  let doorOpen = 0;
  const setDoors = k => { doorOpen = k; for (const d of doors) d.g.position.x = d.side * (DOOR_W / 4 + k * (DOOR_W / 2 - 0.08)); dirty = true; };
  // the sign above the doors
  const sc = document.createElement("canvas"); sc.width = 2048; sc.height = 300;
  { const x = sc.getContext("2d"); x.fillStyle = "#151515"; x.fillRect(0, 0, 2048, 300);
    x.fillStyle = "#f3efe8"; x.textAlign = "center"; x.textBaseline = "middle";
    x.font = "600 150px system-ui, sans-serif"; x.letterSpacing = "36px"; x.fillText("CERAMICA NOVA", 1024, 130);
    x.font = "48px system-ui, sans-serif"; x.letterSpacing = "18px"; x.fillStyle = "#c9b48f"; x.fillText("TILES · BATHROOMS · SHOWROOM", 1024, 245); }
  const signBoard = new THREE.Mesh(new THREE.PlaneGeometry(7.2, 1.05), new THREE.MeshBasicMaterial({ map: texture(sc) }));
  signBoard.position.set(0, H + 0.45, FZ + 0.02); scene.add(signBoard);
  const glow = new THREE.SpotLight(0xffe2b8, 25, 9, 0.6, 0.6, 1.4); glow.position.set(0, H + 1.5, FZ + 2.2); glow.target.position.set(0, H + 0.2, FZ);
  scene.add(glow, glow.target);
  // the square outside, paved with one of our outdoor tiles
  const paveMat = mat(0xb9b2a6, 0.85);
  const pave = byId.CN014 || byId.CN004 || products.find(p => (p.locations || []).includes("outdoor"));
  if (pave) loadImage(pave.image).then(im => {
    const [tw, th] = size(pave);
    paveMat.map = texture(layTiles(im, tw * 2, th * 2, tw, th, 2), [40 * 100 / (tw * 2), 24 * 100 / (th * 2)]);
    paveMat.color.set(0xffffff); paveMat.needsUpdate = true; dirty = true;
  }).catch(() => {});
  const square = new THREE.Mesh(new THREE.PlaneGeometry(40, 24), paveMat); square.rotation.x = -Math.PI / 2; square.position.set(0, -0.01, FZ + 12); scene.add(square);
  const sill = new THREE.Mesh(new THREE.BoxGeometry(DOOR_W, 0.02, 0.4), alu); sill.position.set(0, 0.0, D / 2 + 0.1); scene.add(sill);
  const mat0 = new THREE.Mesh(new THREE.BoxGeometry(2.2, 0.01, 1.3), mat(0x3b3631, 1)); mat0.position.set(0, 0.006, D / 2 - 0.9); scene.add(mat0);   // door mat inside
  for (const x of [-2.25, 2.25]) plant(x, FZ + 0.55, 1.2);                  // two big planters by the doors
  for (const x of [-8.5, 8.5]) plant(x, FZ + 1.2, 1.5);
  const sky = document.createElement("canvas"); sky.width = 4; sky.height = 256;
  { const x = sky.getContext("2d"), g = x.createLinearGradient(0, 0, 0, 256);
    g.addColorStop(0, "#8fb7dc"); g.addColorStop(0.55, "#dbe7ef"); g.addColorStop(1, "#efe6d6"); x.fillStyle = g; x.fillRect(0, 0, 4, 256); }
  scene.background = texture(sky);
  const sun = new THREE.DirectionalLight(0xfff0d8, 1.6); sun.position.set(-6, 10, FZ + 12); sun.target.position.set(0, 0, FZ); scene.add(sun, sun.target);

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
  canvas.addEventListener("pointerdown", e => { if (intro) { skip(); return; } down = { x: e.clientX, y: e.clientY, yaw, pitch, moved: false }; idle = false; glide = null; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener("pointermove", e => {
    if (intro) return;
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
    if (intro || (!e.ctrlKey && Math.abs(e.deltaY) < 2)) return;
    e.preventDefault(); idle = false; glide = null;
    const f = new THREE.Vector3(-Math.sin(yaw), 0, -Math.cos(yaw)).multiplyScalar(-e.deltaY * 0.004);
    clampIn(camera.position.add(f)); dirty = true;
  }, { passive: false });

  // arrow keys / WASD walk (when the showroom has the keyboard: click it first)
  const keys = new Set();
  canvas.tabIndex = 0;
  canvas.addEventListener("keydown", e => {
    const k = e.key.toLowerCase();
    if (["arrowup", "arrowdown", "arrowleft", "arrowright", "w", "a", "s", "d"].includes(k)) {
      e.preventDefault(); if (intro) skip(); keys.add(k); idle = false; glide = null;
    }
  });
  canvas.addEventListener("keyup", e => keys.delete(e.key.toLowerCase()));
  canvas.addEventListener("blur", () => keys.clear());

  let current = -1;
  function focus(sku) {
    if (intro) settle();
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
  let start = performance.now(), yaw0 = 0, home = null, stride = 0, last = performance.now();
  const INSIDE = new THREE.Vector3(0.2, EYE, D / 2 - 2.6), INSIDE_LOOK = new THREE.Vector3(-1.6, 1.45, -D / 2);
  // the walk in: from the square, through the doors, into the hall
  const path = new THREE.CatmullRomCurve3([new THREE.Vector3(-2.6, EYE, OUT), new THREE.Vector3(-1.2, EYE, D / 2 + 4.2),
    new THREE.Vector3(0, EYE, D / 2 + 1.6), new THREE.Vector3(0, EYE, D / 2 - 0.6), INSIDE.clone()]);
  const SIGN = new THREE.Vector3(0, 2.7, FZ);
  const smooth = (a, b, x) => { const t = Math.min(1, Math.max(0, (x - a) / (b - a))); return t * t * (3 - 2 * t); };
  function settle() {                                   // standing inside: from here the visitor takes over
    intro = null; setDoors(1);
    camera.position.copy(INSIDE); [yaw, pitch] = lookAt(INSIDE_LOOK); aim();
    yaw0 = yaw; start = performance.now(); idle = true;
    home = { pos: INSIDE.clone(), yaw, pitch };
    if (onEntered) onEntered();
  }
  function enter() {                                    // play the walk in (again)
    glide = null; keys.clear(); setDoors(0);
    camera.position.copy(path.getPoint(0)); [yaw, pitch] = lookAt(SIGN); aim();
    intro = { t: 0 };                                    // time walked so far, ms (slow frames slow it down, never skip it)
  }
  function skip() { if (intro) settle(); }
  const WAIT = 900, DOORS = [700, 2300], WALK = [1100, 7200];
  function walkIn() {
    const t = intro.t;
    setDoors(smooth(DOORS[0], DOORS[1], t));
    const u = smooth(WALK[0], WALK[1], t);
    const p = path.getPoint(u);
    const moved = p.distanceTo(camera.position);
    stride += moved;
    camera.position.copy(p);
    camera.position.y = EYE + Math.sin(stride * Math.PI / 0.38) * 0.018 * Math.min(1, moved * 60);   // a gentle step
    const look = SIGN.clone().lerp(INSIDE_LOOK, smooth(0.25, 0.85, u));
    [yaw, pitch] = lookAt(look, p); aim();
    if (t >= WALK[1] + 300) settle();
  }
  function walkKeys(dt) {
    if (!keys.size) return;
    const f = (keys.has("arrowup") || keys.has("w") ? 1 : 0) - (keys.has("arrowdown") || keys.has("s") ? 1 : 0);
    const turn = (keys.has("arrowleft") || keys.has("a") ? 1 : 0) - (keys.has("arrowright") || keys.has("d") ? 1 : 0);
    yaw += turn * dt * 1.6;
    if (f) {
      const step = f * dt * 1.6;
      camera.position.add(new THREE.Vector3(-Math.sin(yaw), 0, -Math.cos(yaw)).multiplyScalar(step)); clampIn(camera.position);
      stride += Math.abs(step); camera.position.y = EYE + Math.sin(stride * Math.PI / 0.38) * 0.018;
    }
    aim();
  }
  function loop(now) {
    if (!alive) return;
    requestAnimationFrame(loop);
    const dt = Math.min(0.05, (now - last) / 1000); last = now;
    if (!visible || document.hidden) return;             // the walk in waits while off screen
    if (intro) {
      if (intro.drawn && !window.__srHold) intro.t += dt * 1000;   // (__srHold: lets the screenshot tests pause it)            // the clock starts once the first picture is on screen
      window.__srIntro = intro.t;
      if (intro.t < WAIT) { aim(); } else walkIn();
    } else if (glide) {
      const k = Math.min(1, (now - glide.t0) / glide.ms), e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      const before = camera.position.clone();
      camera.position.lerpVectors(glide.from, glide.to, e);
      stride += camera.position.distanceTo(before);
      camera.position.y = EYE + Math.sin(stride * Math.PI / 0.38) * 0.018 * Math.sin(Math.PI * k);
      yaw = glide.y0 + glide.dy * e; pitch = glide.p0 + (glide.p1 - glide.p0) * e; aim();
      if (k >= 1) { glide = null; camera.position.y = EYE; }
    } else if (keys.size) walkKeys(dt);
    else if (idle && !reduce) {                          // a slow look along the displays until the visitor takes over
      yaw = yaw0 - 0.5 + 0.5 * Math.cos((now - start) / 7000); aim();
    }
    if (dirty) { renderer.render(scene, camera); dirty = false; if (intro) intro.drawn = true; }
  }
  resize();
  if (reduce) settle(); else enter();
  requestAnimationFrame(loop);
  function overview() {
    current = -1;
    const look = home.pos.clone().add(new THREE.Vector3(-Math.sin(home.yaw), Math.tan(home.pitch), -Math.cos(home.yaw)));
    moveTo(home.pos, look);
  }
  return {
    go, focus, resize, overview, enter, skip, displays: displays.map(d => d.sku),
    stop() { alive = false; io.disconnect(); ro.disconnect(); renderer.dispose(); pmrem.dispose(); },
  };
}
