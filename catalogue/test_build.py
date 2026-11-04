import json, sys, tempfile, unittest, zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build

SOURCES = [{"id": i, "name": i, "url": "u", "licence": "MIT"} for i in ("a", "b", "c")]


def compose(name, image="nginx:1", category="Media", extra=""):
    return f"name: {name}\nservices:\n  {name}:\n    image: {image}\n{extra}x-casaos:\n  main: {name}\n  category: {category}\n".encode()


def src(*apps, categories=None, recommend=None):
    out = {"apps": {n: compose(n, **kw) for n, kw in apps}}
    if categories is not None:
        out["category-list.json"] = categories
    if recommend is not None:
        out["recommend-list.json"] = recommend
    return out


ok = lambda ref: "ok"


class Merge(unittest.TestCase):
    def run_merge(self, loaded, previous=None, check=ok, **kw):
        return build.merge(SOURCES, loaded, previous, check, min_apps=kw.pop("min_apps", 1), **kw)

    def test_priority_wins_and_losers_are_recorded(self):
        files, rep = self.run_merge({"a": src(("x", {})), "b": src(("x", {"image": "other:1"}), ("y", {}))})
        self.assertIn(b"nginx:1", files["Apps/x/docker-compose.yml"])
        self.assertEqual(rep["overridden"], {"x": ["b"]})
        self.assertEqual({a["id"]: a["source"] for a in rep["apps"]}, {"x": "a", "y": "b"})

    def test_invalid_compose_and_missing_image_are_dropped_and_a_lower_source_fills_in(self):
        broken = {"apps": {"x": b"name: [", "z": b"name: Z\nservices: {}\n"}}
        check = lambda ref: "missing" if ref == "gone:1" else "ok"
        _, rep = self.run_merge({"a": broken, "b": src(("x", {"image": "gone:1"})), "c": src(("x", {}))}, check=check)
        self.assertEqual([a["source"] for a in rep["apps"]], ["c"])
        self.assertEqual({d["reason"].split(":")[0].split(" ")[0] for d in rep["dropped"]}, {"yaml", "bad", "image"})

    def test_network_mode_with_networks_is_dropped(self):
        both = compose("x", extra="    network_mode: host\n    networks: [n]\n")
        _, rep = self.run_merge({"a": {"apps": {"x": both}}, "b": src(("y", {}))})
        self.assertEqual([a["id"] for a in rep["apps"]], ["y"])

    def test_unknown_image_verdict_keeps_the_app(self):
        _, rep = self.run_merge({"a": src(("x", {}))}, check=lambda ref: "unknown")
        self.assertEqual(len(rep["apps"]), 1)

    def test_dead_source_is_carried_over_from_the_previous_build(self):
        files, _ = self.run_merge({"a": src(("x", {})), "b": src(("y", {}))})
        prev = {"manifest": json.loads(build.dump({"apps": [{"id": "x", "source": "a"}, {"id": "y", "source": "b"}]})), "files": files}
        files2, rep = self.run_merge({"a": src(("x", {})), "b": None}, prev)
        self.assertEqual([a["id"] for a in rep["apps"]], ["x", "y"])
        self.assertEqual(rep["sources"][1]["status"], "carried over from the previous build")
        self.assertEqual(files2["Apps/y/docker-compose.yml"], files["Apps/y/docker-compose.yml"])

    def test_sharp_drop_is_refused(self):
        prev = {"manifest": {"apps": [{"id": f"p{i}", "source": "a"} for i in range(10)]}, "files": {}}
        with self.assertRaises(build.Refused):
            self.run_merge({"a": src(*[(f"p{i}", {}) for i in range(8)])}, prev)  # 8 < 90% of 10, and `a` is fetched
        self.run_merge({"a": src(*[(f"p{i}", {}) for i in range(9)])}, prev)  # 9 is fine

    def test_floor_is_refused(self):
        with self.assertRaises(build.Refused):
            self.run_merge({"a": src(("x", {}))}, min_apps=2)

    def test_categories_extended_and_recommend_filtered(self):
        base = [{"name": "Media", "font": "m", "description": ""}]
        files, _ = self.run_merge({"a": src(("x", {}), ("y", {"category": "Storage"}), categories=base, recommend=[{"appid": "x"}, {"appid": "nope"}])})
        cats = json.loads(files["category-list.json"])
        self.assertEqual([c["name"] for c in cats], ["Media", "Storage"])
        self.assertEqual(cats[1]["font"], "harddisk")
        self.assertEqual(json.loads(files["recommend-list.json"]), [{"appid": "x"}])


class Pieces(unittest.TestCase):
    def test_parse_image(self):
        self.assertEqual(build.parse_image("nginx"), ("registry-1.docker.io", "library/nginx", "latest"))
        self.assertEqual(build.parse_image("ghcr.io/o/r:1.2"), ("ghcr.io", "o/r", "1.2"))
        self.assertEqual(build.parse_image("lscr.io:443/a/b@sha256:abc")[2], "sha256:abc")
        self.assertIsNone(build.parse_image("${IMG}:1"))
        with self.assertRaises(ValueError):
            build.parse_image("Bad Image:1")

    def test_pack_is_deterministic_and_find_apps_reads_nested_layouts(self):
        files = {"b": b"2", "a": b"1"}
        with tempfile.TemporaryDirectory() as t:
            build.pack(files, Path(t) / "1.zip")
            build.pack(dict(reversed(files.items())), Path(t) / "2.zip")
            self.assertEqual((Path(t) / "1.zip").read_bytes(), (Path(t) / "2.zip").read_bytes())
            d = Path(t) / "x" / "deep" / "Apps" / "app"
            d.mkdir(parents=True)
            (d / "docker-compose.yml").write_bytes(b"k")
            self.assertEqual(build.find_apps(Path(t) / "x")["apps"], {"app": b"k"})
            self.assertIsNone(build.find_apps(Path(t) / "missing"))


if __name__ == "__main__":
    unittest.main()
