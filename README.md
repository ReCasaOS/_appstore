<p align="center"><img src="https://raw.githubusercontent.com/ReCasaOS/.github/main/assets/recasaos-banner.svg" alt="ReCasaOS" width="640"></p>

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/recasaos)

[Support ReCasaOS](https://github.com/ReCasaOS/.github/blob/main/FUNDING.md)

# ReCasaOS App Store

This repository builds and serves the app catalogue of ReCasaOS, so that a box does not depend on one third-party store.

| Branch | Holds |
|---|---|
| `sync` (default) | the workflows and `catalogue/build.py`; nothing else |
| `catalogue` | **the ReCasaOS catalogue**: `store/recasaos.zip`, the merged store a box fetches, and a README with the app count of the last build |
| `main` | a byte-for-byte nightly copy of IceWhale's `store/main.zip`, the fallback of boxes that still name IceWhale's URL |

```
https://raw.githubusercontent.com/ReCasaOS/_appstore/catalogue/store/recasaos.zip
```

## The catalogue

At the first build (2026-11-04): **320 apps** from 326 candidates.

| Priority | Source | Licence | Apps kept |
|---|---|---|---|
| 1 | [IceWhaleTech/CasaOS-AppStore](https://github.com/IceWhaleTech/CasaOS-AppStore) (official) | Apache-2.0 | 179 |
| 2 | [roqueribeiro/roqueos-containers-list](https://github.com/roqueribeiro/roqueos-containers-list) | MIT | 80 |
| 3 | [ReCasaOS/big-bear-casaos](https://github.com/ReCasaOS/big-bear-casaos) (fork of the dead bigbeartechworld store, stale since 2024-01) | MIT | 61 |

The current count is in the `catalogue` branch's README and in the zip's `catalogue.json` (origin of every app, duplicates resolved, apps dropped and why); `LICENSES/` in the zip holds each source's licence.
The licence of a source covers its compose files, not the software they install. App compose files are kept exactly as their source ships them, so icons and screenshots are still served from the source's own repository.

Not used: `jborgwardt/appstore` is in Umbrel's format and carries no licence, so it can be neither merged as it is nor redistributed.

### Rules of the merge

1. **Duplicates**: when two sources ship the same app id (the compose `name`, which is what the store keys on), the higher priority wins. The loser is listed in `catalogue.json`. Two apps that are the same software under different ids (`dockge` and `big-bear-dockge`) are both kept.
2. **Invalid apps are dropped**: a compose file that does not parse, has no `name`, services, service images or `x-casaos` block, or an image that its registry says does not exist (`HEAD` on the manifest, anonymous; a network error keeps the app). A dropped candidate lets a lower-priority copy of the same id take its place. The first build dropped `ragflow` and `weknora` for that reason: the `minio/minio` image they need is gone from Docker Hub and quay.
3. **A dead source does not empty the store**: if a source cannot be downloaded, its apps are carried over from the last published catalogue.
4. **A shrinking catalogue is not published**: the run fails, and `catalogue` stays as it was, if the merge has fewer than 250 apps or fewer than 90 % of the published count.
5. **Same bytes, no commit**: the zip is deterministic, so an unchanged catalogue does not touch the branch.

Run `python catalogue/test_build.py` for the tests; `python catalogue/build.py --help` for a local build. Adding a source is an entry in `catalogue/sources.json`, its licence text in `catalogue/licenses/<id>.txt`, and a fetch line in `.github/workflows/catalogue.yml`.

The nightly copy on `main` is IceWhale's work, unedited; [`ReCasaOS/CasaOS-AppManagement`](https://github.com/ReCasaOS/CasaOS-AppManagement) falls back to it when IceWhale's URL stops answering.
