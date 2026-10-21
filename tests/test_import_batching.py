from yt_playlist.library.playlist_writes import add_items_resilient


class Client:
    def __init__(self): self.calls = []
    def add_playlist_items(self, pid, ids):
        self.calls.append(list(ids))
        return "STATUS_SUCCEEDED"


def test_import_adds_in_bounded_batches():
    client = Client()
    added, skipped = add_items_resilient(client, "PL", [str(i) for i in range(205)])
    assert added == 205 and skipped == []
    assert [len(call) for call in client.calls] == [100, 100, 5]
