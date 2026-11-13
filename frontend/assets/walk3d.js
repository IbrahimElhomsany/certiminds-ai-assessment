/* Walk-through in 3D and the 360° view of the customer's own room.
 *
 *  create(canvas, { onPick }) -> { setRoom, setTile, setOptions, render, resize, snapshot, stop }
 *  Same tiles, layouts and grout as the photo visualiser (visualiser.js), so the controls on the page work for both.
 *
 *  room.kind === "3d":   one of our 3D rooms (SCENES below), built from simple shapes. Drag to look around,
 *                        tap the floor to walk there, or use the arrow keys / WASD.
 *  room.kind === "pano": the customer's 360° photo, shown on a sphere around the viewer. The floor is found by
 *                        the room detector (segment.js); tiles are laid on a flat floor 1.5 m below the camera.
 *
 *  three.js is MIT licensed (vendor/THREE-LICENSE.txt). */
import * as THREE from "./vendor/three.module.min.js";
import { RoomEnvironment } from "./vendor/RoomEnvironment.js";

const EYE = 1.6;                          // eye height when walking, m
const PANO_CAMERA = 1.5;                  // a phone held for a 360° photo, m above the floor

// ------------------------------------------------------------------ tile textures (same rules as the photo visualiser)
const imgCache = new Map();
function loadImage(url) {
  if (!imgCache.has(url)) imgCache.set(url, new Promise((ok, bad) => {
    const im = new Image(); im.crossOrigin = "anonymous"; im.onload = () => ok(im); im.onerror = bad; im.src = url;
  }));
  return imgCache.get(url);
}
/** A repeat block of laid tiles with grout. Returns { canvas, bw, bh } with the block size in cm. */
function tileBlock(im, t, o) {
  const patch = t.patch;
  const tw = o.rotate && !patch ? t.h : t.w, th = o.rotate && !patch ? t.w : t.h;
  const layout = patch ? 0 : (o.layout || 0);
  const rows = layout === 2 ? 3 : 2, cols = 2;
  const bw = patch ? patch[0] : cols * tw, bh = patch ? patch[1] : rows * th;
  const k = Math.min(4, 2048 / Math.max(bw, bh));          // px per cm
  const c = document.createElement("canvas"); c.width = Math.round(bw * k); c.height = Math.round(bh * k);
  const x = c.getContext("2d");
  if (patch) x.drawImage(im, 0, 0, c.width, c.height);
  else {
    let n = 0;
    for (let r = 0; r < rows; r++) {
      const shift = layout === 1 ? (r % 2) * tw / 2 : layout === 2 ? r * tw / 3 : 0;
      for (let col = -1; col <= cols; col++) {
        const px = (col * tw + shift) * k, py = r * th * k, flip = (n++ * 7 + r * 3) % 4 === 1;
        x.save(); x.translate(px + tw * k / 2, py + th * k / 2); if (flip) x.rotate(Math.PI);   // vary the tiles a little
        const long = Math.max(tw, th) / Math.min(tw, th);
        if (long >= 2.5) {
          // a plank: the product photo shows several planks side by side, so each plank takes one band of it
          const iw = im.naturalWidth, sh = iw / long, bands = Math.max(1, Math.floor(im.naturalHeight / sh));
          if (th > tw) x.rotate(Math.PI / 2);
          const L = Math.max(tw, th) * k, S = Math.min(tw, th) * k;
          x.drawImage(im, 0, ((n * 5 + r) % bands) * sh, iw, sh, -L / 2, -S / 2, L, S);
        } else x.drawImage(im, -tw * k / 2, -th * k / 2, tw * k, th * k);
        x.restore();
      }
    }
  }
  const g = Math.max(1, (o.grout || 0.3) * k);                // grout is given in cm
  x.fillStyle = o.groutColor || "#d9d6cf";                    // grout lines on every tile edge (patches too)
  const gw = patch ? t.w : tw, gh = patch ? t.h : th;
  for (let r = 0; r * gh < bh + 0.01; r++) {
    x.fillRect(0, r * gh * k - g / 2, c.width, g);
    const shift = patch ? 0 : layout === 1 ? (r % 2) * tw / 2 : layout === 2 ? r * tw / 3 : 0;
    for (let col = -1; col * gw < bw + gw; col++) x.fillRect((col * gw + shift) * k - g / 2, r * gh * k, g, gh * k);
  }
  return { canvas: c, bw, bh };
}

// ------------------------------------------------------------------ small helpers for building rooms
const mat = (color, rough = 0.8, extra = {}) => new THREE.MeshStandardMaterial({ color, roughness: rough, metalness: 0, ...extra });
function box(w, h, d, m, x, y, z, parent, shadow = true) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), m);
  mesh.position.set(x, y, z); mesh.castShadow = shadow; mesh.receiveShadow = true; parent.add(mesh); return mesh;
}
function rounded(w, h, d, r, m, x, y, z, parent) {           // a soft box (cushions, sofa)
  const s = new THREE.Shape(), a = w / 2 - r, b = h / 2 - r;
  s.moveTo(-a, -h / 2); s.lineTo(a, -h / 2); s.quadraticCurveTo(w / 2, -h / 2, w / 2, -b); s.lineTo(w / 2, b);
  s.quadraticCurveTo(w / 2, h / 2, a, h / 2); s.lineTo(-a, h / 2); s.quadraticCurveTo(-w / 2, h / 2, -w / 2, b);
  s.lineTo(-w / 2, -b); s.quadraticCurveTo(-w / 2, -h / 2, -a, -h / 2);
  const geo = new THREE.ExtrudeGeometry(s, { depth: d - 2 * r, bevelEnabled: true, bevelSize: r, bevelThickness: r, bevelSegments: 4, curveSegments: 6 });
  geo.translate(0, 0, -(d - 2 * r) / 2);
  const mesh = new THREE.Mesh(geo, m); mesh.position.set(x, y, z); mesh.castShadow = mesh.receiveShadow = true; parent.add(mesh); return mesh;
}
function lathe(points, m, x, y, z, parent, sx = 1, sz = 1) {
  const mesh = new THREE.Mesh(new THREE.LatheGeometry(points.map(([a, b]) => new THREE.Vector2(a, b)), 48), m);
  mesh.position.set(x, y, z); mesh.scale.set(sx, 1, sz); mesh.castShadow = mesh.receiveShadow = true; parent.add(mesh); return mesh;
}
function plant(x, z, parent, scale = 1) {
  const g = new THREE.Group(); g.position.set(x, 0, z); g.scale.setScalar(scale); parent.add(g);
  lathe([[0, 0], [0.17, 0], [0.21, 0.42], [0.19, 0.44], [0, 0.44]], mat(0xd8d2c8, 0.9), 0, 0, 0, g);
  box(0.03, 0.7, 0.03, mat(0x6b5438), 0, 0.75, 0, g);
  const leaf = mat(0x5f7d4a, 0.7), leaf2 = mat(0x4d6b3b, 0.7);
  const leafGeo = new THREE.SphereGeometry(1, 10, 6);
  for (let i = 0; i < 70; i++) {                               // many small leaves, each a flattened oval
    const a = i * 2.399, t = i / 70, r = 0.05 + 0.3 * Math.sin(t * Math.PI) * (0.6 + 0.4 * ((i * 7) % 5) / 5), y = 0.6 + t * 0.95;
    const m = new THREE.Mesh(leafGeo, i % 3 ? leaf : leaf2);
    m.position.set(Math.cos(a) * r, y, Math.sin(a) * r); m.scale.set(0.085, 0.012, 0.045);
    m.rotation.set(0.5 * Math.sin(i), -a, 0.6 * Math.cos(i * 1.3)); m.castShadow = true; g.add(m);
  }
  return g;
}
function gradientTexture(stops, w = 4, h = 256) {             // a vertical gradient, for the view out of the window
  const c = Object.assign(document.createElement("canvas"), { width: w, height: h }), x = c.getContext("2d");
  const gr = x.createLinearGradient(0, 0, 0, h); stops.forEach(([p, col]) => gr.addColorStop(p, col));
  x.fillStyle = gr; x.fillRect(0, 0, w, h);
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; return t;
}
function artTexture() {
  const c = Object.assign(document.createElement("canvas"), { width: 300, height: 400 }), x = c.getContext("2d");
  x.fillStyle = "#efe9df"; x.fillRect(0, 0, 300, 400);
  [["#c9a27e", 150, 150, 110], ["#8f9c8a", 110, 260, 80], ["#2f3a3f", 200, 290, 46]].forEach(([col, cx, cy, r]) => { x.fillStyle = col; x.beginPath(); x.arc(cx, cy, r, 0, 7); x.fill(); });
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; return t;
}

