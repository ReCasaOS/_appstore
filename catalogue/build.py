#!/usr/bin/env python3
"""Merge several CasaOS-format app stores into one store zip (see ../README.md).

    build.py --src icewhale=DIR --src roqueos=DIR ... --previous prev.zip \
             --out store/recasaos.zip --readme README.md [--check-images]

A --src directory is any tree holding an `Apps/<dir>/docker-compose.yml` layout
(a GitHub archive, IceWhale's nested build zip, a sparse checkout). A source
that is missing or empty is carried over from --previous, so one dead source
never empties the store; a merge that shrinks sharply raises Refused.
"""
import argparse, concurrent.futures, io, json, re, sys, urllib.error, urllib.request, zipfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
REPO_RE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*$")
TAG_RE = re.compile(r"^\w[\w.-]{0,127}$")
# mdi icon names for categories the base category list lacks; unknown ones get the UI's default
CATEGORY_FONTS = {
    "Utilities": "tools", "Network": "lan", "Storage": "harddisk", "Home Automation": "home-automation",
    "Communication": "message-text-outline", "Gaming": "gamepad-variant-outline", "Security": "shield-lock-outline",
}
MANIFEST_TYPES = ",".join([
    "application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json", "application/vnd.docker.distribution.manifest.v2+json",
])


class Refused(Exception):
    """The merge must not be published."""


def find_apps(root):
    """{"apps": {dir name: compose bytes}, plus the sibling JSON lists} for the first `Apps` dir under root, or None."""
    root = Path(root)
    if not root.is_dir():
        return None
    dirs = sorted(p for p in root.rglob("Apps") if p.is_dir())
    if not dirs:
        return None
    store, apps = dirs[0].parent, {}
    for d in sorted(p for p in dirs[0].iterdir() if p.is_dir()):
        for name in ("docker-compose.yml", "docker-compose.yaml"):
            if (d / name).is_file():
                apps[d.name] = (d / name).read_bytes()
                break
    found = {"apps": apps}
    for key in ("category-list.json", "recommend-list.json"):
        try:
            data = json.loads((store / key).read_text(encoding="utf8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, list):  # RoqueOS's category-list.json is another schema: an object
            found[key] = data
    return found


def check_compose(raw):
    """(document, None) for a compose file the store can show, else (None, reason)."""
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return None, "yaml: " + str(e).splitlines()[0]
    if not isinstance(doc, dict):
        return None, "not a mapping"
    if not isinstance(doc.get("name"), str) or not NAME_RE.match(doc["name"]):
        return None, f"bad name {doc.get('name')!r}"
    services = doc.get("services")
    if not isinstance(services, dict) or not services:
        return None, "no services"
    for s, svc in services.items():
        if not isinstance(svc, dict) or not isinstance(svc.get("image"), str):
            return None, f"service {s} has no image"
        if "network_mode" in svc and "networks" in svc:  # compose refuses it; AppManagement skips the app
            return None, f"service {s} declares network_mode and networks"
    if not isinstance(doc.get("x-casaos"), dict):  # AppManagement tolerates a `main` that is no service key (AlbyHub)
        return None, "no x-casaos"
    return doc, None


def parse_image(ref):
    """(registry host, repository, tag or digest); None when it cannot be checked ($VAR); ValueError when malformed."""
    if "$" in ref:
        return None
    name, _, digest = ref.partition("@")
    tag = "latest"
    if ":" in name.rsplit("/", 1)[-1]:
        name, tag = name.rsplit(":", 1)
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        host, repo = parts[0], "/".join(parts[1:])
    else:
        host, repo = "registry-1.docker.io", name
        if "/" not in repo:
            repo = "library/" + repo
    if not REPO_RE.match(repo) or not TAG_RE.match(tag):
        raise ValueError(ref)
    return host, repo, digest or tag


def _head(url, token=None):
    headers = {"Accept": MANIFEST_TYPES}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers=headers), timeout=20) as r:
            return r.status, r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.headers
    except (OSError, ValueError):
        return None, {}


