// Image viewer: stored images (newest first), the selected one at full
// panel size, and its acquisition metadata. "Live" shows the live-scan
// preview instead, refreshed as frames arrive. Arrow keys step through the
// list while the panel has focus; a scale bar sits under calibrated images;
// "FFT" shows the power spectrum (with a reciprocal scale bar) alongside.

import { api, fmtTime } from '../api.js';
import { setState, useStore } from '../store.js';

const { html, useEffect, useRef, useState } = window.htmPreact;

// Sidecar timestamps are naive UTC.
function utc(ts) {
  if (!ts) return null;
  return /[zZ]|[+-]\d\d:?\d\d$/.test(ts) ? new Date(ts) : new Date(ts + 'Z');
}

function num(v, digits = 3) {
  if (v === undefined || v === null || v === '') return null;
  return typeof v === 'number' ? +v.toPrecision(digits) : v;
}

function metaRows(img) {
  const t = img.tags || {};
  const mode = t.imaging_mode || (t.dwell_us !== undefined ? 'STEM' : t.exposure_s !== undefined ? 'TEM' : null);
  const cal = img.calibration || {};
  const st = img.stage_position;
  const rows = [
    ['Name', img.name],
    ['Acquired', img.timestamp && utc(img.timestamp).toLocaleString([], { hour12: false })],
    ['Mode', mode],
    ['Magnification', t.magnification ? `${Math.round(t.magnification).toLocaleString()}${t.mag_unit === 'x' || !t.mag_unit ? '×' : ' ' + t.mag_unit}` : null],
    ['Mag index', t.mag_index],
    ['High tension', t.high_tension_kV ? `${num(t.high_tension_kV)} kV` : null],
    ['Spot size', t.spot_size],
  ];
  if (mode === 'STEM') {
    rows.push(['Dwell time', t.dwell_us !== undefined ? `${num(t.dwell_us)} µs` : null],
      ['Detector signal', t.signal_index], ['Scan rotation', t.rotation !== undefined ? `${t.rotation}°` : null],
      ['Scan mode', t.mode]);
  } else if (mode === 'TEM') {
    rows.push(['Exposure', t.exposure_s !== undefined ? `${num(t.exposure_s)} s` : null],
      ['Binning', t.binning], ['Camera', t.camera_id], ['Processing', t.processing]);
  }
  rows.push(
    ['Size', img.shape ? img.shape.join(' × ') + ` (${img.dtype})` : null],
    ['Pixel size', cal.units ? `${num(cal.x_scale, 4)} × ${num(cal.y_scale, 4)} ${cal.units}` : 'uncalibrated'],
  );
  if (st) {
    rows.push(['Stage X / Y', `${num(st.x_m * 1e6, 5)} / ${num(st.y_m * 1e6, 5)} µm`],
      ['Stage Z', `${num(st.z_m * 1e6, 4)} µm`],
      ['Tilt α / β', `${num(st.alpha_deg, 4)}° / ${num(st.beta_deg, 4)}°`],
      ['Stage source', t.stage_position_source]);
  }
  const shown = new Set(['imaging_mode', 'magnification', 'mag_index', 'mag_unit', 'high_tension_kV',
    'spot_size', 'dwell_us', 'signal_index', 'rotation', 'mode', 'exposure_s', 'binning',
    'camera_id', 'processing', 'stage_position_source']);
  for (const [k, v] of Object.entries(t)) {
    if (!shown.has(k)) rows.push([k, typeof v === 'object' ? JSON.stringify(v) : String(v)]);
  }
  rows.push(['Image id', img.image_id]);
  return rows.filter(([, v]) => v !== null && v !== undefined && v !== '');
}

// nm per unit, mirroring client/analysis/units.py (DM's own units are kept
// in the sidecar; never assume nm).
const NM_PER_UNIT = {
  pm: 1e-3, 'å': 0.1, a: 0.1, angstrom: 0.1, 'ångström': 0.1, nm: 1, 'µm': 1e3, 'μm': 1e3,
  um: 1e3, micron: 1e3, mm: 1e6, m: 1e9, "b'\\xb5m'": 1e3,
};

