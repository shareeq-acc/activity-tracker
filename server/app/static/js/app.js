/* Activity Tracker — front end.
   One page, no framework, no build step. Views fetch on activation and poll
   only while visible, so an idle tab costs nothing. */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const { hm, clock, esc, bars, donut, legend, stackedDays, hourly, strip } = Charts;

/* ---------------- api ---------------- */

async function api(path, opts = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

let toastTimer;
function toast(msg, isError = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.toggle('error', isError);
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3200);
}

const fmtTime = (iso) =>
  new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

/* ---------------- routing ---------------- */

const State = { view: 'today', categories: [], timers: [] };

function stopPolling() {
  State.timers.forEach(clearInterval);
  State.timers = [];
}

function poll(fn, ms) {
  State.timers.push(setInterval(fn, ms));
}

function show(view) {
  State.view = view;
  stopPolling();
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.view === view));
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${view}`));
  location.hash = view;
  ({ today: Today, timeline: Timeline, insights: Insights,
     focus: Focus, chat: Chat, settings: Settings })[view].enter();
}

$('#tabs').addEventListener('click', (e) => {
  const btn = e.target.closest('.tab');
  if (btn) show(btn.dataset.view);
});

function segmented(rootSel, onPick) {
  $(rootSel).addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;
    $(rootSel).querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
    onPick(btn.dataset.period);
  });
}

/* ---------------- shared ---------------- */

async function loadCategories() {
  if (State.categories.length) return State.categories;
  const data = await api('/rules');
  State.categories = data.categories.filter((c) => !['idle', 'uncategorized'].includes(c.key));
  return State.categories;
}

function bucketParts(r) {
  return [
    { label: 'Growth', value: r.growth_seconds, color: Charts.BUCKET_COLORS.growth },
    { label: 'Neutral', value: r.neutral_seconds, color: Charts.BUCKET_COLORS.neutral },
    { label: 'Distraction', value: r.distraction_seconds, color: Charts.BUCKET_COLORS.distraction },
    { label: 'Idle', value: r.idle_seconds, color: Charts.BUCKET_COLORS.idle },
  ];
}

/* ================= TODAY ================= */

const Today = {
  period: 'today',

  enter() {
    this.load();
    this.tickLive();
    poll(() => this.tickLive(), 10000);
    poll(() => this.load(), 60000);
  },

  async tickLive() {
    try {
      const d = await api('/live');
      const dot = $('#status-dot');
      dot.className = `dot ${d.connected ? 'on' : 'off'}`;
      $('#status-text').textContent = d.connected
        ? 'collector live'
        : (d.current ? `last seen ${hm(d.current.last_seen_s)} ago` : 'no data yet');

      const parts = bucketParts(d.today);
      $('#today-ring').innerHTML = donut(
        parts,
        d.today.ratio !== null ? `${d.today.ratio}:1` : hm(d.today.growth_seconds),
        d.today.ratio !== null ? 'growth : distraction' : 'growth today',
      );
      $('#today-ring-legend').innerHTML = legend(parts);

      if (!d.current) return;
      const c = d.current;
      $('#live-now').innerHTML = `
        <div class="app"><span class="swatch" style="background:${c.color};width:14px;height:14px"></span>${esc(c.app)}</div>
        ${c.title ? `<div class="title">${esc(c.title)}</div>` : ''}
        <div class="meta">
          <span class="pill" style="background:${c.color}22;color:${c.color}">${esc(c.label)}</span>
          <span class="muted">${hm(c.seconds)} in this window · since ${fmtTime(c.since)}</span>
        </div>`;
    } catch (e) {
      $('#status-dot').className = 'dot off';
      $('#status-text').textContent = 'server unreachable';
    }
  },

  async load() {
    try {
      const d = await api(`/summary?period=${this.period}`);
      $('#today-range').textContent = d.period_label;

      $('#today-categories').innerHTML = bars(
        d.categories.map((c) => ({ label: c.label, value: c.seconds, color: c.color })),
      );
      $('#today-apps').innerHTML = bars(
        d.top_apps.map((a) => ({ label: a.app, value: a.seconds, color: a.color })),
      );
      $('#today-hourly').innerHTML = hourly(d.hourly);
      $('#today-titles').innerHTML = d.top_titles.length
        ? d.top_titles.map((t) => `
            <div class="list-item">
              <div class="grow-1">
                <div class="t">${esc(t.title)}</div>
                <div class="s">${esc(t.app)}</div>
              </div>
              <div class="v">${hm(t.seconds)}</div>
            </div>`).join('')
        : '<div class="empty">No window titles recorded.</div>';
    } catch (e) {
      toast(e.message, true);
    }
  },
};

segmented('#today-period', (p) => { Today.period = p; Today.load(); });

/* ================= TIMELINE ================= */

const Timeline = {
  date: null,

  enter() {
    if (!this.date) this.date = new Date().toLocaleDateString('en-CA');
    $('#tl-date').value = this.date;
    this.load();
  },

  async load() {
    try {
      const d = await api(`/timeline?day=${this.date}`);
      $('#tl-strip').innerHTML = strip(d.segments);

      const totals = {};
      d.segments.forEach((s) => {
        totals[s.category] ??= { label: s.label, color: s.color, value: 0 };
        totals[s.category].value += s.seconds;
      });
      $('#tl-legend').innerHTML = legend(Object.values(totals).sort((a, b) => b.value - a.value));

      const sorted = [...d.segments].sort((a, b) => b.seconds - a.seconds).slice(0, 150);
      $('#tl-list').innerHTML = sorted.length
        ? sorted.map((s) => `
            <div class="list-item">
              <span class="swatch" style="background:${s.color}"></span>
              <div class="grow-1">
                <div class="t">${esc(s.title || s.app)}</div>
                <div class="s">${esc(s.app)} · ${fmtTime(s.start)}–${fmtTime(s.end)}</div>
              </div>
              <div class="v">${hm(s.seconds)}</div>
            </div>`).join('')
        : '<div class="empty">Nothing recorded on this day.</div>';
    } catch (e) {
      toast(e.message, true);
    }
  },

  shift(days) {
    const d = new Date(this.date + 'T12:00:00');
    d.setDate(d.getDate() + days);
    this.date = d.toLocaleDateString('en-CA');
    $('#tl-date').value = this.date;
    this.load();
  },
};

$('#tl-prev').onclick = () => Timeline.shift(-1);
$('#tl-next').onclick = () => Timeline.shift(1);
$('#tl-today').onclick = () => { Timeline.date = new Date().toLocaleDateString('en-CA'); Timeline.enter(); };
$('#tl-date').onchange = (e) => { Timeline.date = e.target.value; Timeline.load(); };

/* ================= INSIGHTS ================= */

const Insights = {
  period: 'week',

  async enter() {
    await this.load();
    await this.fillGoalPicker();
  },

  async load() {
    try {
      const d = await api(`/insights?period=${this.period}`);

      $('#ins-highlights').innerHTML = d.highlights.map((h) => `<li>${esc(h)}</li>`).join('');

      const wow = d.week_over_week;
      const delta = wow.current.growth_seconds - wow.previous.growth_seconds;
      const cards = [
        {
          n: d.ratio.ratio !== null ? `${d.ratio.ratio}:1` : '—',
          l: 'growth : distraction',
          d: `${hm(d.ratio.growth_seconds)} vs ${hm(d.ratio.distraction_seconds)}`,
        },
        {
          n: `${d.fragmentation.score}`,
          l: 'focus score / 100',
          d: `${d.fragmentation.switches_per_hour} app switches per hour`,
        },
        {
          n: d.streaks.length ? hm(d.streaks[0].seconds) : '—',
          l: 'longest deep-work run',
          d: `${d.streaks.length} stretch${d.streaks.length === 1 ? '' : 'es'} over 15m`,
        },
        {
          n: `${delta >= 0 ? '+' : '−'}${hm(Math.abs(delta))}`,
          l: 'growth vs last week',
          d: `to the same point (day ${wow.days_elapsed})`,
          cls: delta >= 0 ? 'up' : 'down',
        },
      ];
      $('#ins-scorecards').innerHTML = cards.map((c) => `
        <div class="card score">
          <div class="n ${c.cls || ''}">${esc(c.n)}</div>
          <div class="l">${esc(c.l)}</div>
          <div class="d muted">${esc(c.d)}</div>
        </div>`).join('');

      $('#ins-daily').innerHTML = stackedDays(d.daily);

      $('#ins-wow').innerHTML = wow.categories.length
        ? wow.categories.slice(0, 8).map((c) => {
            const up = c.delta >= 0;
            return `<div class="list-item">
              <span class="swatch" style="background:${c.color}"></span>
              <div class="grow-1"><div class="t">${esc(c.label)}</div>
                <div class="s">${hm(c.current)} now · ${hm(c.previous)} then</div></div>
              <div class="v ${up ? 'up' : 'down'}">${up ? '+' : '−'}${hm(Math.abs(c.delta))}</div>
            </div>`;
          }).join('')
        : '<div class="empty">Not enough history to compare yet.</div>';

      $('#ins-streaks').innerHTML = d.streaks.length
        ? d.streaks.slice(0, 20).map((s) => `
            <div class="list-item">
              <div class="grow-1">
                <div class="t">${hm(s.seconds)}</div>
                <div class="s">${new Date(s.start).toLocaleDateString([], { month: 'short', day: 'numeric' })}
                  · ${fmtTime(s.start)}–${fmtTime(s.end)}</div>
              </div>
            </div>`).join('')
        : '<div class="empty">No uninterrupted stretch reached 15 minutes.</div>';

      this.renderGoals(d.goals);
    } catch (e) {
      toast(e.message, true);
    }
  },

  renderGoals(goals) {
    $('#ins-goals').innerHTML = goals.length
      ? goals.map((g) => `
          <div class="bar-row">
            <div class="bar-label">
              <span class="swatch" style="background:${g.color}"></span>
              <span>${esc(g.label)}</span>
            </div>
            <div class="bar-track">
              <div class="bar-fill" style="width:${Math.min(100, g.pct)}%;
                   background:${g.on_pace ? g.color : '#fb7185'}"></div>
            </div>
            <div class="bar-value">${hm(g.actual_seconds)} / ${g.target_hours}h</div>
          </div>`).join('')
      : '<div class="empty">No goals set. Pick a category below to add one.</div>';
  },

  async fillGoalPicker() {
    const cats = await loadCategories();
    $('#goal-key').innerHTML = cats
      .map((c) => `<option value="${c.key}">${esc(c.label)}</option>`).join('');
  },
};

segmented('#ins-period', (p) => { Insights.period = p; Insights.load(); });

$('#goal-save').onclick = async () => {
  const hours = parseFloat($('#goal-hours').value);
  if (isNaN(hours) || hours < 0) return toast('Enter a number of hours', true);
  try {
    await api('/goals', {
      method: 'POST',
      body: { scope: 'category', target_key: $('#goal-key').value, weekly_target_hours: hours },
    });
    $('#goal-hours').value = '';
    toast('Goal saved');
    Insights.load();
  } catch (e) { toast(e.message, true); }
};

/* ================= FOCUS ================= */

// The setup form is static markup. Stash it at boot so a finished session can
// restore it in place rather than reloading the page.
const FOCUS_IDLE_HTML = $('#focus-active-wrap').innerHTML;

const Focus = {
  period: 'today',
  session: null,
  anchor: 0,       // elapsed seconds at last sync
  anchoredAt: 0,   // performance.now() at last sync

  async enter() {
    await this.fillCategories();
    await this.sync();
    await this.loadHistory();
    poll(() => this.render(), 250);
    poll(() => this.sync(), 15000);
  },

  async fillCategories() {
    if ($('#focus-category').options.length) return;
    const cats = await loadCategories();
    $('#focus-category').innerHTML = cats
      .map((c) => `<option value="${c.key}"${c.key === 'building' ? ' selected' : ''}>${esc(c.label)}</option>`)
      .join('');
  },

  async sync() {
    try {
      const d = await api('/focus/active');
      if (d.just_completed && this.session) {
        toast(`Timer finished: ${d.just_completed.label || 'session'}`);
        this.loadHistory();
      }
      this.session = d.active;
      this.anchor = d.active ? d.active.elapsed_s : 0;
      this.anchoredAt = performance.now();
      this.render();
    } catch (e) { /* transient */ }
  },

  elapsed() {
    if (!this.session) return 0;
    if (this.session.status !== 'running') return this.anchor;
    return this.anchor + (performance.now() - this.anchoredAt) / 1000;
  },

  render() {
    const wrap = $('#focus-active-wrap');
    if (!this.session) {
      if (wrap.dataset.mode !== 'idle') { wrap.dataset.mode = 'idle'; this.renderIdle(); }
      return;
    }
    const s = this.session;
    const elapsed = this.elapsed();
    const isTimer = s.kind === 'timer' && s.planned_s;
    const remaining = isTimer ? Math.max(0, s.planned_s - elapsed) : 0;
    const done = isTimer && remaining <= 0;

    if (wrap.dataset.mode !== `run-${s.id}`) {
      wrap.dataset.mode = `run-${s.id}`;
      wrap.innerHTML = `
        <div class="card-title" style="justify-content:center">
          ${s.kind === 'timer' ? 'Timer' : 'Stopwatch'} ·
          <span class="pill" style="background:${s.color}22;color:${s.color}">${esc(s.category_label)}</span>
        </div>
        <div class="clock" id="focus-clock"></div>
        <div class="focus-meta" id="focus-meta"></div>
        ${isTimer ? '<div class="focus-progress"><div id="focus-bar"></div></div>' : ''}
        <div class="focus-actions">
          <button class="btn" id="focus-toggle"></button>
          <button class="btn btn-primary" id="focus-stop">Finish</button>
          <button class="btn btn-danger" id="focus-cancel">Discard</button>
        </div>`;
      $('#focus-toggle').onclick = () => this.toggle();
      $('#focus-stop').onclick = () => this.finish('stop');
      $('#focus-cancel').onclick = () => this.finish('cancel');
    }

    const clockEl = $('#focus-clock');
    clockEl.textContent = isTimer ? clock(remaining) : clock(elapsed);
    clockEl.className = `clock ${done ? 'done' : (s.status === 'paused' ? 'paused' : '')}`;

    $('#focus-meta').textContent =
      `${s.label || 'Untitled'} · started ${fmtTime(s.started_at)}`
      + (isTimer ? ` · ${hm(elapsed)} of ${hm(s.planned_s)} done` : '');

    if (isTimer) $('#focus-bar').style.width = `${Math.min(100, (elapsed / s.planned_s) * 100)}%`;
    $('#focus-toggle').textContent = s.status === 'paused' ? 'Resume' : 'Pause';
  },

  renderIdle() {
    const wrap = $('#focus-active-wrap');
    wrap.innerHTML = FOCUS_IDLE_HTML;
    bindFocusControls();
    this.fillCategories();
  },

  async start(kind, plannedS) {
    try {
      await api('/focus', {
        method: 'POST',
        body: {
          kind,
          label: $('#focus-label').value.trim(),
          category: $('#focus-category').value,
          planned_s: plannedS || null,
        },
      });
      await this.sync();
      this.loadHistory();
    } catch (e) { toast(e.message, true); }
  },

  async toggle() {
    const action = this.session.status === 'paused' ? 'resume' : 'pause';
    try {
      await api(`/focus/${this.session.id}/${action}`, { method: 'POST' });
      await this.sync();
    } catch (e) { toast(e.message, true); }
  },

  async finish(action) {
    if (action === 'cancel' && !confirm('Discard this session without recording it?')) return;
    try {
      const r = await api(`/focus/${this.session.id}/${action}`, { method: 'POST' });
      toast(action === 'stop' ? `Recorded ${hm(r.elapsed_s)}` : 'Session discarded');
      this.session = null;
      $('#focus-active-wrap').dataset.mode = 'idle';
      this.renderIdle();
      await this.loadHistory();
    } catch (e) { toast(e.message, true); }
  },

  async loadHistory() {
    try {
      const d = await api(`/focus?period=${this.period}`);
      $('#focus-total').textContent = `${hm(d.total_seconds)} tracked · ${d.period_label}`;
      $('#focus-by-label').innerHTML = bars(
        d.by_label.slice(0, 10).map((r) => ({ label: r.label, value: r.seconds, color: '#6ea8fe' })),
        { empty: 'No sessions yet.' },
      );
      $('#focus-by-category').innerHTML = bars(
        d.by_category.map((r) => ({ label: r.label, value: r.seconds, color: r.color })),
        { empty: 'No sessions yet.' },
      );
      $('#focus-history').innerHTML = d.sessions.length
        ? d.sessions.map((s) => `
            <div class="list-item">
              <span class="swatch" style="background:${s.color}"></span>
              <div class="grow-1">
                <div class="t">${esc(s.label || '(unlabelled)')}</div>
                <div class="s">${s.kind} · ${new Date(s.started_at).toLocaleDateString([], { month: 'short', day: 'numeric' })}
                  ${fmtTime(s.started_at)} · ${esc(s.status)}</div>
              </div>
              <div class="v">${hm(s.elapsed_s)}</div>
            </div>`).join('')
        : '<div class="empty">No sessions in this period.</div>';
    } catch (e) { toast(e.message, true); }
  },
};

segmented('#focus-period', (p) => { Focus.period = p; Focus.loadHistory(); });

// Re-bound whenever the idle form is restored, since the nodes are replaced.
function bindFocusControls() {
  $('#focus-start-sw').onclick = () => Focus.start('stopwatch');
  $('#timer-presets').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (b) Focus.start('timer', parseInt(b.dataset.min, 10) * 60);
  });
  $('#focus-start-custom').onclick = () => {
    const m = parseInt($('#focus-custom').value, 10);
    if (!m || m < 1) return toast('Enter minutes for the timer', true);
    Focus.start('timer', m * 60);
  };
}
bindFocusControls();

/* ================= CHAT ================= */

const Chat = {
  busy: false,
  loaded: false,

  async enter() {
    const s = await api('/settings').catch(() => null);
    if (s) $('#chat-provider').value = s.llm_provider;
    if (!this.loaded) {
      this.loaded = true;
      const d = await api('/chat').catch(() => ({ messages: [] }));
      d.messages.forEach((m) => this.append(m.role === 'user' ? 'user' : 'bot', m.content));
      if (!d.messages.length) {
        this.append('bot', 'Ask me anything about how you spend your time. I read the same numbers the dashboard shows.');
      }
    }
    $('#chat-text').focus();
  },

  append(kind, text, tools) {
    const el = document.createElement('div');
    el.className = `msg ${kind}`;
    el.textContent = text;
    if (tools?.length) {
      const t = document.createElement('div');
      t.className = 'tools';
      t.textContent = `queried: ${[...new Set(tools)].join(', ')}`;
      el.appendChild(t);
    }
    $('#chat-log').appendChild(el);
    $('#chat-log').scrollTop = $('#chat-log').scrollHeight;
    return el;
  },

  async send(text) {
    if (this.busy || !text.trim()) return;
    this.busy = true;
    $('#chat-send').disabled = true;
    $('#chat-text').value = '';
    this.append('user', text);

    const thinking = this.append('bot', '');
    thinking.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';

    try {
      const d = await api('/chat', {
        method: 'POST',
        body: { message: text, provider: $('#chat-provider').value },
      });
      thinking.remove();
      this.append('bot', d.reply, d.tools_used);
    } catch (e) {
      thinking.remove();
      this.append('err', e.message);
    } finally {
      this.busy = false;
      $('#chat-send').disabled = false;
      $('#chat-text').focus();
    }
  },
};

$('#chat-form').onsubmit = (e) => { e.preventDefault(); Chat.send($('#chat-text').value); };
$('#chat-suggestions').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (b) Chat.send(b.textContent);
});
$('#chat-clear').onclick = async () => {
  await api('/chat', { method: 'DELETE' });
  $('#chat-log').innerHTML = '';
  Chat.loaded = false;
  Chat.enter();
};
$('#chat-provider').onchange = async (e) => {
  try {
    await api('/settings/llm_provider', { method: 'PUT', body: { provider: e.target.value } });
    toast(`Assistant switched to ${e.target.value}`);
  } catch (err) { toast(err.message, true); }
};

/* ================= SETTINGS ================= */

// "Unreachable" and "reachable but nothing pulled" need different fixes, so
// they get different messages.
function ollamaStatus(models, ol) {
  if (!models.ollama_reachable) {
    return `Ollama is <strong>not reachable</strong> at ${esc(ol.detail)}. `
         + 'Start it, or run the bundled container with '
         + '<code>docker compose --profile local-llm up -d</code>.';
  }
  if (!models.ollama_models.length) {
    return `Ollama is reachable at ${esc(ol.detail)} but <strong>no models are pulled</strong>. `
         + `Run <code>ollama pull ${esc(ol.model)}</code>.`;
  }
  if (!models.ollama_model_ready) {
    return `Ollama is reachable, but <strong>${esc(ol.model)} is not pulled</strong>. `
         + `Available: ${models.ollama_models.map(esc).join(', ')}. `
         + `Run <code>ollama pull ${esc(ol.model)}</code> or change OLLAMA_MODEL.`;
  }
  return `Ollama ready at ${esc(ol.detail)} — ${esc(ol.model)} loaded. `
       + `Also pulled: ${models.ollama_models.map(esc).join(', ')}.`;
}

const Settings = {
  async enter() {
    try {
      const [s, stats, rules, models] = await Promise.all([
        api('/settings'), api('/stats'), api('/rules'), api('/chat/models').catch(() => null),
      ]);

      $('#set-provider').innerHTML = Object.entries(s.providers).map(([k, v]) =>
        `<option value="${k}"${k === s.llm_provider ? ' selected' : ''}>
          ${k === 'gemini' ? 'Gemini' : 'Local (Ollama)'} — ${esc(v.model)}
        </option>`).join('');

      const ol = s.providers.ollama;
      const gm = s.providers.gemini;
      $('#set-provider-detail').innerHTML = [
        gm.available ? `Gemini ready (${esc(gm.model)}).` : `Gemini unavailable — ${esc(gm.detail)}.`,
        models ? ollamaStatus(models, ol) : '',
      ].filter(Boolean).join('<br>');

      $('#set-stats').innerHTML = `
        <div class="list-item"><div class="grow-1">Segments recorded</div><div class="v">${stats.segments.toLocaleString()}</div></div>
        <div class="list-item"><div class="grow-1">First seen</div><div class="v">${stats.first_seen ? new Date(stats.first_seen).toLocaleString() : '—'}</div></div>
        <div class="list-item"><div class="grow-1">Last seen</div><div class="v">${stats.last_seen ? new Date(stats.last_seen).toLocaleString() : '—'}</div></div>
        <div class="list-item"><div class="grow-1">Database size</div><div class="v">${(stats.db_bytes / 1048576).toFixed(1)} MB</div></div>
        <div class="list-item"><div class="grow-1">Timezone</div><div class="v">${esc(s.timezone)}</div></div>`;

      $('#rules-path').textContent = `Editing ${rules.path} on the host reloads without a rebuild.`;
      $('#rules-error').innerHTML = rules.error
        ? `<div class="banner error">rules.yml failed to load: ${esc(rules.error)}</div>` : '';

      $('#rules-uncat').innerHTML = rules.uncategorized.length
        ? `<p class="muted small">Unmatched apps, by time cost — add rules for the ones that matter:</p>`
          + bars(rules.uncategorized.map((u) => ({ label: u.app, value: u.seconds, color: '#475569', title: u.exe })))
        : '<div class="empty">Every recorded app matches a rule.</div>';

      $('#rules-list').innerHTML = rules.rules.map((r) => `
        <div class="list-item">
          <div class="grow-1">
            <div class="t"><strong>${esc(r.id)}</strong> → ${esc(r.category)}</div>
            <div class="s">${[...r.exe, ...r.title_any].slice(0, 12).map((x) => `<span class="rule-chip">${esc(x)}</span>`).join('')}</div>
          </div>
        </div>`).join('');

      await loadCategories();
      Classify.load();
    } catch (e) { toast(e.message, true); }
  },
};

/* ---------------- smart categorisation ---------------- */

const Classify = {
  async load() {
    try {
      const [pend, list] = await Promise.all([
        api('/classify/pending?limit=40'),
        api('/classify?limit=200'),
      ]);

      $('#classify-pending').innerHTML = pend.count
        ? `<p class="muted small">${pend.count} window${pend.count === 1 ? '' : 's'} the rules could not resolve, biggest first:</p>`
          + pend.pending.slice(0, 12).map((p) => `
              <div class="list-item">
                <div class="grow-1">
                  <div class="t">${esc(p.title || '(no title)')}</div>
                  <div class="s">${esc(p.app)}</div>
                </div>
                <div class="v">${esc(p.time)}</div>
              </div>`).join('')
        : '<div class="empty">Every recorded window has a category.</div>';

      $('#classify-list').innerHTML = list.count
        ? list.classifications.map((c) => `
            <div class="list-item">
              <span class="swatch" style="background:${c.color}"></span>
              <div class="grow-1">
                <div class="t">${esc(c.title || '(no title)')}</div>
                <div class="s">${esc(c.app)} · ${esc(c.source)}${c.source === 'llm' ? ` · ${Math.round(c.confidence * 100)}%` : ''}</div>
              </div>
              <select class="input narrow-md" data-exe="${esc(c.exe)}" data-title="${esc(c.title)}">
                ${State.categories.map((k) => `<option value="${k.key}"${k.key === c.category ? ' selected' : ''}>${esc(k.label)}</option>`).join('')}
              </select>
            </div>`).join('')
        : '<div class="empty">Nothing decided yet.</div>';

      $('#classify-list').querySelectorAll('select').forEach((sel) => {
        sel.onchange = async () => {
          try {
            const r = await api('/classify/override', {
              method: 'PUT',
              body: { exe: sel.dataset.exe, title: sel.dataset.title, category: sel.value },
            });
            toast(`Pinned · ${r.segments_updated} segment(s) updated`);
            Classify.load();
          } catch (e) { toast(e.message, true); }
        };
      });
    } catch (e) { toast(e.message, true); }
  },
};

$('#classify-run').onclick = async (e) => {
  const btn = e.target;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = 'Classifying…';
  $('#classify-status').innerHTML =
    '<div class="banner warn">Asking the model. On a local CPU model this takes a few minutes per batch — you can leave this page.</div>';
  try {
    const d = await api('/classify/run?limit=20', { method: 'POST' });
    $('#classify-status').innerHTML = d.classified
      ? `<div class="banner">Classified ${d.classified} of ${d.pending} · ${d.segments_updated} segment(s) recategorised via ${esc(d.provider)}.</div>`
      : `<div class="banner warn">Nothing was classified.${d.errors?.length ? ' ' + esc(d.errors[0]) : ''}</div>`;
    await loadCategories();
    Classify.load();
    Today.load();
  } catch (err) {
    $('#classify-status').innerHTML = `<div class="banner error">${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
};

$('#set-provider').onchange = async (e) => {
  try {
    await api('/settings/llm_provider', { method: 'PUT', body: { provider: e.target.value } });
    $('#chat-provider').value = e.target.value;
    toast('Default assistant updated');
  } catch (err) { toast(err.message, true); }
};

$('#rules-reload').onclick = async (e) => {
  e.target.disabled = true;
  e.target.textContent = 'Re-categorising…';
  try {
    const d = await api('/rules/reload', { method: 'POST' });
    toast(`Reloaded ${d.rules} rules · ${d.segments_recategorized.toLocaleString()} segments updated`);
    State.categories = [];
    Settings.enter();
  } catch (err) { toast(err.message, true); }
  finally {
    e.target.disabled = false;
    e.target.textContent = 'Reload rules & re-categorise';
  }
};

/* ---------------- boot ---------------- */

show(location.hash.slice(1) || 'today');
