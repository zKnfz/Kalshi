(() => {
  const els = {
    source: document.getElementById('source'),
    generated_at: document.getElementById('generated_at'),
    counts: document.getElementById('counts'),
    oppCount: document.getElementById('opp_count'),
    conn: document.getElementById('conn'),
    grid: document.getElementById('opps'),
    empty: document.getElementById('empty'),
    filter: document.getElementById('filter'),
    sort: document.getElementById('sort'),
    minEdge: document.getElementById('min_edge'),
    minEdgeVal: document.getElementById('min_edge_val'),
    hideInfeasible: document.getElementById('hide_infeasible'),
    strategyFilters: document.getElementById('strategy_filters'),
    toast: document.getElementById('toast'),
    execStat: document.getElementById('exec_stat'),
    execMode: document.getElementById('exec_mode'),
    paperStat: document.getElementById('paper_stat'),
    paperPnl: document.getElementById('paper_pnl'),
  };

  const state = {
    opps: new Map(),
    stats: {},
    generatedAt: null,
    source: '…',
    demo: false,
    filter: '',
    sortKey: 'score',
    minEdgePct: 2.0,
    hideInfeasible: false,
    activeStrategies: new Set(),
    knownStrategies: new Set(),
    justUpdated: new Map(),
    justAdded: new Map(),
  };

  const STRATEGY_LABEL = {
    yes_no_arbitrage: 'Yes+No arb',
    dutch_book_arbitrage: 'Dutch arb',
    dutch_book_mispricing: 'Dutch mispricing',
    fair_value_yes: 'Fair-value YES',
    fair_value_no: 'Fair-value NO',
  };

  function keyOf(o) {
    return `${o.ticker}:${o.side}`;
  }

  function fmtPct(x, digits = 1) {
    if (x === null || x === undefined || Number.isNaN(x)) return '—';
    return `${x.toFixed(digits)}%`;
  }
  function fmtMoney(x) {
    if (x === null || x === undefined) return '—';
    return `$${x.toFixed(2)}`;
  }
  function fmtCents(x) {
    if (x === null || x === undefined) return '—';
    return `${(x * 100).toFixed(1)}¢`;
  }
  function fmtInt(x) {
    if (x === null || x === undefined) return '—';
    return Intl.NumberFormat().format(x);
  }
  function fmtSeconds(s) {
    if (s === null || s === undefined) return '—';
    if (s < 60) return `${s.toFixed(0)}s`;
    if (s < 3600) return `${(s / 60).toFixed(0)}m`;
    if (s < 86400) return `${(s / 3600).toFixed(1)}h`;
    return `${(s / 86400).toFixed(1)}d`;
  }
  function timeAgo(iso) {
    if (!iso) return '—';
    const t = new Date(iso).getTime();
    const diff = Math.max(0, (Date.now() - t) / 1000);
    return fmtSeconds(diff) + ' ago';
  }

  function setConnection(online) {
    els.conn.textContent = online ? 'live' : 'offline';
    els.conn.classList.toggle('online', online);
  }

  function renderStrategyChips() {
    const wanted = Array.from(state.knownStrategies).sort();
    const existing = new Map();
    Array.from(els.strategyFilters.children).forEach((node) => {
      if (node.dataset && node.dataset.strategy)
        existing.set(node.dataset.strategy, node);
    });
    for (const strat of wanted) {
      if (existing.has(strat)) continue;
      const el = document.createElement('span');
      el.className = 'chip';
      el.dataset.strategy = strat;
      el.textContent = STRATEGY_LABEL[strat] || strat;
      el.addEventListener('click', () => {
        if (state.activeStrategies.has(strat))
          state.activeStrategies.delete(strat);
        else state.activeStrategies.add(strat);
        el.classList.toggle('active', state.activeStrategies.has(strat));
        render();
      });
      els.strategyFilters.appendChild(el);
    }
  }

  function applyFilters(ops) {
    const q = state.filter.trim().toLowerCase();
    return ops.filter((o) => {
      if (state.hideInfeasible && o.fill_feasible === false) return false;
      const cmp = (o.net_edge_pct !== undefined) ? o.net_edge_pct : o.edge_pct;
      if ((cmp ?? 0) < state.minEdgePct) return false;
      if (state.activeStrategies.size) {
        const sigSet = new Set(o.signal_types || [o.strategy]);
        let match = false;
        for (const s of state.activeStrategies)
          if (sigSet.has(s)) {
            match = true;
            break;
          }
        if (!match) return false;
      }
      if (!q) return true;
      const sigs = (o.signal_types || []).join(' ');
      const hay = `${o.title} ${o.ticker} ${o.strategy} ${sigs} ${o.side}`.toLowerCase();
      return hay.includes(q);
    });
  }

  function sortOps(ops) {
    const k = state.sortKey;
    return ops.slice().sort((a, b) => (b[k] ?? 0) - (a[k] ?? 0));
  }

  function sideClass(side) {
    return side.replace('+', '').replace(' ', '');
  }

  function signalChipsHTML(o) {
    const sigs = o.signal_types && o.signal_types.length ? o.signal_types : [o.strategy];
    return (
      `<div class="signal-chips">` +
      sigs
        .map((s) => {
          const cls = s.includes('arbitrage')
            ? 'arb'
            : s === 'dutch_book_mispricing'
            ? 'warn'
            : '';
          return `<span class="signal-chip ${cls}">${
            STRATEGY_LABEL[s] || s
          }</span>`;
        })
        .join('') +
      `</div>`
    );
  }

  function cardHTML(o) {
    const sideBadge = `<span class="badge ${sideClass(o.side)}">${o.side}</span>`;
    const conf = Math.max(0, Math.min(100, Math.round((o.confidence || 0) * 100)));
    const infeasibleClass = o.fill_feasible === false ? ' infeasible' : '';
    const ageClass =
      o.last_trade_age_seconds && o.last_trade_age_seconds > 60 ? ' stale' : '';
    const ageLabel =
      o.last_trade_age_seconds !== null && o.last_trade_age_seconds !== undefined
        ? `<span class="age-tag${ageClass}" title="Time since the last trade printed on this market.">⏱ last ${fmtSeconds(
            o.last_trade_age_seconds
          )} ago</span>`
        : '';
    const firstSeenLabel = `<span class="age-tag" title="How long the analyzer has been seeing this opportunity. Long-stale opps are usually illusory.">👁 first seen ${fmtSeconds(
      o.age_seconds || 0
    )} ago</span>`;
    return `
      <article class="card${infeasibleClass}" data-key="${keyOf(o)}">
        <div class="row">
          <div>
            ${signalChipsHTML(o)}
            <h3>${o.title}</h3>
            <div class="ticker">${o.ticker}</div>
          </div>
          ${sideBadge}
        </div>
        <div class="metrics">
          <div class="metric edge">
            <div class="k">Edge / Net</div>
            <div class="v">${fmtPct(o.edge_pct, 1)} <span style="opacity:0.7;font-size:0.75rem">/ ${fmtPct(o.net_edge_pct || 0, 1)}</span></div>
          </div>
          <div class="metric">
            <div class="k">Entry · Fee</div>
            <div class="v">${fmtCents(o.entry_price)} <span style="opacity:0.6;font-size:0.7rem">−${(o.fees_per_contract*100||0).toFixed(2)}¢</span></div>
          </div>
          <div class="metric">
            <div class="k">Fair</div>
            <div class="v">${fmtCents(o.fair_price)}</div>
          </div>
          <div class="metric kelly">
            <div class="k">Kelly · Stake</div>
            <div class="v">${fmtPct((o.kelly_fraction || 0) * 100, 1)} <span style="opacity:0.7;font-size:0.7rem">${fmtMoney(o.suggested_stake)}</span></div>
          </div>
        </div>
        <div>
          <div class="k" style="font-size:0.65rem;text-transform:uppercase;color:var(--muted);letter-spacing:0.06em;">Confidence ${conf}%</div>
          <div class="bar" title="Liquidity-, volume-, spread- and freshness-weighted confidence"><span style="width:${conf}%"></span></div>
        </div>
        <div class="rationale">${o.rationale}</div>
        <div class="footrow">
          <span>Stake ≈ ${fmtMoney(o.suggested_stake)}</span>
          <span>Liq ${fmtInt(o.liquidity)} · 24h vol ${fmtInt(o.volume_24h)}</span>
        </div>
        <div class="footrow">
          ${ageLabel}
          ${firstSeenLabel}
        </div>
        <div class="footrow">
          <button class="copy-btn" data-copy='${escapeAttr(JSON.stringify({
            ticker: o.ticker,
            side: o.side,
            entry_price: o.entry_price,
            fair_price: o.fair_price,
            edge_pct: o.edge_pct,
            suggested_stake: o.suggested_stake,
            kelly_fraction: o.kelly_fraction,
            signal_types: o.signal_types,
          }))}'>Copy trade params</button>
          <a href="https://kalshi.com/markets/${encodeURIComponent(
            o.ticker
          )}" target="_blank" rel="noreferrer">Open on Kalshi ↗</a>
        </div>
      </article>
    `;
  }

  function escapeAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/'/g, '&apos;').replace(/"/g, '&quot;');
  }

  function showToast(msg) {
    els.toast.textContent = msg;
    els.toast.hidden = false;
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => {
      els.toast.hidden = true;
    }, 1600);
  }

  function attachCopyHandlers() {
    els.grid.querySelectorAll('.copy-btn').forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = '1';
      btn.addEventListener('click', () => {
        try {
          const params = JSON.parse(btn.dataset.copy);
          const text = [
            `Ticker:  ${params.ticker}`,
            `Side:    ${params.side}`,
            `Entry:   ${(params.entry_price * 100).toFixed(1)}¢`,
            `Fair:    ${(params.fair_price * 100).toFixed(1)}¢`,
            `Edge:    ${params.edge_pct.toFixed(2)}%`,
            `Kelly:   ${(params.kelly_fraction * 100).toFixed(2)}%`,
            `Stake:   $${params.suggested_stake.toFixed(2)}`,
            `Signals: ${(params.signal_types || []).join(', ')}`,
          ].join('\n');
          navigator.clipboard
            .writeText(text)
            .then(() => showToast('Trade params copied'))
            .catch(() => showToast('Clipboard blocked — params:\n' + text));
        } catch (e) {
          showToast('Copy failed');
        }
      });
    });
  }

  function render() {
    const opsArr = Array.from(state.opps.values());
    for (const o of opsArr)
      (o.signal_types || []).forEach((s) => state.knownStrategies.add(s));
    renderStrategyChips();

    const stats = state.stats || {};
    els.source.textContent = state.demo ? 'demo synthetic' : state.source || 'kalshi';
    els.generated_at.textContent = timeAgo(state.generatedAt);
    els.counts.textContent = `${stats.events_scanned || 0} / ${
      stats.markets_scanned || 0
    }`;
    const exec = stats.execution;
    if (exec) {
      els.execStat.hidden = false;
      const mode = exec.mode || 'off';
      const ks = exec.kill_switch ? ' (KILL)' : '';
      els.execMode.textContent = `${mode}${ks}`;
      els.execMode.style.color = exec.kill_switch ? 'var(--danger)' :
        mode === 'live' ? 'var(--warn)' : mode === 'paper' ? 'var(--accent)' : 'var(--muted)';
    }
    const paper = stats.paper_pnl;
    if (paper && paper.equity !== undefined) {
      els.paperStat.hidden = false;
      const pnl = (paper.realized_pnl || 0) + (paper.unrealized_pnl || 0);
      els.paperPnl.textContent = `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`;
      els.paperPnl.style.color = pnl >= 0 ? 'var(--accent)' : 'var(--danger)';
    }

    let filtered = applyFilters(opsArr);
    filtered = sortOps(filtered);
    els.oppCount.textContent = filtered.length;
    if (!filtered.length) {
      els.grid.innerHTML = '';
      els.empty.hidden = false;
      return;
    }
    els.empty.hidden = true;
    els.grid.innerHTML = filtered.map(cardHTML).join('');
    const now = Date.now();
    els.grid.querySelectorAll('.card').forEach((card) => {
      const key = card.dataset.key;
      const addedAt = state.justAdded.get(key);
      const updatedAt = state.justUpdated.get(key);
      if (addedAt && now - addedAt < 1500) card.classList.add('new');
      else if (updatedAt && now - updatedAt < 1500) card.classList.add('updated');
    });
    attachCopyHandlers();
  }

  function applySnapshot(snap) {
    state.opps.clear();
    state.knownStrategies.clear();
    state.justAdded.clear();
    state.justUpdated.clear();
    for (const o of snap.opportunities || []) {
      state.opps.set(keyOf(o), o);
    }
    state.stats = snap.stats || {};
    state.generatedAt = snap.generated_at;
    state.source = snap.source;
    state.demo = !!snap.demo;
    render();
  }

  function applyDelta(delta) {
    const now = Date.now();
    for (const o of delta.added || []) {
      state.opps.set(keyOf(o), o);
      state.justAdded.set(keyOf(o), now);
    }
    for (const o of delta.updated || []) {
      state.opps.set(keyOf(o), o);
      state.justUpdated.set(keyOf(o), now);
    }
    for (const key of delta.removed || []) {
      state.opps.delete(key);
      state.justUpdated.delete(key);
      state.justAdded.delete(key);
    }
    state.stats = delta.stats || state.stats;
    state.generatedAt = delta.generated_at || state.generatedAt;
    render();
  }

  function applyHeartbeat(hb) {
    state.stats = hb.stats || state.stats;
    state.generatedAt = hb.generated_at || state.generatedAt;
    els.generated_at.textContent = timeAgo(state.generatedAt);
  }

  async function fetchOnce() {
    try {
      const res = await fetch('/api/opportunities');
      if (!res.ok) return;
      const data = await res.json();
      applySnapshot(data);
    } catch (_) {}
  }

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => setConnection(true);
    ws.onclose = () => {
      setConnection(false);
      setTimeout(connect, 2000);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (e) => {
      try {
        const m = JSON.parse(e.data);
        if (m.type === 'snapshot') applySnapshot(m.snapshot);
        else if (m.type === 'delta') applyDelta(m);
        else if (m.type === 'heartbeat') applyHeartbeat(m);
        else if (m.opportunities) applySnapshot(m);
      } catch (_) {}
    };
  }

  els.filter.addEventListener('input', (e) => {
    state.filter = e.target.value;
    render();
  });
  els.sort.addEventListener('change', (e) => {
    state.sortKey = e.target.value;
    render();
  });
  els.minEdge.addEventListener('input', (e) => {
    state.minEdgePct = parseFloat(e.target.value);
    els.minEdgeVal.textContent = `${state.minEdgePct.toFixed(1)}%`;
    render();
  });
  els.hideInfeasible.addEventListener('change', (e) => {
    state.hideInfeasible = e.target.checked;
    render();
  });

  setInterval(() => {
    if (state.generatedAt) {
      els.generated_at.textContent = timeAgo(state.generatedAt);
      render();
    }
  }, 5000);

  fetchOnce();
  connect();
})();
