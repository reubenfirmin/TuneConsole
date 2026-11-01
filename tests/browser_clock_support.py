"""Advance dwell time only after the real visibility observer has qualified it."""


def discovery_dwell(page, tile, endpoint='/discovery/impressions'):
    subject = tile.get_attribute('data-story-subject')
    kind = tile.get_attribute('data-discovery-kind')
    identity = tile.get_attribute('data-discovery-id')

    def records_tile(response):
        if (not response.url.endswith(endpoint)
                or response.request.method != 'POST' or not response.ok):
            return False
        payload = response.request.post_data_json
        if subject:
            return subject in payload.get('subjects', [])
        return payload.get('kind') == kind and identity in payload.get('ids', [])

    # Let native layout/IntersectionObserver qualify a newly scrolled tile while
    # keeping the dwell itself under explicit clock control.
    page.clock.run_for(50)
    page.wait_for_function('tile => discoveryExposure?.timers.has(tile)', arg=tile.element_handle())
    # Adjacent visible headings can post to the same endpoint during this jump.
    # Wait for this tile's write before inspecting its persisted view count.
    with page.expect_response(records_tile):
        page.clock.fast_forward(1000)


def discovery_interval(page, tile):
    """Exercise an interval with no expected new exposure (hidden or already seen)."""
    # install() alone still advances with wall time. Pause during the initial
    # hidden interval so later assertions cannot race an already-fired dwell.
    page.clock.pause_at(page.evaluate('Date.now() / 1000') + .1)
    page.wait_for_function('tile => discoveryExposure?.observed.has(tile)', arg=tile.element_handle())
    # Deliver native layout/IntersectionObserver updates before jumping timers.
    page.clock.run_for(50)
    page.clock.fast_forward(1100)
