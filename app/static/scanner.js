// Camera barcode scanning: native BarcodeDetector (Chrome/Android), falling back to ZXing (iOS Safari, Firefox).
window.Scanner = (function () {
  let stream = null, timer = null, zxReader = null, running = false;

  function setStatus(el, msg) { if (el) el.textContent = msg; }

  async function start(video, statusEl, onCode) {
    stop();
    running = true;
    if (!window.isSecureContext || !navigator.mediaDevices) {
      setStatus(statusEl, 'Camera needs HTTPS. Open this site over https:// (e.g. via your reverse proxy) or type the code below.');
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

    if ('BarcodeDetector' in window) {
      try {
        const formats = await BarcodeDetector.getSupportedFormats();
        const detector = new BarcodeDetector({ formats });
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment', width: { ideal: 1280 } }, audio: false });
        video.srcObject = stream;
        await video.play();
        setStatus(statusEl, 'Point the camera at a barcode');
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
        console.warn('BarcodeDetector failed, trying ZXing', e);
        stop(); running = true;
      }
    }

    if (window.ZXing && ZXing.BrowserMultiFormatReader) {
      try {
        zxReader = new ZXing.BrowserMultiFormatReader();
        setStatus(statusEl, 'Point the camera at a barcode');
        await zxReader.decodeFromConstraints({ video: { facingMode: 'environment' } }, video, (result) => {
          if (result) accept(result.getText());
        });
        return;
      } catch (e) {
        setStatus(statusEl, 'Could not start camera: ' + (e.message || e));
        return;
      }
    }
    setStatus(statusEl, 'This browser has no barcode scanner available. Type the code below.');
  }

  function stop() {
    running = false;
    if (timer) { clearTimeout(timer); timer = null; }
    if (zxReader) { try { zxReader.reset(); } catch (e) {} zxReader = null; }
    if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
  }

  window.addEventListener('pagehide', stop);
  return { start, stop };
})();
