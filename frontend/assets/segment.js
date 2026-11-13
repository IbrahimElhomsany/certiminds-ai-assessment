/* Automatic floor and wall detection for the customer's own room photo.
 *
 * Runs in the customer's browser: the photo never goes to a third party, and the server does no ML work.
 * Two parts, kept apart so the model can be swapped without touching the rest:
 *   detect(url)  -> a small label grid (0 other, 1 floor, 2 wall) from a segmentation model
 *   fit(seg,W,H) -> the 4 floor dots, 4 wall dots and a "keep" mask (furniture in front)
 *
 * MODEL: SegFormer-B0 trained on ADE20K (150 classes), ONNX, 8-bit, about 4 MB, loaded from Hugging Face
 * by Transformers.js and cached by the browser. Licence note: these weights are for non-commercial use.
 * Before selling, swap MODEL for a commercially licensed model (any ADE20K-class semantic model works).
 */
(function () {
  const MODEL = {
    lib: "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.0.2",
    id: "Xenova/segformer-b0-finetuned-ade-512-512",
    dtype: "q8",
  };
  const FLOOR = new Set(["floor", "road", "sidewalk", "path"]);   // terraces come out as sidewalk or path
  const WALL = new Set(["wall"]);
  const GRID = 480;                                               // fitting works on a grid this wide

  // ------------------------------------------------------------------ the model
  let pipeP = null;
  function loadModel(onProgress) {
    if (!pipeP) {
      pipeP = (async () => {
        const T = await import(MODEL.lib);
        T.env.allowLocalModels = false;
        const files = {};
        return T.pipeline("image-segmentation", MODEL.id, {
          dtype: MODEL.dtype,
          progress_callback: p => {
            if (p.status !== "progress" || !p.total) return;
            files[p.file] = [p.loaded, p.total];
            const v = Object.values(files), got = v.reduce((s, f) => s + f[0], 0), all = v.reduce((s, f) => s + f[1], 0);
            if (onProgress) onProgress(Math.min(1, got / all));
          },
        });
      })();
      pipeP.catch(() => { pipeP = null; });                       // allow a retry after a network error
    }
    return pipeP;
  }

  async function shrink(url, side) {
    const im = await new Promise((ok, bad) => { const i = new Image(); i.crossOrigin = "anonymous"; i.onload = () => ok(i); i.onerror = bad; i.src = url; });
    const k = Math.min(1, side / Math.max(im.naturalWidth, im.naturalHeight));
    if (k === 1) return url;
    const c = Object.assign(document.createElement("canvas"), { width: Math.round(im.naturalWidth * k), height: Math.round(im.naturalHeight * k) });
    c.getContext("2d").drawImage(im, 0, 0, c.width, c.height);
    const blob = await new Promise(ok => c.toBlob(ok, "image/jpeg", 0.92));
    return URL.createObjectURL(blob);
  }

  async function detect(url, onProgress) {
    const seg = await loadModel(onProgress);
    // the model's answer is scaled back to the size of the picture it was given (150 numbers per pixel),
    // so it gets a small copy: about 140 MB of working memory instead of several GB for a big photo
    const small = await shrink(url, 640);
    let out;
    try { out = await seg(small); } finally { if (small !== url) URL.revokeObjectURL(small); }
    if (!out || !out.length) throw new Error("no result");
    const mw = out[0].mask.width, mh = out[0].mask.height;
    const gw = GRID, gh = Math.max(1, Math.round(mh * GRID / mw)), lab = new Uint8Array(gw * gh);
    for (const o of out) {
      const name = String(o.label || "").trim().toLowerCase();
      const v = FLOOR.has(name) ? 1 : WALL.has(name) ? 2 : 0;
      if (!v) continue;
      const d = o.mask.data, ch = o.mask.channels || 1;
      for (let y = 0; y < gh; y++) {
        const sy = Math.min(mh - 1, Math.floor((y + 0.5) * mh / gh)) * mw;
        for (let x = 0; x < gw; x++) if (d[(sy + Math.min(mw - 1, Math.floor((x + 0.5) * mw / gw))) * ch] > 127) lab[y * gw + x] = v;
      }
    }
    return { gw, gh, lab };
  }

  // ------------------------------------------------------------------ geometry from the label grid
  const median = a => { const s = [...a].sort((p, q) => p - q); return s.length ? s[s.length >> 1] : 0; };

  // y = a*x + b, refitted without outliers
  function fitLine(pts, xy = true) {
    let use = pts.slice();
    let a = 0, b = 0;
    for (let it = 0; it < 4 && use.length >= 2; it++) {
      let sx = 0, sy = 0, sxx = 0, sxy = 0; const n = use.length;
      for (const [u, v] of use) { const p = xy ? u : v, q = xy ? v : u; sx += p; sy += q; sxx += p * p; sxy += p * q; }
      const den = n * sxx - sx * sx;
      a = Math.abs(den) < 1e-9 ? 0 : (n * sxy - sx * sy) / den; b = (sy - a * sx) / n;
      const res = use.map(([u, v]) => Math.abs((xy ? v : u) - (a * (xy ? u : v) + b)));
      const lim = Math.max(2, 2.5 * median(res));
      const next = use.filter((_, i) => res[i] <= lim);
      if (next.length === use.length || next.length < 2) break;
      use = next;
    }
    return { a, b, inliers: use };
  }

  function largestComponent(lab, gw, gh, val) {
    const comp = new Int32Array(gw * gh).fill(-1); let best = -1, bestN = 0, id = 0;
    const stack = new Int32Array(gw * gh);
    for (let i = 0; i < gw * gh; i++) {
      if (lab[i] !== val || comp[i] >= 0) continue;
      let top = 0, n = 0; stack[top++] = i; comp[i] = id;
      while (top) {
        const j = stack[--top]; n++;
        const x = j % gw, y = (j / gw) | 0;
        if (x > 0 && lab[j - 1] === val && comp[j - 1] < 0) { comp[j - 1] = id; stack[top++] = j - 1; }
        if (x < gw - 1 && lab[j + 1] === val && comp[j + 1] < 0) { comp[j + 1] = id; stack[top++] = j + 1; }
        if (y > 0 && lab[j - gw] === val && comp[j - gw] < 0) { comp[j - gw] = id; stack[top++] = j - gw; }
        if (y < gh - 1 && lab[j + gw] === val && comp[j + gw] < 0) { comp[j + gw] = id; stack[top++] = j + gw; }
      }
      if (n > bestN) { bestN = n; best = id; }
      id++;
    }
    const out = new Uint8Array(gw * gh);
    for (let i = 0; i < gw * gh; i++) out[i] = comp[i] === best ? 1 : 0;
    return { mask: out, size: bestN };
  }

  function inQuad(q, x, y) {                                      // point in polygon (even-odd)
    let c = false;
    for (let i = 0, j = q.length - 1; i < q.length; j = i++) {
      const [xi, yi] = q[i], [xj, yj] = q[j];
      if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) c = !c;
    }
    return c;
  }

  // no reliable dots: still mark what is neither floor nor wall, so the tiles go behind the furniture
  function maskOnly({ gw, gh, lab }) {
    const keep = new Uint8Array(gw * gh);
    for (let i = 0; i < gw * gh; i++) keep[i] = lab[i] === 0 ? 1 : 0;
    return { floor: null, wall: null, keep, gw, gh };
  }

  /* seg: {gw, gh, lab}; W, H: photo size in px. Returns null when no usable floor was found. */
  function fit(seg, W, H) {
    const { gw, gh, lab } = seg, sx = W / gw, sy = H / gh;
    const near = (x, y, v, dx, dy, n) => { for (let k = 1; k <= n; k++) { const xx = x + dx * k, yy = y + dy * k; if (xx < 0 || yy < 0 || xx >= gw || yy >= gh) return false; if (lab[yy * gw + xx] === v) return true; } return false; };
    const main = largestComponent(lab, gw, gh, 1);
    if (main.size < gw * gh * 0.03) return null;                  // under 3% of the picture: not a floor we can use
    const F = main.mask;

    // 1. the far edge: where the floor meets the back wall. It must run under every bit of that wall and over every
    //    bit of floor, which pins it where the two touch; a bed in the middle leaves a band, then we take the
    //    most level line in that band (people photograph a room facing a wall).
    const tops = new Int32Array(gw).fill(-1);
    for (let x = 0; x < gw; x++) { let y = 0; while (y < gh && !F[y * gw + x]) y++; if (y < gh && y > 0) tops[x] = y; }
    if ([...tops].filter(t => t >= 0).length < gw * 0.15) return maskOnly(seg);
    const back = largestComponent(lab, gw, gh, 2);
    let far = null, xL = 0, xR = 0;
    if (back.size > gw * gh * 0.04) {
      let wx0 = gw, wx1 = -1;
      for (let i = 0; i < gw * gh; i++) if (back.mask[i]) { const x = i % gw; if (x < wx0) wx0 = x; if (x > wx1) wx1 = x; }
      const wb = [], ft = [];
      for (let x = wx0; x <= wx1; x++) {
        if (tops[x] < 0) continue;
        let y = tops[x] - 1; while (y >= 0 && !back.mask[y * gw + x]) y--;
        if (y < 0) continue;
        wb.push([x, y + 1]); ft.push([x, tops[x]]);
      }
      // the back wall's foot is the highest part of the floor outline; side walls run down towards the camera
      const hi = Math.min(...ft.map(p => p[1])) + gh * 0.06;
      for (let i = ft.length - 1; i >= 0; i--) if (ft[i][1] > hi) { ft.splice(i, 1); wb.splice(i, 1); }
      if (wb.length >= gw * 0.06) {
        const pct = (arr, q) => { const s = [...arr].sort((p, r) => p - r); return s[Math.min(s.length - 1, Math.max(0, Math.round(q * (s.length - 1))))]; };
        let best = null;
        for (let k = -80; k <= 80; k++) {
          const sl = k * 0.005;
          const bMin = pct(wb.map(([x, y]) => y - sl * x), 0.9), bMax = pct(ft.map(([x, y]) => y - sl * x), 0.1);
          const gap = bMin - bMax;                               // > 0: the line can't pass between wall and floor
          const score = gap > 2 ? 1000 + gap : Math.abs(sl);
          if (!best || score < best.score) best = { score, a: sl, b: Math.max(bMin, bMax) };   // where the floor starts
        }
        if (best.score < 1000) {
          far = { a: best.a, b: best.b };
          const cols = wb.map(p => p[0]); xL = Math.min(...cols); xR = Math.max(...cols);
        }
      }
    }
    if (!far) {                                                  // no wall to go by: the top of the floor
      const pts = []; tops.forEach((y, x) => { if (y >= 0) pts.push([x, y]); });
      const l = fitLine(pts); far = { a: l.a, b: l.b };
      const xs = l.inliers.map(p => p[0]); xL = Math.min(...xs); xR = Math.max(...xs);
    }
    if (Math.abs(far.a) > 0.8 || xR - xL < gw * 0.15) return maskOnly(seg);
    const yAt = x => far.a * x + far.b;

    // 2. the back wall: from the far edge up to where the wall ends
    let wall = null, rise = null;
    { const r = [];
      for (let x = Math.ceil(xL); x <= xR; x += 2) {
        const yb = Math.round(yAt(x)); let top = -1;
        for (let y = 0; y < yb; y++) if (lab[y * gw + x] === 2) { top = y; break; }
        if (top >= 0) r.push(yb - top);
      }
      if (r.length > (xR - xL) / 2 * 0.4) rise = median(r);
    }
    if (rise && rise > gh * 0.12) {
      wall = [[xL, Math.max(0, yAt(xL) - rise)], [xR, Math.max(0, yAt(xR) - rise)], [xR, yAt(xR)], [xL, yAt(xL)]];
      let n = 0, w = 0;                                           // mostly wall inside? otherwise skip it
      for (let y = 0; y < gh; y += 3) for (let x = 0; x < gw; x += 3) if (inQuad(wall, x, y)) { n++; if (lab[y * gw + x] === 2) w++; }
      if (!n || w / n < 0.35) wall = null;
    }

    // 3. the side edges: where the floor meets a side wall (x = a*y + b)
    const side = dir => {
      const p = [], x0 = dir < 0 ? xL : xR;
      for (let y = Math.ceil(yAt(x0)) + 2; y < gh; y++) {
        let x = dir < 0 ? 0 : gw - 1; while (x >= 0 && x < gw && !F[y * gw + x]) x -= dir;
        if (x <= 1 || x >= gw - 2) continue;                      // floor runs out of the picture here
        if (near(x, y, 2, dir, 0, 4)) p.push([x, y]);
      }
      if (p.length < Math.max(8, gh * 0.08)) return null;
      p.push([x0, yAt(x0)], [x0, yAt(x0)]);                       // the corner counts double
      const l = fitLine(p, false);
      return Math.sign(l.a || 0) === dir && Math.abs(l.a) > 0.2 ? l : null;   // must open towards the camera
    };
    let left = side(-1), right = side(1);
    // a side we can't see runs to the vanishing point: in the middle of the photo, at eye height,
    // which in a room photo is about 3/8 of the wall height above the floor edge
    const horizon = yAt(gw / 2) - Math.max(rise ? 0.38 * rise : 0, gh * 0.27);
    const at = (l, y) => l.a * y + l.b;
    const vp = left && !right ? [at(left, horizon), horizon] : right && !left ? [at(right, horizon), horizon] : [gw / 2, horizon];
    const toVp = x0 => { const y0 = yAt(x0), a = (x0 - vp[0]) / (y0 - vp[1]); return { a, b: x0 - a * y0 }; };
    if (!left) left = toVp(xL);
    if (!right) right = toVp(xR);

    // 4. the near edge: parallel to the far edge, as close to the camera as the dots may go
    //    (the tiles still cover the floor in front of it: the visualiser extends the sides to the bottom)
    const meet = (side, b2) => { const y = (far.a * side.b + b2) / (1 - far.a * side.a); return [side.a * y + side.b, y]; };
    const ok = p => p[0] >= -0.06 * gw && p[0] <= 1.06 * gw && p[1] <= 1.1 * gh;
    let b2 = gh * 1.03 - far.a * gw / 2, NL = meet(left, b2), NR = meet(right, b2);
    const minB = far.b + gh * 0.1;
    while ((!ok(NL) || !ok(NR)) && b2 > minB) { b2 -= gh * 0.01; NL = meet(left, b2); NR = meet(right, b2); }
    const floor = [[xL, yAt(xL)], [xR, yAt(xR)], NR, NL];

    // 5. what stands in front: inside the floor shape but not floor, inside the wall shape but not wall
    //    (the floor shape runs on to the bottom of the photo, like the tiles do)
    const down = (a, b) => { const y = gh * 1.05, t = (y - a[1]) / ((b[1] - a[1]) || 1e-6); return t > 1 ? [a[0] + (b[0] - a[0]) * t, y] : b; };
    const floorArea = [floor[0], floor[1], down(floor[1], floor[2]), down(floor[0], floor[3])];
    const keep = new Uint8Array(gw * gh);
    for (let y = 0; y < gh; y++) for (let x = 0; x < gw; x++) {
      const v = lab[y * gw + x], cx = x + 0.5, cy = y + 0.5;
      if ((v !== 1 && inQuad(floorArea, cx, cy)) || (wall && v !== 2 && inQuad(wall, cx, cy))) keep[y * gw + x] = 1;
    }
    const px = q => q.map(([x, y]) => [Math.round(x * sx), Math.round(y * sy)]);
    return { floor: px(floor), wall: wall ? px(wall) : null, keep, gw, gh };
  }

  // draws the keep mask onto a canvas (soft edges), in the brush colour
  function drawKeep(res, canvas, color = "#e0245e") {
    const { gw, gh, keep } = res, g = document.createElement("canvas"); g.width = gw; g.height = gh;
    const gx = g.getContext("2d"), im = gx.createImageData(gw, gh);
    const [r, gr, b] = [1, 3, 5].map(i => parseInt(color.slice(i, i + 2), 16));
    for (let i = 0; i < gw * gh; i++) if (keep[i]) { im.data[i * 4] = r; im.data[i * 4 + 1] = gr; im.data[i * 4 + 2] = b; im.data[i * 4 + 3] = 255; }
    gx.putImageData(im, 0, 0);
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = "high";
    ctx.drawImage(g, 0, 0, canvas.width, canvas.height);
  }

  window.NovaSegment = { detect, fit, drawKeep, loadModel, MODEL };
})();