/** Walls are flat shapes so a window can be cut out; their texture coordinates are in metres, like the floor. */
function wallMesh(width, height, holes, m) {
  const s = new THREE.Shape(); s.moveTo(-width / 2, 0); s.lineTo(width / 2, 0); s.lineTo(width / 2, height); s.lineTo(-width / 2, height); s.closePath();
  for (const [cx, y0, w, h] of holes) { const p = new THREE.Path(); p.moveTo(cx - w / 2, y0); p.lineTo(cx - w / 2, y0 + h); p.lineTo(cx + w / 2, y0 + h); p.lineTo(cx + w / 2, y0); p.closePath(); s.holes.push(p); }
  const mesh = new THREE.Mesh(new THREE.ShapeGeometry(s), m); mesh.receiveShadow = true; mesh.castShadow = true; return mesh;
}

/** A box-shaped room: floor, ceiling, 4 walls. Returns the meshes so tiles can be put on them. */
function shell(root, W, D, H, opt) {
  const floorMat = mat(0xcfc8bd, 0.6), paint = mat(opt.paint || 0xf1ece4, 0.95);
  const fs = new THREE.Shape(); fs.moveTo(-W / 2, -D / 2); fs.lineTo(W / 2, -D / 2); fs.lineTo(W / 2, D / 2); fs.lineTo(-W / 2, D / 2); fs.closePath();
  const floor = new THREE.Mesh(new THREE.ShapeGeometry(fs), floorMat); floor.rotation.x = -Math.PI / 2; floor.receiveShadow = true; root.add(floor);
  const ceil = new THREE.Mesh(new THREE.PlaneGeometry(W, D), mat(0xfbfaf7, 1)); ceil.rotation.x = Math.PI / 2; ceil.position.y = H; root.add(ceil);
  const walls = {};
  const place = (name, width, rotY, x, z) => {
    const holes = (opt.holes && opt.holes[name]) || [];
    const m = wallMesh(width, H, holes, (opt.tiled || []).includes(name) ? mat(0xe6e0d6, 0.7) : paint);
    m.rotation.y = rotY; m.position.set(x, 0, z); root.add(m); walls[name] = m;
  };
  place("back", W, 0, 0, -D / 2); place("front", W, Math.PI, 0, D / 2);
  place("left", D, Math.PI / 2, -W / 2, 0); place("right", D, -Math.PI / 2, W / 2, 0);
  // skirting on painted walls
  const sk = mat(0xf7f5f0, 0.6);
  for (const [name, w, rot, x, z] of [["back", W, 0, 0, -D / 2 + 0.008], ["front", W, Math.PI, 0, D / 2 - 0.008], ["left", D, Math.PI / 2, -W / 2 + 0.008, 0], ["right", D, -Math.PI / 2, W / 2 - 0.008, 0]]) {
    if ((opt.tiled || []).includes(name)) continue;
    const b = box(w, 0.08, 0.016, sk, x, 0.04, z, root, false); b.rotation.y = rot;
  }
  return { floor, walls, tiledWalls: (opt.tiled || []).map(n => walls[n]) };
}

