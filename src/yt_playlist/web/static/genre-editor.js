function genreEditor() {
  return {
    nameRevision: 0,
    _requests: Promise.resolve(),
    add() {
      const name = this.$refs.name.value;
      const revision = this.nameRevision;
      this.change('add', {name}, () => {
        if (this.nameRevision === revision && this.$refs.name.value === name) {
          this.$refs.name.value = '';
        }
      });
    },
    change(action, values = {}, onSuccess) {
      this.enqueue('POST', '/genres/' + action,
        {...values, q: this.$refs.filter.value}, onSuccess);
    },
    filter() {
      this.enqueue('GET', '/genres/list', {q: this.$refs.filter.value});
    },
    enqueue(method, path, values, onSuccess) {
      // Capture the payload now. HTMX's element queue reads live inputs later,
      // and drops callbacks from Remove buttons replaced by an earlier response.
      const source = this.$root;
      const submitted = {...values};
      this._requests = this._requests.then(async () => {
        if (!source.isConnected) return;
        const complete = event => {
          if (event.detail.elt === source && event.detail.successful) onSuccess?.();
        };
        source.addEventListener('htmx:afterRequest', complete);
        try {
          await htmx.ajax(method, path, {
            source, target: '#genre-list', swap: 'innerHTML settle:0', values: submitted,
          });
        } finally {
          source.removeEventListener('htmx:afterRequest', complete);
        }
      }).catch(() => {
        // HTMX reports the failure. Keep later explicit edits runnable and the
        // failed add's input available instead of clearing it optimistically.
      });
    },
  };
}
