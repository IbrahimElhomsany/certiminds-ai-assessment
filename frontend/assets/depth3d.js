/**
 * 3D effect for an ordinary room photo.
 *
 * A small AI model (Depth Anything V2 Small, Apache-2.0, runs in the customer's browser) guesses how far
 * away each part of the photo is. The tiled picture is then shifted a little more where things are close
 * and a little less where they are far, so the room seems to move in 3D as you move the mouse or tilt
 * the phone. It is a guide made from one photo: nothing behind the furniture can be seen.
 */
import * as THREE from "/assets/vendor/three.module.min.js";

export const DEPTH_MODEL = {
  lib: "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.0.2",
  id: "onnx-community/depth-anything-v2-small",
  dtype: "q8",
};

let pipeP = null;
function loadModel(onProgress) {
  if (!pipeP) {
    pipeP = (async () => {
      const T = await import(DEPTH_MODEL.lib);
      T.env.allowLocalModels = false;
      const files = {};
      return T.pipeline("depth-estimation", DEPTH_MODEL.id, {
        dtype: DEPTH_MODEL.dtype,
        progress_callback: p => {
          if (p.status !== "progress" || !p.total) return;
          files[p.file] = [p.loaded, p.total];
          const v = Object.values(files), got = v.reduce((s, f) => s + f[0], 0), all = v.reduce((s, f) => s + f[1], 0);
          if (onProgress) onProgress(Math.min(1, got / all));
        },
      });
    })();
    pipeP.catch(() => { pipeP = null; });
  }
  return pipeP;
}

function loadImage(url) {
  return new Promise((ok, bad) => { const i = new Image(); i.crossOrigin = "anonymous"; i.onload = () => ok(i); i.onerror = bad; i.src = url; });
}

/** How far away each part of the photo is: { w, h, data: Uint8Array } with 255 = close, 0 = far. */
export async function estimate(url, onProgress) {
  const pipe = await loadModel(onProgress);
  const im = await loadImage(url);
  const k = Math.min(1, 518 / Math.max(im.naturalWidth, im.naturalHeight));
  const c = Object.assign(document.createElement("canvas"), { width: Math.round(im.naturalWidth * k), height: Math.round(im.naturalHeight * k) });
  c.getContext("2d").drawImage(im, 0, 0, c.width, c.height);
  const small = URL.createObjectURL(await new Promise(ok => c.toBlob(ok, "image/jpeg", 0.92)));
  let out;
  try { out = await pipe(small); } finally { URL.revokeObjectURL(small); }
  const d = out.depth, ch = d.channels || 1;
  // bring it to at most 384 wide, then soften the edges so close things don't tear away from the background
  const w = Math.min(384, d.width), h = Math.max(1, Math.round(d.height * w / d.width)), g = new Float32Array(w * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++)
    g[y * w + x] = d.data[(Math.floor(y * d.height / h) * d.width + Math.floor(x * d.width / w)) * ch];
  let lo = Infinity, hi = -Infinity; for (const v of g) { if (v < lo) lo = v; if (v > hi) hi = v; }
  const blur = (a, dx, dy, r) => {
    const b = new Float32Array(w * h);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      let s = 0, n = 0;
      for (let t = -r; t <= r; t++) { const xx = x + t * dx, yy = y + t * dy; if (xx < 0 || yy < 0 || xx >= w || yy >= h) continue; s += a[yy * w + xx]; n++; }
      b[y * w + x] = s / n;
    }
    return b;
  };
  const sm = blur(blur(g, 1, 0, 3), 0, 1, 3), data = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) data[i] = hi > lo ? Math.round((sm[i] - lo) / (hi - lo) * 255) : 128;
  return { w, h, data };
}

/** A depth answer saved next to one of our own room photos (<photo>.depth.png), so customers don't wait. */
export async function saved(url) {
  if (!/^\/images\/[\w-]+\.jpg$/.test(url)) return null;
  try {
    const im = await loadImage(url.replace(/\.jpg$/, ".depth.png"));
    const w = im.naturalWidth, h = im.naturalHeight, c = Object.assign(document.createElement("canvas"), { width: w, height: h });
    const x = c.getContext("2d", { willReadFrequently: true }); x.drawImage(im, 0, 0);
    const px = x.getImageData(0, 0, w, h).data, data = new Uint8Array(w * h);
    for (let i = 0; i < w * h; i++) data[i] = px[i * 4];
    return { w, h, data };
  } catch (e) { return null; }
}
/** The depth answer as a grey PNG (used once, when a room is added). */
export function toPng(d) {
  const c = Object.assign(document.createElement("canvas"), { width: d.w, height: d.h }), x = c.getContext("2d"), im = x.createImageData(d.w, d.h);
  for (let i = 0; i < d.w * d.h; i++) { im.data[i * 4] = im.data[i * 4 + 1] = im.data[i * 4 + 2] = d.data[i]; im.data[i * 4 + 3] = 255; }
  x.putImageData(im, 0, 0); return c.toDataURL("image/png");
}

