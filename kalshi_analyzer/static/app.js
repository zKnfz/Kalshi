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
    sportsSidebar: document.getElementById('sports_sidebar'),
    sportsLiveList: document.getElementById('sports_live_list'),
  };

  const state = {
    opps: new Map(),
    baskets: [],
    sportsLive: [],
    stats: {},
    generatedAt: null,
    source: '…',
    demo: false,
    filter: '',
    sortKey: 'score',
    minEdgePct: 2.0,
    hideInfeasible: false,
    activeStrategies: new Set(),
    sportsFilter: false,
    knownStrategies: new Set(),
    collapsedBaskets: new Set(),
    justUpdated: new Map(),
    justAdded: new Map(),
  };

  const STRATEGY_LABEL = {
    yes_no_arbitrage: 'Yes+No arb',
    dutch_book_arbitrage: 'Dutch arb',
    dutch_book_mispricing: 'Dutch mispricing',
    fair_value_yes: 'Fair-value YES',
    fair_value_no: 'Fair-value NO',
    sports_model_edge: 'Sports model',
  };

  function sportIcon(o) {
    const src = (o.series_ticker || o.ticker || o.extra?.sport || o.sport || '').toUpperCase();
    if (src.includes('TEN') || src.includes('ATP') || src.includes('WTA') || src.includes('KXTEN')) return '🎾';
    if (src.includes('NBA') || src.includes('WNBA') || src.includes('CBB') || src.includes('NCAA')) return '🏀';
    if (src.includes('NFL') || src.includes('CFB') || src.includes('KXNFL')) return '🏈';
    if (src.includes('MLB') || src.includes('KXMLB')) return '⚾';
    if (src.includes('NHL') || src.includes('KXNHL')) return '🏒';
    if (src.includes('MMA') || src.includes('UFC') || src.includes('KXMMA')) return '🥊';
    if (src.includes('GOLF') || src.includes('PGA') || src.includes('LPGA')) return '⛳';
    if (src.includes('SOC') || src.includes('MLS') || src.includes('EPL') || src.includes('WC') || src.includes('FIFA') || src.includes('UCL')) return '⚽';
    if (src.includes('CS2') || src.includes('VAL') || src.includes('LOL') || src.includes('DOTA') || src.includes('ESPORT')) return '🎮';
    if (src.includes('F1') || src.includes('RACING')) return '🏎';
    if (o.is_sports) return '🏟';
    return '';
  }

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

  function isInfeasible(o) {
    if (o.fill_feasible === false) return true;
    if ((o.liquidity ?? 0) <= 0) return true;
    if (o.basket_complete === false) return true;
    return false;
  }

  function setConnection(online) {
    els.conn.textContent = online ? 'live' : 'offline';
    els.conn.classList.toggle('online', online);
  }

  function ensureSportsChip() {
    let chip = document.getElementById('sports_filter_chip');
    if (chip) return chip;
    chip = document.createElement('span');
    chip.id = 'sports_filter_chip';
    chip.className = 'chip sports';
    chip.textContent = 'Sports';
    chip.addEventListener('click', () => {
      state.sportsFilter = !state.sportsFilter;
      chip.classList.toggle('active', state.sportsFilter);
      render();
    });
    els.strategyFilters.appendChild(chip);
    return chip;
  }

  function renderStrategyChips() {
    ensureSportsChip();
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
      if (state.hideInfeasible && isInfeasible(o)) return false;
      const cmp = o.net_edge_pct !== undefined ? o.net_edge_pct : o.edge_pct;
      if ((cmp ?? 0) < state.minEdgePct) return false;
      if (state.sportsFilter && !o.is_sports) return false;
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
      const hay = `${o.title} ${o.ticker} ${o.strategy} ${sigs} ${o.side} ${o.series_ticker || ''}`.toLowerCase();
      return hay.includes(q);
    });
  }

  function applyBasketFilters(baskets) {
    return baskets.filter((b) => {
      if (state.sportsFilter && !b.is_sports) return false;
      if (state.hideInfeasible) {
        if (b.basket_complete === false) return false;
        if ((b.worst_liquidity ?? 0) <= 0) return false;
      }
      const cmp = b.net_edge_pct !== undefined ? b.net_edge_pct : b.edge_pct;
      if ((cmp ?? 0) < state.minEdgePct) return false;
      if (state.activeStrategies.size && !state.activeStrategies.has(b.strategy))
        return false;
      if (!state.filter.trim()) return true;
      const q = state.filter.trim().toLowerCase();
      const hay = `${b.basket_id} ${b.series_ticker || ''} ${b.event_ticker || ''} ${b.strategy}`.toLowerCase();
      return hay.includes(q);
    });
  }

  function liveRank(o) {
    if (o.live_status === 'LIVE') return 0;
    if (o.live_status === 'TODAY') return 1;
    return 2;
  }

  function sortOps(ops) {
    const k = state.sortKey;
    const sorted = ops.slice().sort((a, b) => {
      if (state.sportsFilter) {
        const lr = liveRank(a) - liveRank(b);
        if (lr !== 0) return lr;
      }
      return (b[k] ?? 0) - (a[k] ?? 0);
    });
    return sorted;
  }

  function sortBaskets(baskets) {
    return baskets.slice().sort((a, b) => {
      if (state.sportsFilter) {
        const liveOrder = { LIVE: 0, TODAY: 1 };
        const la = liveOrder[a.live_status] ?? 2;
        const lb = liveOrder[b.live_status] ?? 2;
        if (la !== lb) return la - lb;
      }
      return (b.net_edge_pct ?? 0) - (a.net_edge_pct ?? 0);
    });
  }

  function sideClass(side) {
    return side.replace('+', '').replace(' ', '');
  }

  function liveBadgeHTML(status) {
    if (status === 'LIVE') return '<span class="badge-warn badge-live">🔴 LIVE</span>';
    if (status === 'TODAY') return '<span class="badge-warn badge-today">⚡ TODAY</span>';
    return '';
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
      (o.basket_complete === false
        ? '<span class="signal-chip warn">⚠ Incomplete basket</span>'
        : '') +
      liveBadgeHTML(o.live_status) +
      `</div>`
    );
  }

  function modelEdgeHTML(o) {
    if (o.strategy !== 'sports_model_edge' && !(o.signal_types || []).includes('sports_model_edge'))
      return '';
    const model = (o.model_yes_prob ?? o.fair_price ?? 0) * 100;
    const kalshi = (o.entry_price ?? 0) * 100;
    const edge = o.edge_pct ?? 0;
    return `<div class="model-edge-row">Model: ${model.toFixed(0)}% | Kalshi: ${kalshi.toFixed(0)}% | Edge: ${edge.toFixed(1)}%</div>`;
  }

  function cardHTML(o) {
    const icon = sportIcon(o);
    const sideBadge = `<span class="badge ${sideClass(o.side)}">${o.side}</span>`;
    const conf = Math.max(0, Math.min(100, Math.round((o.confidence || 0) * 100)));
    const infeasibleClass = isInfeasible(o) ? ' infeasible' : '';
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
            <h3>${icon ? `<span class="sport-icon">${icon}</span>` : ''}${o.title}</h3>
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
        ${modelEdgeHTML(o)}
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

  function basketLegHTML(leg) {
    return `<div class="card in-basket${isInfeasible(leg) ? ' infeasible' : ''}">${cardHTML(leg).replace(/^<article[^>]*>/, '').replace(/<\/article>$/, '')}</div>`;
  }

  function basketHTML(b) {
    const collapsed = state.collapsedBaskets.has(b.basket_id);
    const incompleteClass = b.basket_complete === false ? ' incomplete' : '';
    const legsJson = escapeAttr(JSON.stringify(b.legs || []));
    return `
      <section class="basket-card${incompleteClass}" data-basket="${escapeAttr(b.basket_id)}">
        <div class="basket-header" data-toggle-basket="${escapeAttr(b.basket_id)}">
          <div>
            <div class="signal-chips">
              <span class="signal-chip arb">${STRATEGY_LABEL[b.strategy] || b.strategy}</span>
              ${b.basket_complete === false ? '<span class="badge-warn">⚠ Incomplete basket</span>' : ''}
              ${liveBadgeHTML(b.live_status)}
            </div>
            <h3>Basket · ${b.series_ticker || b.event_ticker || b.basket_id}</h3>
            <div class="ticker">${b.basket_id}</div>
          </div>
          <span class="badge YES">${b.legs.length} legs</span>
        </div>
        <div class="basket-summary">
          <span>Edge / Net: ${fmtPct(b.edge_pct, 1)} / ${fmtPct(b.net_edge_pct, 1)}</span>
          <span>Total stake: ${fmtMoney(b.total_stake)}</span>
          <span>Worst liq: ${fmtInt(b.worst_liquidity)}</span>
        </div>
        <div class="basket-actions">
          <button class="copy-btn" data-copy-basket='${legsJson}'>Copy basket params</button>
          <button class="copy-btn execute-btn" data-exec-basket='${escapeAttr(JSON.stringify({ basket_id: b.basket_id, legs: b.legs }))}'>Execute basket</button>
        </div>
        <div class="basket-legs${collapsed ? ' collapsed' : ''}">
          ${(b.legs || []).map(basketLegHTML).join('')}
        </div>
      </section>
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

  function sportsLiveCardHTML(g) {
    const gs = g.game_state || g;
    const modelPct = (g.model_yes_prob ?? 0) * 100;
    const kalshiPct = (g.kalshi_yes_ask ?? 0) * 100;
    const edge = g.edge_pct ?? 0;
    const icon = sportIcon({ series_ticker: g.sport, ticker: g.kalshi_ticker, is_sports: true });
    return `
      <div class="sports-live-card">
        <div class="teams">${icon} ${g.away_team || '?'} @ ${g.home_team || '?'}</div>
        <div class="scoreline">${g.away_team}: ${gs.away_score ?? 0} · ${g.home_team}: ${gs.home_score ?? 0} · ${gs.clock || ''} P${gs.period || '?'}</div>
        ${gs.is_live ? '<span class="badge-warn badge-live">🔴 LIVE</span>' : ''}
        <div class="prob-bar-wrap">
          <div class="prob-bar-labels"><span>Model ${modelPct.toFixed(0)}%</span><span>Kalshi ${kalshiPct.toFixed(0)}%</span></div>
          <div class="prob-bar">
            <span class="model" style="width:${Math.min(100, modelPct)}%"></span>
            <span class="kalshi" style="left:${Math.min(100, kalshiPct)}%"></span>
          </div>
        </div>
        <div class="model-edge-row">Edge: ${edge.toFixed(1)}%</div>
        <a href="https://kalshi.com/markets/${encodeURIComponent(g.kalshi_ticker || '')}" target="_blank" rel="noreferrer">Open on Kalshi ↗</a>
      </div>
    `;
  }

  function renderSportsSidebar() {
    const games = state.sportsLive || [];
    if (!games.length) {
      els.sportsSidebar.hidden = true;
      els.sportsLiveList.innerHTML = '';
      return;
    }
    els.sportsSidebar.hidden = false;
    els.sportsLiveList.innerHTML = games.map(sportsLiveCardHTML).join('');
  }

  function attachCopyHandlers() {
    els.grid.querySelectorAll('.copy-btn[data-copy]').forEach((btn) => {
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
            .catch(() => showToast('Clipboard blocked'));
        } catch (e) {
          showToast('Copy failed');
        }
      });
    });

    els.grid.querySelectorAll('.copy-btn[data-copy-basket]').forEach((btn) => {
      if (btn.dataset.boundBasket) return;
      btn.dataset.boundBasket = '1';
      btn.addEventListener('click', () => {
        try {
          const legs = JSON.parse(btn.dataset.copyBasket);
          const text = legs
            .map(
              (l) =>
                `${l.ticker}\t${l.side}\t${(l.entry_price * 100).toFixed(1)}¢\t$${(l.suggested_stake || 0).toFixed(2)}`
            )
            .join('\n');
          navigator.clipboard
            .writeText(text)
            .then(() => showToast('Basket params copied'))
            .catch(() => showToast('Clipboard blocked'));
        } catch (e) {
          showToast('Copy failed');
        }
      });
    });

    els.grid.querySelectorAll('.execute-btn[data-exec-basket]').forEach((btn) => {
      if (btn.dataset.boundExec) return;
      btn.dataset.boundExec = '1';
      btn.addEventListener('click', async () => {
        try {
          const payload = JSON.parse(btn.dataset.execBasket);
          const res = await fetch('/api/execute-basket', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              basket_id: payload.basket_id,
              legs: (payload.legs || []).map((l) => ({
                ticker: l.ticker,
                side: l.side,
                limit_price: l.entry_price,
                contracts: Math.max(1, Math.round((l.suggested_stake || 0) / Math.max(l.entry_price, 0.01))),
                liquidity: l.liquidity,
              })),
            }),
          });
          const data = await res.json();
          showToast(
            data.accepted
              ? `Basket executed (${data.legs?.length || 0} legs)`
              : `Basket rejected: ${data.rejection_reason || data.error || 'unknown'}`
          );
        } catch (e) {
          showToast('Execute failed');
        }
      });
    });

    els.grid.querySelectorAll('[data-toggle-basket]').forEach((node) => {
      if (node.dataset.boundToggle) return;
      node.dataset.boundToggle = '1';
      node.addEventListener('click', () => {
        const id = node.dataset.toggleBasket;
        if (state.collapsedBaskets.has(id)) state.collapsedBaskets.delete(id);
        else state.collapsedBaskets.add(id);
        render();
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

    const basketIds = new Set(
      (state.baskets || []).map((b) => b.basket_id).filter(Boolean)
    );
    const standaloneOps = opsArr.filter((o) => {
      if (!o.basket_id) return true;
      if (!basketIds.has(o.basket_id)) return true;
      return !['dutch_book_arbitrage', 'dutch_book_mispricing'].includes(
        o.strategy
      );
    });

    let filteredOps = applyFilters(standaloneOps);
    filteredOps = sortOps(filteredOps);

    let filteredBaskets = applyBasketFilters(state.baskets || []);
    filteredBaskets = sortBaskets(filteredBaskets);

    const totalVisible = filteredOps.length + filteredBaskets.length;
    els.oppCount.textContent = totalVisible;
    if (!totalVisible) {
      els.grid.innerHTML = '';
      els.empty.hidden = false;
      return;
    }
    els.empty.hidden = true;
    els.grid.innerHTML =
      filteredBaskets.map(basketHTML).join('') +
      filteredOps.map(cardHTML).join('');
    const now = Date.now();
    els.grid.querySelectorAll('.card').forEach((card) => {
      const key = card.dataset.key;
      if (!key) return;
      const addedAt = state.justAdded.get(key);
      const updatedAt = state.justUpdated.get(key);
      if (addedAt && now - addedAt < 1500) card.classList.add('new');
      else if (updatedAt && now - updatedAt < 1500) card.classList.add('updated');
    });
    attachCopyHandlers();
    renderSportsSidebar();
  }

  function applySnapshot(snap) {
    state.opps.clear();
    state.knownStrategies.clear();
    state.justAdded.clear();
    state.justUpdated.clear();
    for (const o of snap.opportunities || []) {
      state.opps.set(keyOf(o), o);
    }
    state.baskets = snap.baskets || [];
    state.sportsLive = snap.sports_live || [];
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
    if (delta.baskets) state.baskets = delta.baskets;
    if (delta.sports_live) state.sportsLive = delta.sports_live;
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
    }
  }, 5000);

  fetchOnce();
  connect();
})();
