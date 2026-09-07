/* Hand-rolled SVG charts for Warm Design System.
   No external dependencies, offline-capable, WCAG AA contrast calibrated. */

const Charts = (() => {

  const BUCKET_COLORS = {
    growth: '#2E7D32',
    distraction: '#A71D31',
    neutral: '#75756F',
    idle: '#B0AFA6',
  };

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));

  function hm(seconds) {
    seconds = Math.round(seconds || 0);
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (h && m) return `${h}h ${m}m`;
    if (h) return `${h}h`;
    if (m) return `${m}m`;
    return `${seconds}s`;
  }

  function clock(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  }

  /* ---------------- donut ---------------- */

  function donut(parts, centerTop, centerSub, size = 168) {
    const total = parts.reduce((a, p) => a + p.value, 0);
    const r = size / 2 - 14;
    const c = size / 2;
    const circ = 2 * Math.PI * r;

    if (!total) {
      return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
        <circle cx="${c}" cy="${c}" r="${r}" fill="none" stroke="#E3E2D8" stroke-width="16"/>
        <text x="${c}" y="${c + 5}" text-anchor="middle" fill="#75756F" font-family="'Inter', sans-serif" font-size="13">no data</text>
      </svg>`;
    }

    let offset = 0;
    const arcs = parts.filter((p) => p.value > 0).map((p) => {
      const len = (p.value / total) * circ;
      const el = `<circle cx="${c}" cy="${c}" r="${r}" fill="none"
        stroke="${p.color}" stroke-width="16"
        stroke-dasharray="${len} ${circ - len}"
        stroke-dashoffset="${-offset}"
        transform="rotate(-90 ${c} ${c})"><title>${esc(p.label)}: ${hm(p.value)}</title></circle>`;
      offset += len;
      return el;
    }).join('');

    return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
      <circle cx="${c}" cy="${c}" r="${r}" fill="none" stroke="#ECEBE0" stroke-width="16"/>
      ${arcs}
      <text x="${c}" y="${c - 2}" text-anchor="middle" fill="#191918" font-family="'Inter', sans-serif" font-size="22" font-weight="500" letter-spacing="-0.03em">${esc(centerTop)}</text>
      <text x="${c}" y="${c + 18}" text-anchor="middle" fill="#75756F" font-family="'Inter', sans-serif" font-size="11" font-weight="500" letter-spacing="0.04em">${esc(centerSub)}</text>
    </svg>`;
  }

  function legend(parts) {
    return parts.filter((p) => p.value > 0).map((p) => `
      <div><span class="swatch" style="background:${p.color}"></span>
      <span style="font-weight:400;color:#191918">${esc(p.label)}</span>
      <span class="muted" style="margin-left:auto;font-variant-numeric:tabular-nums;font-size:13px">${hm(p.value)}</span></div>`).join('')
      || '<div class="muted">Nothing recorded yet.</div>';
  }

  /* ---------------- horizontal bar rows ---------------- */

  function bars(rows, opts = {}) {
    if (!rows.length) return `<div class="empty">${esc(opts.empty || 'Nothing recorded for this period.')}</div>`;
    const max = Math.max(...rows.map((r) => r.value), 1);
    return rows.map((r) => `
      <div class="bar-row">
        <div class="bar-label" title="${esc(r.title || r.label)}">
          <span class="swatch" style="background:${r.color || '#75756F'}"></span>
          <span>${esc(r.label)}</span>
        </div>
        <div class="bar-track"><div class="bar-fill"
             style="width:${(r.value / max) * 100}%;background:${r.color || '#75756F'}"></div></div>
        <div class="bar-value">${esc(r.display || hm(r.value))}</div>
      </div>`).join('');
  }

  /* ---------------- stacked daily columns ---------------- */

  function stackedDays(days, height = 190) {
    if (!days.length) return '<div class="empty">No days recorded yet.</div>';

    const W = Math.max(560, days.length * 34);
    const padL = 44, padB = 26, padT = 10;
    const plot = height - padB - padT;
    const max = Math.max(...days.map((d) => d.growth + d.neutral + d.distraction), 3600);
    const bw = Math.min(24, (W - padL - 10) / days.length - 6);
    const step = (W - padL - 10) / days.length;

    const cols = days.map((d, i) => {
      const x = padL + i * step + (step - bw) / 2;
      let y = padT + plot;
      const seg = (v, color, name) => {
        if (!v) return '';
        const h = (v / max) * plot;
        y -= h;
        return `<rect x="${x}" y="${y}" width="${bw}" height="${h}" fill="${color}" rx="2">
          <title>${d.date} — ${name}: ${hm(v)}</title></rect>`;
      };
      const label = d.date.slice(5);
      return seg(d.distraction, BUCKET_COLORS.distraction, 'distraction')
        + seg(d.neutral, BUCKET_COLORS.neutral, 'neutral')
        + seg(d.growth, BUCKET_COLORS.growth, 'growth')
        + (days.length <= 32
          ? `<text x="${x + bw / 2}" y="${height - 8}" text-anchor="middle" fill="#75756F" font-family="'Inter', sans-serif" font-size="10">${label}</text>`
          : '');
    }).join('');

    const ticks = [0, 0.5, 1].map((f) => {
      const y = padT + plot - f * plot;
      return `<line x1="${padL}" y1="${y}" x2="${W - 6}" y2="${y}" stroke="#E3E2D8" stroke-width="1"/>
              <text x="${padL - 8}" y="${y + 4}" text-anchor="end" fill="#75756F" font-family="'Inter', sans-serif" font-size="10">${hm(max * f)}</text>`;
    }).join('');

    return `<div style="overflow-x:auto">
      <svg width="${W}" height="${height}" viewBox="0 0 ${W} ${height}">${ticks}${cols}</svg>
    </div>
    <div class="ring-legend" style="flex-direction:row;gap:18px;margin-top:12px;font-size:12px">
      <div><span class="swatch" style="background:${BUCKET_COLORS.growth}"></span>Growth</div>
      <div><span class="swatch" style="background:${BUCKET_COLORS.neutral}"></span>Neutral</div>
      <div><span class="swatch" style="background:${BUCKET_COLORS.distraction}"></span>Distraction</div>
    </div>`;
  }

  /* ---------------- hour-of-day profile ---------------- */

  function hourly(hours, height = 160) {
    const max = Math.max(...hours.map((h) => h.growth + h.neutral + h.distraction), 60);
    const W = 720, padL = 40, padB = 22, padT = 8;
    const plot = height - padB - padT;
    const step = (W - padL - 10) / 24;
    const bw = step - 4;

    const cols = hours.map((h, i) => {
      const x = padL + i * step;
      let y = padT + plot;
      const seg = (v, color, name) => {
        if (!v) return '';
        const hh = (v / max) * plot;
        y -= hh;
        return `<rect x="${x}" y="${y}" width="${bw}" height="${hh}" fill="${color}" rx="2">
          <title>${String(h.hour).padStart(2, '0')}:00 — ${name}: ${hm(v)}</title></rect>`;
      };
      const tick = h.hour % 3 === 0
        ? `<text x="${x + bw / 2}" y="${height - 6}" text-anchor="middle" fill="#75756F" font-family="'Inter', sans-serif" font-size="10">${String(h.hour).padStart(2, '0')}</text>`
        : '';
      return seg(h.distraction, BUCKET_COLORS.distraction, 'distraction')
        + seg(h.neutral, BUCKET_COLORS.neutral, 'neutral')
        + seg(h.growth, BUCKET_COLORS.growth, 'growth')
        + tick;
    }).join('');

    const base = `<line x1="${padL}" y1="${padT + plot}" x2="${W - 6}" y2="${padT + plot}" stroke="#E3E2D8"/>
      <text x="${padL - 8}" y="${padT + 10}" text-anchor="end" fill="#75756F" font-family="'Inter', sans-serif" font-size="10">${hm(max)}</text>`;

    return `<div style="overflow-x:auto"><svg width="${W}" height="${height}" viewBox="0 0 ${W} ${height}">${base}${cols}</svg></div>`;
  }

  /* ---------------- 24h timeline strip ---------------- */

  function strip(segments, height = 54) {
    const W = 1000;
    const DAY = 86400;
    const blocks = segments.map((s) => {
      const x = (s.offset_s / DAY) * W;
      const w = Math.max(0.6, (s.seconds / DAY) * W);
      const title = `${s.app}${s.title ? ' — ' + s.title : ''}\n${new Date(s.start).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} · ${hm(s.seconds)} · ${s.label}`;
      return `<rect x="${x}" y="0" width="${w}" height="${height}" fill="${s.color}" opacity="${s.bucket === 'idle' ? .4 : .95}">
        <title>${esc(title)}</title></rect>`;
    }).join('');

    const grid = Array.from({ length: 25 }, (_, h) => {
      const x = (h / 24) * W;
      return `<line x1="${x}" y1="0" x2="${x}" y2="${height}" stroke="#E3E2D8" stroke-width="${h % 6 === 0 ? 1.5 : .8}" opacity=".8"/>`;
    }).join('');

    const labels = Array.from({ length: 9 }, (_, i) => {
      const h = i * 3;
      return `<span>${String(h).padStart(2, '0')}:00</span>`;
    }).join('');

    return `<div style="overflow-x:auto">
      <svg width="100%" height="${height}" viewBox="0 0 ${W} ${height}" preserveAspectRatio="none"
           style="border-radius:2px;background:#ECEBE0;border:1px solid #E3E2D8;min-width:560px;display:block">
        ${blocks}${grid}
      </svg>
      <div class="strip-hours" style="min-width:560px">${labels}</div>
    </div>`;
  }

  return { donut, legend, bars, stackedDays, hourly, strip, hm, clock, esc, BUCKET_COLORS };
})();
