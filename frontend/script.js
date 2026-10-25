/* ==========================================================================
   1. SCROLL REVEAL OBSERVER
   ========================================================================== */
const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
        if (e.isIntersecting) {
            e.target.classList.add('on');
        }
    });
}, { threshold: 0.15 });

document.querySelectorAll('.rv').forEach((el) => io.observe(el));


/* ==========================================================================
   2. STACKED SLIDE DECK EFFECT
   ========================================================================== */
const slides = [...document.querySelectorAll('.slide')];
function deck() {
    const vh = innerHeight;
    slides.forEach((s, i) => {
        const next = slides[i + 1];
        if (!next) return;
        const p = Math.min(Math.max(1 - (next.getBoundingClientRect().top / vh), 0), 1);
        s.style.transform = `scale(${1 - p * 0.04}) translateY(${p * -20}px)`;
        s.style.filter = `brightness(${1 - p * 0.25})`;
    });
    requestAnimationFrame(deck);
}
requestAnimationFrame(deck);


/* ==========================================================================
   3. IN-PAGE SMOOTH NAVIGATION
   ========================================================================== */
const startOf = (el) => slides.slice(0, slides.indexOf(el)).reduce((n, x) => n + x.offsetHeight, 0);

const goTo = (hash, smooth) => {
    const el = document.getElementById(hash.slice(1));
    if (!el || !slides.includes(el)) return false;
    scrollTo({ top: startOf(el) + 2, behavior: smooth ? 'smooth' : 'auto' });
    return true;
};

document.querySelectorAll('a[href^="#"]').forEach((a) => a.addEventListener('click', (e) => {
    if (goTo(a.getAttribute('href'), true)) {
        e.preventDefault();
        history.replaceState(null, '', a.getAttribute('href'));
    }
}));

if (location.hash) {
    addEventListener('load', () => goTo(location.hash, false));
}