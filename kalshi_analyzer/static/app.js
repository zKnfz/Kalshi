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
    strategyFilters: document.getElementById('strategy_filters'),
  };

  const state = {
    snapshot: null,
    filter: '',
    sortKey: 'score',
    activeStrategies: new Set(),
    knownStrategies: new Set(),
  };

  const STRATEGY_LABEL = {
    yes_no_arbitrage: 'Yes+No arb',
    dutch_book_arbitrage: 'Dutch arb',
    dutch_book_mispricing: 'Dutch mispricing',
    fair_value_yes: 'Fair-value YES',
    fair_value_no: 'Fair-value NO',
  };

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
  function timeAgo(iso) {
    if (!iso) return '—';
    const t = new Date(iso).getTime();
    const diff = Math.max(0, (Date.now() - t) / 1000);
    if (diff < 60) return `${diff.toFixed(0)}s ago`;
    if (diff < 3600) return `${(diff / 60).toFixed(0)}m ago`;
    return `${(diff / 3600).toFixed(1)}h ago`;
  }

  function setConnection(online) {
    els.conn.textContent = online ? 'live' : 'offline';
    els.conn.classList.toggle('online', online);
  }

  function renderStrategyChips() {
    const wanted = Array.from(state.knownStrategies).sort();
    const existing = new Map();
    els.strategyFilters.childNodes.forEach((node) => {
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
        if (state.activeStrategies.has(strat)) state.activeStrategies.delete(strat);
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
      if (state.activeStrategies.size && !state.activeStrategies.has(o.strategy))
        return false;
      if (!q) return true;
      const hay = `${o.title} ${o.ticker} ${o.strategy} ${o.side}`.toLowerCase();
      return hay.includes(q);
    });
  }

  function sortOps(ops) {
    const k = state.sortKey;
    return ops.slice().sort((a, b) => (b[k] ?? 0) - (a[k] ?? 0));
  }

  function cardHTML(o) {
    const sideClass = o.side.replace('+', '\\+');
    const sideBadge = `<span class="badge ${sideClass}">${o.side}</span>`;
    const strategy = STRATEGY_LABEL[o.strategy] || o.strategy;
    const conf = Math.round((o.confidence || 0) * 100);
    const arb =
      o.strategy.includes('arbitrage')
        ? `<span class="badge arb">ARB</span>`
        : '';
    return `
      <article class="card" data-ticker="${o.ticker}">
        <div class="row">
          <div>
            <div class="strategy">${strategy} ${arb}</div>
            <h3>${o.title}</h3>
            <div class="ticker">${o.ticker}</div>
          </div>
          ${sideBadge}
        </div>
        <div class="metrics">
          <div class="metric edge">
            <div class="k">Edge</div>
            <div class="v">${fmtPct(o.edge_pct, 1)}</div>
          </div>
          <div class="metric">
            <div class="k">Entry</div>
            <div class="v">${fmtCents(o.entry_price)}</div>
          </div>
          <div class="metric">
            <div class="k">Fair</div>
            <div class="v">${fmtCents(o.fair_price)}</div>
          </div>
          <div class="metric kelly">
            <div class="k">Kelly</div>
            <div class="v">${fmtPct((o.kelly_fraction || 0) * 100, 1)}</div>
          </div>
        </div>
        <div>
          <div class="k" style="font-size:0.65rem;text-transform:uppercase;color:var(--muted);letter-spacing:0.06em;">Confidence ${conf}%</div>
          <div class="bar" title="Liquidity-weighted confidence"><span style="width:${conf}%"></span></div>
        </div>
        <div class="rationale">${o.rationale}</div>
        <div class="footrow">
          <span>Stake ≈ ${fmtMoney(o.suggested_stake)}</span>
          <span>Liq ${fmtInt(o.liquidity)} · 24h vol ${fmtInt(o.volume_24h)}</span>
        </div>
        <div class="footrow">
          <span>Score ${(o.score || 0).toFixed(3)}</span>
          <a href="https://kalshi.com/markets/${encodeURIComponent(
            o.ticker
          )}" target="_blank" rel="noreferrer">Open on Kalshi ↗</a>
        </div>
      </article>
    `;
  }

  function render() {
    if (!state.snapshot) return;
    const snap = state.snapshot;
    els.source.textContent = snap.demo
      ? 'demo synthetic'
      : snap.source || 'kalshi';
    els.generated_at.textContent = timeAgo(snap.generated_at);
    const stats = snap.stats || {};
    els.counts.textContent = `${stats.events_scanned || 0} / ${
      stats.markets_scanned || 0
    }`;
    let ops = snap.opportunities || [];
    for (const o of ops) state.knownStrategies.add(o.strategy);
    renderStrategyChips();

    ops = applyFilters(ops);
    ops = sortOps(ops);
    els.oppCount.textContent = ops.length;
    if (!ops.length) {
      els.grid.innerHTML = '';
      els.empty.hidden = false;
      return;
    }
    els.empty.hidden = true;
    els.grid.innerHTML = ops.map(cardHTML).join('');
  }

  function applySnapshot(snap) {
    state.snapshot = snap;
    render();
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
        applySnapshot(JSON.parse(e.data));
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

  setInterval(() => {
    if (state.snapshot) {
      els.generated_at.textContent = timeAgo(state.snapshot.generated_at);
    }
  }, 1000);

  fetchOnce();
  connect();
})();
