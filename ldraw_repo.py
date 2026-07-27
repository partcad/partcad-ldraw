#
# partcad-ldraw, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""A PartCAD 'basic' repository plugin that serves the LDraw parts library.

It exposes the categories of https://library.ldraw.org as top-level
sub-packages of ``//pub/universe/lego/ldraw``, and, within each category, the
parts of that category (description, author and license taken from the LDraw
part header). Every part is a ``:ldraw`` partType part (see ``ldraw.py``), which
fetches and meshes the LDraw ``.dat`` on demand.

Nothing is vendored: the category list, the per-category part lists and the part
metadata are all fetched from ldraw.org and cached on disk on first use.

PartCAD runs this script once per data request (via ``runpy``); the answer is
returned in the global ``output`` as ``{"result": <value>}``. Keys are scoped by
sub-package, e.g. ``Brick/objects/part`` or ``Brick/files/ldraw.py``.
"""

import base64
import concurrent.futures
import os
import re
import time
import urllib.parse
import urllib.request

_BASE = "https://library.ldraw.org"
_CATEGORY_LIST_URL = _BASE + "/parts/category-list"
_PARTS_LIST_URL = _BASE + "/parts/list?tableFilters%5Bcategory%5D%5Bvalues%5D%5B0%5D="
_DAT_URL = _BASE + "/library/official/parts/"
_UA = "Mozilla/5.0 (PartCAD ldraw repository)"

# The ldraw.org parts list is paginated at a fixed 25 rows per page; enumerating
# a category walks every page. Metadata (description, author, license) is read
# from each part's .dat header, fetched concurrently. All of it - every list
# page and every .dat - is cached on disk, so the (possibly large) cost of a
# category is paid once. Enumeration is lazy per category (see PartCAD's
# ProjectExternalRepository), so importing the package does not trigger it.
_PER_PAGE = 25
_MAX_PAGES = 2000  # safety bound on pagination
# ldraw.org rate-limits bursts of concurrent requests, so keep the pool modest
# and retry with backoff; a dropped fetch would otherwise leave a part without
# its metadata.
_HEADER_WORKERS = 6
_HTTP_RETRIES = 4


# --- HTTP + cache -----------------------------------------------------------


def _cache_dir():
    base = os.environ.get("PARTCAD_LDRAW_CACHE")
    if not base:
        xdg = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        base = os.path.join(xdg, "partcad-ldraw")
    return base


def _http_get(url):
    for attempt in range(_HTTP_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status == 200:
                    return resp.read().decode("latin-1")
        except Exception:
            pass
        time.sleep(0.5 * (attempt + 1))  # backoff between retries
    return None


def _cached(url, rel):
    """Fetch 'url' once, caching its body under '<cache>/<rel>'."""
    path = os.path.join(_cache_dir(), rel)
    if os.path.exists(path):
        with open(path, "r", encoding="latin-1") as f:
            return f.read()
    body = _http_get(url)
    if body is None:
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="latin-1") as f:
        f.write(body)
    return body


# --- LDraw catalog ----------------------------------------------------------


def _sanitize(category):
    """Category name -> a PartCAD sub-package name (no spaces)."""
    return re.sub(r"\s+", "-", category.strip())


def _categories():
    """Return {sub_package_name: ldraw_category} for every LDraw category."""
    html = _cached(_CATEGORY_LIST_URL, "category-list.html")
    if not html:
        return {}
    names = set(re.findall(r"tableFilters%5Bcategory%5D%5Bvalues%5D%5B0%5D=([A-Za-z0-9_%.\-]+)", html))
    cats = {}
    for enc in names:
        cat = urllib.parse.unquote(enc)
        cats[_sanitize(cat)] = cat
    return cats


def _part_ids(category):
    """Every part id (without '.dat') in a category, across all list pages.

    Each page is fetched at most once and cached on disk. Pagination stops when
    the reported total is reached, when a page yields no new ids, or at the
    safety bound.
    """
    ids = []
    seen = set()
    sub = _sanitize(category)
    total = None
    page = 1
    while page <= _MAX_PAGES:
        url = "%s%s&page=%d" % (_PARTS_LIST_URL, urllib.parse.quote(category), page)
        html = _cached(url, os.path.join("categories", sub, "page-%d.html" % page))
        if not html:
            break
        if total is None:
            m = re.search(r"of ([\d,]+)", html)
            if m:
                total = int(m.group(1).replace(",", ""))
        added = 0
        for m in re.finditer(r"/library/official/parts/([0-9A-Za-z._-]+)\.dat", html):
            pid = m.group(1)
            if pid not in seen:
                seen.add(pid)
                ids.append(pid)
                added += 1
        if added == 0:
            break  # no new parts on this page: past the end
        if total is not None and len(ids) >= total:
            break
        page += 1
    return ids


def _parse_header(text):
    """Parse (description, author, license) from an LDraw .dat header (pure).

    The header is the leading run of '0' meta lines: the first plain '0' line is
    the description, '0 Author:' the author and '0 !LICENSE' the license.
    """
    desc = author = lic = None
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("0"):
            if s:
                break  # a geometry line: past the header
            continue
        body = s[1:].strip()
        if desc is None and body and not body.startswith("!") and body.split(":")[0] not in ("Name", "Author"):
            desc = body
        elif body.startswith("Author:"):
            author = body[len("Author:") :].strip()
        elif body.startswith("!LICENSE"):
            lic = body[len("!LICENSE") :].strip()
    return desc, author, lic


def _dat_header(pid):
    """Return (description, author, license) from a part's .dat header, or None.

    None means the part could not be fetched (so a targeted single fetch of an
    unknown id fails cleanly instead of inventing a broken part).
    """
    text = _cached(_DAT_URL + pid + ".dat", os.path.join("parts", pid + ".dat"))
    if not text:
        return None
    return _parse_header(text)


def _part_config(pid, meta):
    desc, author, lic = meta if meta else (None, None, None)
    config = {"type": ":ldraw", "dat": pid + ".dat"}
    if desc:
        config["desc"] = desc
    if author:
        config["author"] = author
    if lic:
        config["license"] = lic
    return config


def _catalog(category):
    """{part_id: config} for a whole category, metadata from the .dat headers.

    The complete part list comes from every list page; description, author and
    license come from each part's .dat header, fetched concurrently. A part
    whose header could not be read is still listed (with just its type/dat).
    """
    ids = _part_ids(category)
    if not ids:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=_HEADER_WORKERS) as pool:
        metas = list(pool.map(_dat_header, ids))
    return {pid: _part_config(pid, meta) for pid, meta in zip(ids, metas)}


# --- partType wrapper file --------------------------------------------------

_PART_TYPE = {"kind": "wrapper", "path": "ldraw.py"}


def _ldraw_py_b64():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ldraw.py"), "rb") as f:
        return base64.b64encode(f.read()).decode()


# --- the key/value protocol -------------------------------------------------


def get(key):
    cats = _categories()
    first, _, sub = key.partition("/")

    if first not in cats:
        # Top-level package: its children are the categories; it holds no parts.
        if key == "deps":
            return sorted(cats)
        if key == "meta":
            return {"desc": "The LDraw parts library, by category."}
        if key == "objects/partType":
            return {"ldraw": dict(_PART_TYPE)}
        if key.startswith("objects/"):
            return {}
        return None

    category = cats[first]

    if sub == "deps":
        return []
    if sub == "meta":
        return {"desc": "LDraw parts in the '%s' category." % category}
    if sub == "objects/part":
        return _catalog(category)
    if sub.startswith("objects/part/"):
        pid = sub[len("objects/part/") :]
        header = _dat_header(pid)
        return _part_config(pid, header) if header is not None else None
    if sub == "objects/partType":
        return {"ldraw": dict(_PART_TYPE)}
    if sub.startswith("objects/partType/"):
        name = sub[len("objects/partType/") :]
        return dict(_PART_TYPE) if name == "ldraw" else None
    if sub.startswith("objects/"):
        return {}
    if sub == "files/ldraw.py":
        return _ldraw_py_b64()

    return None


if __name__ == "get":
    output = {"result": get(request["key"])}  # noqa: F821 - injected by the runtime
elif __name__ == "__main__":
    _cats = _categories()
    print("categories:", len(_cats))
    demo = "Brick" if "Brick" in _cats else sorted(_cats)[0]
    cat = _cats[demo]
    print("sample category:", demo, "->", cat)
    catalog = _catalog(cat)
    print("parts:", len(catalog))
    for pid, cfg in list(catalog.items())[:3]:
        print("  ", pid, "->", cfg)
else:
    output = {}
