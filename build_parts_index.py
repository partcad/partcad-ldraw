#!/usr/bin/env python3
#
# partcad-ldraw, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Build 'parts-index.json.gz' from the official LDraw library archive.

The index is what lets the repository plugin answer "which categories are
there", "which parts are in this category" and "what is this part called"
without going to the network at all. Only geometry is fetched on demand, by
ldraw.py, at render time.

Two sources, each used for what it is authoritative about.

Which parts are in which category comes from ldraw.org's own list pages,
through the plugin's own _categories() and _part_ids(). That is what the package
paths have always been built from, and the archive does not reproduce it: it
holds 24,735 parts under 'parts/' where the site lists 20,569. The difference is
largely the 4,538 whose description carries a '~', '=' or '_' marker - moved-to
stubs, aliases and colour variants - but not exactly, since some aliases are
listed. Rather than guess at that filter, this reads the listings: ~1000
requests for the whole library, at 25 rows a page.

What a part is called - and what it connects with - comes from
https://library.ldraw.org/library/updates/complete.zip - one download carrying
the whole official library. Reading names from the site meant one HTTP
request per part, ~25,000 of them, rate-limited well before finishing. The
interfaces are worse: they are read from each part's geometry, so computing
them at run time means fetching every part's .dat as well, and it is 67 seconds
for one category even when all of it is already on disk. Both are done here
instead, against the archive, and the answers ship.

Both are build-time costs, paid by whoever runs this, and neither is paid by
anyone using the package.

Needs Python 3.10 or newer, as the plugin it imports does.

Usage:
    ./build_parts_index.py [--archive complete.zip] [--output parts-index.json.gz]

Run it when the LDraw library publishes an update; commit the result. The
list pages are cached on disk between runs like every other fetch, so a second
run is cheap.
"""

import argparse
import datetime
import importlib.util
import gzip
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile

ARCHIVE_URL = "https://library.ldraw.org/library/updates/complete.zip"
FORMAT = 2  # entries are [desc, author, license, implements]

_AUTHOR_LINE = re.compile(r"^0\s+Author:\s*(.+?)\s*$")
_LICENSE_LINE = re.compile(r"^0\s+!LICENSE\s+(.+?)\s*$")


def _header(text):
    """(description, author, license) out of a .dat header.

    The category is deliberately not read here: '!CATEGORY' is absent from most
    parts, and the rule that fills the gap - the first word of the description -
    does not reproduce what ldraw.org files a part under. See the module
    docstring.
    """
    desc = author = lic = None
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if desc is None:
            # The very first line is the description, with no keyword.
            desc = line[1:].strip() if line.startswith("0") else ""
            continue
        if not line.startswith("0"):
            break  # the header is over once geometry starts
        if author is None:
            m = _AUTHOR_LINE.match(line)
            if m:
                author = m.group(1)
                continue
        if lic is None:
            m = _LICENSE_LINE.match(line)
            if m:
                lic = m.group(1)
    return desc, author, lic


def _archive_headers(archive):
    """{part_id: (desc, author, license)} for every part in the library."""
    headers = {}
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            # Parts only. Subparts live under 'parts/s/' and primitives under
            # 'p/'; neither is a part, and neither is listed as one.
            if not re.fullmatch(r"ldraw/parts/[^/]+\.dat", name, re.IGNORECASE):
                continue
            pid = os.path.basename(name)[:-4]
            desc, author, lic = _header(z.read(info).decode("latin-1"))
            headers[pid.lower()] = (desc, author, lic)
    return headers


def _seed_cache(archive, cache):
    """Lay the archive's parts out where the plugin's fetcher looks for them.

    _fetch_ldraw_file() reads '<cache>/parts/<name>', lowercased, so extracting
    'ldraw/parts/**' there lets the interface derivation run against the archive
    with no network at all.
    """
    n = 0
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            m = re.match(r"ldraw/parts/(.+\.dat)$", info.filename.replace("\\", "/"), re.IGNORECASE)
            if not m:
                continue
            dest = os.path.join(cache, "parts", m.group(1).lower())
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(z.read(info))
            n += 1
    return n


def build(archive, plugin):
    headers = _archive_headers(archive)
    strings = {}

    def intern(value):
        if value is None:
            return -1
        if value not in strings:
            strings[value] = len(strings)
        return strings[value]

    categories = {}
    missing = []
    for sub, category in sorted(plugin._categories().items()):
        ids = plugin._part_ids(category)
        if not ids:
            print("  %s: no parts listed" % category, file=sys.stderr)
            continue
        entry = {}
        for pid in ids:
            header = headers.get(pid.lower())
            if header is None:
                # In the site's listing but not in the official archive: an
                # unofficial part. Recorded with no metadata so that it still
                # enumerates, and left to be fetched on demand.
                missing.append(pid)
                entry[pid] = ["", -1, -1, None]
                continue
            desc, author, lic = header
            # What the part connects with, worked out now so that nobody has to
            # work it out later: this is the expensive half.
            implements = plugin._part_config(pid, header).get("implements")
            entry[pid] = [desc or "", intern(author), intern(lic), implements]
        categories[category] = entry
        print("  %s: %d parts" % (category, len(entry)), file=sys.stderr)

    ordered = sorted(strings, key=strings.get)
    return {
        "format": FORMAT,
        "source": ARCHIVE_URL,
        "generated": datetime.date.today().isoformat(),
        "strings": ordered,
        "categories": {name: dict(sorted(parts.items())) for name, parts in sorted(categories.items())},
    }, missing


def main():
    if sys.version_info < (3, 10):
        # The plugin this imports uses zip(strict=True). Say so here rather
        # than let it surface as a TypeError partway through the crawl.
        sys.exit("build_parts_index.py needs Python 3.10 or newer (found %d.%d)" % sys.version_info[:2])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", default=None, help="a local complete.zip; downloaded if omitted")
    parser.add_argument("--output", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "parts-index.json.gz"))
    args = parser.parse_args()

    archive = args.archive
    if not archive:
        archive = "complete.zip"
        print("downloading %s ..." % ARCHIVE_URL, file=sys.stderr)
        req = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "PartCAD ldraw index builder"})
        with urllib.request.urlopen(req) as resp, open(archive, "wb") as f:
            f.write(resp.read())

    cache = tempfile.mkdtemp(prefix="ldraw-index-")
    print("laying out the archive for the interface derivation ...", file=sys.stderr)
    os.environ["PARTCAD_LDRAW_CACHE"] = cache
    # The index being built must not be read while building it.
    os.environ["PARTCAD_LDRAW_IGNORE_INDEX"] = "1"

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "ldraw_repo", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ldraw_repo.py")
    )
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)

    print("  %d part files" % _seed_cache(archive, cache), file=sys.stderr)
    print("reading the category listings ...", file=sys.stderr)
    index, missing = build(archive, plugin)
    with gzip.open(args.output, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(index, f, separators=(",", ":"), sort_keys=False)

    shutil.rmtree(cache, ignore_errors=True)
    parts = sum(len(p) for p in index["categories"].values())
    print(
        "%s: %d categories, %d parts (%d with interfaces), "
        "%d distinct authors/licenses, %d listed but not in the official "
        "archive, %.1f KiB"
        % (
            os.path.basename(args.output),
            len(index["categories"]),
            parts,
            sum(1 for c in index["categories"].values() for e in c.values() if e[3]),
            len(index["strings"]),
            len(missing),
            os.path.getsize(args.output) / 1024.0,
        )
    )


if __name__ == "__main__":
    main()
