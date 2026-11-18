"""Capture action-log facts independently of its HTML presentation."""


class ActionQueries:
    def __init__(self, read_view):
        self.read_view = read_view

    def listing(self):
        with self.read_view() as reader:
            return (reader.actions.get_actions(), {p.id: p for p in reader.playlists.get_playlists()})

    def action(self, action_id):
        with self.read_view() as reader:
            return (reader.actions.get_action(action_id), {p.id: p for p in reader.playlists.get_playlists()})
