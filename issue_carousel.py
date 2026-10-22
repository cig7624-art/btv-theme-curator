"""Horizontal issue navigation; scripts contain only fixed application code."""


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


CAROUSEL_SCRIPT = """<script>
(() => {
  let attempts = 0;
  function mount() {
    const root = document.getElementById('home-issue-carousel');
    if (!root) {
      if (++attempts < 60) requestAnimationFrame(mount);
      return;
    }
    if (window.__btvIssueCarouselCleanup) window.__btvIssueCarouselCleanup();
    const rail = root.querySelector('.editorial-grid');
    const previous = root.querySelector('.carousel-prev');
    const next = root.querySelector('.carousel-next');
    const position = root.querySelector('.carousel-position');
    const events = new AbortController();
    let resize;
    const cleanup = () => { events.abort(); if (resize) resize.disconnect(); };
    window.__btvIssueCarouselCleanup = cleanup;
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
  }
  mount();
})();
</script>"""
