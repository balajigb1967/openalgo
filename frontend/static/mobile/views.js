/* OpenAlgo Mobile — heavy views: chart page (brief/orderflow/news), scalper advisor, scalper terminal */
'use strict';

/* ================= CHART PAGE: brief + orderflow + news ================= */
S.candles = { tf: '5m', data: [] };

VIEWS.chart = function (el) {
  const tabs = [['brief', 'Brief'], ['flow', 'Orderflow'], ['news', 'News']];
  el.innerHTML =
    '<div class="seg">' + tabs.map(([id, lbl]) =>
      '<button data-ct="' + id + '"' + (S.chartsTab === id ? ' class="active"' : '') + '>' + lbl + '</button>').join('') +
    '</div><div id="chart-body"></div>';
  el.querySelectorAll('[data-ct]').forEach((b) =>
    b.addEventListener('click', () => { S.chartsTab = b.dataset.ct; render(); })
  );
  const body = $('chart-body');
  if (S.chartsTab === 'brief') loadBrief(body);
  else if (S.chartsTab === 'flow') loadOrderflow(body);
  else loadNews(body);
};

/* ---- Market brief ---- */
async function loadBrief(body) {
  body.innerHTML = '<div class="card"><div class="spinner"></div> Building brief…</div>';
  try {
    const r = await api('/brief', { timeout: 35000 });
    const st = r.session_stance || {};
    const gq = (arr) => (arr || []).slice(0, 8).map((q) =>
      '<div class="brief-quote"><span>' + esc(q.name || q.symbol) + '</span>' +
      '<span class="mono ' + cls(q.chp || 0) + '">' + fmt(q.ltp) + ' <small>(' + sign(q.chp) + '%)</small></span></div>').join('');
    body.innerHTML =
      '<div class="card"><h3>Session Stance ' + (st.is_live ? '<span class="chip live">● LIVE</span>' : '<span class="chip">CLOSED</span>') + '</h3>' +
      '<div class="stance"><div class="phase">' + esc(st.phase_label || st.phase || '—') + ' · ' + esc(st.exchange || '') + '</div>' +
      '<div class="msg">' + esc(st.narrative || r.summary_markdown || 'No stance yet.') + '</div></div></div>' +
      '<div class="card"><h3>Indices</h3>' + gq(r.indices) + '</div>' +
      '<div class="card"><h3>Commodities (MCX)</h3>' + gq(r.commodities) + '</div>' +
      (r.overnight_cues ? '<div class="card"><h3>Overnight cues</h3><div class="stance"><div class="msg">' +
        esc(r.overnight_cues.sentiment || '') + ' — ' + esc(r.overnight_cues.summary || '') + '</div></div>' +
        gq(r.overnight_cues.global_indices) + '</div>' : '') +
      (r.gameplan ? '<div class="card"><h3>Gameplan</h3>' +
        (r.gameplan.nifty ? '<div class="kv"><span class="k">NIFTY</span><span class="v mono">' + esc(r.gameplan.nifty.view || '') + '</span></div>' : '') +
        '<div class="kv"><span class="k">Direction</span><span class="v">' + esc(r.gameplan.likely_direction || '—') + '</span></div>' +
        '<div class="kv"><span class="k">Range</span><span class="v mono">' + esc(r.gameplan.likely_range || '—') + '</span></div></div>' : '') +
      '<div class="card"><h3>Options</h3>' +
      (r.options ? '<div class="kv"><span class="k">PCR</span><span class="v mono">' + fmt(r.options.pcr) + '</span></div>' +
        '<div class="kv"><span class="k">Max pain</span><span class="v mono">' + fmt(r.options.max_pain, 0) + '</span></div>' : '<div class="muted">—</div>') +
      '</div>';
  } catch (e) { body.innerHTML = '<div class="card down">Brief failed: ' + esc(e.message) + '</div>'; }
}

