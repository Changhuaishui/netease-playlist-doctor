"""Offline tests for navigation to a specific recording in Apple's storefront."""

import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "catalog.py"
spec = importlib.util.spec_from_file_location("catalog", SCRIPT)
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)

SONG = "https://music.apple.com/cn/song/title/23"
ALBUM = "https://music.apple.com/cn/album/the-album/17"
DIRECT = "https://music.apple.com/cn/album/17?i=23"


def response(album_urls, final=SONG, canonical=SONG):
    metadata = "".join(f'<meta property="music:album" content="{url}">' for url in album_urls)
    page = io.BytesIO(f"""<link rel="canonical" href="{canonical}">
        <meta property="og:type" content="music.song">{metadata}
        <h1>Title (Live)</h1><a href="{ALBUM}">Original album</a>""".encode())
    page.geturl = lambda: final
    return page


def store_record(**changes):
    return {
        "kind": "song", "trackId": 23, "trackName": "Title (Live)", "artistName": "Artist",
        "collectionId": 17, "collectionName": "Original album", "trackViewUrl": ALBUM + "?i=23&uo=4",
        "collectionViewUrl": ALBUM,
        **changes,
    }


class AlbumNavigationTests(unittest.TestCase):
    def test_public_song_page(self):
        with patch.object(catalog, "urlopen", return_value=response([ALBUM, ALBUM + "?i=23"])):
            track = catalog.read_song_page("CN", "23")
        self.assertEqual((track["name"], track["album"]), ("Title (Live)", "Original album"))
        self.assertEqual((track["album_id"], track["album_track_url"]), ("17", DIRECT))

    def test_unproven_or_conflicting_album_on_otherwise_valid_song(self):
        for urls in ([], [ALBUM], [ALBUM + "?i=24"], [ALBUM + "?i=23", ALBUM.replace("17", "18")],
                     [ALBUM.replace("/cn/", "/hk/") + "?i=23"]):
            with self.subTest(urls=urls), patch.object(catalog, "urlopen", return_value=response(urls)):
                track = catalog.read_song_page("CN", "23")
                self.assertEqual(track["url"], SONG)
                self.assertIsNone(track["album_id"])
                self.assertIsNone(track["album_track_url"])

    def test_wrong_song_page(self):
        for final, canonical in ((SONG.replace("/cn/", "/us/"), SONG), (SONG, SONG.replace("23", "26"))):
            with patch.object(catalog, "urlopen", return_value=response([DIRECT], final, canonical)):
                with self.assertRaises(ValueError):
                    catalog.read_song_page("CN", "23")

    def test_store_records(self):
        for ids in ([], ["23"]):  # search and lookup share the same projection.
            track, = catalog.song_records([store_record()], ids, "CN")
            self.assertEqual(track["album_track_url"], DIRECT)
            self.assertEqual(track["album_id"], "17")
            self.assertEqual(track["url"], ALBUM + "?i=23&uo=4")
        for changes in ({"collectionId": None}, {"collectionId": 19},
                        {"trackViewUrl": DIRECT.replace("/cn/", "/us/")},
                        {"collectionViewUrl": DIRECT.replace("17", "19")}):
            track, = catalog.song_records([store_record(**changes)], ["23"], "CN")
            self.assertIsNone(track["album_track_url"])
        other_album = store_record(collectionId=19, trackViewUrl=DIRECT.replace("17", "19"),
                                   collectionViewUrl=ALBUM.replace("17", "19"))
        track, = catalog.song_records([store_record(), other_album, other_album], [], "CN")
        self.assertIsNone(track["album_track_url"])

    def test_url_identity(self):
        self.assertEqual(catalog.validate_album_track_url(ALBUM + "?uo=4&i=23", "CN", 17, 23), DIRECT)
        for url in (None, ALBUM, DIRECT + "&i=23", DIRECT + "&i=", DIRECT.replace("?i=23", "?i=21"),
                    DIRECT.replace("music.apple.com", "music.apple.com.example"),
                    DIRECT.replace("/cn/", "/us/"), DIRECT.replace("17", "18")):
            with self.subTest(url=url), self.assertRaises(ValueError):
                catalog.validate_album_track_url(url, "CN", "17", "23")

    def test_open_is_navigation_only_and_requires_page_match(self):
        with patch.object(catalog, "urlopen", return_value=response([DIRECT])), patch.object(catalog.subprocess, "run") as run:
            result = catalog.open_album("CN", "23", "17")
            self.assertTrue(result["navigation_requested"])
            run.assert_called_once_with(["/usr/bin/open", "-a", "Music", DIRECT], check=True)
        with patch.object(catalog, "urlopen", return_value=response([DIRECT])), patch.object(catalog.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                catalog.open_album("CN", "23", "29")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
