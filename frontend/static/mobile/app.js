/* OpenAlgo Mobile — core: state, api, router, shell, hub, watchlist, tools, account, settings */
'use strict';

/* ================= config & state ================= */
const CFG_KEY = 'oa-mobile-config-v1';
const CFG = Object.assign(
  { server: '', user: '', pass: '', tunnelPort: 5000 },
  JSON.parse(localStorage.getItem(CFG_KEY) || '{}')
);
function saveCfg() { try { localStorage.setItem(CFG_KEY, JSON.stringify(CFG)); } catch (e) {} }

const S = {
  view: 'hub',
  session: null,          // /m/api/session payload
  connOk: false,
  chartsSel: 'NIFTY',
  chartsTab: 'brief',
  term: { underlying: 'CRUDEOIL', exchange: 'MCX', expiry: '', ce: '', pe: '', lots: 1, product: 'MIS' },
};

/* ================= tiny helpers ================= */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (v, d = 2) => (v == null || isNaN(Number(v)) ? '—' : Number(v).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d }));
const cls = (v) => (Number(v) >= 0 ? 'up' : 'down');
const sign = (v, d = 2) => (Number(v) >= 0 ? '+' : '') + fmt(v, d);

let toastTimer = null;
function toast(msg, ms = 2600) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), ms);
}

/* ================= api ================= */
async function api(path, opts = {}) {
  const base = CFG.server.replace(/\/+$/, '');
  const res = await fetch(base + '/m/api' + path, {
    method: opts.method || 'GET',
    headers: opts.body ? { 'Content-Type': 'application/json' } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: 'include',
    signal: AbortSignal.timeout(opts.timeout || 20000),
  });
  if (res.status === 401 && !opts.noAuthRedirect) { showLogin('Session expired — sign in again'); throw new Error('401'); }
  const ct = res.headers.get('content-type') || '';
  const data = ct.includes('json') ? await res.json() : { status: 'error', message: await res.text() };
  if (!res.ok && opts.ok !== false) throw new Error(data.message || ('HTTP ' + res.status));
  return data;
}

/* ================= login ================= */
function showLogin(msg) {
  $('shell').classList.add('hidden');
  $('login-view').classList.remove('hidden');
  $('login-server').value = CFG.server;
  $('login-user').value = CFG.user;
  $('login-pass').value = CFG.pass;
  if (msg) { $('login-msg').textContent = msg; $('login-msg').classList.remove('hidden'); }
}

async function doLogin() {
  const btn = $('login-btn');
  CFG.server = $('login-server').value.trim().replace(/\/+$/, '');
  CFG.user = $('login-user').value.trim();
  CFG.pass = $('login-pass').value;
  if (!CFG.server || !CFG.user || !CFG.pass) { $('login-msg').textContent = 'Fill server, username and password'; $('login-msg').classList.remove('hidden'); return; }
  btn.disabled = true; btn.textContent = 'Signing in…';
  try {
    // 1. session cookie for the server domain
    const r = await fetch(CFG.server + '/auth/login', {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: CFG.user, password: CFG.pass }),
      signal: AbortSignal.timeout(20000),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.message || ('Login failed (HTTP ' + r.status + ')'));
    if (j.redirect === '/setup') throw new Error('Server needs initial setup — open it in a browser once');
    saveCfg();
    // 2. verify our API surface + load session
    const sess = await api('/session', { noAuthRedirect: true, timeout: 12000 });
    if (sess.status !== 'success') throw new Error(sess.message || 'Session check failed');
    S.session = sess;
    enterApp();
  } catch (e) {
    $('login-msg').textContent = e.message || 'Login failed';
    $('login-msg').classList.remove('hidden');
  } finally {
    btn.disabled = false; btn.textContent = 'Sign In';
  }
}

async function checkSession() {
  try {
    const sess = await api('/session', { timeout: 8000, noAuthRedirect: true });
    if (sess.status === 'success') { S.session = sess; enterApp(); return true; }
  } catch (e) { /* fallthrough */ }
  return false;
}

function enterApp() {
  $('login-view').classList.add('hidden');
  $('shell').classList.remove('hidden');
  const mb = $('mode-badge');
  mb.textContent = (S.session.mode || 'live').toUpperCase();
  mb.className = 'mode-badge ' + (S.session.mode === 'analyzer' ? 'analyzer' : 'live');
  connPing(true);
  nav(S.view || 'hub');
  if (!S._poll) S._poll = setInterval(connPing, 30000);
}