// ------------------------------------------------------------------ our 3D rooms
const SCENES = {
  living(root, scene) {
    const W = 6, D = 5, H = 2.9;
    const s = shell(root, W, D, H, { tiled: ["back"], holes: { left: [[0.4, 0.45, 2.3, 2.05]] } });
    // the view out of the window, and the sun coming through it
    const out = new THREE.Mesh(new THREE.PlaneGeometry(12, 7), new THREE.MeshBasicMaterial({ map: gradientTexture([[0, "#bcd6ec"], [0.55, "#eef4f6"], [0.62, "#c9d7b6"], [1, "#9fb48a"]]) }));
    out.position.set(-W / 2 - 3, 1.6, 0); out.rotation.y = Math.PI / 2; root.add(out);
    const sun = new THREE.DirectionalLight(0xfff1dc, 2.2); sun.position.set(-W / 2 - 4, 4.2, -2.2); sun.target.position.set(0.4, 0, 0.6);
    sun.castShadow = true; sun.shadow.mapSize.set(2048, 2048); Object.assign(sun.shadow.camera, { left: -6, right: 6, top: 6, bottom: -6, near: 0.5, far: 16 });
    sun.shadow.bias = -0.0004; sun.shadow.normalBias = 0.02; root.add(sun, sun.target);
    // curtains
    const fabric = mat(0xe9e3d8, 1);
    for (const z of [-1.0, 1.75]) box(0.06, 2.6, 0.5, fabric, -W / 2 + 0.1, 1.38, z, root);
    // TV wall: console and TV
    const walnut = mat(0x6e4a30, 0.55), black = mat(0x111111, 0.3, { metalness: 0.2 });
    box(2.4, 0.42, 0.42, walnut, 0, 0.32, -D / 2 + 0.23, root);
    box(1.65, 0.95, 0.05, black, 0, 1.42, -D / 2 + 0.04, root);
    // sofa facing the TV
    const fabricSofa = mat(0xd9d1c4, 0.95), x0 = 0.2, z0 = 1.05;
    rounded(2.5, 0.38, 0.95, 0.05, fabricSofa, x0, 0.25, z0, root);
    rounded(2.5, 0.5, 0.24, 0.07, fabricSofa, x0, 0.62, z0 + 0.36, root);
    for (const dx of [-1.15, 1.15]) rounded(0.24, 0.56, 0.95, 0.06, fabricSofa, x0 + dx, 0.34, z0, root);
    for (const dx of [-0.66, 0, 0.66]) rounded(0.64, 0.14, 0.7, 0.05, mat(0xe2dbcf, 1), x0 + dx, 0.5, z0 - 0.08, root);
    rounded(0.42, 0.38, 0.14, 0.06, mat(0xc49a86, 1), x0 - 0.75, 0.75, z0 + 0.18, root).rotation.z = 0.12;
    for (const dx of [-1.1, 1.1]) box(0.05, 0.08, 0.05, black, x0 + dx, 0.03, z0 + 0.36, root);
    // round coffee table
    lathe([[0, 0], [0.18, 0], [0.12, 0.05], [0.08, 0.36], [0, 0.36]], mat(0x2b2b2b, 0.4), 0.2, 0, -0.25, root);
    lathe([[0, 0.36], [0.52, 0.36], [0.52, 0.4], [0, 0.4]], mat(0xece7df, 0.25), 0.2, 0, -0.25, root);
    lathe([[0, 0.4], [0.06, 0.4], [0.08, 0.52], [0.03, 0.6], [0, 0.6]], mat(0x9a8b74, 0.5), 0.35, 0, -0.2, root);
    // armchair by the window
    const arm = mat(0xb59f86, 0.95);
    const chair = new THREE.Group(); chair.position.set(-2.2, 0, 1.4); chair.rotation.y = 2.3; root.add(chair);
    rounded(0.8, 0.4, 0.78, 0.08, arm, 0, 0.24, 0, chair); rounded(0.8, 0.45, 0.18, 0.08, arm, 0, 0.62, 0.32, chair);
    // floor lamp, plants, art
    lathe([[0, 0], [0.16, 0], [0.16, 0.02], [0.015, 0.03], [0.015, 1.55], [0, 1.55]], mat(0x2b2b2b, 0.4, { metalness: 0.5 }), 2.55, 0, -2.0, root);
    lathe([[0.08, 1.5], [0.24, 1.32], [0.25, 1.33], [0.09, 1.52]], mat(0xf3ead9, 0.9, { emissive: 0xffd9a0, emissiveIntensity: 0.6, side: THREE.DoubleSide }), 2.55, 0, -2.0, root);
    const lampLight = new THREE.PointLight(0xffc98a, 2.2, 5, 1.6); lampLight.position.set(2.55, 1.3, -2.0); root.add(lampLight);
    plant(-2.55, -2.05, root, 1.25); plant(2.6, 2.0, root, 0.9);
    const art = new THREE.Mesh(new THREE.PlaneGeometry(0.9, 1.2), new THREE.MeshStandardMaterial({ map: artTexture(), roughness: 0.9 }));
    art.position.set(W / 2 - 0.03, 1.6, -0.3); art.rotation.y = -Math.PI / 2; root.add(art);
    box(0.03, 1.26, 0.96, mat(0x2b2b2b, 0.5), W / 2 - 0.015, 1.6, -0.3, root, false);
    // ceiling lights
    for (const [x, z] of [[-1.2, -1], [1.2, -1], [-1.2, 1.2], [1.2, 1.2]]) {
      const d = new THREE.Mesh(new THREE.CircleGeometry(0.06, 20), new THREE.MeshBasicMaterial({ color: 0xfff6e8 })); d.rotation.x = Math.PI / 2; d.position.set(x, H - 0.005, z); root.add(d);
      const l = new THREE.PointLight(0xffe7c4, 1.6, 4.5, 1.8); l.position.set(x, H - 0.15, z); root.add(l);
    }
    scene.background = new THREE.Color(0xdcd6cc);
    return { ...s, W, D, H, start: { pos: [2.45, EYE, 2.2], yaw: 0.62, pitch: -0.26 },
             block: [[x0 - 1.3, z0 - 0.5, x0 + 1.3, z0 + 0.5], [-1.2, -D / 2, 1.2, -D / 2 + 0.45], [-0.35, -0.8, 0.75, 0.3], [-2.65, 0.95, -1.75, 1.85], [-2.9, -2.4, -2.2, -1.7], [2.3, -2.3, 2.9, -1.7], [2.3, 1.7, 2.9, 2.3]] };
  },

  bath(root, scene) {
    const W = 3.6, D = 3.0, H = 2.7;
    const s = shell(root, W, D, H, { tiled: ["back", "front", "left", "right"], holes: { back: [[-0.8, 1.55, 1.0, 0.8]] } });
    const out = new THREE.Mesh(new THREE.PlaneGeometry(6, 4), new THREE.MeshBasicMaterial({ map: gradientTexture([[0, "#c7dcee"], [0.7, "#f1f5f6"], [1, "#d9e3cf"]]) }));
    out.position.set(-0.8, 1.9, -D / 2 - 2); root.add(out);
    const sun = new THREE.DirectionalLight(0xfff1dc, 2.6); sun.position.set(-2.2, 5.5, -D / 2 - 3.5); sun.target.position.set(-0.4, 0, 0.4);
    sun.castShadow = true; sun.shadow.mapSize.set(2048, 2048); Object.assign(sun.shadow.camera, { left: -4, right: 4, top: 4, bottom: -4, near: 0.5, far: 14 });
    sun.shadow.bias = -0.0004; sun.shadow.normalBias = 0.02; root.add(sun, sun.target);
    const white = mat(0xfbfbf9, 0.12), gold = mat(0xd9bb84, 0.3, { metalness: 0.85 });
    // freestanding bath under the window
    lathe([[0, 0.02], [0.38, 0.02], [0.47, 0.25], [0.5, 0.58], [0.47, 0.6], [0.43, 0.3], [0.33, 0.12], [0, 0.12]], white, -0.75, 0, -D / 2 + 0.62, root, 1.75, 1);
    box(0.03, 0.18, 0.03, gold, -0.75, 0.68, -D / 2 + 0.2, root);                       // tap on the wall above the bath
    box(0.03, 0.03, 0.16, gold, -0.75, 0.76, -D / 2 + 0.26, root);
    // floating vanity with basin and round mirror on the right wall
    const oak = mat(0xb68a5c, 0.55);
    box(0.52, 0.45, 1.3, oak, W / 2 - 0.27, 0.62, 0.15, root);
    box(0.55, 0.03, 1.34, mat(0xe9e4dc, 0.3), W / 2 - 0.27, 0.86, 0.15, root);
    lathe([[0, 0.875], [0.2, 0.875], [0.23, 1.0], [0.21, 1.0], [0.18, 0.9], [0, 0.9]], white, W / 2 - 0.3, 0, 0.15, root);
    lathe([[0.012, 0.87], [0.012, 1.18], [0.02, 1.18]], gold, W / 2 - 0.08, 0, 0.15, root);
    const mirror = new THREE.Mesh(new THREE.CircleGeometry(0.42, 64), new THREE.MeshStandardMaterial({ color: 0xdfe6ea, roughness: 0.02, metalness: 1 }));
    mirror.position.set(W / 2 - 0.02, 1.62, 0.15); mirror.rotation.y = -Math.PI / 2; root.add(mirror);
    const ring = new THREE.Mesh(new THREE.TorusGeometry(0.43, 0.012, 8, 64), gold); ring.position.copy(mirror.position); ring.rotation.y = -Math.PI / 2; root.add(ring);
    const glow = new THREE.PointLight(0xffd7a0, 0.8, 2.2, 2); glow.position.set(W / 2 - 0.25, 1.62, 0.15); root.add(glow);
    // shower glass in the front-left corner, towels, a plant
    const glass = new THREE.Mesh(new THREE.PlaneGeometry(1.0, 2.0), new THREE.MeshPhysicalMaterial({ color: 0xffffff, roughness: 0.05, transmission: 0.9, transparent: true, opacity: 0.25, side: THREE.DoubleSide }));
    glass.position.set(-W / 2 + 1.0, 1.0, D / 2 - 0.5); glass.rotation.y = Math.PI / 2; root.add(glass);
    lathe([[0, 2.2], [0.14, 2.2], [0.14, 2.21], [0, 2.22]], gold, -W / 2 + 0.5, 0, D / 2 - 0.5, root);
    box(0.03, 0.03, 0.6, gold, -W / 2 + 0.06, 1.15, -0.15, root, false);                  // towel on a rail
    rounded(0.05, 0.62, 0.5, 0.02, mat(0xcdbfae, 1), -W / 2 + 0.085, 0.86, -0.15, root);
    plant(W / 2 - 0.35, -D / 2 + 0.35, root, 0.8);
    for (const [x, z] of [[-0.8, -0.5], [0.6, -0.5], [-0.8, 0.8], [0.6, 0.8]]) {
      const d = new THREE.Mesh(new THREE.CircleGeometry(0.045, 20), new THREE.MeshBasicMaterial({ color: 0xfff6e8 })); d.rotation.x = Math.PI / 2; d.position.set(x, H - 0.005, z); root.add(d);
      const l = new THREE.PointLight(0xffe7c4, 1.1, 3.5, 1.8); l.position.set(x, H - 0.15, z); root.add(l);
    }
    scene.background = new THREE.Color(0xdcd6cc);
    return { ...s, W, D, H, start: { pos: [0.7, EYE, 1.15], yaw: 0.55, pitch: -0.38 },
             block: [[-1.75, -D / 2, 0.25, -D / 2 + 1.05], [W / 2 - 0.56, -0.55, W / 2, 0.85], [-W / 2, D / 2 - 1.0, -W / 2 + 1.05, D / 2], [W / 2 - 0.6, -D / 2, W / 2, -D / 2 + 0.6]] };
  },
};

