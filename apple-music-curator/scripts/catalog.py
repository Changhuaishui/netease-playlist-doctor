#!/usr/bin/env python3
"""Resolve song metadata in a specified iTunes storefront using Apple's public API."""

import argparse
import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen


CLIENT_HINT = "在 Mac「音乐」中选择「Apple Music」搜索，并核对歌手、专辑和版本。"


def apple_resource(url):
    """Identify Apple resources even when a localized title changes the slug."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.netloc.lower() != "music.apple.com":
        return None
    match = re.fullmatch(r"/([a-z]{2})/(song|album|artist)/(?:[^/]+/)?([0-9]+)/?", parts.path)
    return match.groups() if match else None


def validate_album_track_url(url, country, album_id, catalog_id):
    """Return a normalized Apple Music URL for exactly this storefront, album and recording."""
    storefront = str(country).lower()
    album_id, catalog_id = str(album_id), str(catalog_id)
    if not all(re.fullmatch(r"[1-9][0-9]*", value) for value in (album_id, catalog_id)):
        raise ValueError("需要真实的十进制专辑和曲目 ID")
    if apple_resource(url) != (storefront, "album", album_id):
        raise ValueError("专辑链接的域名、地区或专辑 ID 不符")
    selected = parse_qs(urlsplit(url).query, keep_blank_values=True).get("i")
    if selected != [catalog_id]:
        raise ValueError("专辑链接未唯一指向指定曲目")
    return f"https://music.apple.com/{storefront}/album/{album_id}?i={catalog_id}"


def album_fields(urls, country, catalog_id, expected_album=None):
    # Album-only URLs can corroborate an ID. At least one URL must explicitly select the track.
    album_id = expected_album
    direct = None
    for url in urls:
        resource = apple_resource(url)
        if not resource or resource[:2] != (country.lower(), "album"):
            return {"album_id": None, "album_track_url": None}
        if album_id is not None and resource[2] != album_id:
            return {"album_id": None, "album_track_url": None}
        album_id = resource[2]
        if "i" in parse_qs(urlsplit(url).query, keep_blank_values=True):
            try:
                direct = validate_album_track_url(url, country, album_id, catalog_id)
            except ValueError:
                return {"album_id": None, "album_track_url": None}
    return {"album_id": album_id if direct else None, "album_track_url": direct}


class SongPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.canonical = ""
        self.meta = {}
        self.labels = {}
        self.current_resource = None
        self.label_parts = []
        self.in_title = False
        self.title_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and "canonical" in (attrs.get("rel") or "").lower().split():
            self.canonical = attrs.get("href") or ""
        elif tag == "meta" and attrs.get("property") and attrs.get("content"):
            self.meta.setdefault(attrs["property"], []).append(attrs["content"])
        elif tag == "h1" and not self.title_parts:
            self.in_title = True
        elif tag == "a":
            self.current_resource = apple_resource(attrs.get("href") or "")
            self.label_parts = []

    def handle_data(self, data):
        if self.current_resource:
            self.label_parts.append(data)
        if self.in_title:
            self.title_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "h1":
            self.in_title = False
        elif tag == "a" and self.current_resource:
            label = "".join(self.label_parts).strip()
            if label:
                self.labels.setdefault(self.current_resource, label)
            self.current_resource = None

    def meta_value(self, key):
        return next(iter(self.meta.get(key, [])), None)

    def named_resources(self, key):
        names = [self.labels.get(apple_resource(url)) for url in self.meta.get(key, [])]
        return list(dict.fromkeys(name for name in names if name))


def read_song_page(country, identifier):
    expected = (country.lower(), "song", identifier)
    url = f"https://music.apple.com/{country.lower()}/song/{identifier}"
    request = Request(url, headers={"Accept": "text/html", "User-Agent": "AppleMusicCurator/1.0"})
    with urlopen(request, timeout=15) as response:
        if apple_resource(response.geturl()) != expected:
            raise ValueError("最终网址与请求的地区或曲目不符")
        page = SongPage()
        page.feed(response.read().decode("utf-8"))
        page.close()

    if apple_resource(page.canonical) != expected:
        raise ValueError("页面 canonical 未指向请求地区的指定曲目")
    name = "".join(page.title_parts).strip()
    if page.meta_value("og:type") != "music.song" or not name:
        raise ValueError("缺少歌曲类型或曲名，无法核验页面")
    return {
        "catalog_id": identifier,
        "name": name,
        "artist": ", ".join(page.named_resources("music:musician")) or None,
        "album": next(iter(page.named_resources("music:album")), None),
        "url": page.canonical,
        "description": page.meta_value("og:description"),
        "release_date": page.meta_value("music:release_date"),
        **album_fields(page.meta.get("music:album", []), country, identifier),
    }


def country_code(value):
    if not re.fullmatch(r"[A-Za-z]{2}", value):
        raise argparse.ArgumentTypeError("地区须为两位代码，例如 CN 或 US。")
    return value.upper()


def track_id(value):
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("请提供 Apple 目录曲目的十进制 ID。")
    return value


def public_request(endpoint, params):
    url = "https://itunes.apple.com/" + endpoint + "?" + urlencode(params)
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "AppleMusicCurator/1.0"})
    with urlopen(request, timeout=15) as response:
        data = json.load(response)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("Apple 返回的结果格式异常")
    return data["results"]


def song_records(results, wanted_ids, country):
    records = {}
    inconsistent_albums = set()
    for item in results:
        if not isinstance(item, dict) or item.get("kind") != "song":
            continue
        identifier = str(item.get("trackId", ""))
        if not identifier.isdecimal() or not item.get("trackName") or not item.get("artistName"):
            continue
        if wanted_ids and identifier not in wanted_ids:
            continue
        collection_id = str(item.get("collectionId", ""))
        urls = [item[k] for k in ("trackViewUrl", "collectionViewUrl") if item.get(k)]
        album = album_fields(urls, country, identifier, expected_album=collection_id)
        if identifier in records and records[identifier]["album_id"] != album["album_id"]:
            inconsistent_albums.add(identifier)
        records[identifier] = {
            "catalog_id": identifier,
            "name": item["trackName"],
            "artist": item["artistName"],
            "album": item.get("collectionName"),
            "url": item.get("trackViewUrl"),
            "duration_ms": item.get("trackTimeMillis"),
            "release_date": item.get("releaseDate"),
            "genre": item.get("primaryGenreName"),
            "explicitness": item.get("trackExplicitness"),
            "is_streamable": item.get("isStreamable"),
            **album,
        }
    for identifier in inconsistent_albums:
        records[identifier].update(album_id=None, album_track_url=None)
    if wanted_ids:
        return [records[identifier] for identifier in wanted_ids if identifier in records]
    return list(records.values())


def open_album(country, catalog_id, album_id=None):
    """Recheck the public song page, then ask Launch Services to navigate Music."""
    track = read_song_page(country, catalog_id)
    expected_album = track["album_id"]
    if not expected_album or (album_id is not None and album_id != expected_album):
        raise ValueError("公开页面没有核实该版本的专辑 ID")
    url = validate_album_track_url(track["album_track_url"], country, expected_album, catalog_id)
    subprocess.run(["/usr/bin/open", "-a", "Music", url], check=True)
    return {"navigation_requested": True, "track": track}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="按歌手、歌名和版本查候选")
    lookup = commands.add_parser("lookup", help="按 Apple 目录曲目 ID 核对")
    page = commands.add_parser("page", help="核对指定地区的 Apple Music 公开曲目页")
    navigate = commands.add_parser("open-album", help="核对录音版本并在 Mac 音乐中打开相应专辑曲目")
    for command in (search, lookup, page, navigate):
        command.add_argument("--country", required=True, type=country_code)
    search.add_argument("--term", required=True)
    search.add_argument("--limit", type=int, default=8, metavar="1..200")
    for command in (lookup, page):
        command.add_argument("--id", dest="ids", action="append", type=track_id, required=True)
    navigate.add_argument("--id", type=track_id, required=True)
    navigate.add_argument("--album-id", type=track_id, help="要求与公开歌曲页一致的专辑 ID")
    args = parser.parse_args()

    if args.command == "open-album":
        try:
            result = open_album(args.country, args.id, args.album_id)
        except (HTTPException, OSError, ValueError, subprocess.CalledProcessError) as exc:
            print(f"无法打开已核实的专辑曲目：{exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "page":
        tracks = []
        errors = []
        for identifier in dict.fromkeys(args.ids):
            try:
                tracks.append(read_song_page(args.country, identifier))
            except (HTTPError, URLError, HTTPException, OSError, ValueError) as exc:
                errors.append({"catalog_id": identifier, "error": str(exc)})
        output = {"country": args.country, "source": "Apple Music public song page", "tracks": tracks, "errors": errors}
        if errors:
            output["hint"] = CLIENT_HINT
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return int(bool(errors))

    wanted_ids = []
    params = {"country": args.country}
    if args.command == "search":
        if not args.term.strip() or not 1 <= args.limit <= 200:
            parser.error("搜索词不能为空，limit 应为 1 至 200。")
        params.update(term=args.term.strip(), media="music", entity="song", limit=args.limit)
    else:
        wanted_ids = list(dict.fromkeys(args.ids))
        params["id"] = ",".join(wanted_ids)

    try:
        tracks = song_records(public_request(args.command, params), wanted_ids, args.country)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        print(f"Apple 目录查询失败（{args.country}，{exc}）。{CLIENT_HINT}", file=sys.stderr)
        return 1

    output = {"country": args.country, "source": "iTunes Search API", "tracks": tracks}
    if wanted_ids:
        found = {track["catalog_id"] for track in tracks}
        output["missing_ids"] = [identifier for identifier in wanted_ids if identifier not in found]
    if not tracks or output.get("missing_ids"):
        output["hint"] = f"该地区的 iTunes 查询未返回部分或全部曲目。{CLIENT_HINT}"
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
