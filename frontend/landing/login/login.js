// Shared login for both AUROX AI terminals. Verifies against the chosen broker's backend,
// stores the session keys that terminal reads, then opens it.
(() => {
    const $ = id => document.getElementById(id);

    const BROKERS = {
        pocket: {
            label: 'Pocket Option Trader ID',
            hint: 'Forex pairs · broker candles',
            name: 'Pocket Option',
            signup: 'https://pocketoption.com',   // replace with your own affiliate/referral link if you have one
            api: '/api',                       // AUROX backend
            terminal: '/pocketoption/',
            session: (id, token) => {          // keys read by the Pocket Option terminal
                sessionStorage.setItem('aurox_trader_id', id);
                sessionStorage.setItem('aurox_token', token);
                localStorage.setItem(`aurox_timezone_${id.toUpperCase()}`, state.tz);
            },
        },
        quotex: {
            label: 'Quotex Access ID',
            hint: 'Currency & OTC pairs · live ticks',
            name: 'Quotex',
            signup: 'https://quotex.com',          // replace with your own affiliate/referral link if you have one
            api: '/quotex-api',                // Quotex backend
            terminal: '/quotex/',
            session: (id, token) => {          // keys read by the Quotex terminal
                sessionStorage.setItem('pulseqx_user', id);
                sessionStorage.setItem('pulseqx_token', token);
                localStorage.setItem(`pulseqx_user_tz_${id.toUpperCase()}`, state.tz);
            },
        },
    };

    const submitLabel = $('submitText').textContent;
    const state = { broker: 'quotex', tz: 'Asia/Kolkata', tzExplicit: false, catalog: [] };
    const safe = (fn, fb) => { try { return fn(); } catch { return fb; } };

    // ───── broker choice ─────
    function setBroker(key, remember = true) {
        state.broker = BROKERS[key] ? key : 'quotex';
        $('brokerTitle').textContent = BROKERS[state.broker].name;
        $('idLabel').textContent = BROKERS[state.broker].label;
        if ($('brokerHint')) $('brokerHint').textContent = BROKERS[state.broker].hint;
        $('signupName').textContent = BROKERS[state.broker].name;
        $('signupLink').href = BROKERS[state.broker].signup;
        if (remember) safe(() => localStorage.setItem('aurox_last_broker', state.broker));
    }
    // two screens: the broker cards first, then that broker's sign-in form
    function showScreen(form) {
        $('pick').hidden = form; $('form').hidden = !form;
        if (form) $('idInput').focus();
    }
    document.querySelectorAll('.tcard').forEach(c => c.addEventListener('click', () => { setBroker(c.dataset.broker); show(''); showScreen(true); }));
    $('changeBroker').addEventListener('click', () => { show(''); showScreen(false); });

    // ───── time zones ─────
    function renderTz(q = '') {
        const box = $('tzOptions'), n = q.trim().toLowerCase();
        box.textContent = '';
        state.catalog.filter(t => !n || t.search.includes(n)).slice(0, 80).forEach(t => {
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
        const ALIAS = { 'Asia/Calcutta': 'Asia/Kolkata', 'Asia/Saigon': 'Asia/Ho_Chi_Minh', 'Asia/Katmandu': 'Asia/Kathmandu', 'Asia/Rangoon': 'Asia/Yangon' };
        const raw = safe(() => Intl.DateTimeFormat().resolvedOptions().timeZone);
        const guess = ALIAS[raw] || raw;
        for (const base of ['/api', '/quotex-api']) {
            try {
                const res = await fetch(`${base}/timezones`);
                if (res.ok) { state.catalog = await res.json(); break; }
            } catch { /* try next */ }
        }
        const pick = state.catalog.find(t => t.id === guess) || state.catalog.find(t => t.id === state.tz);
        if (pick) pickTz(pick, false);
        else $('tzTrigger').textContent = state.tz;
    }
    $('tzTrigger').addEventListener('click', () => {
        const m = $('tzMenu'); m.hidden = !m.hidden;
        $('tzTrigger').setAttribute('aria-expanded', String(!m.hidden));
        if (!m.hidden) { $('tzSearch').value = ''; renderTz(); $('tzSearch').focus(); }
    });
    $('tzSearch').addEventListener('input', e => renderTz(e.target.value));
    document.addEventListener('click', e => { if (!$('tzPicker').contains(e.target)) $('tzMenu').hidden = true; });

    // ───── submit ─────
    function show(text, info = false) { const m = $('msg'); m.textContent = text; m.className = `msg${info ? ' info' : ''}`; }

    $('form').addEventListener('submit', async e => {
        e.preventDefault();
        const id = $('idInput').value.trim(), cfg = BROKERS[state.broker], btn = $('submit');
        show('');
        if (!id) { show('Enter your ID to continue.'); $('idInput').focus(); return; }
        btn.disabled = true; $('submitText').textContent = 'Verifying…';
        try {
            const res = await fetch(`${cfg.api}/verify-trader`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ trader_id: id, ...(state.tzExplicit ? { timezone: state.tz } : {}) }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) { show(data.detail || 'Verification failed. Please try again.'); return; }
            if (!data.found) { show(data.message || 'ID not recognised.'); return; }
            if (data.timezone) state.tz = data.timezone;
            if (!data.token) { show('The server did not issue a session. Please try again.'); return; }
            cfg.session(id, data.token);
            show('Verified, opening terminal…', true);
            $('submitText').textContent = 'Opening…';
            location.href = cfg.terminal;
        } catch {
            show('Cannot reach the server right now. Please try again in a moment.');
        } finally {
            if (!location.href.endsWith(cfg.terminal)) { btn.disabled = false; $('submitText').textContent = submitLabel; }
        }
    });

    // ───── init ─────
    const params = new URLSearchParams(location.search);
    const asked = params.get('broker');
    setBroker(asked || 'quotex', false);
    if (BROKERS[asked]) showScreen(true);      // signed out of a terminal: go straight back to its form
    if (params.get('other_device')) show('You were signed out because this ID was used to sign in on another device.', true);
    else if (params.get('expired')) show('You were signed out after 30 minutes of inactivity.', true);
    loadTimezones();
})();