const VERT = "varying vec2 vUv; void main(){ vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }";
const FRAG = `
precision highp float;
uniform sampler2D uImg, uDepth; uniform vec2 uOff; uniform float uScale, uFocus;
varying vec2 vUv;
void main(){
  vec2 uv = (vUv - 0.5) / uScale + 0.5;
  // look the depth up again where the shifted pixel comes from, so close edges hold together
  float d = texture2D(uDepth, uv).r; vec2 o = uOff * (d - uFocus);
  d = texture2D(uDepth, uv - o).r; o = uOff * (d - uFocus);
  d = texture2D(uDepth, uv - o).r; o = uOff * (d - uFocus);
  gl_FragColor = vec4(texture2D(uImg, uv - o).rgb, 1.0);
}`;

/** The moving picture. create(canvas) -> { show(source, depth), refresh(source), start(), stop() } */
export function create(canvas) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: false, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  const scene = new THREE.Scene(), cam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
  const copy = document.createElement("canvas"), imgTex = new THREE.CanvasTexture(copy);
  imgTex.colorSpace = THREE.NoColorSpace; imgTex.minFilter = THREE.LinearFilter; imgTex.generateMipmaps = false;
  const AMP = 0.045;                                         // how far the view can lean (share of the picture)
  const mat = new THREE.ShaderMaterial({
    vertexShader: VERT, fragmentShader: FRAG, toneMapped: false,
    uniforms: { uImg: { value: imgTex }, uDepth: { value: null }, uOff: { value: new THREE.Vector2() }, uScale: { value: 1 + AMP * 1.4 }, uFocus: { value: 0.3 } },
  });
  scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2, 2), mat));

  let raf = 0, running = false, lastInput = 0, tx = 0, ty = 0, cx = 0, cy = 0, t0 = performance.now();
  const target = (x, y) => { tx = Math.max(-1, Math.min(1, x)); ty = Math.max(-1, Math.min(1, y)); lastInput = performance.now(); };
  // mouse: lean towards the pointer. touch: drag. phone: tilt.
  canvas.addEventListener("pointermove", e => {
    if (e.pointerType === "touch" && !e.buttons) return;
    const b = canvas.getBoundingClientRect();
    target(((e.clientX - b.left) / b.width) * 2 - 1, ((e.clientY - b.top) / b.height) * 2 - 1);
  });
  canvas.addEventListener("pointerleave", () => { lastInput = 0; });
  let tilt0 = null;
  window.addEventListener("deviceorientation", e => {
    if (!running || e.gamma == null) return;
    if (!tilt0) tilt0 = [e.gamma, e.beta];
    target((e.gamma - tilt0[0]) / 20, (e.beta - tilt0[1]) / 20);
  });

  function resize() {
    const w = canvas.clientWidth || 800, h = canvas.clientHeight || 450;
    renderer.setSize(w, h, false);
  }
  function frame(now) {
    if (!running) return;
    raf = requestAnimationFrame(frame);
    if (now - lastInput > 2500) {                             // nobody moving: sway gently so the effect shows
      const t = (now - t0) / 1000; tx = Math.sin(t * 0.7) * 0.8; ty = Math.sin(t * 0.45) * 0.35;
    }
    cx += (tx - cx) * 0.08; cy += (ty - cy) * 0.08;
    mat.uniforms.uOff.value.set(-cx * AMP, cy * AMP);
    renderer.render(scene, cam);
  }
  function refresh(source) {
    copy.width = source.width; copy.height = source.height;
    copy.getContext("2d").drawImage(source, 0, 0);
    imgTex.needsUpdate = true;
  }
  function show(source, depth) {
    const flipped = new Uint8Array(depth.w * depth.h);         // textures count rows from the bottom
    for (let y = 0; y < depth.h; y++) flipped.set(depth.data.subarray(y * depth.w, (y + 1) * depth.w), (depth.h - 1 - y) * depth.w);
    const dt = new THREE.DataTexture(flipped, depth.w, depth.h, THREE.RedFormat);
    dt.minFilter = dt.magFilter = THREE.LinearFilter; dt.needsUpdate = true;
    if (mat.uniforms.uDepth.value) mat.uniforms.uDepth.value.dispose();
    mat.uniforms.uDepth.value = dt;
    refresh(source); start();
  }
  function start() { if (running) return; running = true; tilt0 = null; resize(); t0 = performance.now(); raf = requestAnimationFrame(frame); }
  function stop() { running = false; cancelAnimationFrame(raf); }
  new ResizeObserver(resize).observe(canvas);
  return { show, refresh, start, stop, resize };
}