// Pixel size in nm, or null when the image isn't spatially calibrated.
function nmPerPx(img) {
  const cal = img && img.calibration;
  if (!cal) return null;
  const u = (cal.units || '').trim();
  const per = NM_PER_UNIT[u in NM_PER_UNIT ? u : u.toLowerCase()];
  return per && cal.x_scale > 0 ? cal.x_scale * per : null;
}

// Physical width of the image in nm (null: uncalibrated). The FFT spans
// 1 / nmPerPx in reciprocal space (−Nyquist..+Nyquist), whatever its size.
function widthNm(img) {
  const d = nmPerPx(img);
  if (!d || !img.shape || !img.shape.length) return null;
  const dims = img.shape.filter(n => n > 1);
  return (dims.length ? dims[dims.length - 1] : img.shape[img.shape.length - 1]) * d;
}

// Largest 1/2/5 × 10^k not above x.
function nice(x) {
  const p = Math.pow(10, Math.floor(Math.log10(x)));
  const m = x / p;
  return (m >= 5 ? 5 : m >= 2 ? 2 : 1) * p;
}

function fmtLen(nm, recip) {
  const r = v => +v.toPrecision(3);
  if (recip) return nm < 0.1 ? `${r(nm * 1e3)} µm⁻¹` : `${r(nm)} nm⁻¹`;
  if (nm >= 1e6) return `${r(nm / 1e6)} mm`;
  if (nm >= 1e3) return `${r(nm / 1e3)} µm`;
  if (nm < 1) return `${r(nm * 10)} Å`;
  return `${r(nm)} nm`;
}

// One image plus the scale bar underneath it. `extent` is the physical width
// of the whole image (nm, or nm⁻¹ when `recip`); null hides the bar.
function Figure({ src, extent, recip }) {
  const boxRef = useRef(null);
  const [geom, setGeom] = useState(null);   // painted image {w, left}
  const [nat, setNat] = useState(null);     // natural {w, h}

  const measure = () => {
    const box = boxRef.current;
    if (!box || !nat) return;
    const bw = box.clientWidth, bh = box.clientHeight;
    const w = Math.min(bw, bh * nat.w / nat.h);   // object-fit: contain
    setGeom({ w, left: (bw - w) / 2 });
  };
  useEffect(() => {
    measure();
    if (!boxRef.current || !window.ResizeObserver) return;
    const ro = new ResizeObserver(measure);
    ro.observe(boxRef.current);
    return () => ro.disconnect();
  }, [nat]);

  let bar = null;
  if (extent && geom && geom.w > 0) {
    const perPx = extent / geom.w;
    const len = nice(perPx * Math.max(40, geom.w / 5));
    bar = { px: len / perPx, label: fmtLen(len, recip) };
  }
  return html`
    <div class="figure">
      <div class="imgbox" ref=${boxRef}>
        <img src=${src} alt="" onLoad=${e => setNat({ w: e.target.naturalWidth, h: e.target.naturalHeight })} />
      </div>
      <div class="scalebar">
        ${bar ? html`<div class="sb" style=${{ marginLeft: `${geom.left}px` }}>
            <div class="sbline" style=${{ width: `${bar.px}px` }}></div>
            <span>${bar.label}</span></div>`
          : extent === null && html`<span class="sbnone">uncalibrated</span>`}
      </div>
    </div>`;
}

function loadFftPref() {
  try { return localStorage.getItem('amadeus.images.fft') === '1'; } catch { return false; }
}