/* ---- Orderflow table ---- */
async function loadOrderflow(body) {
  body.innerHTML = '<div class="card"><div class="spinner"></div> Computing orderflow…</div>';
  try {
    const r = await api('/orderflow/table?tf=' + (S.candles.tf || '5m'), { timeout: 60000 });
    const rows = r.rows || [];
    body.innerHTML =
      '<div class="card">' + (rows.length ? rows.map((row) => {
        const bias = row.delta_bias || (row.session_delta >= 0 ? 'BUY' : 'SELL');
        return '<div class="of-row"><div class="of-top"><span class="of-name">' + esc(row.name || row.key) +
          ' <span class="chip ' + (bias === 'BUY' || bias === 'BULLISH' ? 'buy' : 'sell') + '">' + esc(bias) + '</span></span>' +
          '<span class="of-ltp mono">' + fmt(row.ltp) + '</span></div>' +
          '<div class="of-stats"><span>Δ <b class="' + cls(row.session_delta) + '">' + fmt(row.session_delta, 0) + '</b></span>' +
          '<span>CVD <b class="' + cls(row.session_cvd) + '">' + fmt(row.session_cvd, 0) + '</b></span>' +
          (row.chp != null ? '<span>Day <b class="' + cls(row.chp) + '">' + sign(row.chp) + '%</b></span>' : '') + '</div></div>';
      }).join('') : '<div class="muted">No orderflow rows.</div>') + '</div>';
  } catch (e) { body.innerHTML = '<div class="card down">Orderflow failed: ' + esc(e.message) + '</div>'; }
}

/* ---- News ---- */
async function loadNews(body) {
  body.innerHTML = '<div class="card"><div class="spinner"></div> Loading news…</div>';
  try {
    const r = await api('/news?limit=40', { timeout: 30000 });
    const items = r.articles || r.items || r.data || [];
    body.innerHTML = '<div class="card">' + (items.length ? items.map((n) =>
      '<div class="news-item"><div class="news-title">' + esc(n.title) + '</div>' +
      '<div class="news-meta">' + esc(n.source || '') + ' · ' + esc(n.published || n.time || '') + '</div>' +
      (n.summary ? '<div class="news-sum">' + esc(n.summary) + '</div>' : '') + '</div>').join('')
      : '<div class="muted">No news.</div>') + '</div>';
  } catch (e) { body.innerHTML = '<div class="card down">News failed: ' + esc(e.message) + '</div>'; }
}

/* ================= SCALPER ADVISOR ================= */
S.advTab = 'signals';

VIEWS.scalper = function (el) {
  const tabs = [['signals', 'Signals'], ['monitor', 'Monitor'], ['events', 'Events']];
  el.innerHTML =
    '<div class="seg">' + tabs.map(([id, lbl]) =>
      '<button data-at="' + id + '"' + (S.advTab === id ? ' class="active"' : '') + '>' + lbl + '</button>').join('') +
    '</div><div id="adv-body"><div class="card"><div class="spinner"></div></div></div>';
  el.querySelectorAll('[data-at]').forEach((b) =>
    b.addEventListener('click', () => { S.advTab = b.dataset.at; render(); })
  );
  loadAdvisor($('adv-body'));
};

