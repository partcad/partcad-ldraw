#!/usr/bin/env python3
#
# partcad-ldraw, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Build 'parts-index.zip' from the official LDraw library archive.

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

The output is a zip with one member per category rather than one compressed
document, because the plugin is run afresh for every key PartCAD asks for and
a key that names one category must not pay for the other 91. See the layout
note in ldraw_repo.py.

Usage:
    ./build_parts_index.py [--archive complete.zip] [--output parts-index.zip]
    ./build_parts_index.py --refresh LIBRARY --reaching PRIMITIVE [PRIMITIVE ...]

The second form is for a change to how connectors are read, rather than to the
library: it recomputes only the entries of parts whose geometry reaches one of
the named primitives - read from LIBRARY, an unpacked 'ldraw' directory - and
takes a new entry only where it keeps every port the old one had, instance
names included. Everything else in the index is left exactly as it is, so it
needs neither the archive nor the category listings, and cannot move a port an
assembly already names. A part that would lose or move a port is reported and
keeps its old entry: that is a change to the rules, and the full rebuild is the
place for it.

Run it when the LDraw library publishes an update; commit the result. The
list pages are cached on disk between runs like every other fetch, so a second
run is cheap.
"""

import argparse
import datetime
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile

ARCHIVE_URL = "https://library.ldraw.org/library/updates/complete.zip"
FORMAT = 3  # a zip: 'index.json' plus one member per category

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


def write(index, path, plugin):
    """Write the index as the zip the plugin reads.

    'index.json' carries what every key needs - the categories, the part ids in
    each, and the interned authors and licenses - and each category's parts go
    in a member of their own, which a key reads only when it names that
    category.
    """
    members = {}
    for category in index["categories"]:
        member = plugin.member_name(category)
        if member in members:
            raise SystemExit(
                "two categories share one index member (%s): %r and %r"
                % (member, members[member], category)
            )
        members[member] = category
    meta = {
        "format": index["format"],
        "source": index["source"],
        "generated": index["generated"],
        "strings": index["strings"],
        "categories": {name: list(parts) for name, parts in index["categories"].items()},
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr(plugin._INDEX_META, json.dumps(meta, separators=(",", ":")))
        for category, parts in index["categories"].items():
            z.writestr(plugin.member_name(category), json.dumps(parts, separators=(",", ":")))


def _library_files(library):
    """{reference name, lowercased: path} for 'parts/' (with 's/') and 'p/'."""
    files = {}
    for sub in ("parts", "p"):
        root = os.path.join(library, sub)
        for directory, _, names in os.walk(root):
            for name in names:
                if name.lower().endswith(".dat"):
                    rel = os.path.relpath(os.path.join(directory, name), root).replace(os.sep, "/")
                    files.setdefault(rel.lower(), os.path.join(directory, name))
    return files


def _library_files_parts(library):
    """The part and subpart files alone, which is all the plugin ever fetches."""
    parts = os.path.join(library, "parts")
    return {
        name: path for name, path in _library_files(library).items() if os.path.commonpath([path, parts]) == parts
    }


def _reaches(pid, files, primitives, memo):
    """Whether a part places one of 'primitives', through its subparts and primitives alike.

    Primitives are followed too, unlike the plugin's walk: 'bush.dat' is a
    primitive that places 'bush0.dat', and a part drawn with the one uses the
    other.
    """
    name = (pid + ".dat").lower()
    if name in memo:
        return memo[name]
    memo[name] = False  # a cycle reaches nothing new
    found = False
    path = files.get(name) or files.get("s/" + name)
    if path:
        with open(path, encoding="latin-1") as f:
            for line in f:
                fields = line.split()
                if len(fields) < 15 or fields[0] != "1":
                    continue
                ref = " ".join(fields[14:]).replace("\\", "/").lower()
                if ref in primitives or (ref[:-4] != pid.lower() and _reaches(ref[:-4], files, primitives, memo)):
                    found = True
                    break
    memo[name] = found
    return found


def _keeps(old, new):
    """Whether 'new' has every interface instance of 'old', unchanged."""
    for iface, instances in (old or {}).items():
        for instance, port in instances.items():
            if ((new or {}).get(iface) or {}).get(instance) != port:
                return False
    return True


def refresh(path, library, primitives, plugin):
    """Recompute, in place, the entries of the parts that reach 'primitives'."""
    files = _library_files(library)
    cache = tempfile.mkdtemp(prefix="ldraw-index-")
    # Laid out where the plugin's fetcher looks, as _seed_cache does for the
    # archive. Primitives are never fetched, so only the parts are copied.
    for name, source in _library_files_parts(library).items():
        dest = os.path.join(cache, "parts", name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(source, dest)
    os.environ["PARTCAD_LDRAW_CACHE"] = cache
    with zipfile.ZipFile(path) as z:
        meta = json.loads(z.read(plugin._INDEX_META))
        members = {n: json.loads(z.read(n)) for n in z.namelist() if n != plugin._INDEX_META}
    memo = {}
    changed = kept = 0
    for member, parts in sorted(members.items()):
        for pid, entry in sorted(parts.items()):
            if not _reaches(pid, files, primitives, memo):
                continue
            with open(files.get((pid + ".dat").lower()), encoding="latin-1") as f:
                header = _header(f.read())
            new = plugin._part_config(pid, header).get("implements")
            if new == entry[3]:
                continue
            if not _keeps(entry[3], new):
                kept += 1
                print("  %s %s: would lose or move a port; left as it was" % (member, pid), file=sys.stderr)
                continue
            entry[3] = new
            changed += 1
            print("  %s %s: %s" % (member, pid, entry[0]), file=sys.stderr)
    shutil.rmtree(cache, ignore_errors=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr(plugin._INDEX_META, json.dumps(meta, separators=(",", ":")))
        for member, parts in members.items():
            z.writestr(member, json.dumps(parts, separators=(",", ":")))
    print("%s: %d entries refreshed, %d left as they were" % (os.path.basename(path), changed, kept), file=sys.stderr)


def _load_plugin():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "ldraw_repo", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ldraw_repo.py")
    )
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    return plugin


def main():
    if sys.version_info < (3, 10):
        # The plugin this imports uses zip(strict=True). Say so here rather
        # than let it surface as a TypeError partway through the crawl.
        sys.exit("build_parts_index.py needs Python 3.10 or newer (found %d.%d)" % sys.version_info[:2])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", default=None, help="a local complete.zip; downloaded if omitted")
    parser.add_argument("--output", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "parts-index.zip"))
    parser.add_argument(
        "--refresh", metavar="LIBRARY", default=None, help="an unpacked 'ldraw' directory to refresh entries from"
    )
    parser.add_argument(
        "--reaching", nargs="+", default=(), help="with --refresh: the primitives whose users to recompute"
    )
    args = parser.parse_args()

    if args.refresh:
        if not args.reaching:
            parser.error("--refresh needs --reaching")
        # The index being refreshed must not answer for the parts being recomputed.
        os.environ["PARTCAD_LDRAW_IGNORE_INDEX"] = "1"
        plugin = _load_plugin()
        refresh(args.output, args.refresh, {p.lower() for p in args.reaching}, plugin)
        return

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

    plugin = _load_plugin()

    print("  %d part files" % _seed_cache(archive, cache), file=sys.stderr)
    print("reading the category listings ...", file=sys.stderr)
    index, missing = build(archive, plugin)
    write(index, args.output, plugin)

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
