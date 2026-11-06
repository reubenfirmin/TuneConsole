"""Assign outcomes to the latest prior live impression within its horizon."""
from bisect import bisect_right
from collections import defaultdict

LIVE = "COALESCE(json_extract(provenance,'$.policy_kind'),'live')='live'"
VIDEO = "json_extract(provenance,'$.candidate.video_id')"


class _Timeline:
    def __init__(self, rows, key, value, horizon):
        grouped = defaultdict(list)
        for row in rows:
            grouped[row[key]].append((row['served_at'], row['id'], row[value]))
        self.groups, self.horizon = {}, horizon
        for name, entries in grouped.items():
            entries.sort(key=lambda entry: (entry[0], entry[1]))
            self.groups[name] = ([entry[0] for entry in entries], [entry[2] for entry in entries])

    def nearest(self, key, at):
        timeline = self.groups.get(key)
        if timeline is None:
            return None
        times, values = timeline
        index = bisect_right(times, at) - 1
        return values[index] if index >= 0 and times[index] >= at - self.horizon else None


class Attribution:
    def __init__(self, owners, videos, plays, responses, exits, horizon):
        self.plays, self.responses, self.exits = plays, responses, exits
        self.owners = _Timeline(owners, 'identity_key', 'id', horizon)
        self.video_owners = _Timeline(videos, 'video_id', 'identity_key', horizon)
        self.fallback = {}

    def missing_videos(self):
        return {row['video_id'] for row in self.exits if row['video_id']
                and self.video_owners.nearest(row['video_id'], row['at']) is None}

    def add_fallback(self, rows):
        for row in rows:
            if row['identity_key'] is not None:
                self.fallback.setdefault(row['video_id'], row['identity_key'])

    def nearest(self, key, at):
        return self.owners.nearest(key, at)

    def exit_key(self, video, at):
        historical = self.video_owners.nearest(video, at)
        return historical if historical is not None else self.fallback.get(video)