async function loadAdvisor(body) {
  try {
    const r = await api('/advisor', { timeout: 45000 });
    if (!body.isConnected) return;
    const instruments = r.instruments || [];
    const alerts = (r.monitor && r.monitor.alerts) || [];
    const armed = (r.monitor && r.monitor.armed) || {};
    const events = (r.monitor && r.monitor.events) || [];

    if (S.advTab === 'signals') {
      body.innerHTML = instruments.length ? instruments.map((a) =>
        '<div class="adv-card' + (a.armed ? ' armed' : '') + '">' +
        '<div class="adv-top"><span class="adv-name">' + esc(a.name || a.key) +
        (a.armed ? ' <span class="chip live">● LIVE</span>' : '') + '</span>' +
        '<span class="chip ' + (String(a.signal || '').startsWith('BUY') ? 'buy' : '') + '">' + esc(a.signal || 'WAIT') + '</span></div>' +
        (a.spot != null ? '<div class="adv-sub mono">spot ' + fmt(a.spot) + (a.option_symbol ? ' · ' + esc(a.option_symbol) : '') +
          (a.entry_premium != null ? ' · entry ₹' + fmt(a.entry_premium, 1) : '') + '</div>' : '') +
        (a.armed && a.current_premium != null ?
          '<div class="adv-sub mono">now ₹' + fmt(a.current_premium, 1) + ' · P&L <b class="' + cls(a.armed_pnl_pct || 0) + '">' + sign(a.armed_pnl_pct, 1) + '%</b></div>' : '') +
        '<div class="adv-sub muted">conf ' + (a.confidence ?? '—') + '%' +
        (a.pcr != null ? ' · PCR ' + fmt(a.pcr) : '') + (a.dte != null ? ' · ' + a.dte + 'd' : '') +
        (a.momentum && a.momentum.trend && a.momentum.trend !== 'FLAT' ? ' · <b class="' + (a.momentum.trend === 'UP' ? 'up' : 'down') + '">' + esc(a.momentum.trend) + '</b>' : '') + '</div>' +
        (a.note ? '<div class="adv-note">' + esc(a.note) + '</div>' : '') +
        ((a.basis || []).length ? '<div class="basis">' + a.basis.slice(0, 4).map((x) => '• ' + esc(x)).join('<br>') + '</div>' : '') +
        '<div class="row" style="margin-top:8px">' +
        (String(a.signal || '').startsWith('BUY') ?
          '<button class="btn ' + (a.armed ? 'btn-ghost' : 'btn-primary') + '" style="flex:1" data-arm="' + esc(a.key) + '">' + (a.armed ? '◼ Disarm' : '▶ Arm monitor') + '</button>' : '<span></span>') +
        (a.strike != null && a.option_symbol ? '<button class="btn btn-ghost" style="flex:1" data-load="' + esc(a.option_symbol) + '" data-exch="' + esc(a.market) + '">Load →</button>' : '') +
        '</div></div>'
      ).join('') : '<div class="card muted">No instruments yet.</div>';
      body.querySelectorAll('[data-arm]').forEach((b) => b.addEventListener('click', async () => {
        b.disabled = true; b.textContent = '…';
        try { await api('/advisor?refresh=1&arm=' + encodeURIComponent(b.dataset.arm), { timeout: 45000 }); toast('Monitor updated'); loadAdvisor(body); }
        catch (e) { toast(e.message); b.disabled = false; b.textContent = 'Retry'; }
      }));
      body.querySelectorAll('[data-load]').forEach((b) => b.addEventListener('click', () => {
        const sym = b.dataset.load; // e.g. CRUDEOIL17SEP269800CE
        const m = sym.match(/^(.+?)(\d+(?:\.\d+)?)(CE|PE)$/);
        if (!m) { toast('Cannot parse ' + sym); return; }
        S.term.underlying = m[1]; S.term.exchange = b.dataset.exch === 'MCX' ? 'MCX' : (b.dataset.exch === 'BSE' ? 'BFO' : 'NFO');
        S.term.strike = m[2]; S.term.side = m[3]; S.term.expiry = '';
        S._stackView = 'scalper';
        renderSub('terminal');
      }));
    } else if (S.advTab === 'monitor') {
      const entries = Object.entries(armed);
      const activeAlerts = alerts.filter((a) => a.status === 'ACTIVE');
      body.innerHTML =
        (activeAlerts.length ? '<div class="card"><h3>Alerts (' + activeAlerts.length + ')</h3>' +
          activeAlerts.map((a) => '<div class="of-row"><div class="of-top"><b>' + esc(a.name || a.key) +
            ' <span class="chip buy">' + esc(a.side) + '</span></b><span class="mono ' + cls(a.pnl_pct || 0) + '">' + sign(a.pnl_pct, 1) + '%</span></div>' +
            '<div class="of-stats"><span class="mono">' + esc(a.option_symbol) + '</span><span>entry ₹' + fmt(a.entry_premium, 1) + ' · now ₹' + fmt(a.current_premium, 1) + '</span></div></div>').join('') + '</div>' : '') +
        '<div class="card"><h3>Armed positions (' + entries.length + ')</h3>' +
        (entries.length ? entries.map(([key, p]) => {
          const trail = p.trail_done ? 'TRAILING' : p.be_done ? 'BREAKEVEN' : 'INITIAL';
          return '<div class="adv-card armed"><div class="adv-top"><span class="adv-name">' + esc(p.name || key) +
            ' <span class="chip ' + (p.side === 'CE' ? 'buy' : 'sell') + '">' + esc(p.side || '') + '</span></span>' +
            '<span class="chip">' + esc(trail) + '</span></div>' +
            '<div class="adv-sub mono">' + esc(p.option_symbol || '') + '</div>' +
            '<div class="adv-sub mono">₹' + fmt(p.entry_premium, 1) + ' → <b>₹' + fmt(p.current_premium, 1) + '</b> · P&L <b class="' + cls(p.pnl_pct || 0) + '">' + sign(p.pnl_pct, 1) + '%</b></div>' +
            ((p.revision_log || []).length ? '<div class="basis">' + p.revision_log.slice(-2).map((rv) => esc(rv.ts) + ' — ' + esc(rv.msg)).join('<br>') + '</div>' : '') +
            '</div>';
        }).join('') : '<div class="muted">Nothing live-monitored. Arm a signal (or AUTO).</div>') + '</div>';
    } else {
      body.innerHTML = '<div class="card">' + (events.length ? events.slice(0, 40).map((ev, i) =>
        '<div class="of-row"><div class="of-top"><b class="' +
        (ev.severity === 'DANGER' ? 'down' : ev.severity === 'SUCCESS' ? 'up' : ev.severity === 'WARN' ? '' : 'muted') +
        '">' + esc(ev.key) + '</b><span class="muted mono" style="font-size:10px">' + esc(ev.ts) + '</span></div>' +
        '<div style="font-size:12px;margin-top:2px">' + esc(ev.msg) + '</div></div>').join('')
        : '<div class="muted">No monitor events yet.</div>') + '</div>';
    }
  } catch (e) { if (body.isConnected) body.innerHTML = '<div class="card down">Advisor failed: ' + esc(e.message) + '</div>'; }
}

