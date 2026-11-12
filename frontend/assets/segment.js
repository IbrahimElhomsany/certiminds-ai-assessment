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

  async function detect(url, onProgress) {
    const seg = await loadModel(onProgress);
    const out = await seg(url);
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

  /* seg: {gw, gh, lab}; W, H: photo size in px. Returns null when no usable floor was found. */
  function fit(seg, W, H) {
    const { gw, gh, lab } = seg, sx = W / gw, sy = H / gh;
    const near = (x, y, v, dx, dy, n) => { for (let k = 1; k <= n; k++) { const xx = x + dx * k, yy = y + dy * k; if (xx < 0 || yy < 0 || xx >= gw || yy >= gh) return false; if (lab[yy * gw + xx] === v) return true; } return false; };
    const main = largestComponent(lab, gw, gh, 1);
    if (main.size < gw * gh * 0.03) return null;                  // under 3% of the picture: not a floor we can use
    const F = main.mask;

    // 1. the far edge: where the floor meets the wall behind it
    const tops = [], junction = [], wallBottoms = [];
    for (let x = 0; x < gw; x++) {
      let y = 0; while (y < gh && !F[y * gw + x]) y++;
      if (y >= gh || y === 0) continue;
      tops.push([x, y]);
      if (near(x, y, 2, 0, -1, 4)) junction.push([x, y]);
      let w = y - 1; while (w >= 0 && lab[w * gw + x] !== 2) w--;
      if (w >= 0) wallBottoms.push([x, w + 1]);                  // the lowest wall seen above this bit of floor
    }
    if (tops.length < gw * 0.15) return null;
    // floor next to wall is the real edge; a bed or sofa in the middle hides it, so a few stretches are enough
    const span = junction.length ? junction[junction.length - 1][0] - junction[0][0] : 0;
    let far = null, pts = null;
    if (junction.length >= Math.max(8, gw * 0.03) && span >= gw * 0.12) {
      pts = junction; far = fitLine(pts);
      if (far.inliers.length < pts.length * 0.6) {                // a corner view: two walls make a "V", keep the longer side
        const apex = pts.reduce((m, p) => (p[1] < m[1] ? p : m), pts[0])[0];
        const L = pts.filter(p => p[0] <= apex), R = pts.filter(p => p[0] >= apex);
        pts = L.length >= R.length ? L : R;
        far = fitLine(pts);
      }
    }
    if ((!far || far.inliers.length < 10) && wallBottoms.length >= gw * 0.1) {
      // furniture hides the edge: it runs just under the lowest bits of wall (the bottom of their hull)
      const cross = (o, a, b) => (a[0] - o[0]) * (o[1] - b[1]) - (o[1] - a[1]) * (b[0] - o[0]);
      const hull = [];
      for (const p of wallBottoms) { while (hull.length >= 2 && cross(hull[hull.length - 2], hull[hull.length - 1], p) <= 0) hull.pop(); hull.push(p); }
      let best = null;
      for (let i = 1; i < hull.length; i++) {
        const [p, q] = [hull[i - 1], hull[i]], dx = q[0] - p[0];
        if (dx > 0 && Math.abs((q[1] - p[1]) / dx) < 0.5 && (!best || dx > best[1][0] - best[0][0])) best = [p, q];
      }
      const topAt = x => { const t = tops.find(p => p[0] === x); return t ? t[1] : Infinity; };
      const touches = p => topAt(p[0]) - p[1] <= gh * 0.04;      // the floor really starts there (not a kitchen worktop)
      if (best && best[1][0] - best[0][0] >= gw * 0.25) {
        const a = (best[1][1] - best[0][1]) / (best[1][0] - best[0][0]);
        const on = wallBottoms.filter(p => Math.abs(p[1] - (best[0][1] + a * (p[0] - best[0][0]))) <= 3);
        if (on.filter(touches).length >= 3) { pts = on; far = { a, b: best[0][1] - a * best[0][0], inliers: [best[0], ...on, best[1]] }; }
      }
    }
    if (!far) { pts = tops; far = fitLine(pts); }
    if (Math.abs(far.a) > 0.8 || far.inliers.length < 10) return null;
    const xs = far.inliers.map(p => p[0]);
    const xL = Math.min(...xs), xR = Math.max(...xs);
    if (xR - xL < gw * 0.15) return null;
    const yAt = x => far.a * x + far.b;

    // 2. the side edges: where the floor meets a side wall (x = c*y + d)
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
      return Math.sign(l.a || 0) === dir || Math.abs(l.a) < 0.05 ? l : null;   // must open towards the camera
    };
    let left = side(-1), right = side(1);
    const yNear = gh * 1.03;
    const deflt = (x0, xTo) => { const c = (xTo - x0) / (yNear - yAt(x0)); return { a: c, b: x0 - c * yAt(x0) }; };
    if (!left && right) left = { a: -right.a, b: xL + right.a * yAt(xL) };
    if (!right && left) right = { a: -left.a, b: xR + left.a * yAt(xR) };
    if (!left) left = deflt(xL, -0.03 * gw);
    if (!right) right = deflt(xR, 1.03 * gw);

    // 3. the near edge: parallel to the far edge, as close to the camera as the dots may go
    const meet = (side, b2) => { const y = (far.a * side.b + b2) / (1 - far.a * side.a); return [side.a * y + side.b, y]; };
    const ok = p => p[0] >= -0.06 * gw && p[0] <= 1.06 * gw && p[1] <= 1.1 * gh;
    let b2 = yNear - far.a * gw / 2, NL = meet(left, b2), NR = meet(right, b2);
    const minB = far.b + gh * 0.1;
    while ((!ok(NL) || !ok(NR)) && b2 > minB) { b2 -= gh * 0.01; NL = meet(left, b2); NR = meet(right, b2); }
    const floor = [[xL, yAt(xL)], [xR, yAt(xR)], NR, NL];

    // 4. the back wall: from the far edge up to where the wall ends
    let wall = null;
    const rise = [];
    for (let x = Math.ceil(xL); x <= xR; x += 2) {
      const yb = Math.round(yAt(x)); let top = -1;
      for (let y = 0; y < yb; y++) if (lab[y * gw + x] === 2) { top = y; break; }
      if (top >= 0) rise.push(yb - top);
    }
    if (rise.length > (xR - xL) / 2 * 0.4) {
      const h = median(rise);
      if (h > gh * 0.12) {
        wall = [[xL, Math.max(0, yAt(xL) - h)], [xR, Math.max(0, yAt(xR) - h)], [xR, yAt(xR)], [xL, yAt(xL)]];
        let n = 0, w = 0;                                         // mostly wall inside? otherwise skip it
        for (let y = 0; y < gh; y += 3) for (let x = 0; x < gw; x += 3) if (inQuad(wall, x, y)) { n++; if (lab[y * gw + x] === 2) w++; }
        if (!n || w / n < 0.35) wall = null;
      }
    }

    // 5. what stands in front: inside the floor shape but not floor, inside the wall shape but not wall
    const keep = new Uint8Array(gw * gh);
    for (let y = 0; y < gh; y++) for (let x = 0; x < gw; x++) {
      const v = lab[y * gw + x], cx = x + 0.5, cy = y + 0.5;
      if ((v !== 1 && inQuad(floor, cx, cy)) || (wall && v !== 2 && inQuad(wall, cx, cy))) keep[y * gw + x] = 1;
    }
    const px = q => q.map(([x, y]) => [Math.round(x * sx), Math.round(y * sy)]);
    return { floor: px(floor), wall: wall ? px(wall) : null, keep, gw, gh,
             share: main.size / (gw * gh) };
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
