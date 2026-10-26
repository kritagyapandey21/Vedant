// Login background: slowly scrolling candlestick charts drawn on a canvas, behind soft drifting glows.
(() => {
    const cv = document.getElementById('bgCanvas');
    if (!cv || !cv.getContext) return;
    const ctx = cv.getContext('2d');
    const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
    let w = 0, h = 0, dpr = 1, last = 0, layers = [];

    // one chart: a random walk of candles that scrolls left and grows on the right
    function makeLayer(yMid, amp, size, speed, alpha) {
        const gap = size * 1.9, l = { yMid, amp, size, gap, speed, alpha, off: 0, candles: [], price: 0 };
        for (let x = -gap; x < w + gap * 2; x += gap) push(l);
        return l;
    }
    function push(l) {
        const o = l.price, c = o + (Math.random() - .5) * l.amp * .5 + (Math.sin(l.candles.length / 9) * l.amp * .06);
        const hi = Math.max(o, c) + Math.random() * l.amp * .22, lo = Math.min(o, c) - Math.random() * l.amp * .22;
        l.price = Math.max(-l.amp * 2, Math.min(l.amp * 2, c * .985));    // pulled back to the middle so it stays on screen
        l.candles.push({ o, c, hi, lo });
    }

    function resize() {
        dpr = Math.min(window.devicePixelRatio || 1, 2);
        w = innerWidth; h = innerHeight;
        cv.width = w * dpr; cv.height = h * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        const s = w < 600 ? .7 : 1;
        layers = [
            makeLayer(h * .22, 70 * s, 9 * s, 9, .10),
            makeLayer(h * .52, 110 * s, 13 * s, 16, .17),
            makeLayer(h * .82, 80 * s, 10 * s, 11, .11),
        ];
        draw();
    }

    function draw() {
        ctx.clearRect(0, 0, w, h);
        layers.forEach(l => {
            ctx.globalAlpha = l.alpha;
            l.candles.forEach((k, i) => {
                const x = i * l.gap - l.off, up = k.c >= k.o;
                if (x < -l.gap || x > w + l.gap) return;
                ctx.strokeStyle = ctx.fillStyle = up ? '#4c8dff' : '#ff6b7d';
                ctx.lineWidth = 1.5;
                const y = v => l.yMid - v;
                ctx.beginPath(); ctx.moveTo(x, y(k.hi)); ctx.lineTo(x, y(k.lo)); ctx.stroke();
                ctx.fillRect(x - l.size / 2, y(Math.max(k.o, k.c)), l.size, Math.max(2, Math.abs(k.c - k.o)));
            });
        });
        ctx.globalAlpha = 1;
    }

    function tick(t) {
        const dt = Math.min(.05, (t - last) / 1000 || 0); last = t;
        layers.forEach(l => {
            l.off += l.speed * dt;
            while (l.off >= l.gap) { l.off -= l.gap; l.candles.shift(); push(l); }
        });
        draw();
        requestAnimationFrame(tick);
    }

    addEventListener('resize', resize);
    resize();
    if (!still) requestAnimationFrame(tick);
})();