/* ================= SCALPER TERMINAL ================= */
VIEWS.terminal = function (el) {
  el.innerHTML =
    '<div class="card"><div class="term-head">' +
    '<select id="tm-exch"><option>NFO</option><option>BFO</option><option>MCX</option><option>CDS</option></select>' +
    '<select id="tm-und"><option>' + esc(S.term.underlying) + '</option></select>' +
    '<select id="tm-exp"><option value="">expiry…</option></select>' +
    '</div><div id="tm-chain"><div class="spinner"></div></div>' +
    '<div class="row" style="margin-top:10px;gap:8px">' +
    '<div class="pill row" style="gap:6px"><button class="btn btn-ghost" style="padding:4px 10px" id="tm-lot-minus">−</button>' +
    '<span class="mono" id="tm-lot">1</span>' +
    '<button class="btn btn-ghost" style="padding:4px 10px" id="tm-lot-plus">+</button><span class="muted" style="font-size:11px">lots</span></div>' +
    '<select id="tm-product" style="flex:1"><option>MIS</option><option>NRML</option></select></div>' +
    '<div class="trade-grid">' +
    '<button class="btn btn-up" id="tm-buy-ce">B CE</button><button class="btn btn-down" id="tm-sell-ce">S CE</button>' +
    '<button class="btn btn-up" id="tm-buy-pe">B PE</button><button class="btn btn-down" id="tm-sell-pe">S PE</button>' +
    '</div><div class="note">One-Click trades live — verify the selected strikes before tapping. Market orders.</div></div>';

  const exchSel = $('tm-exch'), undSel = $('tm-und'), expSel = $('tm-exp');
  exchSel.value = S.term.exchange;
  $('tm-product').value = S.term.product;
  $('tm-lot').textContent = S.term.lots;

  exchSel.addEventListener('change', async () => {
    S.term.exchange = exchSel.value; S.term.expiry = ''; S.term.ce = ''; S.term.pe = '';
    await loadUnderlyings();
  });
  undSel.addEventListener('change', () => {
    S.term.underlying = undSel.value; S.term.expiry = ''; S.term.ce = ''; S.term.pe = '';
    loadExpiries(); loadChain();
  });
  expSel.addEventListener('change', () => { S.term.expiry = expSel.value; loadChain(); });
  $('tm-lot-minus').addEventListener('click', () => { S.term.lots = Math.max(1, S.term.lots - 1); $('tm-lot').textContent = S.term.lots; });
  $('tm-lot-plus').addEventListener('click', () => { S.term.lots = Math.min(20, S.term.lots + 1); $('tm-lot').textContent = S.term.lots; });
  $('tm-product').addEventListener('change', () => { S.term.product = $('tm-product').value; });
  $('tm-buy-ce').addEventListener('click', () => termOrder('CE', 'BUY'));
  $('tm-sell-ce').addEventListener('click', () => termOrder('CE', 'SELL'));
  $('tm-buy-pe').addEventListener('click', () => termOrder('PE', 'BUY'));
  $('tm-sell-pe').addEventListener('click', () => termOrder('PE', 'SELL'));

  loadUnderlyings().then(() => { loadExpiries(); loadChain(); });
};

async function loadUnderlyings() {
  // The desktop /scalping underlyings endpoint is session-auth — reuse it.
  const sel = $('tm-und');
  if (!sel) return;
  try {
    const r = await fetch(CFG.server + '/scalping/api/all_underlyings?exchange=' + S.term.exchange + '&instrumenttype=options', { credentials: 'include', signal: AbortSignal.timeout(20000) });
    const j = await r.json();
    const names = (j.data || []);
    if (names.length && !names.includes(S.term.underlying)) S.term.underlying = names.includes('CRUDEOIL') && S.term.exchange === 'MCX' ? 'CRUDEOIL' : names[0];
    if (names.length) S._underlyings = names;
    sel.innerHTML = (names.length ? names : [S.term.underlying]).map((u) => '<option' + (u === S.term.underlying ? ' selected' : '') + '>' + esc(u) + '</option>').join('');
  } catch (e) {
    sel.innerHTML = '<option>' + esc(S.term.underlying) + '</option>';
  }
}