async function connPing(setOnly) {
  try {
    await api('/session', { timeout: 7000, noAuthRedirect: true });
    S.connOk = true;
  } catch (e) { S.connOk = false; }
  $('conn-dot').classList.toggle('on', S.connOk);
  if (!S.connOk && S.view === 'hub') render(); // show the banner
}

/* ================= router ================= */
const VIEWS = {};
function nav(view) {
  S.view = view;
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.view === view));
  const titles = { hub: 'Widgets Hub', watchlist: 'Watchlist', chart: 'Charts & Flow', scalper: 'Scalper', tools: 'OpenAlgo Tools', account: 'Account' };
  $('topbar-title').textContent = titles[view] || view;
  render();
  window.scrollTo(0, 0);
}

function render() {
  const v = $('view');
  const fn = VIEWS[S.view] || VIEWS.hub;
  v.innerHTML = '';
  try { fn(v); } catch (e) {
    v.innerHTML = '<div class="card">Render error: ' + esc(e.message) + '</div>';
  }
}

/* ================= HUB ================= */
VIEWS.hub = function (el) {
  const tiles = [
    { id: 'watchlist', ico: '★', name: 'Watchlist', sub: 'Lists + live quotes' },
    { id: 'chart', ico: '📈', name: 'Charts & Flow', sub: 'Brief · Orderflow · News' },
    { id: 'scalper', ico: '🎯', name: 'Scalper Advisor', sub: 'Signals · monitor' },
    { id: 'terminal', ico: '⚡', name: 'Scalper Terminal', sub: 'Chain · depth · orders' },
    { id: 'tools', ico: '🛠', name: 'OpenAlgo Tools', sub: 'Search · quotes · depth' },
    { id: 'account', ico: '👤', name: 'Account', sub: 'Funds · positions' },
    { id: 'settings', ico: '🔧', name: 'Settings', sub: 'Server · tunnel port' },
  ];
  el.innerHTML =
    (!S.connOk ? '<div class="card down">⚠ Server unreachable — retrying…</div>' : '') +
    '<div class="hub-grid">' +
    tiles.map((t) =>
      '<button class="hub-tile" data-go="' + t.id + '"><span class="h-ico">' + t.ico + '</span>' +
      '<span class="h-name">' + t.name + '</span><span class="h-sub">' + t.sub + '</span></button>'
    ).join('') +
    '</div>' +
    '<div class="card" style="margin-top:10px"><h3>Server</h3>' +
    '<div class="kv"><span class="k">User</span><span class="v">' + esc(S.session?.user || '—') + '</span></div>' +
    '<div class="kv"><span class="k">Broker</span><span class="v">' + esc(S.session?.broker || '—') + '</span></div>' +
    '<div class="kv"><span class="k">Mode</span><span class="v">' + esc(S.session?.mode || '—') + '</span></div>' +
    '<div class="kv"><span class="k">URL</span><span class="v mono" style="font-size:11px">' + esc(CFG.server) + '</span></div></div>';
  el.querySelectorAll('[data-go]').forEach((b) =>
    b.addEventListener('click', () => {
      const id = b.dataset.go;
      if (id === 'terminal' || id === 'settings') { S._stackView = 'hub'; renderSub(id); }
      else nav(id);
    })
  );
};

/* sub-views reachable from the hub but not the tab bar */
function renderSub(id) {
  if (id === 'terminal') VIEWS.terminal($('view'));
  if (id === 'settings') VIEWS.settings($('view'));
  document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
  $('topbar-title').textContent = id === 'terminal' ? 'Scalper Terminal' : 'Settings';
  window.scrollTo(0, 0);
}

/* ================= WATCHLIST ================= */
S.wl = { lists: [], activeId: null, rows: [] };
VIEWS.watchlist = function (el) {
  el.innerHTML = '<div class="card"><div class="spinner"></div> Loading watchlists…</div>';
  api('/watchlists', { timeout: 15000 })
    .then((r) => {
      S.wl.lists = r.data || [];
      if (!S.wl.lists.length) { el.innerHTML = '<div class="card">No watchlists yet — create one in the desktop terminal.</div>'; return; }
      if (!S.wl.lists.some((l) => String(l.id) === String(S.wl.activeId))) S.wl.activeId = S.wl.lists[0].id;
      paintWatchlist(el);
      loadQuotes(el);
    })
    .catch((e) => { el.innerHTML = '<div class="card down">Failed: ' + esc(e.message) + '</div>'; });
};

