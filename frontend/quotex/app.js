// AUROX AI (Quotex terminal) — front end. Talks to the same backend API; analysis only, no trading.
(() => {
    const isLocal = ['localhost', '127.0.0.1'].includes(location.hostname);
    // served at /quotex/ behind the site gateway -> /quotex-api ; served by the backend itself -> same origin
    const API = window.API_BASE_URL ?? (location.pathname.startsWith('/quotex/') ? '/quotex-api' : (isLocal ? '' : '/api'));
    const $ = id => document.getElementById(id);
    const KEY_USER = 'pulseqx_user';
    const KEY_TOKEN = 'pulseqx_token';
    const IDLE_MS = 30 * 60 * 1000;

    const state = {
        user: null,
        tz: 'Asia/Kolkata',
        tzExplicit: false,
        tzCatalog: [],
        offset: 0,                // server clock − local clock (ms)
        signal: null,
        points: [],
        timers: [],
        idle: null,
    };

    // ───────── helpers ─────────
    const now = () => Date.now() + state.offset;
    const safe = (fn, fallback) => { try { return fn(); } catch { return fallback; } };

    function fmtTime(ms) {
        return safe(() => new Intl.DateTimeFormat('en-GB', {
            timeZone: state.tz, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
        }).format(ms), new Date(ms).toTimeString().slice(0, 8));
    }
    function prettyPair(sym) {
        const m = /^([A-Z]{3})([A-Z]{3})(_otc)?$/i.exec(sym);
        return m ? `${m[1]}/${m[2]}`.toUpperCase() + (m[3] ? ' (OTC)' : '') : sym;
    }
    function decimals(p) { return p >= 1000 ? 2 : p >= 100 ? 3 : 5; }
    function setNotice(text, kind = '') { const n = $('notice'); n.textContent = text; n.className = `notice ${kind}`; }
    async function getJSON(path, opts = {}) {
        const token = safe(() => sessionStorage.getItem(KEY_TOKEN));
        if (token) opts = { ...opts, headers: { ...(opts.headers || {}), Authorization: `Bearer ${token}` } };
        const res = await fetch(`${API}${path}`, opts);
        const body = await res.json().catch(() => ({}));
        if (res.status === 401 && token && state.user) {
            signOut(true, /another device/i.test(body.detail || ''));
            throw new Error(body.detail || 'Session expired');
        }
        if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
        return body;
    }

    // ───────── clock & server time ─────────
    async function syncTime() {
        try {
            const t0 = Date.now();
            const d = await getJSON('/server-time');
            const t1 = Date.now();
            state.offset = d.unix_timestamp * 1000 + 500 - (t0 + t1) / 2;   // server sends whole seconds
        } catch { /* keep previous offset */ }
    }
    function tickClock() {
        $('clock').textContent = fmtTime(now());
        renderTimer();
    }

    // ───────── feed status / watchlist / correlations ─────────
    async function refreshFeed() {
        try {
            const [s, p] = await Promise.all([getJSON('/status'), getJSON('/pairs')]);
            const live = s.cache_valid;
            $('feedDot').className = `dot ${live ? 'ok' : (s.connected ? '' : 'bad')}`;
            $('feedText').textContent = live ? 'Live' : (s.connected ? 'Catching up…' : 'Reconnecting…');
            $('feedMeta').textContent = live ? `updated ${s.cache_age_seconds}s ago` : (s.last_error || 'waiting for Quotex');
            $('scanBtn').disabled = !live;
            if (!state.signal) setNotice(live ? 'Feed is live. Press Scan market for the next candle call.' : 'Waiting for the data feed… it can take a few minutes after a restart.', live ? 'ok' : 'warn');
            renderWatchlist(p.assets || []);
        } catch {
            $('feedDot').className = 'dot bad';
            $('feedText').textContent = 'Backend offline';
            $('feedMeta').textContent = 'cannot reach the API';
            $('scanBtn').disabled = true;
        }
    }
    function renderWatchlist(assets) {
        $('pairCount').textContent = assets.length;
        const picked = state.signal?.pair;
        const ul = $('watchlist');
        ul.textContent = '';
        assets.map(prettyPair).sort().forEach(name => {
            const li = document.createElement('li');
            li.textContent = name;
            if (name === picked) li.className = 'picked';
            ul.appendChild(li);
        });
    }
    async function refreshCorrelations() {
        try {
            const d = await getJSON('/correlations');
            const top = [...(d.links || [])].sort((a, b) => Math.abs(b.value) - Math.abs(a.value)).slice(0, 6);
            const ul = $('corrList');
            ul.textContent = '';
            top.forEach(l => {
                const li = document.createElement('li');
                const pct = Math.round(Math.abs(l.value) * 50);
                li.innerHTML = `<div class="row"><span>${prettyPair(l.source)} · ${prettyPair(l.target)}</span><b>${l.value >= 0 ? '+' : ''}${l.value.toFixed(2)}</b></div>` +
                    `<div class="track"><i class="fill ${l.value >= 0 ? 'pos' : 'neg'}" style="width:${pct}%"></i></div>`;
                ul.appendChild(li);
            });
            $('corrEmpty').hidden = top.length > 0;
        } catch { /* silent */ }
    }

    // ───────── signal ─────────
    async function scan() {
        const btn = $('scanBtn');
        btn.disabled = true; $('scanSpin').hidden = false; $('scanLabel').textContent = 'Scanning…';
        try {
            await syncTime();
            const s = await getJSON('/signal');
            state.signal = s;
            state.points = s.chart_candles || [];
            renderSignal();
            setNotice('Call generated. Enter on the next candle open and do not chase it after the entry time.', 'ok');
            refreshFeed();
        } catch (e) {
            setNotice(e.message || 'Could not generate a signal.', 'err');
        } finally {
            $('scanSpin').hidden = true; $('scanLabel').textContent = 'Scan market';
            btn.disabled = false;
        }
    }

    function renderSignal() {
        const s = state.signal;
        $('empty').hidden = true;
        $('ticket').hidden = false;
        $('ticketSide').hidden = false;
        const buy = s.direction === 'BUY';
        $('tPair').textContent = s.pair;
        $('tPrice').textContent = Number(s.current_price).toFixed(decimals(s.current_price));
        $('tCall').className = `call ${buy ? 'buy' : 'sell'}`;
        $('tDir').textContent = s.direction;
        $('tDirSub').textContent = buy ? 'Price expected up' : 'Price expected down';
        $('tGen').textContent = fmtTime(Date.parse(s.generated_timestamp));
        $('tEntry').textContent = fmtTime(Date.parse(s.entry_timestamp));
        $('tExpiry').textContent = fmtTime(Date.parse(s.expiry_timestamp));
        $('tEntry2').textContent = $('tEntry').textContent;
        $('tExpiry2').textContent = $('tExpiry').textContent;
        $('tConf').textContent = `${s.confidence.toFixed(1)}%`;
        const seg = $('segments');
        seg.className = `segments ${buy ? 'buy' : 'sell'}`;
        seg.textContent = '';
        for (let i = 0; i < 10; i++) { const el = document.createElement('i'); if (i < Math.round(s.confidence / 10)) el.className = 'on'; seg.appendChild(el); }
        [['Rsi', 'osc_rsi'], ['Ema', 'osc_bollinger'], ['Macd', 'osc_stoch_rsi']].forEach(([id, key]) => {
            const v = s.indicator_scores?.[key] ?? 0;
            $(`v${id}`).style.width = `${Math.max(0, Math.min(100, v))}%`;
            $(`v${id}N`).textContent = Math.round(v);
        });
        document.querySelectorAll('#watchlist li').forEach(li => li.classList.toggle('picked', li.textContent === s.pair));
        drawChart();
        renderTimer();
    }

    function renderTimer() {
        const s = state.signal;
        if (!s) return;
        const entry = Date.parse(s.entry_timestamp), expiry = Date.parse(s.expiry_timestamp), gen = Date.parse(s.generated_timestamp);
        const t = now();
        let label, value, frac;
        if (t < entry) { label = 'Time to entry'; value = entry - t; frac = 1 - (entry - t) / Math.max(1, entry - gen); }
        else if (t < expiry) { label = 'Trade window · expires in'; value = expiry - t; frac = 1 - (expiry - t) / (expiry - entry); }
        else { label = 'Expired, scan again'; value = 0; frac = 1; }
        const sec = Math.ceil(value / 1000);
        $('timerLabel').textContent = label;
        $('timerValue').textContent = `${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(sec % 60).padStart(2, '0')}`;
        $('timerBar').style.width = `${Math.max(0, Math.min(1, frac)) * 100}%`;
    }

    // ───────── chart ─────────
    function drawChart() {
        const cv = $('chart'), pts = state.points;
        if (!pts.length) return;
        const dpr = Math.min(window.devicePixelRatio || 1, 2), w = cv.clientWidth, h = cv.clientHeight;
        cv.width = w * dpr; cv.height = h * dpr;
        const c = cv.getContext('2d'); c.scale(dpr, dpr); c.clearRect(0, 0, w, h);
        const vals = pts.map(p => p.close);
        let lo = Math.min(...vals), hi = Math.max(...vals);
        const pad = (hi - lo || Math.abs(hi) * 1e-4 || 1) * 0.15; lo -= pad; hi += pad;
        const padR = 64, x = i => (i / (pts.length - 1 || 1)) * (w - padR), y = v => h - 8 - ((v - lo) / (hi - lo)) * (h - 16);
        const styles = getComputedStyle(document.documentElement);
        const ink = styles.getPropertyValue('--ink').trim(), line = styles.getPropertyValue('--line').trim();
        const buy = state.signal?.direction === 'BUY';
        const tone = styles.getPropertyValue(buy ? '--buy' : '--sell').trim();

        c.strokeStyle = line; c.lineWidth = 1; c.font = '11px "IBM Plex Mono", monospace'; c.fillStyle = '#8a92a3'; c.textAlign = 'left';
        for (let g = 0; g <= 3; g++) {
            const v = lo + ((hi - lo) * g) / 3, gy = y(v);
            c.beginPath(); c.moveTo(0, gy); c.lineTo(w - padR, gy); c.stroke();
            c.fillText(v.toFixed(decimals(v)), w - padR + 8, gy + 4);
        }
        const grad = c.createLinearGradient(0, 0, 0, h);
        grad.addColorStop(0, tone + '33'); grad.addColorStop(1, tone + '00');
        c.beginPath(); pts.forEach((p, i) => (i ? c.lineTo(x(i), y(p.close)) : c.moveTo(x(i), y(p.close))));
        c.lineTo(x(pts.length - 1), h); c.lineTo(0, h); c.closePath(); c.fillStyle = grad; c.fill();
        c.beginPath(); pts.forEach((p, i) => (i ? c.lineTo(x(i), y(p.close)) : c.moveTo(x(i), y(p.close))));
        c.strokeStyle = ink; c.lineWidth = 1.8; c.lineJoin = 'round'; c.stroke();
        const lx = x(pts.length - 1), ly = y(vals[vals.length - 1]);
        c.beginPath(); c.arc(lx, ly, 5, 0, 7); c.fillStyle = tone; c.fill(); c.lineWidth = 2; c.strokeStyle = '#000'; c.stroke();
        cv._geom = { x, y, w, padR };
    }
    function initChartHover() {
        const cv = $('chart'), tip = $('tip');
        cv.addEventListener('pointermove', e => {
            const pts = state.points, g = cv._geom;
            if (!pts.length || !g) return;
            const r = cv.getBoundingClientRect(), mx = e.clientX - r.left;
            const i = Math.max(0, Math.min(pts.length - 1, Math.round((mx / (g.w - g.padR)) * (pts.length - 1))));
            tip.hidden = false;
            tip.style.left = `${g.x(i)}px`; tip.style.top = `${g.y(pts[i].close) - 10}px`;
            tip.textContent = `${fmtTime(pts[i].time * 1000).slice(0, 5)} · ${pts[i].close.toFixed(decimals(pts[i].close))}`;
        });
        cv.addEventListener('pointerleave', () => { tip.hidden = true; });
        cv.addEventListener('pointercancel', () => { tip.hidden = true; });
        window.addEventListener('resize', () => { if (state.signal) drawChart(); });
    }

    // ───────── AI market monitor ─────────
    // An animated scope: draws the real price, Bollinger bands and RSI of one pair at a time, sweeps a scan beam
    // across them, and settles on the engine's readings. Series come from /monitor, so every number is live.
    const MON_ROWS = 8, MON_CYCLE = 7.5;
    const mon = { pairs: [], cycle: 0, active: 0, start: 0, cur: null, raf: 0, phase: -1, shown: {}, flickerAt: 0, flick: [50, 0.5, 0.5], size: '' };
    const clamp01 = x => Math.max(0, Math.min(1, x));
    const ease = x => 1 - Math.pow(1 - clamp01(x), 3);
    const reduceMotion = () => safe(() => matchMedia('(prefers-reduced-motion: reduce)').matches, false);

    async function refreshMonitor() {
        try {
            const d = await getJSON('/monitor');
            mon.pairs = (d.pairs || []).filter(p => p.ready && p.series).slice(0, MON_ROWS);
            mon.total = d.pairs?.length || 0;
            mon.minCandles = d.min_candles;
            mon.cycle++;
            $('monCycle').textContent = `cycle ${mon.cycle}`;
            $('monCount').textContent = `${mon.total} pairs watched · ${mon.pairs.length} on the scope`;
            $('monEmpty').hidden = mon.pairs.length > 0;
            if (!mon.cur && mon.pairs.length) beginPair(0);
            renderQueue();
        } catch { /* keep the last readings on screen */ }
    }
    function stochAt(rsi, i) {
        const w = rsi.slice(Math.max(0, i - 13), i + 1), lo = Math.min(...w), hi = Math.max(...w);
        return w.length < 3 || hi === lo ? 0.5 : (rsi[i] - lo) / (hi - lo);
    }
    function beginPair(i) {
        if (!mon.pairs.length) { mon.cur = null; return; }
        mon.active = i % mon.pairs.length;
        mon.cur = mon.pairs[mon.active];
        mon.start = performance.now();
        mon.phase = -1;
        $('monPair').textContent = prettyPair(mon.cur.symbol);
        $('monPrice').textContent = ` ${mon.cur.price ? Number(mon.cur.price).toFixed(decimals(mon.cur.price)) : ''}`;
        renderQueue();
    }
    function renderQueue() {
        const q = $('monQueue');
        if (q.children.length !== mon.pairs.length || [...q.children].some((b, i) => b.dataset.symbol !== mon.pairs[i].symbol)) {
            q.textContent = '';
            mon.pairs.forEach((p, i) => {
                const b = document.createElement('button');
                b.type = 'button'; b.dataset.symbol = p.symbol;
                b.innerHTML = '<span></span><i></i>';
                b.firstChild.textContent = prettyPair(p.symbol).replace(' (OTC)', '');
                b.addEventListener('click', () => beginPair(i));
                q.appendChild(b);
            });
        }
        [...q.children].forEach((b, i) => {
            const p = mon.pairs[i];
            b.className = `${i === mon.active ? 'on' : ''} ${p.setup ? (p.score >= 0 ? 'buy' : 'sell') : ''}`.trim();
        });
    }

    function setGauges(rsi, pb, sr, calculating) {
        const rows = [['gRsi', 'gRsiN', rsi / 100, rsi.toFixed(1)], ['gBb', 'gBbN', clamp01(pb), pb.toFixed(2)], ['gSr', 'gSrN', clamp01(sr), sr.toFixed(2)]];
        rows.forEach(([bar, num, frac, txt]) => {
            $(bar).style.width = `${(clamp01(frac) * 100).toFixed(1)}%`;
            $(bar).className = calculating ? 'calc' : '';
            $(num).textContent = txt;
        });
    }
    function setStage(phase, p) {
        if (phase === mon.phase) return;
        mon.phase = phase;
        const n = prettyPair(p.symbol);
        $('monLine').textContent = [
            `${n} · loading ${p.series.closes.length} candles`,
            `${n} · fitting Bollinger bands (20, 2σ)`,
            `${n} · scanning RSI(14) and Stochastic RSI`,
            `${n} · stretch ${p.score >= 0 ? '+' : ''}${p.score.toFixed(2)} → ${p.setup ? `clean setup, leaning ${p.score >= 0 ? 'BUY' : 'SELL'}` : 'no clean setup'}`,
        ][phase];
    }

    function frame(ts) {
        mon.raf = requestAnimationFrame(frame);
        if (state.user === null || $('deskView').hidden) return;
        const cv = $('monCanvas'), p = mon.cur;
        if (!cv || !p) { if (!p) $('monLine').textContent = mon.total ? 'Waiting for candles…' : 'Connecting to the Quotex feed…'; return; }

        const dpr = Math.min(window.devicePixelRatio || 1, 2), w = cv.clientWidth, h = cv.clientHeight;
        if (!w || !h) return;
        const key = `${w}x${h}x${dpr}`;
        if (mon.size !== key) { cv.width = w * dpr; cv.height = h * dpr; mon.size = key; }

        let t = (ts - mon.start) / 1000;
        if (t > MON_CYCLE) { beginPair(mon.active + 1); return; }
        if (reduceMotion()) t = 5.5;

        const s = p.series, n = s.closes.length;
        const c = cv.getContext('2d');
        c.setTransform(dpr, 0, 0, dpr, 0, 0);
        c.clearRect(0, 0, w, h);
        const fade = t > MON_CYCLE - 0.6 ? (MON_CYCLE - t) / 0.6 : Math.min(1, t / 0.3);
        c.globalAlpha = Math.max(0, fade);

        const padL = 8, padR = 44, priceH = h * 0.64, rsiTop = h * 0.72, rsiH = h * 0.24;
        const X = i => padL + i * (w - padL - padR) / (n - 1);
        const lo = Math.min(...s.closes, ...s.lower), hi = Math.max(...s.closes, ...s.upper), span = (hi - lo) || 1e-9;
        const Y = v => 6 + (1 - (v - lo) / span) * (priceH - 12);
        const YR = v => rsiTop + (1 - v / 100) * rsiH;

        // grid
        c.lineWidth = 1; c.strokeStyle = 'rgba(255,255,255,.05)';
        for (let g = 0; g <= 4; g++) { const y = 6 + g * (priceH - 12) / 4; c.beginPath(); c.moveTo(padL, y); c.lineTo(w - padR, y); c.stroke(); }
        // RSI zones
        c.fillStyle = 'rgba(255,107,125,.07)'; c.fillRect(padL, YR(100), w - padL - padR, YR(70) - YR(100));
        c.fillStyle = 'rgba(61,220,151,.07)'; c.fillRect(padL, YR(30), w - padL - padR, YR(0) - YR(30));
        c.strokeStyle = 'rgba(255,255,255,.12)'; c.setLineDash([3, 4]);
        [70, 30].forEach(v => { c.beginPath(); c.moveTo(padL, YR(v)); c.lineTo(w - padR, YR(v)); c.stroke(); });
        c.setLineDash([]);
        c.font = '10px ui-monospace, Menlo, Consolas, monospace'; c.fillStyle = '#8a92a3'; c.textAlign = 'left';
        c.fillText('70', w - padR + 6, YR(70) + 3); c.fillText('30', w - padR + 6, YR(30) + 3); c.fillText('RSI', w - padR + 6, rsiTop + 10);

        // price line draws in
        const pLine = ease(t / 1.4), lastI = pLine * (n - 1), full = Math.floor(lastI);
        const partial = (arr, i) => (i < n - 1 ? arr[i] + (arr[i + 1] - arr[i]) * (lastI - i) : arr[i]);
        // Bollinger bands fade in left to right
        const pBand = ease((t - 1.2) / 1.4), bandI = pBand * (n - 1);
        if (pBand > 0) {
            c.beginPath();
            for (let i = 0; i <= Math.floor(bandI); i++) c.lineTo(X(i), Y(s.upper[i]));
            for (let i = Math.floor(bandI); i >= 0; i--) c.lineTo(X(i), Y(s.lower[i]));
            c.closePath(); c.fillStyle = 'rgba(76,141,255,.10)'; c.fill();
            c.strokeStyle = 'rgba(134,173,255,.55)'; c.lineWidth = 1;
            [s.upper, s.lower].forEach(arr => { c.beginPath(); for (let i = 0; i <= Math.floor(bandI); i++) c[i ? 'lineTo' : 'moveTo'](X(i), Y(arr[i])); c.stroke(); });
        }
        c.lineWidth = 2; c.strokeStyle = '#eceef3'; c.shadowColor = 'rgba(134,173,255,.6)'; c.shadowBlur = 8;
        c.beginPath();
        for (let i = 0; i <= full; i++) c[i ? 'lineTo' : 'moveTo'](X(i), Y(s.closes[i]));
        if (full < n - 1) c.lineTo(X(lastI), Y(partial(s.closes, full)));
        c.stroke(); c.shadowBlur = 0;

        // scan beam: RSI line follows it, readouts ride on it
        const pBeam = ease((t - 2.6) / 2.2), bi = pBeam * (n - 1), bf = Math.floor(bi);
        const at = (arr, i) => (i < n - 1 ? arr[i] + (arr[i + 1] - arr[i]) * (bi - i) : arr[i]);
        const beaming = t >= 2.6;
        if (beaming) {
            c.lineWidth = 1.6; c.strokeStyle = '#86adff';
            c.beginPath();
            for (let i = 0; i <= bf; i++) c[i ? 'lineTo' : 'moveTo'](X(i), YR(s.rsi[i]));
            if (bf < n - 1) c.lineTo(X(bi), YR(at(s.rsi, bf)));
            c.stroke();
        }
        const settled = t >= 4.8;
        if (beaming && !settled) {
            const bx = X(bi), g = c.createLinearGradient(bx - 26, 0, bx, 0);
            g.addColorStop(0, 'rgba(134,173,255,0)'); g.addColorStop(1, 'rgba(134,173,255,.28)');
            c.fillStyle = g; c.fillRect(bx - 26, 4, 26, rsiTop + rsiH - 4);
            c.strokeStyle = 'rgba(134,173,255,.9)'; c.lineWidth = 1; c.beginPath(); c.moveTo(bx, 4); c.lineTo(bx, rsiTop + rsiH); c.stroke();
            const py = Y(at(s.closes, bf)), pr = at(s.closes, bf), rs = at(s.rsi, bf);
            const band = (at(s.upper, bf) - at(s.lower, bf)), pb = band ? (pr - at(s.lower, bf)) / band : 0.5;
            c.fillStyle = '#4c8dff'; c.beginPath(); c.arc(bx, py, 4, 0, 7); c.fill();
            const label = `${pr.toFixed(decimals(pr))}  RSI ${rs.toFixed(0)}  %B ${pb.toFixed(2)}`;
            c.font = '11px ui-monospace, Menlo, Consolas, monospace';
            const tw = c.measureText(label).width + 14, lx = Math.min(Math.max(bx - tw / 2, padL), w - padR - tw);
            c.fillStyle = 'rgba(8,9,12,.88)'; c.strokeStyle = 'rgba(134,173,255,.5)';
            c.beginPath(); c.roundRect(lx, Math.max(2, py - 30), tw, 20, 6); c.fill(); c.stroke();
            c.fillStyle = '#eceef3'; c.fillText(label, lx + 7, Math.max(2, py - 30) + 14);
            // gauges follow the beam
            setGauges(rs, pb, stochAt(s.rsi, bf), true);
        }
        if (settled) {
            const buy = p.score >= 0, col = buy ? '#3ddc97' : '#ff6b7d';
            const pulse = 1 + 0.35 * Math.sin(t * 6);
            if (p.setup) {
                c.strokeStyle = col; c.lineWidth = 2; c.beginPath(); c.arc(X(n - 1), Y(s.closes[n - 1]), 6 * pulse, 0, 7); c.stroke();
            }
            c.fillStyle = col; c.beginPath(); c.arc(X(n - 1), Y(s.closes[n - 1]), 3.5, 0, 7); c.fill();
            c.strokeStyle = 'rgba(134,173,255,.9)'; c.lineWidth = 1.4; c.beginPath();
            s.rsi.forEach((v, i) => c[i ? 'lineTo' : 'moveTo'](X(i), YR(v))); c.stroke();
            c.font = '700 12px ui-monospace, Menlo, Consolas, monospace'; c.fillStyle = p.setup ? col : '#8a92a3'; c.textAlign = 'left';
            c.fillText(p.setup ? `${buy ? 'BUY' : 'SELL'} SETUP · stretch ${p.score >= 0 ? '+' : ''}${p.score.toFixed(2)}` : `NO CLEAN SETUP · stretch ${p.score >= 0 ? '+' : ''}${p.score.toFixed(2)}`, padL + 6, 18);
            setGauges(p.rsi, p.pct_b, p.stoch_rsi, false);
        }
        c.globalAlpha = 1;

        // numbers flicker while the first stages run, like a calculation in progress
        if (t < 2.6 && ts - mon.flickerAt > 70) {
            mon.flickerAt = ts;
            mon.flick = [10 + Math.random() * 80, Math.random() * 1.2 - 0.1, Math.random()];
            setGauges(mon.flick[0], mon.flick[1], mon.flick[2], true);
        }
        setStage(t < 1.2 ? 0 : t < 2.6 ? 1 : t < 4.8 ? 2 : 3, p);
        const bq = $('monQueue').children[mon.active];
        if (bq) bq.lastChild.style.width = `${Math.min(100, t / MON_CYCLE * 100).toFixed(1)}%`;
    }
    function startMonitor() { cancelAnimationFrame(mon.raf); mon.raf = requestAnimationFrame(frame); }
    function stopMonitor() { cancelAnimationFrame(mon.raf); mon.cur = null; mon.pairs = []; }

    // ───────── sign in / out ─────────
    function renderTzOptions(q = '') {
        const box = $('tzOptions'), n = q.trim().toLowerCase();
        box.textContent = '';
        state.tzCatalog.filter(t => !n || t.search.includes(n)).slice(0, 80).forEach(t => {
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'tz-option';
            b.innerHTML = `<span>${t.country || t.city}</span><small>${t.id} · ${t.offset} · ${t.abbreviation}</small>`;
            b.addEventListener('click', () => pickTz(t, true));
            box.appendChild(b);
        });
        if (!box.children.length) box.textContent = 'No matching time zones';
    }
    function pickTz(t, explicit) {
        state.tz = t.id; if (explicit) state.tzExplicit = true;
        $('tzTrigger').textContent = `${t.country || t.city} · ${t.id} · ${t.offset}`;
        $('tzMenu').hidden = true; $('tzTrigger').setAttribute('aria-expanded', 'false');
    }
    async function loadTimezones() {
        try { state.tzCatalog = await getJSON('/timezones'); } catch { state.tzCatalog = []; }
        const cur = state.tzCatalog.find(t => t.id === state.tz);
        if (cur) pickTz(cur, false);
    }

    async function signIn(e) {
        e.preventDefault();
        const id = $('idInput').value.trim(), err = $('loginError'), btn = $('loginBtn');
        err.textContent = '';
        if (!id) { err.textContent = 'Enter your access ID.'; return; }
        btn.disabled = true; btn.textContent = 'Checking…';
        try {
            const r = await getJSON('/verify-trader', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ trader_id: id, ...(state.tzExplicit ? { timezone: state.tz } : {}) }),
            });
            if (!r.found) { err.textContent = r.message || 'Access denied.'; return; }
            if (r.token) safe(() => sessionStorage.setItem(KEY_TOKEN, r.token));
            if (r.timezone) state.tz = r.timezone;
            safe(() => localStorage.setItem(`${KEY_USER}_tz_${id.toUpperCase()}`, state.tz));
            safe(() => sessionStorage.setItem(KEY_USER, id));
            openDesk(id);
        } catch (ex) {
            err.textContent = ex instanceof TypeError ? 'Cannot reach the server. Is the backend running?' : ex.message;
        } finally {
            btn.disabled = false; btn.textContent = 'Continue';
        }
    }

    function openDesk(id) {
        state.user = id;
        const saved = safe(() => localStorage.getItem(`${KEY_USER}_tz_${id.toUpperCase()}`));
        if (saved) state.tz = saved;
        $('loginView').hidden = true; $('deskView').hidden = false;
        showWho();
        $('tzName').textContent = state.tz.split('/').pop().replace(/_/g, ' ');
        syncTime().then(tickClock);
        refreshFeed(); refreshCorrelations(); refreshMonitor(); startMonitor();
        state.timers = [
            setInterval(tickClock, 250),
            setInterval(refreshMonitor, 5000),
            setInterval(refreshFeed, 10000),
            setInterval(refreshCorrelations, 60000),
            setInterval(syncTime, 30000),
        ];
        armIdle();
    }

    function signOut(expired = false, otherDevice = false) {
        state.timers.forEach(clearInterval); state.timers = [];
        stopMonitor();
        clearTimeout(state.idle);
        safe(() => sessionStorage.removeItem(KEY_USER));
        safe(() => sessionStorage.removeItem(KEY_TOKEN));
        if (location.pathname.startsWith('/quotex/')) { location.replace('/login/?broker=quotex' + (otherDevice ? '&other_device=1' : expired ? '&expired=1' : '')); return; }
        state.signal = null; state.points = [];
        $('ticket').hidden = true; $('ticketSide').hidden = true; $('empty').hidden = false;
        $('deskView').hidden = true; $('loginView').hidden = false;
        $('loginError').textContent = otherDevice ? 'Signed out: this ID was used to sign in on another device.' : expired ? 'Signed out after 30 minutes of inactivity.' : '';
        $('idInput').value = ''; $('idInput').focus();
    }
    function armIdle() { clearTimeout(state.idle); state.idle = setTimeout(() => signOut(true), IDLE_MS); }

    // ───────── trader ID mask ─────────
    // the ID can be hidden completely (••••••••) for screenshots or screen sharing; the choice is remembered on this device
    const masked = () => safe(() => localStorage.getItem('qx_mask_id'), '0') === '1';
    function maskId(id) { return '•'.repeat(id.length); }
    function showWho() {
        const id = (state.user || '').toUpperCase(), on = masked();
        $('who').textContent = on ? maskId(id) : id;
        $('maskBtn').setAttribute('aria-pressed', String(on));
        const label = on ? 'Show trader ID' : 'Hide trader ID';
        $('maskBtn').setAttribute('aria-label', label); $('maskBtn').title = label;
    }

    // ───────── boot ─────────
    function init() {
        $('loginForm').addEventListener('submit', signIn);
        $('scanBtn').addEventListener('click', scan);
        $('logoutBtn').addEventListener('click', () => signOut(false));
        $('maskBtn').addEventListener('click', () => { safe(() => localStorage.setItem('qx_mask_id', masked() ? '0' : '1')); showWho(); });
        // side panel: hide / show on wide screens, remembered between visits (phones always show it)
        const setRail = hidden => {
            $('deskView').classList.toggle('rail-hidden', hidden);
            $('railOpen').hidden = !hidden;
            safe(() => localStorage.setItem('qx_rail_hidden', hidden ? '1' : '0'));
        };
        $('railClose').addEventListener('click', () => setRail(true));
        $('railOpen').addEventListener('click', () => setRail(false));
        setRail(safe(() => localStorage.getItem('qx_rail_hidden'), '0') === '1');
        $('tzTrigger').addEventListener('click', () => {
            const m = $('tzMenu'); m.hidden = !m.hidden;
            $('tzTrigger').setAttribute('aria-expanded', String(!m.hidden));
            if (!m.hidden) { $('tzSearch').value = ''; renderTzOptions(); $('tzSearch').focus(); }
        });
        $('tzSearch').addEventListener('input', e => renderTzOptions(e.target.value));
        document.addEventListener('click', e => { if (!$('tzPicker').contains(e.target)) $('tzMenu').hidden = true; });
        ['mousemove', 'keydown', 'click', 'touchstart'].forEach(ev => document.addEventListener(ev, () => { if (state.user) armIdle(); }, { passive: true }));
        initChartHover();
        loadTimezones();

        const stored = safe(() => sessionStorage.getItem(KEY_USER));
        if (stored) openDesk(stored);
        else if (location.pathname.startsWith('/quotex/')) location.replace('/login/?broker=quotex');
        else $('idInput').focus();
    }
    init();
})();
