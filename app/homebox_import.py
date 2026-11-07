"""Import a Homebox CSV export (Tools > Export Inventory) into RelicBox."""
import csv
import datetime
import io
import re

from db import q, ex

PLATFORMS = {"ebay": "eBay", "amibay": "Amibay", "discord": "Discord", "facebook": "Facebook",
             "autoshite": "Autoshite", "vinted": "Vinted", "gumtree": "Gumtree", "etsy": "Etsy"}
UUID_RE = re.compile(r"/(item|location)/([0-9a-fA-F-]{36})")


def _date(s):
    s = (s or "").strip()[:10]
    try:
        d = datetime.date.fromisoformat(s)
    except ValueError:
        return None
    return d.isoformat() if d.year >= 1900 else None  # Homebox uses 0001-xx-xx for "not set"


def _money(s):
    try:
        v = round(float((s or "").replace(",", "")), 2)
    except ValueError:
        return None
    return v or None


def _clean(s):
    s = (s or "").strip()
    return s or None


def split_seller(text):
    """'steve-o333 - ebay' -> ('steve-o333', 'eBay'); 'Amibay - Steveed' -> ('Steveed', 'Amibay')."""
    text = _clean(text)
    if not text:
        return None, None
    if text.lower() in PLATFORMS:
        return None, PLATFORMS[text.lower()]
    parts = [p.strip() for p in text.split(" - ", 1)]
    if len(parts) == 2:
        a, b = parts
        if b.lower() in PLATFORMS:
            return a or None, PLATFORMS[b.lower()]
        if a.lower() in PLATFORMS:
            return b or None, PLATFORMS[a.lower()]
    return text, None


def _descriptor_id(name, cache):
    key = name.lower()
    if key not in cache:
        row = q("SELECT id FROM descriptor_types WHERE name = ? COLLATE NOCASE", (name,), one=True)
        if row:
            cache[key] = row["id"]
        else:
            n = q("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM descriptor_types", one=True)["n"]
            cache[key] = ex("INSERT INTO descriptor_types(name, sort_order) VALUES (?, ?)", (name, n))
    return cache[key]


