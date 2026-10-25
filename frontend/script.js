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

/* ==========================================================================
   4. NAV SELECTION PILL
   Slides under the link for whichever slide is in view
   ========================================================================== */
const navLinks = [...document.querySelectorAll('.nav-link')];
const navPill = document.getElementById('navPill');

function movePill(link) {
    navLinks.forEach((l) => l.classList.toggle('active', l === link));
    navPill.style.left = `${link.offsetLeft}px`;
    navPill.style.width = `${link.offsetWidth}px`;
}

function syncNav() {
    // Active slide = the last one whose start is above the middle of the screen
    const mid = scrollY + innerHeight / 2;
    let current = navLinks[0];
    navLinks.forEach((l) => {
        const el = document.querySelector(l.getAttribute('href'));
        if (el && startOf(el) <= mid) current = l;
    });
    if (!current.classList.contains('active') || !navPill.style.width) movePill(current);
}

addEventListener('scroll', syncNav, { passive: true });
addEventListener('resize', () => movePill(document.querySelector('.nav-link.active')));
if (document.fonts) document.fonts.ready.then(syncNav);
syncNav();