// ------------------------------------------------------------------ the 360° photo: floor tiles drawn into the sphere
const PANO_VERT = `
  varying vec3 vDir; varying vec2 vUv;
  void main() { vUv = uv; vDir = normalize((modelMatrix * vec4(position, 1.0)).xyz); gl_Position = projectionMatrix * viewMatrix * modelMatrix * vec4(position, 1.0); }`;
const PANO_FRAG = `
  uniform sampler2D uPano, uMask, uTile, uWTile; uniform float uHasTile, uHasWTile, uCam, uMean, uWMean, uRot, uAlpha, uHasRect, uHfov, uVfov;
  uniform vec2 uBlock, uWBlock; uniform vec4 uRect;   // the room's walls, metres from the camera: x-, x+, z-, z+
  uniform vec3 uHot[4];                                // places you can move to: floor x, z (metres), 1 when used
  uniform float uNoMask;                               // 1 when the floor was not found: show the circles everywhere
  varying vec3 vDir; varying vec2 vUv;
  vec3 lightIt(vec3 tile, vec3 photo, float mean) {
    float lum = dot(photo, vec3(0.299, 0.587, 0.114));
    return tile * clamp(mix(1.0, lum / max(mean, 0.05), 0.85), 0.25, 1.6);
  }
  void main() {
    vec3 photo = texture2D(uPano, vUv).rgb;
    vec2 mk = texture2D(uMask, vUv).rg;
    float mf = mk.r * uHasTile, mw = mk.g * uHasWTile * uHasRect;
    // the direction of this pixel in the photo, turned so the room's walls run along x and z
    float th = (vUv.x - 0.5) * uHfov, ph = (vUv.y - 0.5) * uVfov;
    vec3 d0 = vec3(sin(th) * cos(ph), sin(ph), cos(th) * cos(ph));
    float ca = cos(uAlpha), sa = sin(uAlpha);
    vec3 d = vec3(d0.x * ca + d0.z * sa, d0.y, -d0.x * sa + d0.z * ca);
    vec3 col = photo;
    if (mf > 0.01 && d.y < -0.02) {
      vec2 p = d.xz * (uCam / -d.y) * 100.0;                              // point on the floor, cm
      float c = cos(uRot), s = sin(uRot); p = mat2(c, -s, s, c) * p;
      float fade = smoothstep(-0.02, -0.08, d.y);                        // far away the floor melts into the photo
      col = mix(col, lightIt(texture2D(uTile, p / uBlock).rgb, photo, uMean), mf * fade);
    }
    if (mw > 0.01) {
      float tx = d.x > 0.0 ? uRect.y / d.x : d.x < 0.0 ? uRect.x / d.x : 1e9;
      float tz = d.z > 0.0 ? uRect.w / d.z : d.z < 0.0 ? uRect.z / d.z : 1e9;
      float t = min(tx, tz); vec3 h = d * t;
      vec2 q = vec2(tx < tz ? h.z : h.x, h.y + uCam) * 100.0;           // along the wall and up from the floor, cm
      if (q.y > 0.0) col = mix(col, lightIt(texture2D(uWTile, q / uWBlock).rgb, photo, uWMean), mw);
    }
    if (d0.y < -0.05) {                                 // circles on the floor where you can move to
      vec2 f = d0.xz * (uCam / -d0.y);
      for (int i = 0; i < 4; i++) {
        if (uHot[i].z < 0.5) continue;
        float r = length(f - uHot[i].xy);
        float ring = smoothstep(0.30, 0.27, r) * smoothstep(0.20, 0.23, r);
        float seen = max(smoothstep(0.3, 0.7, mk.r), uNoMask);          // hidden behind furniture
        col = mix(col, vec3(1.0), (ring * 0.95 + smoothstep(0.23, 0.18, r) * 0.22) * seen);
      }
    }
    gl_FragColor = vec4(col, 1.0);
  }`;