function paintWatchlist(el) {
  const list = S.wl.lists.find((l) => String(l.id) === String(S.wl.activeId));
  el.innerHTML =
    '<div class="wl-selector">' +
    S.wl.lists.map((l) => '<button class="wl-tab' + (String(l.id) === String(S.wl.activeId) ? ' active' : '') + '" data-wl="' + l.id + '">' + esc(l.name) + ' (' + l.items.length + ')</button>').join('') +
    '</div><div class="card" id="wl-body"><div class="spinner"></div> Quotes…</div>';
  el.querySelectorAll('[data-wl]').forEach((b) =>
    b.addEventListener('click', () => { S.wl.activeId = b.dataset.wl; paintWatchlist(el); loadQuotes(el); })
  );
  void list;
}

async function loadQuotes(el) {
  const list = S.wl.lists.find((l) => String(l.id) === String(S.wl.activeId));
  if (!list) return;
  const body = $('wl-body');
  if (!body) return;
  try {
    const r = await api('/watchlist/quotes', { method: 'POST', body: { symbols: list.items.map((i) => ({ symbol: i.symbol, exchange: i.exchange })) }, timeout: 25000 });
    S.wl.rows = r.data || [];
    if (!$('wl-body')) return;
    $('wl-body').innerHTML = S.wl.rows.length
      ? S.wl.rows.map((q) => {
          const hit = list.items.find((i) => i.symbol === q.symbol && i.exchange === q.exchange) || {};
          return '<div class="wl-row"><div class="wl-tap" data-sym="' + esc(q.symbol) + '" data-exch="' + esc(q.exchange) + '">' +
            '<span class="wl-sym">' + esc(q.symbol) + '</span><span class="wl-exch">' + esc(q.exchange) + (hit.expiry ? ' · ' + esc(hit.expiry) : '') + '</span></div>' +
            '<div class="wl-px"><div class="wl-ltp mono">' + fmt(q.ltp) + '</div>' +
            '<div class="wl-chp mono ' + cls(q.chp || 0) + '">' + (q.chp == null ? '' : sign(q.chp) + '%') + '</div></div></div>';
        }).join('')
      : 'No symbols in this list.';
    $('wl-body').querySelectorAll('[data-sym]').forEach((row) =>
      row.addEventListener('click', () => {
        S.chartsSel = row.dataset.exch + ':' + row.dataset.sym;
        S.chartsTab = 'brief';
        nav('chart');
      })
    );
  } catch (e) {
    if ($('wl-body')) $('wl-body').innerHTML = '<span class="down">Quotes failed: ' + esc(e.message) + '</span>';
  }
}