async function loadExpiries() {
  const sel = $('tm-exp');
  if (!sel) return;
  sel.innerHTML = '<option value="">loading…</option>';
  try {
    const r = await api('/expiries?underlying=' + encodeURIComponent(S.term.underlying) + '&exchange=' + encodeURIComponent(S.term.exchange), { timeout: 25000 });
    const exps = r.data || [];
    if (!exps.length) { sel.innerHTML = '<option value="">no expiries</option>'; return; }
    if (!exps.includes(S.term.expiry)) S.term.expiry = exps[0];
    sel.innerHTML = exps.map((x) => '<option' + (x === S.term.expiry ? ' selected' : '') + '>' + esc(x) + '</option>').join('');
  } catch (e) { sel.innerHTML = '<option value="">error</option>'; }
}

async function loadChain() {
  const box = $('tm-chain');
  if (!box) return;
  if (!S.term.expiry) { box.innerHTML = '<div class="muted">Pick an expiry.</div>'; return; }
  box.innerHTML = '<div class="spinner"></div>';
  try {
    const r = await api('/chain?underlying=' + encodeURIComponent(S.term.underlying) + '&exchange=' + encodeURIComponent(S.term.exchange) + '&expiry=' + encodeURIComponent(S.term.expiry) + '&count=8', { timeout: 45000 });
    const rows = r.chain || [];
    S._lastChain = rows;
    const atm = r.atm_strike;
    if (rows.length) {
      const strikes = rows.map((x) => String(x.strike));
      if (!strikes.includes(S.term.ce)) S.term.ce = String(atm);
      if (!strikes.includes(S.term.pe)) S.term.pe = String(atm);
    }
    box.innerHTML = '<table class="chain-table"><thead><tr><th>CE</th><th>Strike</th><th>PE</th></tr></thead><tbody>' +
      rows.map((row) => {
        const s = String(row.strike);
        return '<tr' + (row.strike === atm ? ' class="atm"' : '') + '>' +
          '<td><span class="mono' + (S.term.ce === s ? ' sel' : '') + '" data-sel-ce="' + esc(s) + '">' +
          fmt((row.ce && row.ce.ltp) != null ? row.ce.ltp : null) + '</span></td>' +
          '<td class="mono">' + Number(row.strike).toLocaleString('en-IN') + '</td>' +
          '<td><span class="mono' + (S.term.pe === s ? ' sel' : '') + '" data-sel-pe="' + esc(s) + '">' +
          fmt((row.pe && row.pe.ltp) != null ? row.pe.ltp : null) + '</span></td></tr>';
      }).join('') + '</tbody></table>' +
      '<div class="note">Tap a premium to select that strike. CE selected: <b>' + esc(S.term.ce) + '</b> · PE: <b>' + esc(S.term.pe) + '</b></div>';
    box.querySelectorAll('[data-sel-ce]').forEach((n) => n.addEventListener('click', () => { S.term.ce = n.dataset.selCe; loadChain(); }));
    box.querySelectorAll('[data-sel-pe]').forEach((n) => n.addEventListener('click', () => { S.term.pe = n.dataset.selPe; loadChain(); }));
  } catch (e) { box.innerHTML = '<div class="down">Chain failed: ' + esc(e.message) + '</div>'; }
}

function termChainLeg(side) {
  const s = side === 'CE' ? S.term.ce : S.term.pe;
  if (!s) return null;
  // Rebuild the option symbol from what the chain gave us.
  const row = (S._lastChain || []).find((x) => String(x.strike) === s);
  return row && row[side.toLowerCase()] ? row[side.toLowerCase()].symbol : null;
}

async function termOrder(side, action) {
  const legSym = termChainLeg(side);
  if (!legSym) { toast('Select a ' + side + ' strike first'); return; }
  const row = (S._lastChain || []).find((x) => String(x.strike) === (side === 'CE' ? S.term.ce : S.term.pe));
  const leg = row && row[side.toLowerCase()];
  const lotsize = Number(leg && leg.lotsize) || 0;
  if (!lotsize) { toast('Lot size unavailable — reload the chain'); return; }
  const qty = S.term.lots * lotsize;
  if (!confirm(action + ' ' + S.term.lots + ' lot(s) ' + legSym + ' (qty ' + qty + ') @ MARKET?')) return;
  toast('Placing order…');
  try {
    const r = await api('/order', {
      method: 'POST', timeout: 40000,
      body: { symbol: legSym, exchange: S.term.exchange, action, quantity: qty, product: S.term.product },
    });
    toast('Order: ' + (r.orderid || r.status || 'sent'));
  } catch (e) { toast('Order failed: ' + e.message, 5000); }
}