// ------------------------------------------------------------------ the viewer
export function create(canvas, { onPick, onJump } = {}) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 0.95;
  renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  const pmrem = new THREE.PMREMGenerator(renderer), envTex = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;

  let scene = null, camera = new THREE.PerspectiveCamera(68, 16 / 9, 0.05, 200), room = null, built = null, kind = null;
  let yaw = 0, pitch = 0, walkTo = null, panoMat = null, raf = 0, dirty = true;
  const chosen = {}, keys = new Set();
  let opts = { layout: 0, rotate: false, grout: 0.3, groutColor: "#d9d6cf" };
  const maxAniso = renderer.capabilities.getMaxAnisotropy();

  function aim() { camera.rotation.set(pitch, yaw, 0, "YXZ"); dirty = true; }
  function resize() {
    const w = canvas.clientWidth || 800, h = canvas.clientHeight || 450;
    renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix(); dirty = true;
  }

  async function setRoom(r) {
    room = r; kind = r.kind; walkTo = null;
    scene = new THREE.Scene(); scene.environment = envTex; scene.environmentIntensity = 0.35;
    const root = new THREE.Group(); scene.add(root);
    if (kind === "3d") {
      scene.add(new THREE.HemisphereLight(0xfffaf2, 0xb9ad9d, 0.55));
      built = SCENES[r.scene](root, scene);
      camera.fov = 68; camera.position.set(...built.start.pos); yaw = built.start.yaw; pitch = built.start.pitch;
    } else {                                                       // 360° photo
      const im = await loadImage(r.photo);
      const tex = new THREE.Texture(im); tex.needsUpdate = true; tex.anisotropy = maxAniso;    // raw colours: the shader shows the photo as it is
      const ratio = im.naturalWidth / im.naturalHeight;
      // a full photo sphere is twice as wide as high; a phone panorama covers less, about 67° per photo height
      const hfov = ratio >= 1.95 && ratio <= 2.05 ? Math.PI * 2 : Math.min(Math.PI * 2, ratio * 67 * Math.PI / 180);
      const vfov = ratio >= 1.95 && ratio <= 2.05 ? Math.PI : hfov / ratio;
      const geo = new THREE.SphereGeometry(50, 96, 64, Math.PI / 2 - hfov / 2 + Math.PI, hfov, Math.PI / 2 - vfov / 2, vfov);
      geo.scale(-1, 1, 1);                                         // seen from inside
      panoMat = new THREE.ShaderMaterial({
        vertexShader: PANO_VERT, fragmentShader: PANO_FRAG,
        uniforms: { uPano: { value: tex }, uMask: { value: r.mask || blackTex() }, uTile: { value: blackTex() }, uHasTile: { value: 0 },
                    uWTile: { value: blackTex() }, uHasWTile: { value: 0 }, uWBlock: { value: new THREE.Vector2(60, 60) },
                    uCam: { value: PANO_CAMERA }, uMean: { value: r.floorMean || 0.5 }, uWMean: { value: r.wallMean || 0.7 }, uRot: { value: 0 },
                    uBlock: { value: new THREE.Vector2(60, 60) }, uHfov: { value: hfov }, uVfov: { value: vfov },
                    uAlpha: { value: r.rect ? r.rect.a : 0 }, uHasRect: { value: r.rect ? 1 : 0 },
                    uNoMask: { value: r.mask ? 0 : 1 },
                    uHot: { value: [0, 1, 2, 3].map(i => r.hot && r.hot[i] ? new THREE.Vector3(r.hot[i][0], r.hot[i][1], 1) : new THREE.Vector3()) },
                    uRect: { value: r.rect ? new THREE.Vector4(r.rect.x0, r.rect.x1, r.rect.z0, r.rect.z1) : new THREE.Vector4(-3, 3, -3, 3) } },
        toneMapped: false,
      });
      root.add(new THREE.Mesh(geo, panoMat));
      scene.background = new THREE.Color(0x222222);
      built = null; camera.fov = 75; camera.position.set(0, 0, 0); yaw = r.startYaw != null ? r.startYaw : 0; pitch = -0.35;
    }
    camera.updateProjectionMatrix(); aim(); resize();
    for (const s of Object.keys(chosen)) await applyTile(s);
    loop();
  }
  const blackTex = () => { const t = new THREE.DataTexture(new Uint8Array([0, 0, 0, 255]), 1, 1); t.needsUpdate = true; return t; };

  async function setTile(surface, tile) {
    if (!tile) delete chosen[surface]; else chosen[surface] = { ...tile, im: await loadImage(tile.image) };
    await applyTile(surface);
  }
  function setOptions(o) { opts = { ...opts, ...o }; for (const s of Object.keys(chosen)) applyTile(s); for (const s of ["floor", "wall"]) if (!chosen[s]) applyTile(s); }

  function texFor(surface) {
    const t = chosen[surface]; if (!t) return null;
    const so = { ...opts, ...(opts[surface] || {}) };
    const b = tileBlock(t.im, t, so), tex = new THREE.CanvasTexture(b.canvas);
    tex.colorSpace = THREE.SRGBColorSpace; tex.wrapS = tex.wrapT = THREE.RepeatWrapping; tex.anisotropy = maxAniso;
    return { tex, b, t };
  }
  async function applyTile(surface) {
    if (!scene) return;
    const made = texFor(surface);
    if (kind === "pano") {
      const u = panoMat.uniforms, w = surface === "wall";
      u[w ? "uHasWTile" : "uHasTile"].value = made ? 1 : 0;
      if (made) { made.tex.colorSpace = THREE.NoColorSpace; u[w ? "uWTile" : "uTile"].value = made.tex; u[w ? "uWBlock" : "uBlock"].value.set(made.b.bw, made.b.bh); }
      dirty = true; return;
    }
    if (!built) return;
    const meshes = surface === "floor" ? [built.floor] : built.tiledWalls;
    for (const m of meshes) {
      if (m.material.map) m.material.map.dispose();
      if (made) {
        made.tex.repeat.set(100 / made.b.bw, 100 / made.b.bh);    // geometry is in metres, the block in cm
        m.material = new THREE.MeshStandardMaterial({ map: made.tex.clone(), roughness: Math.max(0.08, 0.85 - (made.t.gloss || 0) * 1.5),
                                                      metalness: 0, envMapIntensity: 0.4 + (made.t.gloss || 0) * 1.4 });
        m.material.map.needsUpdate = true;
      } else m.material = mat(surface === "floor" ? 0xcfc8bd : 0xe6e0d6, 0.7);
    }
    dirty = true;
  }

  // -------------------------------------------------- moving around
  const ray = new THREE.Raycaster(), v2 = new THREE.Vector2();
  function hit(e) {
    const b = canvas.getBoundingClientRect(); v2.set((e.clientX - b.left) / b.width * 2 - 1, -((e.clientY - b.top) / b.height) * 2 + 1);
    ray.setFromCamera(v2, camera);
    if (!built) return null;
    const list = [built.floor, ...Object.values(built.walls)], h = ray.intersectObjects(scene.children, true)[0];
    if (!h) return null;
    if (h.object === built.floor) return { surface: "floor", point: h.point };
    if (built.tiledWalls.includes(h.object)) return { surface: "wall", point: h.point };
    return list.includes(h.object) ? { surface: null, point: h.point } : { surface: null };
  }
  const free = (x, z) => built && x > -built.W / 2 + 0.3 && x < built.W / 2 - 0.3 && z > -built.D / 2 + 0.3 && z < built.D / 2 - 0.3 &&
    !built.block.some(([a, b, c, d]) => x > a - 0.25 && x < c + 0.25 && z > b - 0.25 && z < d + 0.25);
  let down = null;
  canvas.addEventListener("pointerdown", e => { down = { x: e.clientX, y: e.clientY, yaw, pitch, moved: false }; canvas.setPointerCapture(e.pointerId); });
  // which circle on the floor of a 360° room is under the pointer
  function hotAt(e) {
    if (kind !== "pano" || !room || !room.hot || !room.hot.length) return null;
    const b = canvas.getBoundingClientRect(); v2.set((e.clientX - b.left) / b.width * 2 - 1, -((e.clientY - b.top) / b.height) * 2 + 1);
    ray.setFromCamera(v2, camera);
    const w = ray.ray.direction, th = Math.atan2(w.x, -w.z), dy = w.y;
    if (dy > -0.05) return null;
    const k = PANO_CAMERA / -dy, fx = Math.sin(th) * Math.hypot(w.x, w.z) * k, fz = Math.cos(th) * Math.hypot(w.x, w.z) * k;
    let best = null;
    for (const [x, z, j] of room.hot) { const r = Math.hypot(fx - x, fz - z); if (r < 0.42 && (!best || r < best.r)) best = { r, j, th }; }
    return best;
  }
  canvas.addEventListener("pointermove", e => {
    if (!down) { if (kind === "pano") canvas.style.cursor = hotAt(e) ? "pointer" : ""; return; }
    const dx = e.clientX - down.x, dy = e.clientY - down.y;
    if (Math.abs(dx) + Math.abs(dy) > 5) down.moved = true;
    const k = (camera.fov / 68) * 0.005;
    yaw = down.yaw + dx * k; pitch = Math.max(-1.35, Math.min(1.2, down.pitch + dy * k)); aim();
  });
  canvas.addEventListener("pointerup", e => {
    const wasClick = down && !down.moved; down = null;
    if (wasClick && kind === "pano") { const h = hotAt(e); if (h && onJump) onJump(h.j, h.th); return; }
    if (!wasClick || kind !== "3d") return;
    const h = hit(e); if (!h) return;
    if (h.surface && onPick) onPick(h.surface);
    if (h.surface === "floor") {
      let { x, z } = h.point;
      for (let i = 0; i < 12 && !free(x, z); i++) { x += (camera.position.x - x) * 0.25; z += (camera.position.z - z) * 0.25; }
      if (free(x, z)) walkTo = new THREE.Vector3(x, EYE, z);
    }
  });
  canvas.addEventListener("pointercancel", () => { down = null; });
  canvas.addEventListener("wheel", e => { e.preventDefault(); camera.fov = Math.max(35, Math.min(90, camera.fov + e.deltaY * 0.03)); camera.updateProjectionMatrix(); dirty = true; }, { passive: false });
  canvas.tabIndex = 0;
  canvas.addEventListener("keydown", e => { if (/^(Arrow|Key[WASD])/.test(e.code)) { keys.add(e.code); e.preventDefault(); } });
  canvas.addEventListener("keyup", e => keys.delete(e.code));
  canvas.addEventListener("blur", () => keys.clear());

  let last = 0;
  function loop(t = performance.now()) {
    cancelAnimationFrame(raf);
    const dt = Math.min(0.05, (t - (last || t)) / 1000); last = t;
    if (!canvas.isConnected || canvas.offsetParent === null) { last = 0; raf = requestAnimationFrame(loop); return; }  // hidden: idle cheaply
    if (kind === "3d") {
      const f = (keys.has("KeyW") || keys.has("ArrowUp") ? 1 : 0) - (keys.has("KeyS") || keys.has("ArrowDown") ? 1 : 0);
      const turn = (keys.has("ArrowLeft") ? 1 : 0) - (keys.has("ArrowRight") ? 1 : 0), side = (keys.has("KeyD") ? 1 : 0) - (keys.has("KeyA") ? 1 : 0);
      if (turn) { yaw += turn * dt * 1.6; aim(); }
      if (f || side) {
        walkTo = null;
        const sp = 1.6 * dt, nx = camera.position.x - Math.sin(yaw) * f * sp + Math.cos(yaw) * side * sp, nz = camera.position.z - Math.cos(yaw) * f * sp - Math.sin(yaw) * side * sp;
        if (free(nx, camera.position.z)) camera.position.x = nx;
        if (free(camera.position.x, nz)) camera.position.z = nz;
        dirty = true;
      }
      if (walkTo) {
        const d = walkTo.clone().sub(camera.position); d.y = 0;
        if (d.length() < 0.02) walkTo = null; else camera.position.add(d.multiplyScalar(Math.min(1, dt * 3.2)));
        dirty = true;
      }
    }
    if (dirty && scene) { renderer.render(scene, camera); dirty = false; }
    raf = requestAnimationFrame(loop);
  }
  function render() { if (scene) { renderer.render(scene, camera); dirty = false; } }
  const snapshot = () => new Promise(ok => { render(); canvas.toBlob(ok, "image/jpeg", 0.9); });
  new ResizeObserver(resize).observe(canvas);
  return { setRoom, setTile, setOptions, render, resize, snapshot, stop: () => cancelAnimationFrame(raf),
           get state() { return { kind, chosen: Object.keys(chosen), floorMap: !!(built && built.floor.material.map) }; } };
}

