function trackDetails(videoId, status) {
  return {
    videoId, status, busy: false, error: '', destroyed: false,
    destroy() { this.destroyed = true; },
    ratingChanged(detail) {
      if (detail.videoId === this.videoId && !this.busy) {
        this.status = detail.likeStatus || (detail.liked ? 'LIKE' :
          this.status === 'LIKE' ? 'INDIFFERENT' : this.status);
      }
    },
    async rate(want) {
      if (this.busy) return;
      const desired = this.status === want ? 'INDIFFERENT' : want;
      this.busy = true; this.error = '';
      try {
        const response = await fetch('/now-playing/rate', {
          method: 'POST', signal: AbortSignal.timeout(20000), headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ videoId: this.videoId, action: want === 'LIKE' ? 'like' : 'dislike', likeStatus: desired })
        });
        if (!response.ok) {
          console.debug('[Track rating] response unavailable', response.status, await response.text());
          throw new Error('Rating request failed');
        }
        const result = await response.json();
        if (!result.ok) throw new Error(result.error || 'Rating not confirmed');
        if (this.destroyed) return;
        this.status = desired;
        if (result.player_error) console.debug('[Track rating] player update unavailable', result.player_error);
        this.error = result.player_error ? 'Rating saved. Some updates are not confirmed; check it in YouTube Music.' : '';
        window.dispatchEvent(new CustomEvent('track-like-changed', { detail: {
          videoId: this.videoId, liked: desired === 'LIKE', likeStatus: desired
        }}));
      } catch (error) {
        console.debug('[Track rating] request not confirmed', error);
        if (!this.destroyed) this.error = 'Rating not confirmed. Check it in YouTube Music before trying again.';
      }
      finally { this.busy = false; }
    }
  };
}