def check_image_online(ref):
    """'ok', 'missing' (registry says no, or refuses an anonymous pull), 'invalid', or 'unknown' (network trouble: keep)."""
    try:
        parsed = parse_image(ref)
    except ValueError:
        return "invalid"
    if parsed is None:
        return "unknown"
    host, repo, target = parsed
    url = f"https://{host}/v2/{repo}/manifests/{target}"
    token = None
    for _ in range(3):  # shortcut: no backoff; upgrade if registries start throttling the nightly run
        status, headers = _head(url, token)
        if status == 401 and token is None:
            m = dict(re.findall(r'(\w+)="([^"]*)"', headers.get("www-authenticate", "")))
            if "realm" not in m:
                return "missing"
            try:
                with urllib.request.urlopen(f"{m['realm']}?service={m.get('service', '')}&scope=repository:{repo}:pull", timeout=20) as r:
                    body = json.load(r)
                token = body.get("token") or body.get("access_token")
            except (OSError, ValueError):
                continue
            if not token:
                return "unknown"
            continue
        if status == 200:
            return "ok"
        if status in (401, 403, 404):
            return "missing"
    return "unknown"


def dump(obj):
    return (json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode()


def merge(sources, loaded, previous, check_image, min_ratio=0.9, min_apps=250):
    """-> (files {path: bytes}, report). `loaded` {source id: find_apps() result or None}; `previous` the last
    published catalogue as {"manifest": dict, "files": {path: bytes}} or None."""
    prev_files = (previous or {}).get("files", {})
    prev_apps = (previous or {}).get("manifest", {}).get("apps", [])
    status, candidates = {}, []  # candidates in priority order: (source, dir, raw)
    for src in sources:
        got = loaded.get(src["id"])
        if got and got["apps"]:
            status[src["id"]] = "fetched"
            candidates += [(src, d, raw) for d, raw in got["apps"].items()]
        else:
            carried = [a for a in prev_apps if a["source"] == src["id"]]
            status[src["id"]] = "carried over from the previous build" if carried else "unavailable"
            candidates += [(src, a["id"], prev_files[f"Apps/{a['id']}/docker-compose.yml"]) for a in carried]

    parsed, dropped = [], []
    for src, d, raw in candidates:
        doc, err = check_compose(raw)
        if err:
            dropped.append({"id": d, "source": src["id"], "reason": err})
        else:
            parsed.append((src, doc, raw))

    refs = sorted({s["image"] for _, doc, _ in parsed for s in doc["services"].values()})
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        verdict = dict(zip(refs, pool.map(check_image, refs)))

    apps, overridden = {}, {}
    for src, doc, raw in parsed:
        bad = [r for r in (s["image"] for s in doc["services"].values()) if verdict[r] in ("missing", "invalid")]
        if doc["name"] in apps:
            overridden.setdefault(doc["name"], []).append(src["id"])
        elif bad:  # a lower-priority source may still supply a working copy of the same id
            dropped.append({"id": doc["name"], "source": src["id"], "reason": "image " + ", ".join(bad)})
        else:
            apps[doc["name"]] = {"source": src["id"], "raw": raw, "category": doc["x-casaos"].get("category")}

    prev_count = len(prev_apps)
    if len(apps) < min_apps or (prev_count and len(apps) < min_ratio * prev_count):
        raise Refused(f"{len(apps)} apps after the merge, {prev_count} before: not published")

    def first_list(key):
        for s in sources:
            if loaded.get(s["id"]) and key in loaded[s["id"]]:
                return loaded[s["id"]][key]
        return json.loads(prev_files.get(key, b"[]"))

    files = {f"Apps/{i}/docker-compose.yml": a["raw"] for i, a in sorted(apps.items())}
    categories = [dict(c) for c in first_list("category-list.json")]
    for cat in sorted({a["category"] for a in apps.values() if isinstance(a["category"], str)} - {c["name"] for c in categories}):
        categories.append({"name": cat, **({"font": CATEGORY_FONTS[cat]} if cat in CATEGORY_FONTS else {}), "description": f"{cat} Apps"})
    files["category-list.json"] = dump(categories)
    files["recommend-list.json"] = dump([r for r in first_list("recommend-list.json") if r.get("appid") in apps])
    report = {
        "sources": [{**{k: s[k] for k in ("id", "name", "url", "licence")}, "status": status[s["id"]],
                     "apps": sum(a["source"] == s["id"] for a in apps.values())} for s in sources],
        "apps": [{"id": i, "source": a["source"]} for i, a in sorted(apps.items())],
        "overridden": overridden, "dropped": dropped,
    }
    return files, report


def readme(report):
    rows = "\n".join(f"| [{s['name']}]({s['url']}) | {s['licence']} | {s['status']} | {s['apps']} |" for s in report["sources"])
    kept = {a["id"]: a["source"] for a in report["apps"]}
    dup = "\n".join(f"- `{i}`: kept from `{kept[i]}`, also in {', '.join(f'`{x}`' for x in v)}" for i, v in sorted(report["overridden"].items())) or "- none"
    drop = "\n".join(f"- `{d['id']}` ({d['source']}): {d['reason']}" for d in report["dropped"]) or "- none"
    return f"""# ReCasaOS app catalogue

**{len(report['apps'])} apps**, merged from the sources below by `catalogue/build.py` and rebuilt every night.
`store/recasaos.zip` is the store a ReCasaOS box fetches. Apps keep their upstream compose file unchanged;
`catalogue.json` records the origin of each one and `LICENSES/` holds each source's licence text.
The licence of a source covers its compose files, not the software they install.

| Source (priority order) | Licence | This build | Apps kept |
|---|---|---|---|
{rows}

When two sources ship the same app id, the one higher in the table wins. An app is dropped when its compose
file is invalid or a registry says one of its images does not exist.

## Duplicates resolved ({len(report['overridden'])})
{dup}

## Dropped ({len(report['dropped'])})
{drop}
"""


def pack(files, out):
    """Deterministic zip: sorted names, fixed timestamps, so an unchanged catalogue is the same bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            z.writestr(info, files[name], zipfile.ZIP_DEFLATED)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_bytes(buf.getvalue())


def read_previous(path):
    if not path or not Path(path).is_file():
        return None
    try:
        with zipfile.ZipFile(path) as z:
            files = {n: z.read(n) for n in z.namelist() if not n.endswith("/")}
        return {"manifest": json.loads(files["catalogue.json"]), "files": files}
    except (zipfile.BadZipFile, KeyError, ValueError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", action="append", default=[], metavar="ID=DIR")
    ap.add_argument("--sources", default=HERE / "sources.json")
    ap.add_argument("--licenses", default=HERE / "licenses")
    ap.add_argument("--previous")
    ap.add_argument("--out", required=True)
    ap.add_argument("--readme")
    ap.add_argument("--check-images", action="store_true")
    ap.add_argument("--min-apps", type=int, default=250)
    ap.add_argument("--min-ratio", type=float, default=0.9)
    a = ap.parse_args(argv)
    sources = json.loads(Path(a.sources).read_text(encoding="utf8"))
    loaded = {i: find_apps(d) for i, _, d in (s.partition("=") for s in a.src)}
    try:
        files, report = merge(sources, loaded, read_previous(a.previous),
                              check_image_online if a.check_images else (lambda ref: "unknown"), a.min_ratio, a.min_apps)
    except Refused as e:
        print("REFUSED:", e, file=sys.stderr)
        return 1
    text = readme(report).encode()
    files["README.md"] = text
    files["catalogue.json"] = dump({"schema": 1, **report})
    for s in sources:
        files[f"LICENSES/{s['id']}.txt"] = (Path(a.licenses) / f"{s['id']}.txt").read_bytes()
    pack(files, a.out)
    if a.readme:
        Path(a.readme).write_bytes(text)
    print(f"{len(report['apps'])} apps |", ", ".join(f"{s['id']}: {s['apps']} ({s['status']})" for s in report["sources"]))
    for d in report["dropped"]:
        print("dropped", d["id"], d["source"], d["reason"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
