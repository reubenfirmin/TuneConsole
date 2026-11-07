"""Construct file collectors for the explicit group and recommendation roots."""
from . import group_documents, recommendation_outputs


class GroupEvidenceFiles:
    def __init__(self, groups, recommendations):
        self.groups, self.recommendations = groups, recommendations

    def collector(self, *, groups=False):
        if groups:
            owners = tuple((table, prefix + '_filename') for table, _keys, prefix, _kind in group_documents.OWNERS)
            return recommendation_outputs.OrphanCollector(self.groups, owners=owners)
        return recommendation_outputs.OrphanCollector(self.recommendations)
