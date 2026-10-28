function trackDetails(videoId, status) {
  return {
    videoId, status, busy: false, error: '',
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
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ videoId: this.videoId, action: want === 'LIKE' ? 'like' : 'dislike', likeStatus: desired })
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || 'Couldn’t update this rating. Try again.');
        this.status = desired;
        this.error = result.player_error || '';
        window.dispatchEvent(new CustomEvent('track-like-changed', { detail: {
          videoId: this.videoId, liked: desired === 'LIKE', likeStatus: desired
        }}));
      } catch (error) { this.error = error.message; }
      finally { this.busy = false; }
    }
  };
}