/** Floor and walls of a 360° photo. The room detector was trained on ordinary photos, and a stretched
 *  360° picture confuses it, so we cut the sphere into ordinary views (down at the floor all around,
 *  level at the walls, one straight down), find the floor and walls in each, and vote the answers back.
 *  The vote is then sharpened against the photo itself so the edges follow the furniture, and the room's
 *  shape (a box) is worked out from where the floor meets the walls, so wall tiles can be laid on it.
 *  Returns { mask: DataTexture (R floor, G wall), mean, wallMean, share, wallShare, rect }. */
export async function panoFloor(url, onStep) {
  if (!window.NovaSegment) return null;
  const im = await loadImage(url), ratio = im.naturalWidth / im.naturalHeight;
  const full = ratio >= 1.95 && ratio <= 2.05;
  const hfov = full ? Math.PI * 2 : Math.min(Math.PI * 2, ratio * 67 * Math.PI / 180), vfov = full ? Math.PI : hfov / ratio;
  const sw = Math.min(2048, im.naturalWidth), sh = Math.round(sw / ratio);
  const sc = Object.assign(document.createElement("canvas"), { width: sw, height: sh }), sx = sc.getContext("2d", { willReadFrequently: true });
  sx.drawImage(im, 0, 0, sw, sh); const src = sx.getImageData(0, 0, sw, sh).data;
  // 1. votes on a coarse grid
  const cw = 480, ch = Math.max(1, Math.round(cw / ratio)), fV = new Uint16Array(cw * ch), wV = new Uint16Array(cw * ch), aV = new Uint16Array(cw * ch);
  const VW = 480, VH = 360, D = Math.PI / 180, views = [];
  const span = Math.max(0, hfov - 80 * D), n = full ? 6 : Math.max(1, Math.ceil(span / (60 * D)) + 1);
  for (let k = 0; k < n; k++) views.push({ yaw: full ? k * 60 * D : (n === 1 ? 0 : -span / 2 + k * span / (n - 1)), pitch: -40 * D, f: 1 });
  if (full) {
    views.push({ yaw: 0, pitch: -90 * D, f: 1 });
    for (let k = 0; k < 4; k++) views.push({ yaw: (45 + k * 90) * D, pitch: 5 * D, f: Math.tan(52 * D) });   // the walls, level
  }
  const vc = Object.assign(document.createElement("canvas"), { width: VW, height: VH }), vx = vc.getContext("2d");
  for (let v = 0; v < views.length; v++) {
    onStep && onStep(v + 1, views.length);
    const { yaw, pitch, f } = views[v], F = f, FY = f * VH / VW;
    const cp = Math.cos(pitch), sp = Math.sin(pitch), cy = Math.cos(yaw), sy = Math.sin(yaw);
    const out = vx.createImageData(VW, VH), cell = new Int32Array(VW * VH).fill(-1), below = new Uint8Array(VW * VH);
    for (let j = 0; j < VH; j++) for (let i = 0; i < VW; i++) {
      const x = (2 * (i + 0.5) / VW - 1) * F, y = -(2 * (j + 0.5) / VH - 1) * FY;
      const y1 = y * cp + sp, z1 = -y * sp + cp;                 // tilt, then turn
      const dx = x * cy + z1 * sy, dz = -x * sy + z1 * cy;
      const th = Math.atan2(dx, dz), ph = Math.atan2(y1, Math.hypot(dx, dz));
      const u = 0.5 + th / hfov, w = 0.5 - ph / vfov, o = (j * VW + i) * 4;
      out.data[o + 3] = 255;
      if (u < 0 || u >= 1 || w < 0 || w >= 1) continue;
      const q = (Math.floor(w * sh) * sw + Math.floor(u * sw)) * 4;
      out.data[o] = src[q]; out.data[o + 1] = src[q + 1]; out.data[o + 2] = src[q + 2];
      cell[j * VW + i] = Math.floor(w * ch) * cw + Math.floor(u * cw); below[j * VW + i] = ph < 0 ? 1 : 0;
    }
    vx.putImageData(out, 0, 0);
    const burl = URL.createObjectURL(await new Promise(ok => vc.toBlob(ok, "image/jpeg", 0.92)));
    try {
      const seg = await window.NovaSegment.detect(burl);
      for (let j = 0; j < VH; j++) for (let i = 0; i < VW; i++) {
        const c = cell[j * VW + i]; if (c < 0) continue;
        const l = seg.lab[Math.min(seg.gh - 1, Math.floor(j * seg.gh / VH)) * seg.gw + Math.min(seg.gw - 1, Math.floor(i * seg.gw / VW))];
        aV[c]++; if (l === 1 && below[j * VW + i]) fV[c]++; else if (l === 2) wV[c]++;
      }
    } finally { URL.revokeObjectURL(burl); }
  }
  const pf = new Float32Array(cw * ch), pw = new Float32Array(cw * ch);
  for (let i = 0; i < cw * ch; i++) if (aV[i]) { pf[i] = fV[i] / aV[i]; pw[i] = wV[i] / aV[i]; }
  // straight below the camera there is usually a tripod or a hand; when the floor runs all round it, fill it in
  if (full) {
    const r0 = Math.floor(ch * (0.5 + 62 / 180)), r1 = Math.floor(ch * (0.5 + 72 / 180));
    let fl = 0, t = 0;
    for (let y = r0; y < r1; y++) for (let x = 0; x < cw; x++) { t++; if (pf[y * cw + x] > 0.5) fl++; }
    if (t && fl > 0.6 * t) pf.fill(1, r1 * cw);
  }
  // 2. sharpen on a finer grid, following the edges in the photo (a guided filter)
  const gw = 1024, gh = Math.max(1, Math.round(gw / ratio)), N = gw * gh;
  const gc = Object.assign(document.createElement("canvas"), { width: gw, height: gh }), gx = gc.getContext("2d", { willReadFrequently: true });
  gx.drawImage(im, 0, 0, gw, gh); const px = gx.getImageData(0, 0, gw, gh).data, I = new Float32Array(N);
  for (let i = 0; i < N; i++) I[i] = (0.299 * px[i * 4] + 0.587 * px[i * 4 + 1] + 0.114 * px[i * 4 + 2]) / 255;
  const up = c => {                                              // coarse grid to fine grid, smoothly
    const o = new Float32Array(N);
    for (let y = 0; y < gh; y++) {
      const fy = Math.max(0, Math.min(ch - 1, (y + 0.5) * ch / gh - 0.5)), y0 = Math.floor(fy), y1 = Math.min(ch - 1, y0 + 1), ty = fy - y0;
      for (let x = 0; x < gw; x++) {
        const fx = (x + 0.5) * cw / gw - 0.5, x0 = Math.floor(fx), tx = fx - x0, xa = (x0 + cw) % cw, xb = (x0 + 1) % cw;
        o[y * gw + x] = (c[y0 * cw + xa] * (1 - tx) + c[y0 * cw + xb] * tx) * (1 - ty) + (c[y1 * cw + xa] * (1 - tx) + c[y1 * cw + xb] * tx) * ty;
      }
    }
    return o;
  };
  const box = (a, r) => {                                        // mean over a (2r+1)² square
    const S = new Float64Array((gw + 1) * (gh + 1)), o = new Float32Array(N), W1 = gw + 1;
    for (let y = 0; y < gh; y++) { let row = 0; for (let x = 0; x < gw; x++) { row += a[y * gw + x]; S[(y + 1) * W1 + x + 1] = S[y * W1 + x + 1] + row; } }
    for (let y = 0; y < gh; y++) {
      const ya = Math.max(0, y - r), yb = Math.min(gh, y + r + 1);
      for (let x = 0; x < gw; x++) {
        const xa = Math.max(0, x - r), xb = Math.min(gw, x + r + 1);
        o[y * gw + x] = (S[yb * W1 + xb] - S[ya * W1 + xb] - S[yb * W1 + xa] + S[ya * W1 + xa]) / ((yb - ya) * (xb - xa));
      }
    }
    return o;
  };
  const R = 6, EPS = 0.002, mI = box(I, R), II = new Float32Array(N);
  for (let i = 0; i < N; i++) II[i] = I[i] * I[i];
  const vI = box(II, R); for (let i = 0; i < N; i++) vI[i] -= mI[i] * mI[i];
  const guided = p => {
    const mp = box(p, R), Ip = new Float32Array(N); for (let i = 0; i < N; i++) Ip[i] = I[i] * p[i];
    const cIp = box(Ip, R), A = new Float32Array(N), B = new Float32Array(N);
    for (let i = 0; i < N; i++) { A[i] = (cIp[i] - mI[i] * mp[i]) / (vI[i] + EPS); B[i] = mp[i] - A[i] * mI[i]; }
    const mA = box(A, R), mB = box(B, R), q = new Float32Array(N);
    for (let i = 0; i < N; i++) { const v = mA[i] * I[i] + mB[i]; q[i] = Math.max(0, Math.min(1, (v - 0.35) / 0.3)); }
    return q;
  };
  const qf = guided(up(pf)), qw = guided(up(pw));
  for (let i = 0; i < N; i++) { if (qf[i] >= qw[i]) qw[i] = Math.min(qw[i], 1 - qf[i]); else qf[i] = Math.min(qf[i], 1 - qw[i]); }
  for (let y = 0; y < gh; y++) if ((0.5 - (y + 0.5) / gh) * vfov > -0.02) qf.fill(0, y * gw, y * gw + gw);   // no floor at or above eye level
  // 3. the room's shape: where the floor meets the walls, all the way round
  let rect = null;
  if (full) {
    const pts = [];
    for (let x = 0; x < gw; x += 2) {
      for (let y = Math.ceil(gh * 0.52); y < gh - 4; y++) {
        if (qf[y * gw + x] > 0.5 && qf[(y + 3) * gw + x] > 0.5) {
          const ph = (0.5 - (y + 0.5) / gh) * vfov, th = ((x + 0.5) / gw - 0.5) * hfov;
          const d = PANO_CAMERA / Math.tan(-ph);
          if (d > 0.4 && d < 15) pts.push([d * Math.sin(th), d * Math.cos(th), d]);
          break;
        }
      }
    }
    if (pts.length > 100) rect = fitRoom(pts);
  }
  // 4. how bright the floor and the walls are, to light the tiles like the photo
  let sf = 0, nf = 0, swl = 0, nw = 0;
  for (let i = 0; i < N; i++) { if (qf[i] > 0.5) { sf += I[i]; nf++; } if (qw[i] > 0.5) { swl += I[i]; nw++; } }
  const data = new Uint8Array(N * 2);                            // textures count rows from the bottom
  for (let y = 0; y < gh; y++) for (let x = 0; x < gw; x++) {
    const i = y * gw + x, o = ((gh - 1 - y) * gw + x) * 2;
    data[o] = Math.round(qf[i] * 255); data[o + 1] = Math.round(qw[i] * 255);
  }
  const tex = new THREE.DataTexture(data, gw, gh, THREE.RGFormat); tex.magFilter = tex.minFilter = THREE.LinearFilter; tex.needsUpdate = true;
  return { mask: tex, mean: nf ? sf / nf : 0.5, wallMean: nw ? swl / nw : 0.7, share: nf / N, wallShare: nw / N, rect };
}