def run(text, *, sold_locations=("Sold",), broken_locations=("Breaking for Parts",), ignore_locations=("Various",),
        allocate_ref, set_setting, now):
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows or "HB.name" not in rows[0]:
        raise ValueError("This doesn't look like a Homebox CSV export (no HB.name column).")
    lower = lambda xs: {x.strip().lower() for x in xs if x.strip()}  # noqa: E731
    sold_l, broken_l, ignore_l = lower(sold_locations), lower(broken_locations), lower(ignore_locations)
    special = sold_l | broken_l | ignore_l

    report = {"locations": 0, "items": 0, "skipped": [], "renumbered": [], "broken": [], "check": [],
              "status": {}, "damaged": []}
    today = datetime.date.today().isoformat()

    # --- reference numbering: continue Homebox's asset ID scheme if this is a fresh database
    empty_db = q("SELECT COUNT(*) AS n FROM items", one=True)["n"] == 0
    asset_ids = [r.get("HB.asset_id", "") for r in rows if "/item/" in r.get("HB.url", "")]
    nums = [int(m.group(2)) for a in asset_ids if (m := re.fullmatch(r"(\d+-)(\d+)", a.strip()))]
    if empty_db and nums:
        m = re.fullmatch(r"(\d+-)(\d+)", next(a.strip() for a in asset_ids if re.fullmatch(r"\d+-\d+", a.strip())))
        set_setting("ref_prefix", m.group(1))
        set_setting("ref_digits", len(m.group(2)))
        set_setting("ref_next", max(nums) + 1)
        report["ref_scheme"] = f"{m.group(1)}{max(nums) + 1:0{len(m.group(2))}d}"

    # --- locations
    loc_cache = {}

    def find_loc(name, parent_id):
        key = (name.lower(), parent_id)
        if key not in loc_cache:
            row = q("SELECT id FROM locations WHERE name = ? COLLATE NOCASE AND parent_id IS ?", (name, parent_id), one=True)
            loc_cache[key] = row["id"] if row else None
        return loc_cache[key]

    def ensure_path(path, legacy=None, description=None):
        names = [p.strip() for p in (path or "").split(" / ") if p.strip()]
        if not names or names[0].lower() in special:
            return None
        pid = None
        for i, name in enumerate(names):
            lid = find_loc(name, pid)
            last = i == len(names) - 1
            if lid is None:
                lid = ex("INSERT INTO locations(name, parent_id, description, legacy_id) VALUES (?, ?, ?, ?)",
                         (name, pid, description if last else None, legacy if last else None))
                loc_cache[(name.lower(), pid)] = lid
                report["locations"] += 1
            elif last and legacy:
                ex("UPDATE locations SET legacy_id = COALESCE(legacy_id, ?), "
                   "description = COALESCE(description, ?) WHERE id = ?", (legacy, description, lid))
            pid = lid
        return pid

    loc_rows = [r for r in rows if "/location/" in r.get("HB.url", "")]
    loc_rows.sort(key=lambda r: len([p for p in r.get("HB.location", "").split(" / ") if p.strip()]))
    for r in loc_rows:
        m = UUID_RE.search(r["HB.url"])
        full = " / ".join(p for p in [r.get("HB.location", "").strip(), r["HB.name"].strip()] if p)
        ensure_path(full, legacy=m.group(2) if m else None, description=_clean(r.get("HB.description")))

    # --- items
    desc_cache = {}
    std_desc = {"HB.manufacturer": "Manufacturer", "HB.model_number": "Model", "HB.serial_number": "Serial number"}
    by_import_ref, pending_parents = {}, []
    for r in rows:
        url = r.get("HB.url", "")
        if "/location/" in url:
            continue
        name = _clean(r.get("HB.name"))
        if not name:
            continue
        m = UUID_RE.search(url)
        legacy = m.group(2) if m else None
        if legacy:
            existing = q("SELECT id FROM items WHERE legacy_id = ?", (legacy,), one=True)
            if existing:
                report["skipped"].append(name)
                if r.get("HB.import_ref"):
                    by_import_ref[r["HB.import_ref"].strip()] = existing["id"]
                continue

        asset = (r.get("HB.asset_id") or "").strip()
        ref = asset
        if not ref or q("SELECT 1 FROM items WHERE ref = ? COLLATE NOCASE", (ref,), one=True):
            ref = allocate_ref()
            if asset:
                report["renumbered"].append(f"{name}: {asset} was already used, now {ref}")

        loc_path = (r.get("HB.location") or "").strip()
        top = loc_path.split(" / ")[0].strip().lower() if loc_path else ""
        sold_date, sold_price, sold_to = _date(r.get("HB.sold_date")), _money(r.get("HB.sold_price")), \
            _clean(r.get("HB.sold_to"))
        if top in broken_l:
            status = "broken"
        elif top in sold_l or sold_date or sold_price or sold_to:
            status = "sold" if sold_price else "given_away"
        else:
            status = "active"

        notes = [n for n in [_clean(r.get("HB.notes"))] if n]
        if _clean(r.get("HB.sold_notes")):
            notes.append("Sale notes: " + r["HB.sold_notes"].strip())
        if _date(r.get("HB.warranty_expires")) or _clean(r.get("HB.warranty_details")):
            notes.append("Warranty: " + " ".join(x for x in [_date(r.get("HB.warranty_expires")),
                                                            _clean(r.get("HB.warranty_details"))] if x))
        if (r.get("HB.lifetime_warranty") or "").lower() == "true":
            notes.append("Lifetime warranty")
        if _clean(r.get("HB.tags")):
            notes.append("Tags: " + r["HB.tags"].strip())
        if status == "broken" and sold_price:
            notes.append(f"Homebox sold price: {sold_price}")

        seller, platform = split_seller(r.get("HB.purchase_from"))
        try:
            qty = max(1, int(float(r.get("HB.quantity") or 1)))
        except ValueError:
            qty = 1
        fields = {
            "ref": ref, "name": name, "description": _clean(r.get("HB.description")),
            "notes": "\n".join(notes) or None, "quantity": qty,
            "location_id": None if status in ("sold", "given_away") or top in special else ensure_path(loc_path),
            "status": status, "purchase_date": _date(r.get("HB.purchase_date")),
            "purchase_price": _money(r.get("HB.purchase_price")), "purchase_seller": seller,
            "purchase_platform": platform,
            "disposal_date": sold_date if status in ("sold", "given_away") else None,
            "disposal_price": sold_price if status == "sold" else None,
            "disposal_destination": sold_to if status in ("sold", "given_away") else None,
            "broken_date": today if status == "broken" else None,
            "legacy_id": legacy, "created_at": now(), "updated_at": now(),
        }
        item_id = ex(f"INSERT INTO items({','.join(fields)}) VALUES ({','.join('?' * len(fields))})",
                     list(fields.values()))
        report["items"] += 1
        report["status"][status] = report["status"].get(status, 0) + 1
        if status == "broken":
            report["broken"].append((item_id, ref, name))
        text_blob = " ".join(notes + [fields["description"] or ""]).lower()
        if status != "broken" and re.search(r"\b(sold (items|parts)|bought to break|for spares|strip)\b", text_blob):
            report["check"].append((item_id, ref, name))

        for col, dname in std_desc.items():
            v = _clean(r.get(col))
            if v:
                if re.fullmatch(r"\d\.\d+E\+\d+", v, re.I):
                    report["damaged"].append(f"{ref} {name}: {dname} '{v}'")
                ex("INSERT OR REPLACE INTO item_descriptors(item_id, descriptor_type_id, value) VALUES (?, ?, ?)",
                   (item_id, _descriptor_id(dname, desc_cache), v))
        for col, v in r.items():
            if col and col.startswith("HB.field.") and _clean(v):
                ex("INSERT OR REPLACE INTO item_descriptors(item_id, descriptor_type_id, value) VALUES (?, ?, ?)",
                   (item_id, _descriptor_id(col[len("HB.field."):].strip(), desc_cache), v.strip()))

        iref = (r.get("HB.import_ref") or "").strip()
        if iref:
            if re.fullmatch(r"\d\.\d+E\+\d+", iref, re.I):
                report["damaged"].append(f"{ref} {name}: import ref '{iref}'")
            by_import_ref[iref] = item_id
        pref = (r.get("HB.parent_import_ref") or "").strip()
        if pref:
            pending_parents.append((item_id, pref, name))

    for item_id, pref, name in pending_parents:
        pid = by_import_ref.get(pref)
        if pid and pid != item_id:
            ex("UPDATE items SET parent_id = ?, location_id = NULL WHERE id = ?", (pid, item_id))
        else:
            report["check"].append((item_id, "", f"{name} (parent '{pref}' not found)"))
    return report
