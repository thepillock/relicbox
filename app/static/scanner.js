// Camera scanning for QR codes and barcodes.
// Uses the browser's built-in BarcodeDetector where it supports QR codes (Chrome on Android),
// otherwise ZXing (iPhone Safari, Firefox, desktop). Also decodes a single photo, which works
// even where live camera access isn't allowed (plain http://).
window.Scanner = (function () {
  let stream = null, timer = null, zxReader = null, running = false;
  const WANT = ['qr_code', 'code_128', 'ean_13', 'ean_8', 'upc_a', 'upc_e', 'code_39', 'data_matrix'];

  function setStatus(el, msg) { if (el) el.textContent = msg; }

  async function nativeDetector() {
    if (!('BarcodeDetector' in window)) return null;
    try {
      const supported = await BarcodeDetector.getSupportedFormats();
      if (!supported.includes('qr_code')) return null;   // no QR support: let ZXing do it
      return new BarcodeDetector({ formats: WANT.filter(f => supported.includes(f)) });
    } catch (e) { return null; }
  }

  function zxingReader() {
    if (!(window.ZXing && ZXing.BrowserMultiFormatReader)) return null;
    const hints = new Map();
    const F = ZXing.BarcodeFormat;
    hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS,
      [F.QR_CODE, F.CODE_128, F.EAN_13, F.EAN_8, F.UPC_A, F.UPC_E, F.CODE_39, F.DATA_MATRIX]);
    hints.set(ZXing.DecodeHintType.TRY_HARDER, true);
    const r = new ZXing.BrowserMultiFormatReader(hints);
    r.timeBetweenScansMillis = 150;
    return r;
  }

  async function start(video, statusEl, onCode) {
    stop();
    running = true;
    if (!window.isSecureContext || !navigator.mediaDevices) {
      setStatus(statusEl, 'Live camera needs HTTPS. Use "Scan a photo" below, or type the code.');
      return;
    }
    let last = null, hits = 0;
    const accept = code => {
      // Require two identical reads in a row to avoid mis-scans.
      if (code === last) { hits++; } else { last = code; hits = 1; }
      if (hits >= 2 && running) {
        running = false;
        if (navigator.vibrate) navigator.vibrate(80);
        setStatus(statusEl, 'Found ' + code);
        onCode(code);
      }
    };
    const constraints = { video: { facingMode: 'environment', width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: false };

    const detector = await nativeDetector();
    if (detector) {
      try {
        stream = await navigator.mediaDevices.getUserMedia(constraints);
        video.srcObject = stream;
        await video.play();
        setStatus(statusEl, 'Point the camera at a QR code or barcode');
        const tick = async () => {
          if (!running) return;
          try {
            const codes = await detector.detect(video);
            if (codes.length) accept(codes[0].rawValue);
          } catch (e) { /* frame not ready */ }
          timer = setTimeout(tick, 150);
        };
        tick();
        return;
      } catch (e) {
        console.warn('Built-in detector failed, trying ZXing', e);
        stop(); running = true;
      }
    }

    const reader = zxingReader();
    if (reader) {
      zxReader = reader;
      try {
        setStatus(statusEl, 'Point the camera at a QR code or barcode');
        await reader.decodeFromConstraints(constraints, video, result => { if (result) accept(result.getText()); });
        return;
      } catch (e) {
        setStatus(statusEl, 'Could not start camera: ' + (e.message || e) + '. Try "Scan a photo" below.');
        return;
      }
    }
    setStatus(statusEl, 'This browser has no live scanner. Use "Scan a photo" below, or type the code.');
  }

  // Decode one still image (from <input type=file capture>). Resolves to the text, or null.
  async function fromFile(file) {
    const url = URL.createObjectURL(file);
    try {
      const detector = await nativeDetector();
      if (detector) {
        try {
          const bmp = await createImageBitmap(file);
          const codes = await detector.detect(bmp);
          if (codes.length) return codes[0].rawValue;
        } catch (e) { /* fall through to ZXing */ }
      }
      const reader = zxingReader();
      if (reader) {
        try { return (await reader.decodeFromImageUrl(url)).getText(); } catch (e) { return null; }
      }
      return null;
    } finally {
      URL.revokeObjectURL(url);
    }
  }

  function stop() {
    running = false;
    if (timer) { clearTimeout(timer); timer = null; }
    if (zxReader) { try { zxReader.reset(); } catch (e) {} zxReader = null; }
    if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
  }

  window.addEventListener('pagehide', stop);
  return { start, stop, fromFile };
})();