/** A box-shaped room around the camera that fits where the floor meets the walls. Furniture hides some of
 *  that line and makes it look closer, so each wall sits at the far side of the points facing it. */
function fitRoom(pts) {
  // try every turn of the box a degree apart, then tenths of a degree around the best one
  const coarse = fitAt(pts, 0, 89, 1, 0.07);
  if (!coarse) return null;
  const c = coarse.a * 180 / Math.PI, fine = fitAt(pts, c - 1.5, c + 1.5, 0.1, 0.035) || coarse;
  if (!(fine.score > pts.length * 0.15 || coarse.score > pts.length * 0.3)) return null;
  return polish(pts, fine);
}
// line each wall up with the points lying on it (a straight-line fit per wall), then square the box up again
function polish(pts, r) {
  const ca = Math.cos(r.a), sa = Math.sin(r.a), groups = [[], [], [], []];
  for (const [x, z] of pts) {
    const xr = x * ca + z * sa, zr = -x * sa + z * ca;
    const dists = [Math.abs(xr - r.x0), Math.abs(xr - r.x1), Math.abs(zr - r.z0), Math.abs(zr - r.z1)];
    const k = dists.indexOf(Math.min(...dists)), off = k < 2 ? zr : xr, along = k < 2 ? xr : zr;
    if (dists[k] < 0.06 * Math.hypot(xr, zr)) groups[k].push([off, along]);
  }
  let sx = 0, sy = 0;
  for (const g of groups) {
    if (g.length < 15) continue;
    const mo = g.reduce((s, p) => s + p[0], 0) / g.length, ma = g.reduce((s, p) => s + p[1], 0) / g.length;
    let soo = 0, soa = 0; for (const [o, a] of g) { soo += (o - mo) ** 2; soa += (o - mo) * (a - ma); }
    const slope = soa / (soo || 1e-9), ang = Math.atan(slope) * 4;   // 4x so walls 90° apart agree
    sx += Math.cos(ang) * g.length; sy += Math.sin(ang) * g.length;
  }
  if (!sx && !sy) return r;
  // a wall running along z whose x grows with z means the box is turned the other way by atan(slope)
  const a = r.a - Math.atan2(sy, sx) / 4, c2 = Math.cos(a), s2 = Math.sin(a), side = [[], [], [], []];
  for (const [x, z] of pts) {
    const xr = x * c2 + z * s2, zr = -x * s2 + z * c2;
    const dists = [Math.abs(xr - r.x0), Math.abs(xr - r.x1), Math.abs(zr - r.z0), Math.abs(zr - r.z1)];
    const k = dists.indexOf(Math.min(...dists));
    if (dists[k] < 0.08 * Math.hypot(xr, zr)) side[k].push(k < 2 ? xr : zr);
  }
  const med = (v, d) => v.length < 10 ? d : v.slice().sort((p, q) => p - q)[v.length >> 1];
  return { ...r, a, x0: med(side[0], r.x0), x1: med(side[1], r.x1), z0: med(side[2], r.z0), z1: med(side[3], r.z1) };
}
function fitAt(pts, from, to, step, tol) {
  let best = null;
  for (let deg = from; deg < to + 1e-6; deg += step) {
    const a = deg * Math.PI / 180, ca = Math.cos(a), sa = Math.sin(a);
    const side = [[], [], [], []];                               // x-, x+, z-, z+
    const rp = pts.map(([x, z, d]) => {
      const xr = x * ca + z * sa, zr = -x * sa + z * ca, psi = Math.atan2(xr, zr);
      const k = Math.abs(psi) < Math.PI / 4 ? 3 : Math.abs(psi) > Math.PI * 3 / 4 ? 2 : psi > 0 ? 1 : 0;
      side[k].push(k < 2 ? xr : zr);
      return [xr, zr, d];
    });
    if (side.some(v => v.length < 12)) continue;
    const pct = (v, q) => { const s = v.slice().sort((p, q) => p - q); return s[Math.min(s.length - 1, Math.floor(q * s.length))]; };
    const r = { x0: pct(side[0], 0.15), x1: pct(side[1], 0.85), z0: pct(side[2], 0.15), z1: pct(side[3], 0.85) };
    if (r.x0 > -0.3 || r.z0 > -0.3 || r.x1 < 0.3 || r.z1 < 0.3) continue;
    let score = 0;
    for (const [xr, zr, d] of rp) {
      const ux = xr / d, uz = zr / d;
      const tx = ux > 0 ? r.x1 / ux : ux < 0 ? r.x0 / ux : 1e9, tz = uz > 0 ? r.z1 / uz : uz < 0 ? r.z0 / uz : 1e9;
      if (Math.abs(Math.log(d / Math.min(tx, tz))) < tol) score++;
    }
    if (!best || score > best.score) best = { ...r, a, score };
  }
  return best;
}
export { SCENES };          // for tests and tools (e.g. making a 360° picture of a room)
