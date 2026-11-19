import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from reconcile import reconcile


def plan(**overrides):
    return dict({"catalog_id": "10", "name": "Home", "artist": "Singer", "album": "Spring",
                 "duration_ms": 200_000}, **overrides)


def row(**overrides):
    return dict({"persistentID": "A1", "name": "Home", "artist": "Singer", "album": "Spring",
                 "duration": 200}, **overrides)


class ReconcileTests(unittest.TestCase):
    def check_status(self, expected, tracks, snapshot):
        actual = reconcile(tracks, snapshot)
        self.assertEqual([record["status"] for record in actual], expected)
        self.assertTrue(all("persistentID" not in record for record in actual
                            if record["status"] != "present"))
        return actual

    def test_catalog_id_and_prior_local_id(self):
        track = plan(library_id="a1")
        self.check_status(["present"], [track], [row(name="Translated", catalog_id=10)])
        self.check_status(["present"], [track], [row(name="Translated")])
        self.check_status(["review"], [track], [row(catalog_id=11)])
        self.check_status(["missing"], [plan()], [row(catalog_id=11)])

    def test_exact_metadata_optional_duration_and_rounding(self):
        for seconds in [None, 200, 201.5]:
            with self.subTest(seconds=seconds):
                result = self.check_status(["present"], [plan()], [row(duration=seconds)])
                self.assertEqual(result[0]["persistentID"], "A1")
        self.check_status(["review"], [plan()], [row(duration=211)])

    def test_multiple_versions_and_missing_fields(self):
        for changes in [dict(name="Home (Live)"), dict(name="Home - Remastered"),
                        dict(name="Home", album="Tour"), dict(name="Home", artist="Other singer"),
                        dict(artist=None), dict(album=""), dict(name=None), dict(persistentID=None)]:
            with self.subTest(changes=changes):
                self.check_status(["review"], [plan()], [row(**changes)])
        self.check_status(["review"], [plan(album="")], [])
        self.check_status(["missing"], [plan()], [])

    @unittest.skipUnless(sys.platform == "darwin", "uses macOS Traditional-Simplified transform")
    def test_localized_metadata_only_flags_for_review(self):
        self.check_status(["review"], [plan(name="风的形状", artist="岑宁儿")],
                          [row(name="風的形狀", artist="Yoyo Sham")])

    def test_single_local_row_cannot_satisfy_two_plan_entries(self):
        self.check_status(["review", "review"], [plan(), plan(catalog_id="11")], [row()])
        self.check_status(["present", "review"], [plan(library_id="A1"), plan(catalog_id="11")],
                          [row()])
        self.check_status(["present", "missing"], [plan(), plan(catalog_id="11")],
                          [row(catalog_id="10")])
        self.check_status(["review"], [plan()], [row(), row(persistentID="A2")])

    def test_preserves_plan_order(self):
        actual = self.check_status(["missing", "present"], [plan(catalog_id="20", name="Other"),
                                   plan()], [row(catalog_id="10")])
        self.assertEqual([r["catalog_id"] for r in actual], ["20", "10"])


if __name__ == "__main__":
    unittest.main()
