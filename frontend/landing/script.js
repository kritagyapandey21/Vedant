// TradingSignal landing: scroll driven effects. Everything degrades to a static page without JS.
(() => {
    const $ = s => document.querySelector(s);
    const $$ = s => [...document.querySelectorAll(s)];
    const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
    const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

    // ───── reveal on scroll ─────
    const reveals = $$('.reveal');
    if ('IntersectionObserver' in window && !reduce) {
        const io = new IntersectionObserver(entries => {
            entries.forEach(e => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
        }, { threshold: 0.14, rootMargin: '0px 0px -6% 0px' });
        reveals.forEach(el => io.observe(el));
    } else {
        reveals.forEach(el => el.classList.add('in'));
    }

    // ───── count-up numbers in the hero ─────
    $$('[data-count]').forEach(el => {
        const target = +el.dataset.count;
        if (reduce || target === 0) return;
        el.textContent = '0';
        const start = performance.now() + 500, dur = 1100;
        const step = t => {
            const p = clamp((t - start) / dur, 0, 1);
            el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3)));
            if (p < 1) requestAnimationFrame(step);
        };
        requestAnimationFrame(step);
    });

    // ───── scroll-linked: progress bar, nav, parallax glows, timeline fill, active link ─────
    const progress = $('#progress'), nav = $('#nav'), glows = $$('.glow'), grid = $('#bgGrid');
    const tl = $('#timeline'), tlFill = $('#tlFill');
    const links = $$('.nav-links a'), targets = links.map(a => $(a.getAttribute('href')));
    let ticking = false;

    function onScroll() {
        const y = scrollY, max = document.documentElement.scrollHeight - innerHeight;
        progress.style.transform = `scaleX(${max > 0 ? y / max : 0})`;
        nav.classList.toggle('scrolled', y > 30);
        if (!reduce) {
            glows.forEach(g => { g.style.transform = `translate3d(0, ${y * parseFloat(g.dataset.speed)}px, 0)`; });
            if (grid) grid.style.transform = `translate3d(0, ${y * -0.08}px, 0)`;
        }
        if (tl) {
            const r = tl.getBoundingClientRect();
            const p = clamp((innerHeight * 0.62 - r.top) / r.height, 0, 1);
            tlFill.style.height = `${p * 100}%`;
        }
        let current = -1;
        targets.forEach((t, i) => { if (t && t.getBoundingClientRect().top < innerHeight * 0.4) current = i; });
        links.forEach((a, i) => a.classList.toggle('active', i === current));
        ticking = false;
    }
    addEventListener('scroll', () => { if (!ticking) { ticking = true; requestAnimationFrame(onScroll); } }, { passive: true });
    addEventListener('resize', onScroll);
    onScroll();

    // ───── cursor spotlight on cards ─────
    $$('.spot').forEach(el => el.addEventListener('pointermove', e => {
        const r = el.getBoundingClientRect();
        el.style.setProperty('--mx', `${e.clientX - r.left}px`);
        el.style.setProperty('--my', `${e.clientY - r.top}px`);
    }));

    // ───── 3D tilt on the hero ticket ─────
    const ticket = $('#sampleTicket');
    if (ticket && !reduce && matchMedia('(hover: hover)').matches) {
        const host = ticket.parentElement;
        host.addEventListener('pointermove', e => {
            const r = host.getBoundingClientRect();
            const x = (e.clientX - r.left) / r.width - 0.5, y = (e.clientY - r.top) / r.height - 0.5;
            ticket.style.transform = `rotateY(${x * 6}deg) rotateX(${-y * 5}deg)`;
        });
        host.addEventListener('pointerleave', () => { ticket.style.transform = ''; });
    }

    // ───── ticker: duplicate content so the marquee loops seamlessly ─────
    const track = $('#tickerTrack');
    if (track) track.innerHTML += track.innerHTML;

    // ───── looping demo countdowns ─────
    const timer = $('#demoTimer'), bar = $('#demoBar'), mini = $('#miniSec');
    const TOTAL = 45;
    let left = 30;
    const render = () => {
        if (timer) timer.textContent = `00:${String(left).padStart(2, '0')}`;
        if (bar) bar.style.width = `${((TOTAL - left) / TOTAL) * 100}%`;
        if (mini) mini.textContent = String(left).padStart(2, '0');
    };
    render();
    setInterval(() => { left = left <= 0 ? TOTAL : left - 1; render(); }, 1000);

    // ───── hero background: drifting market lines on a canvas ─────
    const cv = $('#heroCanvas');
    if (cv && !reduce) {
        const ctx = cv.getContext('2d');
        let w = 0, h = 0, dpr = Math.min(devicePixelRatio || 1, 2), t0 = performance.now();
        const lines = Array.from({ length: 4 }, (_, i) => ({ seed: Math.random() * 1000, amp: 40 + i * 22, base: 0.3 + i * 0.13, speed: 0.012 + i * 0.006, alpha: 0.5 - i * 0.09 }));
        const resize = () => {
            w = cv.clientWidth; h = cv.clientHeight;
            cv.width = w * dpr; cv.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        };
        const noise = (x, s) => Math.sin(x * 0.9 + s) * 0.55 + Math.sin(x * 2.1 + s * 1.7) * 0.3 + Math.sin(x * 4.3 + s * 0.6) * 0.15;
        const frame = now => {
            if (document.hidden || scrollY > innerHeight * 1.2) { requestAnimationFrame(frame); return; }
            const t = (now - t0) / 1000;
            ctx.clearRect(0, 0, w, h);
            lines.forEach(l => {
                ctx.beginPath();
                for (let x = 0; x <= w; x += 6) {
                    const y = h * l.base + noise(x / 140 + t * l.speed * 30, l.seed) * l.amp;
                    x ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
                }
                const g = ctx.createLinearGradient(0, 0, w, 0);
                g.addColorStop(0, 'rgba(76,141,255,0)'); g.addColorStop(0.5, `rgba(134,173,255,${l.alpha})`); g.addColorStop(1, 'rgba(76,141,255,0)');
                ctx.strokeStyle = g; ctx.lineWidth = 1.4; ctx.stroke();
            });
            requestAnimationFrame(frame);
        };
        resize(); addEventListener('resize', resize); requestAnimationFrame(frame);
    }
})();