/* ================= TOOLS ================= */
VIEWS.tools = function (el) {
  el.innerHTML =
    '<div class="card tool-card"><h3>Symbol search</h3>' +
    '<div class="search-box"><input id="tool-q" placeholder="e.g. CRUDEOIL, NIFTY, RELIANCE…" autocapitalize="off"><button class="btn btn-ghost" id="tool-go">Go</button></div>' +
    '<div id="tool-results"></div></div>' +
    '<div class="card tool-card"><h3>Quote & depth</h3>' +
    '<div class="search-box"><input id="qd-sym" placeholder="SYMBOL e.g. CRUDEOIL" autocapitalize="off"><input id="qd-exch" placeholder="EXCH" style="max-width:80px" value="MCX"><button class="btn btn-ghost" id="qd-go">Fetch</button></div>' +
    '<div id="qd-out"></div></div>';
  const runSearch = async () => {
    const q = $('tool-q').value.trim();
    if (q.length < 2) return;
    $('tool-results').innerHTML = '<div class="spinner"></div>';
    try {
      const r = await api('/search?q=' + encodeURIComponent(q), { timeout: 20000 });
      $('tool-results').innerHTML = (r.data || []).map((s) =>
        '<div class="sr-row"><div><b>' + esc(s.symbol) + '</b> <span class="muted">' + esc(s.exchange) + '</span>' +
        (s.name ? '<div class="muted" style="font-size:11px">' + esc(s.name) + '</div>' : '') + '</div>' +
        '<button class="btn btn-ghost" data-q="' + esc(s.symbol) + '" data-qe="' + esc(s.exchange) + '">Quote</button></div>'
      ).join('') || '<div class="muted">No results.</div>';
      $('tool-results').querySelectorAll('[data-q]').forEach((b) =>
        b.addEventListener('click', () => { $('qd-sym').value = b.dataset.q; $('qd-exch').value = b.dataset.qe; fetchQD(); })
      );
    } catch (e) { $('tool-results').innerHTML = '<span class="down">' + esc(e.message) + '</span>'; }
  };
  const fetchQD = async () => {
    const sym = $('qd-sym').value.trim().toUpperCase(), exch = $('qd-exch').value.trim().toUpperCase();
    if (!sym || !exch) return;
    $('qd-out').innerHTML = '<div class="spinner"></div>';
    try {
      const wl = await api('/watchlist/quotes', { method: 'POST', body: { symbols: [{ symbol: sym, exchange: exch }] }, timeout: 20000 });
      const q = (wl.data || [])[0] || {};
      let depthHtml = '';
      try {
        const d = await api('/depth?symbol=' + encodeURIComponent(sym) + '&exchange=' + encodeURIComponent(exch), { timeout: 20000 });
        const dd = d.data || {};
        const bids = (dd.bids || []).slice(0, 5), asks = (dd.asks || []).slice(0, 5);
        depthHtml = '<h3 style="margin-top:10px">Depth</h3><div class="depth-5">' +
          '<div>' + bids.map((b) => '<div class="dcell bid"><span>' + fmt(b.price) + '</span><span>' + fmt(b.quantity, 0) + '</span></div>').join('') + '</div>' +
          '<div>' + asks.map((a) => '<div class="dcell ask"><span>' + fmt(a.price) + '</span><span>' + fmt(a.quantity, 0) + '</span></div>').join('') + '</div></div>';
      } catch (e) { depthHtml = '<div class="note">Depth unavailable: ' + esc(e.message) + '</div>'; }
      $('qd-out').innerHTML =
        '<div class="kv"><span class="k">LTP</span><span class="v mono">' + fmt(q.ltp) + '</span></div>' +
        '<div class="kv"><span class="k">Change</span><span class="v mono ' + cls(q.chp || 0) + '">' + (q.chp == null ? '—' : sign(q.chp) + '%') + '</span></div>' +
        '<div class="kv"><span class="k">O / H / L</span><span class="v mono">' + fmt(q.open) + ' / ' + fmt(q.high) + ' / ' + fmt(q.low) + '</span></div>' +
        '<div class="kv"><span class="k">Prev close</span><span class="v mono">' + fmt(q.prev_close) + '</span></div>' +
        '<div class="kv"><span class="k">Volume</span><span class="v mono">' + fmt(q.volume, 0) + '</span></div>' + depthHtml;
    } catch (e) { $('qd-out').innerHTML = '<span class="down">' + esc(e.message) + '</span>'; }
  };
  $('tool-go').addEventListener('click', runSearch);
  $('tool-q').addEventListener('keydown', (e) => { if (e.key === 'Enter') runSearch(); });
  $('qd-go').addEventListener('click', fetchQD);
  $('qd-sym').addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchQD(); });
};

/* ================= ACCOUNT ================= */
VIEWS.account = function (el) {
  el.innerHTML = '<div class="card"><div class="spinner"></div> Loading account…</div>';
  api('/account', { timeout: 30000 })
    .then((r) => {
      const f = r.funds || {};
      const rows = Object.entries(f).filter(([k]) => k !== 'availablecash' || true).slice(0, 10);
      el.innerHTML =
        '<div class="card"><h3>Funds ' + (r.mode === 'analyzer' ? '<span class="chip">ANALYZER</span>' : '') + '</h3>' +
        (rows.length ? rows.map(([k, v]) => '<div class="kv"><span class="k">' + esc(k.replace(/_/g, ' ')) + '</span><span class="v mono">' + fmt(v) + '</span></div>').join('') : '<div class="muted">No funds data (broker session may be idle).</div>') +
        '</div>' +
        '<div class="card"><h3>Positions (' + (r.positions || []).length + ')</h3><div id="acc-pos"></div></div>' +
        '<div class="card"><h3>Today\'s orders (' + (r.orders || []).length + ')</h3>' +
        ((r.orders || []).slice(0, 12).map((o) =>
          '<div class="kv"><span class="k">' + esc(o.symbol) + ' <span class="muted">' + esc(o.action || '') + ' ' + esc(o.quantity || '') + '</span></span>' +
          '<span class="v ' + (o.order_status === 'COMPLETE' ? 'up' : '') + '">' + esc(o.order_status || '') + '</span></div>').join('') || '<div class="muted">None.</div>') +
        '</div>';
      const posEl = $('acc-pos');
      const positions = r.positions || [];
      posEl.innerHTML = positions.length ? positions.map((p, i) => {
        const qty = Number(p.quantity || 0);
        const pnl = Number(p.pnl != null ? p.pnl : (p.unrealized || 0));
        return '<div class="kv"><span class="k">' + esc(p.symbol) + ' <span class="muted">' + esc(p.product || '') + '</span></span>' +
          '<span class="v"><span class="mono ' + cls(pnl) + '">' + sign(pnl) + '</span> <button class="btn btn-ghost" style="padding:4px 9px;font-size:11px" data-close="' + i + '">Close</button></span></div>';
      }).join('') : '<div class="muted">No open positions.</div>';
      posEl.querySelectorAll('[data-close]').forEach((b) =>
        b.addEventListener('click', async () => {
          const p = positions[Number(b.dataset.close)];
          if (!confirm('Close ' + p.symbol + '?')) return;
          b.disabled = true; b.textContent = '…';
          try {
            await api('/positions/close', { method: 'POST', body: { symbol: p.symbol, exchange: p.exchange, product: p.product }, timeout: 30000 });
            toast('Close order sent'); render();
          } catch (e) { toast('Close failed: ' + e.message); b.disabled = false; b.textContent = 'Close'; }
        })
      );
    })
    .catch((e) => { el.innerHTML = '<div class="card down">Failed: ' + esc(e.message) + '</div>'; });
};

