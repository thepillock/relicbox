// Print labels on a NIIMBOT printer (e.g. D110) over Web Bluetooth, using NiimBlueLib.
// Labels are drawn on a canvas at the printer's resolution (203 dpi = 8 dots/mm): QR on the left,
// item name and reference on the right, then sent as a single print job.
window.Niimbot = (function () {
  const DOTS_PER_MM = 203 / 25.4;
  let client = null, meta = null, taskName = null;

  const supported = () => !!(window.isSecureContext && navigator.bluetooth && window.niimbluelib &&
                             window.niimbluelib.NiimbotBluetoothClient);

  function fitFont(ctx, text, family, weight, maxW, maxH) {
    let size = Math.floor(maxH);
    for (; size > 6; size--) {
      ctx.font = `${weight} ${size}px ${family}`;
      if (ctx.measureText(text).width <= maxW) break;
    }
    return size;
  }

  function ellipsize(ctx, text, maxW) {
    if (ctx.measureText(text).width <= maxW) return text;
    let t = text;
    while (t.length > 1 && ctx.measureText(t + '…').width > maxW) t = t.slice(0, -1);
    return t.trimEnd() + '…';
  }

  // Draw one label. qrRows: array of "0101…" strings. Returns the canvas.
  function draw(canvas, { ref, name, qrRows, showName = true }, widthMm, heightMm) {
    const W = Math.round(widthMm * DOTS_PER_MM), H = Math.round(heightMm * DOTS_PER_MM);
    canvas.width = W; canvas.height = H;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, W, H);
    const m = Math.max(3, Math.round(H * 0.05));

    // QR: whole dots per module (at most 3, about 0.375 mm, plenty for a phone), kept clear of the
    // label's rounded corners by ~0.6 mm on every side
    const inset = Math.round(DOTS_PER_MM * 0.6);
    const n = qrRows.length;
    const scale = Math.max(1, Math.min(3, Math.floor((H - 2 * inset) / n)));
    const size = n * scale, qy = Math.round((H - size) / 2), qx = Math.max(qy, inset + 2);
    ctx.fillStyle = '#000';
    qrRows.forEach((row, y) => {
      for (let x = 0; x < n; x++) if (row[x] === '1') ctx.fillRect(qx + x * scale, qy + y * scale, scale, scale);
    });

    // Text: optional name on top, reference large below (split after its last "-", like 000- / 094)
    const tx = qx + size + Math.round(m * 1.5), tw = W - tx - m;
    if (tw > 10) {
      ctx.textBaseline = 'top';
      let top = m;
      if (showName && name) {
        const nameH = Math.round(H * 0.2);
        // shrink the name to fit (down to ~1.5 mm tall), then shorten it if it still doesn't
        const nameSize = Math.max(Math.round(DOTS_PER_MM * 1.5), fitFont(ctx, name, 'system-ui, sans-serif', 600, tw, nameH));
        ctx.font = `600 ${nameSize}px system-ui, sans-serif`;
        ctx.fillText(ellipsize(ctx, name, tw), tx, m + (nameH - nameSize));
        top = m + nameH + Math.round(m * 0.6);
      }
      const cut = ref.lastIndexOf('-');
      const lines = cut > 0 && cut < ref.length - 1 ? [ref.slice(0, cut + 1), ref.slice(cut + 1)] : [ref];
      const avail = H - top - m;
      const lineH = avail / lines.length;
      const longest = lines.reduce((a, b) => (a.length >= b.length ? a : b));
      const fs = fitFont(ctx, longest, 'ui-monospace, Menlo, Consolas, monospace', 'bold', tw, lineH * 1.05);
      ctx.font = `bold ${fs}px ui-monospace, Menlo, Consolas, monospace`;
      // keep the lines together and centre the block in the space left
      const step = Math.min(lineH, fs * 1.08), block = step * (lines.length - 1) + fs;
      const y0 = top + Math.max(0, (avail - block) / 2);
      lines.forEach((ln, i) => ctx.fillText(ln, tx, y0 + i * step));
    }

    // Thermal printers are 1-bit: snap every pixel to pure black or white
    const img = ctx.getImageData(0, 0, W, H), d = img.data;
    for (let i = 0; i < d.length; i += 4) {
      const v = (d[i] * 0.299 + d[i + 1] * 0.587 + d[i + 2] * 0.114) < 150 ? 0 : 255;
      d[i] = d[i + 1] = d[i + 2] = v; d[i + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
    return canvas;
  }

  async function connect() {
    if (!supported()) throw new Error('Bluetooth printing needs Chrome (Android or desktop) and an https:// address.');
    if (client && client.isConnected()) return describe();
    client = new niimbluelib.NiimbotBluetoothClient();
    await client.connect();               // shows the browser's device picker
    meta = client.getModelMetadata() || null;
    taskName = client.getPrintTaskType() || null;
    return describe();
  }

  function describe() {
    return {
      model: meta ? meta.model : 'NIIMBOT printer',
      densityMin: meta ? meta.densityMin : 1,
      densityMax: meta ? meta.densityMax : 3,
      densityDefault: meta ? meta.densityDefault : 2,
      printheadMm: meta ? meta.printheadPixels / DOTS_PER_MM : 12,
    };
  }

  async function print(canvases, density, onProgress) {
    if (!client || !client.isConnected()) throw new Error('Not connected');
    const lib = window.niimbluelib;
    const dir = (meta && meta.printDirection) || 'left';
    const labelType = (lib.LabelType && lib.LabelType.WithGaps) || 1;
    const task = client.abstraction.newPrintTask(taskName || 'D110', { totalPages: canvases.length, density, labelType });
    try {
      await task.printInit();
      for (let i = 0; i < canvases.length; i++) {
        if (onProgress) onProgress(i + 1, canvases.length);
        await task.printPage(lib.ImageEncoder.encodeCanvas(canvases[i], dir), 1);
        if (i < canvases.length - 1 && task.waitForPageFinished) await task.waitForPageFinished();
      }
      await task.waitForFinished();
    } finally {
      try { await task.printEnd(); } catch (e) { /* ignore */ }
    }
  }

  async function disconnect() {
    if (client) { try { await client.disconnect(); } catch (e) {} }
    client = null; meta = null; taskName = null;
  }

  return { supported, draw, connect, print, disconnect };
})();