export function ImagesPanel() {
  const version = useStore(s => s.imagesVersion);
  const selected = useStore(s => s.selectedImage);
  const live = useStore(s => s.live);
  const [images, setImages] = useState([]);
  const [showLive, setShowLive] = useState(false);
  const [err, setErr] = useState('');
  const [showFft, setShowFft] = useState(loadFftPref);
  const listRef = useRef(null);

  const toggleFft = () => setShowFft(v => {
    try { localStorage.setItem('amadeus.images.fft', v ? '0' : '1'); } catch { /* per-viewer only */ }
    return !v;
  });

  useEffect(() => {
    const el = listRef.current && listRef.current.querySelector('.imgitem.sel');
    if (el) el.scrollIntoView({ block: 'nearest' });
  }, [selected, images]);

  const onKeyDown = e => {
    if (e.altKey || e.ctrlKey || e.metaKey || !images.length) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable) return;
    const step = { ArrowUp: -1, ArrowLeft: -1, ArrowDown: 1, ArrowRight: 1 }[e.key];
    if (e.key === 'f' || e.key === 'F') { e.preventDefault(); toggleFft(); return; }
    if (!step) return;
    e.preventDefault();
    const i = images.findIndex(x => x.image_id === selected);
    const j = i < 0 ? 0 : Math.min(images.length - 1, Math.max(0, i + step));
    setShowLive(false);
    setState({ selectedImage: images[j].image_id });
  };

  useEffect(() => {
    api.images().then(d => {
      setImages(d.images);
      if (!selected && d.images.length) setState({ selectedImage: d.images[0].image_id });
    }).catch(e => setErr(e.message));
  }, [version]);

  const img = images.find(i => i.image_id === selected);
  const src = showLive
    ? `/api/live.png?t=${live ? live.mtime : 0}`
    : img && `/api/images/${encodeURIComponent(img.image_id)}.png?max_px=1024`;
  const ext = !showLive && img ? widthNm(img) : undefined;   // undefined: no bar, no note
  const fftSrc = !showLive && img && showFft
    && `/api/images/${encodeURIComponent(img.image_id)}/fft.png?max_px=1024`;

  return html`
    <div class="panel images" tabIndex="0" onKeyDown=${onKeyDown}
      title="↑/↓ or ←/→: previous/next image · F: toggle FFT">
      <div class="toolbar">
        <button class=${showLive ? '' : 'on'} onClick=${() => setShowLive(false)}>Stored</button>
        <button class=${showLive ? 'on' : ''} onClick=${() => setShowLive(true)}>
          Live${live ? ` #${live.frame_index ?? ''}` : ''}</button>
        <span class="spacer"></span>
        <button class=${showFft ? 'on' : ''} disabled=${showLive} onClick=${toggleFft}
          title="Show the power spectrum alongside (F)">FFT</button>
        <button onClick=${() => setState(s => ({ imagesVersion: s.imagesVersion + 1 }))}>↻</button>
      </div>
      <div class="imgsplit">
        <div class="imglist" ref=${listRef}>
          ${err && html`<div class="err">${err}</div>`}
          ${images.length === 0 && html`<div class="empty">No images yet.</div>`}
          ${images.map(i => html`
            <div key=${i.image_id} class=${`imgitem ${i.image_id === selected && !showLive ? 'sel' : ''}`}
              onClick=${() => { setShowLive(false); setState({ selectedImage: i.image_id }); }}>
              <div class="iname">${i.name || i.image_id.slice(0, 8)}</div>
              <small>${(i.tags || {}).imaging_mode || (i.tags || {}).type || ''}
                · ${i.timestamp ? fmtTime(utc(i.timestamp).getTime() / 1000) : ''}</small>
            </div>`)}
        </div>
        <div class="imgmain">
          <div class="imgview">
            ${src ? html`<${Figure} key="main" src=${src} extent=${ext} />`
              : html`<div class="empty">Nothing selected.</div>`}
            ${fftSrc && html`<${Figure} key="fft" src=${fftSrc}
              extent=${ext && 1 / nmPerPx(img)} recip=${true} />`}
          </div>
          ${!showLive && img && html`
            <table class="meta"><tbody>
              ${metaRows(img).map(([k, v]) => html`<tr><th>${k}</th><td>${v}</td></tr>`)}
            </tbody></table>`}
          ${showLive && live && html`<table class="meta"><tbody>
            <tr><th>Detector</th><td>${live.detector}</td></tr>
            <tr><th>Frame</th><td>${live.frame_index}</td></tr>
          </tbody></table>`}
        </div>
      </div>
    </div>`;
}
