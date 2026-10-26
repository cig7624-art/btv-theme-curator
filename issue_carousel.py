"""Shared drag, touch and keyboard navigation for issue and title rails."""


def issue_carousel_html(cards):
    """Wrap already-escaped issue cards; native scrolling works without scripts."""
    return (
        '<section class="editorial-carousel" id="home-issue-carousel" aria-label="지금 주목할 이야기">'
        '<div class="carousel-controls"><span class="carousel-position" aria-live="polite"></span>'
        '<button type="button" class="carousel-prev" aria-label="이전 이슈" aria-controls="home-issue-rail" disabled>‹</button>'
        '<button type="button" class="carousel-next" aria-label="다음 이슈" aria-controls="home-issue-rail">›</button></div>'
        '<div class="editorial-grid" id="home-issue-rail" tabindex="0" aria-label="이슈 목록, 가로로 넘겨 보기">'
        + cards + '</div></section>'
    )


_CAROUSEL_TEMPLATE = """<script>
(() => {
  let attempts = 0;
  function mount() {
    const roots = document.querySelectorAll('__ROOT_SELECTOR__');
    if (!roots.length) {
      if (++attempts < 60) requestAnimationFrame(mount);
      return;
    }
    if (window.__CLEANUP_NAME__) window.__CLEANUP_NAME__();
    const cleanups = Array.from(roots, mountCarousel);
    window.__CLEANUP_NAME__ = () => cleanups.forEach(cleanup => cleanup());
  }
  function mountCarousel(root) {
    const rail = root.querySelector('__RAIL_SELECTOR__');
    const previous = root.querySelector('.carousel-prev');
    const next = root.querySelector('.carousel-next');
    const position = root.querySelector('.carousel-position');
    const events = new AbortController();
    let resize;
    let drag = null;
    let suppressClick = false;
    let clickTimer;
    const cleanup = () => {
      events.abort(); if (resize) resize.disconnect();
      finishDrag(false); clearTimeout(clickTimer);
    };
    const step = () => (rail.firstElementChild?.getBoundingClientRect().width || rail.clientWidth)
      + (parseFloat(getComputedStyle(rail).columnGap) || 0);
    function update() {
      if (!root.isConnected) { cleanup(); return; }
      const count = rail.children.length;
      const bounds = rail.getBoundingClientRect();
      const visible = Array.from(rail.children).flatMap((card, index) => {
        const box = card.getBoundingClientRect();
        return box.left >= bounds.left - 2 && box.right <= bounds.right + 2 ? [index + 1] : [];
      });
      const first = visible[0] || Math.min(count, Math.floor((rail.scrollLeft + 2) / step()) + 1);
      const last = visible[visible.length - 1] || first;
      position.textContent = count ? `${first}–${last} / ${count}` : '';
      previous.disabled = rail.scrollLeft <= 2;
      next.disabled = rail.scrollLeft + rail.clientWidth >= rail.scrollWidth - 2;
    }
    function move(direction) {
      rail.scrollBy({left: direction * step(), behavior:
        matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
    }
    function finishDrag(blockClick = true) {
      const current = drag;
      drag = null;
      rail.classList.remove('is-dragging');
      if (current && rail.hasPointerCapture(current.id)) rail.releasePointerCapture(current.id);
      if (current?.active && blockClick) {
        suppressClick = true;
        clearTimeout(clickTimer);
        clickTimer = setTimeout(() => { suppressClick = false; }, 250);
      }
    }
    rail.addEventListener('pointerdown', (event) => {
      // Touch keeps native swiping and vertical page scrolling. Mouse/pen can
      // grab either the cover link or card body; a simple click still opens it.
      if (event.pointerType === 'touch' || !event.isPrimary || event.button !== 0) return;
      if (event.target.closest('summary, button, input, select, textarea, [contenteditable="true"], .evidence-panel')) return;
      finishDrag(false); clearTimeout(clickTimer); suppressClick = false;
      drag = {id: event.pointerId, x: event.clientX, y: event.clientY, left: rail.scrollLeft, active: false};
    }, {signal: events.signal});
    rail.addEventListener('pointermove', (event) => {
      if (!drag || event.pointerId !== drag.id) return;
      const distance = event.clientX - drag.x;
      if (!drag.active) {
        if (Math.abs(distance) < 6) return;
        if (Math.abs(event.clientY - drag.y) > Math.abs(distance)) { finishDrag(false); return; }
        drag.active = true;
        rail.classList.add('is-dragging');
        rail.setPointerCapture(drag.id);
        window.getSelection()?.removeAllRanges();
      }
      event.preventDefault();
      rail.scrollLeft = drag.left - distance;
    }, {passive: false, signal: events.signal});
    window.addEventListener('pointerup', () => finishDrag(), {signal: events.signal});
    window.addEventListener('pointercancel', () => finishDrag(), {signal: events.signal});
    window.addEventListener('blur', () => finishDrag(), {signal: events.signal});
    rail.addEventListener('dragstart', (event) => event.preventDefault(), {signal: events.signal});
    rail.addEventListener('click', (event) => {
      if (!suppressClick) return;
      event.preventDefault(); event.stopImmediatePropagation(); suppressClick = false;
    }, {capture: true, signal: events.signal});
    previous.addEventListener('click', () => move(-1), {signal: events.signal});
    next.addEventListener('click', () => move(1), {signal: events.signal});
    rail.addEventListener('scroll', update, {passive: true, signal: events.signal});
    rail.addEventListener('keydown', (event) => {
      if (event.target !== rail || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault(); move(event.key === 'ArrowRight' ? 1 : -1);
    }, {signal: events.signal});
    resize = new ResizeObserver(update);
    resize.observe(rail);
    update();
    return cleanup;
  }
  mount();
})();
</script>"""


def _carousel_script(root_selector, rail_selector, cleanup_name):
    # Arguments are fixed selectors owned by this module; content never enters JS.
    return (_CAROUSEL_TEMPLATE.replace('__ROOT_SELECTOR__', root_selector)
            .replace('__RAIL_SELECTOR__', rail_selector)
            .replace('__CLEANUP_NAME__', cleanup_name))


CAROUSEL_SCRIPT = _carousel_script('#home-issue-carousel', '.editorial-grid', '__btvIssueCarouselCleanup')
THEME_CAROUSEL_SCRIPT = _carousel_script('.theme-carousel', '.theme-poster-rail', '__btvThemeCarouselCleanup')
