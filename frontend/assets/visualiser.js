/* Ceramica Nova tile visualiser.
 *
 * How it works, in plain words:
 *  1. A room photo is drawn as it is.
 *  2. Each surface (floor, wall) has a "plane": 4 points in the photo that are the corners of a real
 *     rectangle (for example 3.6 m x 1.9 m of floor). From those 4 pairs we compute a homography:
 *     the maths that stretches a flat sheet so its corners land on the 4 points (like a rubber sheet).
 *  3. For every pixel of the surface the GPU works backwards: which spot (in cm) of the flat floor is this?
 *     From that it knows which tile, where the grout lines are, and which part of the tile photo to show,
 *     so tiles keep their real size (60 x 60 cm, 120 x 120 cm ...) and shrink towards the back of the room.
 *  4. A mask says where the surface is visible; furniture in front (sofa, bathtub, tap) is cut out of it.
 *  5. The photo's own light and shadows (a blurred copy of the photo) are multiplied back over the tiles.
 *
 * It runs in the browser with WebGL2, so switching tiles is instant and costs nothing.
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------- maths
  /** 3x3 homography H (row-major, 9 numbers) that maps src[i] -> dst[i] for 4 point pairs. */
  function homography(src, dst) {
    const A = [], b = [];
    for (let i = 0; i < 4; i++) {
      const [x, y] = src[i], [u, v] = dst[i];
      A.push([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.push(u);
      A.push([0, 0, 0, x, y, 1, -v * x, -v * y]); b.push(v);
    }
    const h = solve(A, b);
    return [h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7], 1];
  }
  function solve(A, b) {                    // Gaussian elimination with partial pivoting
    const n = b.length, M = A.map((r, i) => [...r, b[i]]);
    for (let c = 0; c < n; c++) {
      let p = c;
      for (let r = c + 1; r < n; r++) if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r;
      [M[c], M[p]] = [M[p], M[c]];
      for (let r = c + 1; r < n; r++) {
        const f = M[r][c] / M[c][c];
        for (let k = c; k <= n; k++) M[r][k] -= f * M[c][k];
      }
    }
    const x = new Array(n).fill(0);
    for (let r = n - 1; r >= 0; r--) {
      let s = M[r][n];
      for (let k = r + 1; k < n; k++) s -= M[r][k] * x[k];
      x[r] = s / M[r][r];
    }
    return x;
  }
  function invert3(m) {
    const [a, b, c, d, e, f, g, h, i] = m;
    const A = e * i - f * h, B = -(d * i - f * g), C = d * h - e * g;
    const det = a * A + b * B + c * C;
    return [A / det, -(b * i - c * h) / det, (b * f - c * e) / det,
            B / det, (a * i - c * g) / det, -(a * f - c * d) / det,
            C / det, -(a * h - b * g) / det, (a * e - b * d) / det];
  }

  // ---------------------------------------------------------------- shaders
  const VS = `#version 300 es
  in vec2 a; out vec2 vUv;
  void main() { vUv = a * 0.5 + 0.5; gl_Position = vec4(a, 0.0, 1.0); }`;

  const FS_PHOTO = `#version 300 es
  precision highp float; in vec2 vUv; out vec4 o; uniform sampler2D uPhoto;
  void main() { o = texture(uPhoto, vUv); }`;

  const FS_TILE = `#version 300 es
  precision highp float;
  in vec2 vUv; out vec4 o;
  uniform mat3 uHinv;          // photo pixel -> plane cm
  uniform vec2 uRes;           // canvas size in px
  uniform sampler2D uPhoto, uLight, uMask, uTex;
  uniform vec2 uTile;          // tile size in cm, as laid (after rotation)
  uniform int uLayout;         // 0 straight, 1 half offset, 2 third offset
  uniform float uGrout;        // grout width in cm
  uniform vec3 uGroutCol;
  uniform int uPatch;          // 1 = the photo already shows several tiles: repeat it as a patch
  uniform vec2 uPatchSize;     // cm covered by the photo when uPatch = 1
  uniform float uMean;         // average brightness of the original surface (blurred)
  uniform float uUseLight, uGloss;

  float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
  float luma(vec3 c) { return dot(c, vec3(0.299, 0.587, 0.114)); }

  void main() {
    vec2 px = vec2(gl_FragCoord.x, uRes.y - gl_FragCoord.y);
    vec3 q = uHinv * vec3(px, 1.0);
    if (q.z <= 0.0) discard;
    vec2 uv = q.xy / q.z;
    float m = texture(uMask, vUv).r;
    if (m < 0.004) discard;

    vec3 col;
    if (uPatch == 1) {
      vec2 t = uv / uPatchSize;
      col = textureGrad(uTex, fract(t), dFdx(t), dFdy(t)).rgb;
    } else {
      vec2 g = uv / uTile;
      float row = floor(g.y);
      if (uLayout == 1) g.x += mod(row, 2.0) * 0.5;
      else if (uLayout == 2) g.x += mod(row, 3.0) / 3.0;
      vec2 cell = floor(g), l = fract(g);

      vec2 dcm = min(l, 1.0 - l) * uTile;                 // distance to the tile edge, in cm
      float d = min(dcm.x, dcm.y);
      float aa = fwidth(d) * 0.75 + 1e-4;
      float inside = smoothstep(uGrout * 0.5 - aa, uGrout * 0.5 + aa, d);

      // The product photo is one tile face. Long tiles use a strip of it along the grain;
      // square tiles get a random quarter turn so the floor doesn't look copy-pasted.
      bool tall = uTile.y > uTile.x;
      vec2 ll = tall ? l.yx : l, gg = tall ? g.yx : g;
      vec2 Tl = tall ? uTile.yx : uTile;
      vec2 crop = Tl / Tl.x;
      float h1 = hash(cell), h2 = hash(cell + 17.31);
      if (crop.y > 0.999) {
        float r = floor(h1 * 4.0);
        if (r == 1.0) ll = vec2(1.0 - ll.y, ll.x); else if (r == 2.0) ll = 1.0 - ll; else if (r == 3.0) ll = vec2(ll.y, 1.0 - ll.x);
      }
      vec2 off = vec2(0.0, h2 * (1.0 - crop.y));
      vec2 tc = off + ll * crop;
      vec3 tcol = textureGrad(uTex, tc, dFdx(gg) * crop, dFdy(gg) * crop).rgb;
      tcol *= 0.965 + 0.07 * hash(cell + 3.7);              // tiny shade differences between tiles
      col = mix(uGroutCol, tcol, inside);
    }

    if (uUseLight > 0.5) {
      float lum = luma(texture(uLight, vUv).rgb);
      col *= clamp(lum / uMean, 0.28, 1.75);                // the room's light and shadows
      float hi = luma(texture(uPhoto, vUv).rgb) - lum;      // bright reflections on a glossy floor
      col += smoothstep(0.10, 0.40, hi) * uGloss * 0.45;
    }
    o = vec4(clamp(col, 0.0, 1.0), m);
  }`;

  // ---------------------------------------------------------------- helpers
  const loadImage = src => new Promise((ok, bad) => {
    const im = new Image(); im.crossOrigin = "anonymous";
    im.onload = () => ok(im); im.onerror = () => bad(new Error("image " + src)); im.src = src;
  });
  function hexRgb(h) { const n = parseInt(h.slice(1), 16); return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255]; }

  function drawShapes(ctx, shapes, sx) {
    for (const s of shapes || []) {
      ctx.beginPath();
      if (s.ring) {                                         // ring = ellipse with a hole (e.g. a ring chandelier)
        const [cx, cy, rx, ry, irx, iry] = s.ring.map(v => v * sx);
        ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2); ctx.ellipse(cx, cy, irx, iry, 0, 0, Math.PI * 2);
        ctx.fill("evenodd");
      } else {
        s.forEach(([x, y], i) => i ? ctx.lineTo(x * sx, y * sx) : ctx.moveTo(x * sx, y * sx));
        ctx.closePath(); ctx.fill();
      }
    }
  }

  // ---------------------------------------------------------------- the visualiser
  function create(canvas) {
    const gl = canvas.getContext("webgl2", { preserveDrawingBuffer: true, premultipliedAlpha: false, antialias: false });
    if (!gl) return null;
    const aniso = gl.getExtension("EXT_texture_filter_anisotropic");

    function program(fs) {
      const p = gl.createProgram();
      for (const [type, src] of [[gl.VERTEX_SHADER, VS], [gl.FRAGMENT_SHADER, fs]]) {
        const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
        if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
        gl.attachShader(p, s);
      }
      gl.bindAttribLocation(p, 0, "a"); gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
      const u = {}; const n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
      for (let i = 0; i < n; i++) { const name = gl.getActiveUniform(p, i).name; u[name] = gl.getUniformLocation(p, name); }
      return { p, u };
    }
    const P_PHOTO = program(FS_PHOTO), P_TILE = program(FS_TILE);
    const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

    function texture(source, { flip = true, mip = false, repeat = false } = {}) {
      const t = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, t);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, flip);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
      const wrap = repeat ? gl.REPEAT : gl.CLAMP_TO_EDGE;
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, wrap); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, wrap);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      if (mip) {
        gl.generateMipmap(gl.TEXTURE_2D);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
        if (aniso) gl.texParameterf(gl.TEXTURE_2D, aniso.TEXTURE_MAX_ANISOTROPY_EXT, Math.min(8, gl.getParameter(aniso.MAX_TEXTURE_MAX_ANISOTROPY_EXT)));
      } else gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      return t;
    }

    const tileTex = new Map();                              // url -> texture (loaded once)
    async function tileTexture(url) {
      if (!tileTex.has(url)) tileTex.set(url, loadImage(url).then(im => texture(im, { flip: false, mip: true, repeat: true })));
      return tileTex.get(url);
    }

    let scene = null;                                       // { photoTex, lightTex, planes: [...], w, h }
    const chosen = {};                                      // surface -> { tex, w, h, patch, gloss }
    let opts = { layout: 0, rotate: false, grout: 0.3, groutColor: "#d9d6cf" };

    /** Load a room: { photo, width?, surfaces: { floor: [plane...], wall: [...] } }
     *  plane = { quad: [[x,y] x4 far-left, far-right, near-right, near-left], size: [w_cm, d_cm], area: [[x,y]...], keep: [shape...] } */
    async function setRoom(room) {
      const im = await loadImage(room.photo);
      const maxW = room.closeup ? 1600 : Math.min(1800, im.naturalWidth);
      const sx = maxW / im.naturalWidth, W = Math.round(im.naturalWidth * sx), H = Math.round(im.naturalHeight * sx);
      canvas.width = W; canvas.height = H;

      const c2 = Object.assign(document.createElement("canvas"), { width: W, height: H }).getContext("2d", { willReadFrequently: true });
      c2.drawImage(im, 0, 0, W, H);
      const photoTex = texture(c2.canvas);
      const sharp = c2.getImageData(0, 0, W, H).data;
      // the light: a heavily blurred photo keeps light and shadow but loses the old floor's pattern
      c2.filter = `blur(${Math.round(W / 45)}px)`; c2.drawImage(im, 0, 0, W, H); c2.filter = "none";
      const lightTex = texture(c2.canvas);
      const small = c2.getImageData(0, 0, W, H).data;

      const keepMask = room.keepMask ? await loadImage(room.keepMask) : null;   // brush strokes from the customer
      const planes = [];
      for (const [surface, list] of Object.entries(room.surfaces || {})) {
        for (const pl of list) {
          const m = Object.assign(document.createElement("canvas"), { width: W, height: H }).getContext("2d", { willReadFrequently: true });
          m.fillStyle = "#fff"; m.filter = "blur(0.8px)";
          drawShapes(m, [pl.area], sx);
          m.globalCompositeOperation = "destination-out";
          drawShapes(m, pl.keep, sx);
          if (keepMask) m.drawImage(keepMask, 0, 0, W, H);
          m.filter = "none";
          if (pl.auto) m.drawImage(autoKeep(pl, sharp, W, H, sx), 0, 0);
          m.globalCompositeOperation = "source-over";
          // average brightness of the original surface, so the new tiles keep their own colour
          const md = m.getImageData(0, 0, W, H).data; let sum = 0, n = 0;
          for (let i = 0; i < md.length; i += 4 * 37) if (md[i + 3] > 200) { sum += (0.299 * small[i] + 0.587 * small[i + 1] + 0.114 * small[i + 2]) / 255; n++; }
          const [w, d] = pl.size;
          const H2 = homography([[0, 0], [w, 0], [w, d], [0, d]], pl.quad.map(([x, y]) => [x * sx, y * sx]));
          const hit = new Uint8Array(Math.ceil(W / 4) * Math.ceil(H / 4));   // coarse copy of the mask, to know what was clicked
          for (let y = 0, k = 0; y < H; y += 4) for (let x = 0; x < W; x += 4, k++) hit[k] = md[(y * W + x) * 4 + 3];
          planes.push({ surface, maskTex: texture(m.canvas), hinv: invert3(H2), mean: n ? Math.max(0.12, sum / n) : 0.6, hit });
        }
      }
      scene = { photoTex, lightTex, planes, W, H, closeup: !!room.closeup };
      render();
      return { width: W, height: H };
    }

    /* "auto" keep boxes: inside each box, pixels that clearly aren't the plain surface (dark leaves,
       bottles, a green plant) stay in front. Useful for plants, where a hand-drawn outline would be too rough. */
    function autoKeep(pl, px, W, H, sx) {
      const lumAt = i => (0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2]) / 255;
      const inBox = (x, y) => pl.auto.some(([x0, y0, x1, y1]) => x >= x0 * sx && x <= x1 * sx && y >= y0 * sx && y <= y1 * sx);
      // reference: the surface's own brightness, sampled in its area outside the boxes
      const ref = document.createElement("canvas").getContext("2d"); ref.canvas.width = W; ref.canvas.height = H;
      ref.fillStyle = "#fff"; drawShapes(ref, [pl.area], sx);
      const rd = ref.getImageData(0, 0, W, H).data; let s = 0, n = 0;
      for (let i = 0; i < rd.length; i += 4 * 29) { const x = (i / 4) % W, y = (i / 4 / W) | 0; if (rd[i + 3] > 200 && !inBox(x, y)) { s += lumAt(i); n++; } }
      const mean = n ? s / n : 0.7;
      const out = new ImageData(W, H);
      for (const [x0, y0, x1, y1] of pl.auto) {
        for (let y = Math.max(0, y0 * sx | 0); y < Math.min(H, y1 * sx); y++) for (let x = Math.max(0, x0 * sx | 0); x < Math.min(W, x1 * sx); x++) {
          const i = (y * W + x) * 4, r = px[i], g = px[i + 1], b = px[i + 2];
          const green = g - (r + b) / 2 > 6 && Math.max(r, g, b) - Math.min(r, g, b) > 28;
          if (lumAt(i) < mean * (pl.autoDark || 0.6) || green) out.data[i + 3] = 255;
        }
      }
      const c = document.createElement("canvas"); c.width = W; c.height = H; const cx = c.getContext("2d");
      cx.putImageData(out, 0, 0);
      const c3 = document.createElement("canvas"); c3.width = W; c3.height = H; const x3 = c3.getContext("2d");
      x3.filter = "blur(1.2px)"; x3.drawImage(c, 0, 0); x3.drawImage(c, 0, 0);   // soften and slightly grow the cut-out
      return c3;
    }

    async function setTile(surface, tile) {                 // tile: { image, w, h, patch:[w,h]|null, gloss } or null
      if (!tile) { delete chosen[surface]; render(); return; }
      const tex = await tileTexture(tile.image);
      chosen[surface] = { ...tile, tex };
      render();
    }
    function setOptions(o) { opts = { ...opts, ...o }; render(); }

    function render() {
      if (!scene) return;
      gl.viewport(0, 0, scene.W, scene.H);
      gl.disable(gl.BLEND);
      gl.useProgram(P_PHOTO.p);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, scene.photoTex); gl.uniform1i(P_PHOTO.u.uPhoto, 0);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

      gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      const u = P_TILE.u; gl.useProgram(P_TILE.p);
      for (const pl of scene.planes) {
        const t = chosen[pl.surface]; if (!t) continue;
        const so = { ...opts, ...(opts[pl.surface] || {}) };     // per-surface layout / rotation
        const tw = so.rotate ? t.h : t.w, th = so.rotate ? t.w : t.h;
        [[scene.photoTex, "uPhoto"], [scene.lightTex, "uLight"], [pl.maskTex, "uMask"], [t.tex, "uTex"]].forEach(([tx, name], i) => {
          gl.activeTexture(gl.TEXTURE0 + i); gl.bindTexture(gl.TEXTURE_2D, tx); gl.uniform1i(u[name], i);
        });
        gl.uniformMatrix3fv(u.uHinv, true, new Float32Array(pl.hinv));
        gl.uniform2f(u.uRes, scene.W, scene.H);
        gl.uniform2f(u.uTile, tw, th);
        gl.uniform1i(u.uLayout, t.patch ? 0 : so.layout);
        gl.uniform1f(u.uGrout, t.patch ? 0 : opts.grout);
        gl.uniform3fv(u.uGroutCol, hexRgb(opts.groutColor));
        gl.uniform1i(u.uPatch, t.patch ? 1 : 0);
        gl.uniform2f(u.uPatchSize, ...(t.patch || [100, 100]));
        gl.uniform1f(u.uMean, pl.mean);
        gl.uniform1f(u.uUseLight, scene.closeup ? 0 : 1);
        gl.uniform1f(u.uGloss, t.gloss || 0);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      }
    }

    /** Which surface is at (u, v), both 0..1 across the photo? Walls first (they are drawn in front of floor edges). */
    function surfaceAt(u, v) {
      if (!scene) return null;
      const cw = Math.ceil(scene.W / 4), x = Math.min(cw - 1, Math.max(0, (u * scene.W / 4) | 0)), y = Math.max(0, (v * scene.H / 4) | 0);
      const order = [...scene.planes].sort((a, b) => (a.surface === "wall" ? -1 : 1) - (b.surface === "wall" ? -1 : 1));
      for (const pl of order) if ((pl.hit[y * cw + x] || 0) > 110) return pl.surface;
      return null;
    }

    return { setRoom, setTile, setOptions, render, surfaceAt, get size() { return scene ? [scene.W, scene.H] : null; } };
  }

  /** Real proportions of a rectangle seen in a photo: its 4 corners (pixels, in order around it) and the photo size.
   *  Uses the camera's focal length, found from the two vanishing directions when they are usable,
   *  otherwise a typical phone camera. Returns { across, along } with across = 1 (the first side). */
  function rectShape(pts, W, H) {
    const Hm = homography([[0, 0], [1, 0], [1, 1], [0, 1]], pts);          // unit square -> photo
    const cx = W / 2, cy = H / 2, col = j => [Hm[j], Hm[3 + j], Hm[6 + j]];
    const sh = ([a, b, c]) => [a - cx * c, b - cy * c, c];
    const h1 = sh(col(0)), h2 = sh(col(1));
    let f2 = -(h1[0] * h2[0] + h1[1] * h2[1]) / (h1[2] * h2[2]);
    const big = Math.max(W, H), fDefault = 0.8 * big;
    let f = Number.isFinite(f2) && f2 > (0.45 * big) ** 2 && f2 < (2.6 * big) ** 2 ? Math.sqrt(f2) : fDefault;
    const len = h => Math.hypot(h[0] / f, h[1] / f, h[2]);
    return { across: 1, along: len(h2) / len(h1), focal: f };
  }

  window.NovaVisualiser = { create, homography, invert3, rectShape };
})();
