from yt_playlist.web.routes.charts import _more_albums


def test_more_albums_excludes_collection_album_by_browse_id_and_title():
    collection = [{"album": "Strawberry Hotel", "browse": "MPREb_strawberry"}]
    discography = [
        {"title": "Strawberry Hotel", "browse_id": "MPREb_strawberry"},
        {"title": "  STRAWBERRY HOTEL ", "browse_id": "different_duplicate"},
        {"title": "Dubnobasswithmyheadman", "browse_id": "MPREb_dubno"},
    ]

    assert _more_albums(discography, collection) == [discography[2]]