/* ================= SETTINGS (incl. tunnel port) ================= */
VIEWS.settings = function (el) {
  el.innerHTML = '<div class="card"><div class="spinner"></div> Loading settings…</div>';
  api('/tunnel', { timeout: 15000 })
    .then((r) => {
      const d = r.data || {};
      el.innerHTML =
        '<div class="card"><h3>Server</h3>' +
        '<div class="kv"><span class="k">URL</span><span class="v mono" style="font-size:11px">' + esc(CFG.server) + '</span></div>' +
        '<div class="kv"><span class="k">User</span><span class="v">' + esc(d.user || '—') + '</span></div>' +
        '<div class="kv"><span class="k">App port</span><span class="v mono">' + esc(d.flask_port || '5000') + '</span></div>' +
        '<button class="btn btn-ghost btn-block" id="set-logout" style="margin-top:10px">Sign out</button></div>' +
        '<div class="card"><h3>Tunnel</h3>' +
        '<div class="kv"><span class="k">ngrok status</span><span class="v">' + (d.enabled ? '<span class="up">ENABLED</span>' : '<span class="down">DISABLED</span>') + '</span></div>' +
        '<div class="kv"><span class="k">Tunnel URL</span><span class="v mono" style="font-size:10px;word-break:break-all">' + esc(d.url || 'not running') + '</span></div>' +
        '<div class="set-row"><span class="k muted">Tunnel port</span><input id="tun-port" inputmode="numeric" value="' + esc(CFG.tunnelPort || 5000) + '"></div>' +
        '<div class="row" style="margin-top:10px;gap:8px">' +
        '<button class="btn btn-primary" style="flex:1" id="tun-start">Open tunnel</button>' +
        '<button class="btn btn-ghost" style="flex:1" id="tun-stop">Close tunnel</button></div>' +
        '<div class="note">Opens an ngrok tunnel from the server to the given local port (default 5000 — the trading app). Use the resulting https URL to reach the server from outside your network. ngrok must be enabled on the server (NGROK_ALLOW=TRUE).</div></div>';
      $('tun-start').addEventListener('click', async () => {
        const port = Number($('tun-port').value) || 5000;
        CFG.tunnelPort = port; saveCfg();
        $('tun-start').disabled = true; $('tun-start').textContent = 'Opening…';
        try {
          const res = await api('/tunnel/port', { method: 'POST', body: { port }, timeout: 40000 });
          toast('Tunnel: ' + (res.data?.url || 'started'), 6000);
          renderSub('settings');
        } catch (e) { toast(e.message); $('tun-start').disabled = false; $('tun-start').textContent = 'Open tunnel'; }
      });
      $('tun-stop').addEventListener('click', async () => {
        try { await api('/tunnel/port', { method: 'POST', body: { port: 0 }, timeout: 30000 }); toast('Tunnel closed'); renderSub('settings'); }
        catch (e) { toast(e.message); }
      });
      $('set-logout').addEventListener('click', () => {
        CFG.pass = ''; saveCfg();
        fetch(CFG.server + '/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {});
        showLogin('Signed out');
      });
    })
    .catch((e) => { el.innerHTML = '<div class="card down">Failed: ' + esc(e.message) + '</div>'; });
};

/* ================= boot ================= */
document.addEventListener('DOMContentLoaded', () => {
  $('login-btn').addEventListener('click', doLogin);
  $('login-pass').addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });
  $('refresh-btn').addEventListener('click', () => { render(); toast('Refreshed'); });
  document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => nav(t.dataset.view)));
  if (CFG.server) checkSession().then((ok) => { if (!ok) showLogin(); });
  else showLogin();
});
