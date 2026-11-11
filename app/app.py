"""RelicBox - a self-hosted collection & repairs inventory."""
import csv
import datetime
import io
import os
import re
import secrets
import uuid
from collections import defaultdict

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)

from urllib.parse import quote, unquote, urlsplit

import code128
import homebox_import
import qr
import db as dbm
from db import q, ex, commit

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    Image = None

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
THUMB_DIR = os.path.join(PHOTO_DIR, "thumbs")
for d in (DATA_DIR, PHOTO_DIR, THUMB_DIR):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)
app.config["DB_PATH"] = os.path.join(DATA_DIR, "inventory.db")
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "64")) * 1024 * 1024
app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(days=90)


def _secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(DATA_DIR, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app.secret_key = _secret_key()
dbm.init_db(app.config["DB_PATH"])
app.teardown_appcontext(dbm.close_db)

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
APP_VERSION = os.environ.get("APP_VERSION", "dev")
APP_CHANNEL = os.environ.get("APP_CHANNEL", "dev")

STATUSES = {
    "active": "In collection",
    "sold": "Sold",
    "given_away": "Given away",
    "broken": "Breaking for spares",
    "scrapped": "Scrapped",
}
PART_OUTCOMES = {
    "leave": "Leave inside",
    "spare": "Create new item or subitem",
    "repair": "Add to work log of another item",
    "sold": "Sold / charged to someone",
    "given_away": "Given away",
    "scrapped": "Scrapped",
}
DISPOSED = dbm.DISPOSED
NB_SIZES = {"22x12": "22 × 12 mm", "30x12": "30 × 12 mm", "40x12": "40 × 12 mm", "30x15": "30 × 15 mm"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def setting(key, default=None):
    row = q("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    return row["value"] if row else default


def set_setting(key, value):
    ex("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
       (key, str(value)))


def allocate_ref():
    prefix = setting("ref_prefix", "")
    digits = int(setting("ref_digits", "5") or 5)
    n = int(setting("ref_next", "1") or 1)
    while True:
        ref = f"{prefix}{n:0{digits}d}"
        n += 1
        if not q("SELECT 1 FROM items WHERE ref = ?", (ref,), one=True):
            set_setting("ref_next", n)
            return ref


def parse_money(s):
    if s is None:
        return None
    s = re.sub(r"[^\d.\-]", "", str(s))
    if s in ("", "-", ".", "-."):
        return None
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def today():
    return datetime.date.today().isoformat()


def clean(s):
    s = (s or "").strip()
    return s or None


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def existing_location_id(raw, errors, what="The location you chose"):
    """Turn a submitted location id into a real one. If it was deleted meanwhile
    (e.g. in another tab), add a friendly error instead of failing on save."""
    loc_id = int_or_none(raw)
    if loc_id is None:
        return None
    if not q("SELECT 1 FROM locations WHERE id = ?", (loc_id,), one=True):
        errors.append(f"{what} no longer exists. It may have been deleted. Please choose another.")
        return None
    return loc_id


def int_or_none(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


# --- locations --------------------------------------------------------------
def load_locations():
    by_id = {r["id"]: dict(r) for r in q("SELECT * FROM locations ORDER BY name COLLATE NOCASE")}
    children = defaultdict(list)
    for loc in by_id.values():
        pid = loc["parent_id"] if loc["parent_id"] in by_id else None
        children[pid].append(loc)
    return by_id, children


def location_tree(exclude_id=None):
    """Depth-first list of (location, depth). Optionally skip a subtree."""
    _, children = load_locations()
    out = []

    def walk(pid, depth):
        for c in children.get(pid, []):
            if c["id"] == exclude_id:
                continue
            out.append((c, depth))
            walk(c["id"], depth + 1)

    walk(None, 0)
    return out


def location_path(loc_id, by_id=None):
    if by_id is None:
        by_id, _ = load_locations()
    path, seen = [], set()
    while loc_id and loc_id in by_id and loc_id not in seen:
        seen.add(loc_id)
        path.append(by_id[loc_id])
        loc_id = by_id[loc_id]["parent_id"]
    return list(reversed(path))


def location_descendants(loc_id, children):
    out, stack = set(), [loc_id]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.add(cur)
        stack.extend(c["id"] for c in children.get(cur, []))
    return out


# --- items ----------------------------------------------------------------
def move_out(item_id):
    """A sold / given-away / scrapped item leaves its location (it's listed under "Sold") and any
    container still in the collection, remembering both so "Back in collection" can restore them."""
    it = q("SELECT * FROM items WHERE id = ?", (item_id,), one=True)
    parent = q("SELECT status FROM items WHERE id = ?", (it["parent_id"],), one=True) if it["parent_id"] else None
    if parent and parent["status"] in DISPOSED:
        return  # went along with a container that is itself gone: stays inside it
    ex("""UPDATE items SET former_location_id = COALESCE(?, former_location_id),
          former_parent_id = COALESCE(?, former_parent_id), location_id = NULL, parent_id = NULL WHERE id = ?""",
       (it["location_id"], it["parent_id"], item_id))


def move_back(item_id):
    """Put a returned item back where it was, if that place still exists. Returns True if placed."""
    it = q("SELECT * FROM items WHERE id = ?", (item_id,), one=True)
    placed = it["location_id"] is not None or it["parent_id"] is not None
    if not placed and it["former_parent_id"]:
        p = q("SELECT status FROM items WHERE id = ?", (it["former_parent_id"],), one=True)
        if p and p["status"] not in DISPOSED and it["former_parent_id"] not in item_descendant_ids(item_id):
            ex("UPDATE items SET parent_id = ? WHERE id = ?", (it["former_parent_id"], item_id))
            placed = True
    if not placed and it["former_location_id"] and \
            q("SELECT 1 FROM locations WHERE id = ?", (it["former_location_id"],), one=True):
        ex("UPDATE items SET location_id = ? WHERE id = ?", (it["former_location_id"], item_id))
        placed = True
    ex("UPDATE items SET former_location_id = NULL, former_parent_id = NULL WHERE id = ?", (item_id,))
    return placed


def former_place(item):
    """(location path, container row) an item was in before it was sold, for display."""
    parent = q("SELECT * FROM items WHERE id = ?", (item["former_parent_id"],), one=True) \
        if item["former_parent_id"] else None
    loc_id = effective_location_id(parent) if parent else item["former_location_id"]
    return location_path(loc_id), parent


def get_item(item_id):
    row = q("SELECT * FROM items WHERE id = ?", (item_id,), one=True)
    if not row:
        abort(404)
    return row


def item_ancestors(item):
    """Containers of this item, outermost first."""
    chain, seen = [], {item["id"]}
    pid = item["parent_id"]
    while pid and pid not in seen:
        seen.add(pid)
        p = q("SELECT * FROM items WHERE id = ?", (pid,), one=True)
        if not p:
            break
        chain.append(p)
        pid = p["parent_id"]
    return list(reversed(chain))


def effective_location_id(item, items_by_id=None):
    seen = set()
    cur = item
    while cur["parent_id"] and cur["id"] not in seen:
        seen.add(cur["id"])
        nxt = items_by_id.get(cur["parent_id"]) if items_by_id is not None else \
            q("SELECT * FROM items WHERE id = ?", (cur["parent_id"],), one=True)
        if not nxt:
            break
        cur = nxt
    return cur["location_id"]


def item_descendant_ids(item_id):
    out, stack = set(), [item_id]
    while stack:
        cur = stack.pop()
        for r in q("SELECT id FROM items WHERE parent_id = ?", (cur,)):
            if r["id"] not in out:
                out.add(r["id"])
                stack.append(r["id"])
    return out


def resolve_item(text):
    """Turn 'REF — name', a ref, or a barcode into an item row."""
    text = (text or "").strip()
    if not text:
        return None
    token = re.split(r"\s+[—-]\s+", text, maxsplit=1)[0].strip()
    for cand in (token, text):
        row = q("SELECT * FROM items WHERE ref = ? COLLATE NOCASE", (cand,), one=True)
        if row:
            return row
        row = q("SELECT * FROM items WHERE barcode = ? ORDER BY status = 'active' DESC, id LIMIT 1", (cand,), one=True)
        if row:
            return row
    return None


def item_label(row):
    return f"{row['ref']} — {row['name']}" if row else ""


def descriptor_types():
    return q("SELECT * FROM descriptor_types ORDER BY sort_order, name COLLATE NOCASE")


def item_descriptors(item_id):
    return q("""SELECT dt.id, dt.name, d.value FROM descriptor_types dt
                LEFT JOIN item_descriptors d ON d.descriptor_type_id = dt.id AND d.item_id = ?
                ORDER BY dt.sort_order, dt.name COLLATE NOCASE""", (item_id,))


def save_descriptors(item_id, form):
    for dt in descriptor_types():
        v = (form.get(f"desc_{dt['id']}") or "").strip()
        if v:
            ex("""INSERT INTO item_descriptors(item_id, descriptor_type_id, value) VALUES (?, ?, ?)
                  ON CONFLICT(item_id, descriptor_type_id) DO UPDATE SET value = excluded.value""",
               (item_id, dt["id"], v))
        else:
            ex("DELETE FROM item_descriptors WHERE item_id = ? AND descriptor_type_id = ?", (item_id, dt["id"]))


def primary_photos(item_ids):
    if not item_ids:
        return {}
    ids = list(item_ids)
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        rows = q(f"""SELECT item_id, filename FROM photos WHERE item_id IN ({','.join('?' * len(chunk))})
                     ORDER BY sort_order, id""", chunk)
        for r in rows:
            out.setdefault(r["item_id"], r["filename"])
    return out


def part_transfers(part_id):
    """Work-log entries on other items that record this part being fitted (its internal price)."""
    return q("SELECT * FROM work_log WHERE part_id = ? ORDER BY date, id", (part_id,))


def part_flow(part):
    """What happened to a part taken off an item, and what it means for that item.

    Returns (outcome, moved, income, date):
      moved  - cost moved from the item to the part: the value given when it was kept as its own
               item, or the internal price it was added to another item's work log at.
      income - real money: what it sold for, if it was sold without being given a value first.
    A part given a value keeps that as "moved"; anything it later sells for is its own profit or loss.
    A part broken down further is a donor in its own right.
    """
    transfers = part_transfers(part["id"])
    moved = income = 0.0
    s = part["status"]
    if transfers:
        oc, moved, date = "repair", sum(t["cost"] or 0 for t in transfers), transfers[-1]["date"]
    elif s == "sold":
        oc, income, date = "sold", part["disposal_price"] or 0.0, part["disposal_date"]
    elif s in ("given_away", "scrapped"):
        oc, date = s, part["disposal_date"]
    elif part["broken_date"]:
        oc, date = "broken", part["broken_date"]
    elif part["parent_id"] and part["parent_id"] == part["harvested_from_id"]:
        oc, date = "leave", None
    else:
        oc, date = "spare", None  # its own item, on a shelf or inside another item
    if part["salvage_value"] is not None:
        moved, income = part["salvage_value"], 0.0
    return oc, moved, income, date


# --- value history and work log ------------------------------------------------
def cost_basis(it):
    """Spend on an item: purchase price + logged work - cost moved to parts taken off it.
    Valuations are kept separate."""
    return (it["purchase_price"] or 0) + (it["work_cost"] or 0) - (it["moved_out"] or 0)


def known_cost(it):
    """cost_basis, or None when nothing has been recorded (for display)."""
    return cost_basis(it) if it["purchase_price"] is not None or it["work_cost"] or it["moved_out"] else None


def list_totals(rows):
    """Running totals for a list of items: actual spend, and (separately) latest valuations."""
    return {"cost": sum(cost_basis(r) for r in rows),
            "value": sum(r["value"] or 0 for r in rows),
            "valued": sum(1 for r in rows if r["value"] is not None)}


def refresh_value(item_id):
    """Recompute the cached items.value (latest valuation, or NULL) and items.work_cost (total work spend)."""
    work_cost = q("SELECT COALESCE(SUM(cost), 0) AS t FROM work_log WHERE item_id = ?", (item_id,), one=True)["t"]
    v = q("SELECT value FROM valuations WHERE item_id = ? ORDER BY date DESC, id DESC LIMIT 1",
          (item_id,), one=True)
    value = v["value"] if v else None
    ex("UPDATE items SET value = ?, work_cost = ? WHERE id = ?", (value, work_cost, item_id))


def add_valuation(item_id, value, date=None, source=None, only_if_changed=False):
    if value is None:
        return
    if only_if_changed:
        cur = q("SELECT value FROM items WHERE id = ?", (item_id,), one=True)
        if cur and cur["value"] is not None and abs(cur["value"] - value) < 0.005:
            return
    ex("INSERT INTO valuations(item_id, date, value, source) VALUES (?, ?, ?, ?)",
       (item_id, date or today(), value, source))
    refresh_value(item_id)


OUTCOME_LABELS = dict(PART_OUTCOMES, broken="Broken down further", leave="Still inside donor",
                      repair="Added to work log of", spare="Kept as its own item")


def breakdown_summary(donor_id, _depth=0):
    """Parts taken off an item, with the cost moved to them and the real income they brought.

    totals: moved (spare = kept as items with a value, repair = added to work logs), sold (parts sold),
    shell (the item's own sale, if sold), income (sold + shell), finished, and n_<outcome> counts.
    """
    donor = q("SELECT * FROM items WHERE id = ?", (donor_id,), one=True)
    parts = q("SELECT * FROM items WHERE harvested_from_id = ? ORDER BY id", (donor_id,))
    rows, totals = [], defaultdict(float)
    for p in parts:
        oc, moved, income, date = part_flow(p)
        target = q("SELECT id, ref, name FROM items WHERE id = ?", (p["parent_id"],), one=True) \
            if oc in ("repair", "spare") and p["parent_id"] else None
        rows.append({"part": p, "outcome": oc, "moved": moved, "income": income,
                     "date": p["taken_off_date"] or date, "target": target,
                     "transfer": part_transfers(p["id"])[-1] if oc == "repair" else None})
        totals["moved"] += moved
        totals["moved_" + oc] += moved
        totals["sold"] += income
        totals["n_" + oc] += 1
    totals["shell"] = (donor["disposal_price"] or 0.0) if donor and donor["status"] == "sold" else 0.0
    totals["income"] = totals["sold"] + totals["shell"]
    totals["finished"] = bool(donor and donor["status"] in ("sold", "given_away", "scrapped"))
    return rows, totals


# --- search -----------------------------------------------------------------
SEARCH_TEXT = """(i.ref || ' ' || COALESCE(i.barcode,'') || ' ' || i.name || ' ' || COALESCE(i.description,'') || ' ' ||
    COALESCE(i.notes,'') || ' ' || COALESCE(i.purchase_seller,'') || ' ' || COALESCE(i.purchase_platform,'') || ' ' ||
    COALESCE(i.disposal_destination,'') || ' ' ||
    COALESCE((SELECT group_concat(value, ' ') FROM item_descriptors d WHERE d.item_id = i.id), ''))"""


def search_items(qtext="", status="active", location_id=None, limit=None):
    where, args = [], []
    dts = {dt["name"].lower().replace(" ", ""): dt["id"] for dt in descriptor_types()}
    for tok in re.findall(r'"[^"]+"|\S+', qtext or ""):
        tok = tok.strip('"')
        if not tok:
            continue
        if ":" in tok:
            field, _, val = tok.partition(":")
            key = field.lower().replace("_", "").replace("-", "")
            if key in dts and val:
                where.append("EXISTS (SELECT 1 FROM item_descriptors d WHERE d.item_id = i.id "
                              "AND d.descriptor_type_id = ? AND d.value LIKE ?)")
                args += [dts[key], f"%{val}%"]
                continue
        where.append(f"{SEARCH_TEXT} LIKE ?")
        args.append(f"%{tok}%")
    if status == "gone":
        where.append(f"i.status IN ({','.join('?' * len(DISPOSED))})")
        args.extend(DISPOSED)
    elif status and status != "all":
        where.append("i.status = ?")
        args.append(status)
    sql = "SELECT i.* FROM items i"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.name COLLATE NOCASE, i.ref"
    rows = [dict(r) for r in q(sql, args)]
    if location_id:
        _, children = load_locations()
        wanted = location_descendants(location_id, children)
        by_id = {r["id"]: dict(r) for r in q("SELECT id, parent_id, location_id FROM items")}
        rows = [r for r in rows if effective_location_id(r, by_id) in wanted]
    if limit:
        rows = rows[:limit]
    return rows


def decorate(rows):
    """Add location path / container info and thumbnails to item dicts for listing."""
    loc_by_id, _ = load_locations()
    items_by_id = {r["id"]: dict(r) for r in q("SELECT id, ref, name, parent_id, location_id FROM items")}
    photos = primary_photos([r["id"] for r in rows])
    for r in rows:
        r["photo"] = photos.get(r["id"])
        r["container"] = items_by_id.get(r["parent_id"]) if r["parent_id"] else None
        chain, seen, pid = [], {r["id"]}, r["parent_id"]
        while pid and pid in items_by_id and pid not in seen:
            seen.add(pid)
            chain.append(items_by_id[pid])
            pid = items_by_id[pid]["parent_id"]
        r["containers"] = chain
        r["loc_path"] = location_path(effective_location_id(r, items_by_id), loc_by_id)
    return rows


def attach_contents(rows, status="active"):
    """Give each listed item a 'children' list (its contents, recursively) for the drop-down tree."""
    sql = "SELECT * FROM items WHERE parent_id IS NOT NULL"
    args = ()
    if status != "all":
        sql, args = sql + " AND status = ?", (status,)
    nested = [dict(r) for r in q(sql + " ORDER BY name COLLATE NOCASE", args)]
    decorate(nested)
    by_parent = defaultdict(list)
    for r in nested:
        by_parent[r["parent_id"]].append(r)

    def fill(row, seen):
        row["children"] = [c for c in by_parent.get(row["id"], []) if c["id"] not in seen]
        row["n_inside"] = 0
        row["cost_incl"] = cost_basis(row)
        for c in row["children"]:
            fill(c, seen | {c["id"]})
            row["n_inside"] += 1 + c["n_inside"]
            row["cost_incl"] += c["cost_incl"]

    for r in rows:
        fill(r, {r["id"]})
    return rows


# ---------------------------------------------------------------------------
# Template plumbing
# ---------------------------------------------------------------------------
@app.template_filter("money")
def money_filter(v, blank="—"):
    if v is None or v == "":
        return blank
    cur = setting("currency", "£")
    neg = v < 0
    return f"{'-' if neg else ''}{cur}{abs(v):,.2f}"


@app.template_filter("num")
def num_filter(v):
    return "" if v is None else (f"{v:.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v))


@app.context_processor
def inject():
    return {
        "STATUSES": STATUSES,
        "PART_OUTCOMES": PART_OUTCOMES,
        "OUTCOME_LABELS": OUTCOME_LABELS,
        "site_name": setting("site_name", "RelicBox"),
        "currency": setting("currency", "£"),
        "auth_enabled": bool(APP_PASSWORD),
        "item_label": item_label,
        "label_format": setting("label_format", "qr"),
        "item_qr_url": item_qr_url,
        "label_qr_content": label_qr_content,
        "nb": {"size": setting("nb_size", "22x12"), "density": int(setting("nb_density", "2") or 2),
               "qr": setting("nb_qr", "link"), "name": setting("nb_name", "1") == "1"},
        "NB_SIZES": NB_SIZES,
        "cost_basis": cost_basis,
        "known_cost": known_cost,
        "app_version": APP_VERSION,
        "app_channel": APP_CHANNEL,
    }


@app.before_request
def require_login():
    if not APP_PASSWORD or request.endpoint in ("login", "static", "healthz"):
        return None
    if not session.get("auth"):
        return redirect(url_for("login", next=request.full_path))
    return None


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if APP_PASSWORD and secrets.compare_digest(request.form.get("password", ""), APP_PASSWORD):
            session["auth"] = True
            session.permanent = True
            nxt = request.args.get("next") or url_for("index")
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("index")
            return redirect(nxt)
        flash("Wrong password", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    stats = q("""SELECT
        SUM(status = 'active') AS active,
        SUM(CASE WHEN status = 'active' THEN quantity ELSE 0 END) AS pieces,
        SUM(CASE WHEN status = 'active' THEN COALESCE(purchase_price, 0) + work_cost - moved_out ELSE 0 END) AS cost,
        SUM(CASE WHEN status = 'active' THEN COALESCE(value, 0) ELSE 0 END) AS value,
        SUM(status = 'sold') AS sold,
        SUM(CASE WHEN status = 'sold' THEN COALESCE(disposal_price, 0) ELSE 0 END) AS income,
        SUM(status = 'broken') AS broken
        FROM items""", one=True)
    recent = decorate([dict(r) for r in q("SELECT * FROM items ORDER BY updated_at DESC, id DESC LIMIT 12")])
    nloc = q("SELECT COUNT(*) AS n FROM locations", one=True)["n"]
    return render_template("index.html", stats=stats, recent=recent, nloc=nloc)


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------
@app.route("/items")
def items_list():
    qtext = request.args.get("q", "")
    status = request.args.get("status", "active")
    loc = int_or_none(request.args.get("location"))
    rows = decorate(search_items(qtext, status, loc))
    return render_template("items.html", rows=rows, qtext=qtext, status=status, loc=loc,
                           locations=location_tree(), totals=list_totals(rows))


def item_form_data(form, existing=None):
    errors = []
    data = {
        "name": (form.get("name") or "").strip(),
        "barcode": clean(form.get("barcode")),
        "description": clean(form.get("description")),
        "notes": clean(form.get("notes")),
        "purchase_date": parse_date(form.get("purchase_date")),
        "purchase_price": parse_money(form.get("purchase_price")),
        "purchase_seller": clean(form.get("purchase_seller")),
        "purchase_platform": clean(form.get("purchase_platform")),
        "status": form.get("status") if form.get("status") in STATUSES else "active",
        "disposal_date": parse_date(form.get("disposal_date")),
        "disposal_price": parse_money(form.get("disposal_price")),
        "disposal_destination": clean(form.get("disposal_destination")),
        "broken_date": parse_date(form.get("broken_date")),
    }
    if not data["name"]:
        errors.append("Name is required.")
    try:
        data["quantity"] = max(1, int(form.get("quantity") or 1))
    except ValueError:
        data["quantity"] = 1

    ref = (form.get("ref") or "").strip()
    if ref:
        clash = q("SELECT id FROM items WHERE ref = ? COLLATE NOCASE", (ref,), one=True)
        if clash and (not existing or clash["id"] != existing["id"]):
            errors.append(f"Reference {ref} is already used.")
    data["ref"] = ref

    parent_text = (form.get("parent") or "").strip()
    parent = resolve_item(parent_text) if parent_text else None
    if parent_text and not parent:
        errors.append(f"Couldn't find container item '{parent_text}'.")
    if parent and parent["status"] != "active" and not (existing and parent["id"] == existing["parent_id"]):
        errors.append(f"{parent['ref']} {parent['name']} is {STATUSES[parent['status']].lower()}. "
                      "Choose an item that's still in the collection.")
        parent = None
    if parent and existing:
        if parent["id"] == existing["id"] or parent["id"] in item_descendant_ids(existing["id"]):
            errors.append("An item can't be placed inside itself or one of its own parts.")
            parent = None
    data["parent_id"] = parent["id"] if parent else None
    data["location_id"] = None if parent else existing_location_id(form.get("location_id"), errors)
    if existing and existing["status"] in DISPOSED and data["status"] in DISPOSED:
        data["parent_id"], data["location_id"] = existing["parent_id"], existing["location_id"]

    if "harvested_from" not in form:  # not on the form any more: keep the existing donor link
        data["harvested_from_id"] = existing["harvested_from_id"] if existing else None
        return data, errors
    hv_text = (form.get("harvested_from") or "").strip()
    hv = resolve_item(hv_text) if hv_text else None
    if hv_text and not hv:
        errors.append(f"Couldn't find donor item '{hv_text}'.")
    if hv and existing and hv["id"] == existing["id"]:
        hv = None
    data["harvested_from_id"] = hv["id"] if hv else None
    return data, errors


ITEM_FIELDS = ["ref", "barcode", "name", "description", "notes", "quantity", "location_id", "parent_id",
               "harvested_from_id", "status", "purchase_date", "purchase_price", "purchase_seller",
               "purchase_platform", "disposal_date", "disposal_price", "disposal_destination", "broken_date"]


def form_context(item, data=None):
    platforms = [r["purchase_platform"] for r in
                 q("SELECT DISTINCT purchase_platform FROM items WHERE purchase_platform IS NOT NULL ORDER BY 1")]
    sellers = [r["purchase_seller"] for r in
               q("SELECT DISTINCT purchase_seller FROM items WHERE purchase_seller IS NOT NULL ORDER BY 1")]
    return dict(item=item, f=data or {}, locations=location_tree(), platforms=platforms, sellers=sellers)


@app.route("/items/new", methods=["GET", "POST"])
def item_new():
    if request.method == "POST":
        data, errors = item_form_data(request.form)
        # New items always start in the collection; status changes happen by editing later.
        data.update(status="active", disposal_date=None, disposal_price=None,
                    disposal_destination=None, broken_date=None)
        if errors:
            for e in errors:
                flash(e, "error")
            ctx = form_context(None, dict(request.form))
            ctx["descriptors"] = [{"id": dt["id"], "name": dt["name"],
                                   "value": request.form.get(f"desc_{dt['id']}", "")} for dt in descriptor_types()]
            return render_template("item_form.html", **ctx)
        if not data["ref"]:
            data["ref"] = allocate_ref()
        data["created_at"] = data["updated_at"] = now()
        cols = ITEM_FIELDS + ["created_at", "updated_at"]
        item_id = ex(f"INSERT INTO items({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     [data[c] for c in cols])
        save_descriptors(item_id, request.form)
        for fs in request.files.getlist("photos"):
            save_photo(item_id, fs)
        commit()
        flash(f"Added {data['ref']} — {data['name']}", "ok")
        if request.form.get("again"):
            return redirect(url_for("item_new", location_id=data["location_id"] or "",
                                    parent=request.form.get("parent", "")))
        return redirect(url_for("item_view", item_id=item_id))

    # GET: prefill from query string
    f = {
        "location_id": request.args.get("location_id", ""),
        "barcode": request.args.get("barcode", ""),
        "status": "active",
        "quantity": 1,
    }
    for key in ("parent", "harvested_from"):
        iid = int_or_none(request.args.get(key + "_id"))
        if iid:
            row = q("SELECT * FROM items WHERE id = ?", (iid,), one=True)
            f[key] = item_label(row)
        elif request.args.get(key):
            f[key] = request.args.get(key)
    ctx = form_context(None, f)
    ctx["descriptors"] = [{"id": dt["id"], "name": dt["name"], "value": ""} for dt in descriptor_types()]
    # copy descriptors from another item (e.g. "add similar")
    src = int_or_none(request.args.get("copy"))
    if src:
        s = q("SELECT * FROM items WHERE id = ?", (src,), one=True)
        if s:
            for k in ("name", "description", "purchase_date", "purchase_price", "purchase_seller",
                      "purchase_platform", "location_id"):
                f[k] = s[k]
            if s["parent_id"]:
                f["parent"] = item_label(q("SELECT * FROM items WHERE id = ?", (s["parent_id"],), one=True))
            ctx["descriptors"] = [dict(d) for d in item_descriptors(src)]
    return render_template("item_form.html", **ctx)


@app.route("/items/<int:item_id>")
def item_view(item_id):
    item = get_item(item_id)
    loc_by_id, _ = load_locations()
    ancestors = item_ancestors(item)
    loc_path = location_path(effective_location_id(item), loc_by_id)
    contents = decorate([dict(r) for r in q("SELECT * FROM items WHERE parent_id = ? ORDER BY name COLLATE NOCASE",
                                            (item_id,))])
    photos = q("SELECT * FROM photos WHERE item_id = ? ORDER BY sort_order, id", (item_id,))
    descriptors = [d for d in item_descriptors(item_id) if d["value"]]
    donor = q("SELECT * FROM items WHERE id = ?", (item["harvested_from_id"],), one=True) \
        if item["harvested_from_id"] else None
    parts, part_totals = breakdown_summary(item_id)
    contents_cost = sum(cost_basis(c) for c in contents if c["status"] == "active")
    valuations = q("SELECT * FROM valuations WHERE item_id = ? ORDER BY date DESC, id DESC", (item_id,))
    work = q("SELECT * FROM work_log WHERE item_id = ? ORDER BY date DESC, id DESC", (item_id,))
    latest = valuations[0] if valuations else None
    was_path, was_in = former_place(item) if item["status"] in DISPOSED else ([], None)
    inside_donor = q("SELECT ref, name FROM items WHERE parent_id = ? AND status = 'active' "
                     "ORDER BY name COLLATE NOCASE", (item_id,)) if item["status"] == "broken" else []
    sources = [r["source"] for r in q("SELECT DISTINCT source FROM valuations WHERE source IS NOT NULL "
                                      "AND source != 'Recorded before value history' ORDER BY 1")]
    return render_template("item_view.html", item=item, ancestors=ancestors, loc_path=loc_path,
                           contents=contents, photos=photos, descriptors=descriptors, donor=donor,
                           parts=parts, part_totals=part_totals, contents_cost=contents_cost,
                           valuations=valuations, work=work, latest=latest,
                           sources=sources, today=today(), was_path=was_path, was_in=was_in,
                           inside_donor=inside_donor, locations=location_tree(), DISPOSED=DISPOSED)


@app.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
def item_edit(item_id):
    item = get_item(item_id)
    if request.method == "POST":
        data, errors = item_form_data(request.form, existing=item)
        if errors:
            for e in errors:
                flash(e, "error")
            ctx = form_context(item, dict(request.form))
            ctx["descriptors"] = [{"id": dt["id"], "name": dt["name"],
                                   "value": request.form.get(f"desc_{dt['id']}", "")} for dt in descriptor_types()]
            return render_template("item_form.html", **ctx)
        if not data["ref"]:
            data["ref"] = item["ref"]
        data["updated_at"] = now()
        cols = ITEM_FIELDS + ["updated_at"]
        ex(f"UPDATE items SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
           [data[c] for c in cols] + [item_id])
        if data["status"] in DISPOSED and item["status"] not in DISPOSED:
            move_out(item_id)
        elif item["status"] in DISPOSED and data["status"] not in DISPOSED and data["location_id"] is None \
                and data["parent_id"] is None:
            move_back(item_id)
        save_descriptors(item_id, request.form)
        for fs in request.files.getlist("photos"):
            save_photo(item_id, fs)
        commit()
        flash("Saved", "ok")
        return redirect(url_for("item_view", item_id=item_id))
    f = dict(item)
    if item["parent_id"]:
        f["parent"] = item_label(q("SELECT * FROM items WHERE id = ?", (item["parent_id"],), one=True))
    if item["harvested_from_id"]:
        f["harvested_from"] = item_label(q("SELECT * FROM items WHERE id = ?", (item["harvested_from_id"],), one=True))
    ctx = form_context(item, f)
    ctx["descriptors"] = [dict(d) for d in item_descriptors(item_id)]
    return render_template("item_form.html", **ctx)


@app.route("/items/<int:item_id>/delete", methods=["POST"])
def item_delete(item_id):
    item = get_item(item_id)
    # Children move up to wherever this item was.
    loc = effective_location_id(item)
    ex("UPDATE items SET parent_id = ?, location_id = CASE WHEN ? IS NULL THEN ? ELSE NULL END WHERE parent_id = ?",
       (item["parent_id"], item["parent_id"], loc, item_id))
    ex("UPDATE items SET harvested_from_id = NULL WHERE harvested_from_id = ?", (item_id,))
    for p in q("SELECT filename FROM photos WHERE item_id = ?", (item_id,)):
        remove_photo_files(p["filename"])
    ex("DELETE FROM items WHERE id = ?", (item_id,))
    commit()
    flash(f"Deleted {item['ref']} — {item['name']}", "ok")
    if item["parent_id"]:
        return redirect(url_for("item_view", item_id=item["parent_id"]))
    return redirect(url_for("items_list"))


@app.route("/items/<int:item_id>/dispose", methods=["GET", "POST"])
def item_dispose(item_id):
    item = get_item(item_id)
    if request.method == "POST":
        kind = request.form.get("kind")
        if kind not in ("sold", "given_away", "scrapped"):
            abort(400)
        price = parse_money(request.form.get("price")) if kind == "sold" else None
        was_at = effective_location_id(item)
        ex("""UPDATE items SET status = ?, disposal_date = ?, disposal_price = ?, disposal_destination = ?,
              notes = CASE WHEN ? IS NULL THEN notes ELSE COALESCE(notes || char(10), '') || ? END,
              updated_at = ? WHERE id = ?""",
           (kind, parse_date(request.form.get("date")) or today(), price, clean(request.form.get("destination")),
            clean(request.form.get("notes")), clean(request.form.get("notes")), now(), item_id))
        if request.form.get("with_contents") == "1":
            for cid in item_descendant_ids(item_id):
                ex("""UPDATE items SET status = ?, disposal_date = ?, disposal_price = NULL,
                      disposal_destination = ?, updated_at = ? WHERE id = ? AND status = 'active'""",
                   (kind, parse_date(request.form.get("date")) or today(), f"With {item['ref']}", now(), cid))
        else:  # parts it didn't go with stay in the collection, where the item was
            ex("UPDATE items SET parent_id = NULL, location_id = ? WHERE parent_id = ? AND status NOT IN (?, ?, ?)",
               (was_at, item_id) + DISPOSED)
        move_out(item_id)
        commit()
        flash(f"Marked as {STATUSES[kind].lower()}", "ok")
        return redirect(url_for("item_view", item_id=item_id))
    ncontents = sum(1 for cid in item_descendant_ids(item_id)
                    if q("SELECT status FROM items WHERE id = ?", (cid,), one=True)["status"] == "active")
    return render_template("dispose.html", item=item, kind=request.args.get("kind", "sold"), today=today(),
                           ncontents=ncontents)


@app.route("/items/<int:item_id>/reinstate", methods=["POST"])
def item_reinstate(item_id):
    item = get_item(item_id)
    for cid in item_descendant_ids(item_id):
        ex("""UPDATE items SET status = 'active', disposal_date = NULL, disposal_price = NULL,
              disposal_destination = NULL WHERE id = ? AND disposal_destination = ?""", (cid, f"With {item['ref']}"))
    ex("""UPDATE items SET status = 'active', disposal_date = NULL, disposal_price = NULL,
          disposal_destination = NULL, broken_date = NULL, updated_at = ? WHERE id = ?""", (now(), item_id))
    placed = move_back(item_id)
    commit()
    if placed:
        flash("Returned to the collection, back where it was", "ok")
    else:
        flash("Returned to the collection. Its old place no longer exists, so edit it to say where it is now.", "ok")
    return redirect(url_for("item_view", item_id=item_id))


# --- valuations & work log -------------------------------------------------
@app.route("/items/<int:item_id>/valuations", methods=["POST"])
def valuation_add(item_id):
    get_item(item_id)
    value = parse_money(request.form.get("value"))
    if value is None:
        flash("Enter a value.", "error")
    else:
        add_valuation(item_id, value, parse_date(request.form.get("date")) or today(),
                      clean(request.form.get("source")))
        ex("UPDATE items SET updated_at = ? WHERE id = ?", (now(), item_id))
        commit()
        flash("Valuation added", "ok")
    return redirect(url_for("item_view", item_id=item_id) + "#value")


@app.route("/valuations/<int:val_id>/edit", methods=["POST"])
def valuation_edit(val_id):
    row = q("SELECT * FROM valuations WHERE id = ?", (val_id,), one=True) or abort(404)
    value = parse_money(request.form.get("value"))
    if value is None:
        flash("Enter a value.", "error")
    else:
        ex("UPDATE valuations SET date = ?, value = ?, source = ? WHERE id = ?",
           (parse_date(request.form.get("date")) or row["date"], value, clean(request.form.get("source")), val_id))
        refresh_value(row["item_id"])
        commit()
        flash("Valuation updated", "ok")
    return redirect(url_for("item_view", item_id=row["item_id"]) + "#value")


@app.route("/valuations/<int:val_id>/delete", methods=["POST"])
def valuation_delete(val_id):
    row = q("SELECT * FROM valuations WHERE id = ?", (val_id,), one=True) or abort(404)
    ex("DELETE FROM valuations WHERE id = ?", (val_id,))
    refresh_value(row["item_id"])
    commit()
    flash("Valuation deleted", "ok")
    return redirect(url_for("item_view", item_id=row["item_id"]) + "#value")


def _work_fields(form):
    return (parse_date(form.get("date")) or today(), (form.get("description") or "").strip(),
            parse_money(form.get("cost")))


@app.route("/items/<int:item_id>/work", methods=["POST"])
def work_add(item_id):
    get_item(item_id)
    date, desc, cost = _work_fields(request.form)
    if not desc:
        flash("Describe the work done.", "error")
    else:
        ex("INSERT INTO work_log(item_id, date, description, cost) VALUES (?, ?, ?, ?)", (item_id, date, desc, cost))
        refresh_value(item_id)
        ex("UPDATE items SET updated_at = ? WHERE id = ?", (now(), item_id))
        commit()
        flash("Log entry added", "ok")
    return redirect(url_for("item_view", item_id=item_id) + "#work")


@app.route("/work/<int:entry_id>/edit", methods=["POST"])
def work_edit(entry_id):
    row = q("SELECT * FROM work_log WHERE id = ?", (entry_id,), one=True) or abort(404)
    date, desc, cost = _work_fields(request.form)
    if not desc:
        flash("Describe the work done.", "error")
    else:
        ex("UPDATE work_log SET date = ?, description = ?, cost = ? WHERE id = ?", (date, desc, cost, entry_id))
        refresh_value(row["item_id"])
        commit()
        flash("Log entry updated", "ok")
    return redirect(url_for("item_view", item_id=row["item_id"]) + "#work")


@app.route("/work/<int:entry_id>/delete", methods=["POST"])
def work_delete(entry_id):
    row = q("SELECT * FROM work_log WHERE id = ?", (entry_id,), one=True) or abort(404)
    ex("DELETE FROM work_log WHERE id = ?", (entry_id,))
    refresh_value(row["item_id"])
    commit()
    flash("Log entry deleted", "ok")
    return redirect(url_for("item_view", item_id=row["item_id"]) + "#work")


def record_fit(part_id, target, price, date, note=None):
    """A donor part fitted into one of your items: log it on that item, with any internal price.

    The price is a work cost on the repaired item and, through part_id, income for the donor.
    """
    part = q("SELECT * FROM items WHERE id = ?", (part_id,), one=True)
    donor = q("SELECT ref FROM items WHERE id = ?", (part["harvested_from_id"],), one=True) \
        if part["harvested_from_id"] else None
    desc = f"Fitted {part['name']} ({part['ref']}" + (f", from {donor['ref']}" if donor else "") + ")"
    if note:
        desc += f": {note}"
    ex("INSERT INTO work_log(item_id, date, description, cost, part_id) VALUES (?, ?, ?, ?, ?)",
       (target["id"], date, desc, price, part_id))
    refresh_value(target["id"])


@app.route("/items/<int:item_id>/fit", methods=["GET", "POST"])
def item_fit(item_id):
    """Fit a spare into another of your items, optionally at an internal price."""
    part = get_item(item_id)
    if request.method == "POST":
        target = resolve_item(request.form.get("target") or "")
        if target and target["status"] != "active":
            target = None
        if not target:
            flash("Couldn't find that item.", "error")
        elif target["id"] == item_id or target["id"] in item_descendant_ids(item_id):
            flash("A part can't be fitted into itself.", "error")
        else:
            date = parse_date(request.form.get("date")) or today()
            ex("UPDATE items SET parent_id = ?, location_id = NULL, status = 'active', updated_at = ? WHERE id = ?",
               (target["id"], now(), item_id))
            record_fit(item_id, target, parse_money(request.form.get("price")), date, clean(request.form.get("notes")))
            if part["purchase_price"] is not None and part["harvested_from_id"]:
                # its cost is now on the repaired item's work log; don't count it twice
                ex("UPDATE items SET purchase_price = NULL WHERE id = ?", (item_id,))
            commit()
            flash(f"Fitted into {target['ref']} — {target['name']}", "ok")
            return redirect(url_for("item_view", item_id=target["id"]) + "#work")
    donor = q("SELECT * FROM items WHERE id = ?", (part["harvested_from_id"],), one=True) \
        if part["harvested_from_id"] else None
    return render_template("fit.html", item=part, donor=donor, today=today())


# --- breakdown for spares ---------------------------------------------------
def apply_part(donor_id, donor_loc, existing, oc, r, target, date):
    """Record one part taken off a donor (new, or one already recorded inside it).

    spare  -> its own item, in a location or inside another item (a subitem). An optional value is
              fixed income for the donor and the new item's cost.
    repair -> fitted as a repair: nested in the target and added to its work log, with any
              internal price as a work cost there and income for the donor.
    """
    donor = q("SELECT * FROM items WHERE id = ?", (donor_id,), one=True)
    val = parse_money(r["value"])
    fields = {
        "harvested_from_id": donor_id,
        "parent_id": None,
        "location_id": None,
        "status": "active",
        "disposal_date": None,
        "disposal_price": None,
        "disposal_destination": None,
        "salvage_value": None,
        "purchase_date": donor["purchase_date"],
        "taken_off_date": date,
        "updated_at": now(),
    }
    if oc == "spare":
        if target:
            fields["parent_id"] = target["id"]
        else:
            fields["location_id"] = int_or_none(r["location"]) or donor_loc
        if val is not None:
            fields["salvage_value"] = val
            fields["purchase_price"] = val
    elif oc == "repair":
        fields["parent_id"] = target["id"]
    elif oc in ("sold", "given_away", "scrapped"):
        fields["status"] = oc
        fields["former_location_id"] = donor_loc
        fields["disposal_date"] = date
        fields["disposal_price"] = (val or 0.0) if oc == "sold" else None
        fields["disposal_destination"] = clean(r["dest"])
    note = f"Salvaged from {donor['ref']} {donor['name']} on {date}"
    if clean(r["desc"]):
        note += f": {r['desc'].strip()}"
    if existing:
        ex("UPDATE items SET notes = COALESCE(notes || char(10), '') || ? WHERE id = ?", (note, existing))
        ex(f"UPDATE items SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?",
           list(fields.values()) + [existing])
        part_id = existing
    else:
        # a new part's "added" date is the day it came off the donor
        fields.update(ref=allocate_ref(), name=r["name"].strip(), notes=note, taken_off_new=1,
                      created_at=date if date != today() else now())
        part_id = ex(f"INSERT INTO items({','.join(fields)}) VALUES ({','.join('?' * len(fields))})",
                     list(fields.values()))
    if oc == "repair":
        record_fit(part_id, target, val, date, clean(r["desc"]))
    return part_id


@app.route("/items/<int:item_id>/breakdown", methods=["GET", "POST"])
def item_breakdown(item_id):
    donor = get_item(item_id)
    donor_loc = effective_location_id(donor)
    children = q("SELECT * FROM items WHERE parent_id = ? ORDER BY name COLLATE NOCASE", (item_id,))
    errors, rows = [], []

    if request.method == "POST":
        f = request.form
        date = parse_date(f.get("date")) or today()
        cols = ["existing_id", "name", "outcome", "value", "location", "target", "dest", "desc"]
        lists = {c: f.getlist("row_" + c) for c in cols}
        n = len(lists["outcome"])
        for i in range(n):
            r = {c: (lists[c][i] if i < len(lists[c]) else "") for c in cols}
            rows.append(r)
        plan = []
        for r in rows:
            existing = int_or_none(r["existing_id"])
            oc = r["outcome"]
            if oc not in PART_OUTCOMES or (oc == "leave" and not existing):
                continue
            if not existing and not r["name"].strip():
                continue
            if oc == "leave":
                continue
            target = None
            if oc == "repair" or (oc == "spare" and r["target"].strip()):
                target = resolve_item(r["target"])
                if target and target["status"] != "active":
                    errors.append(f"'{r['name'] or 'part'}': {target['ref']} {target['name']} isn't in the collection any more.")
                    target = None
                elif not target:
                    errors.append(f"'{r['name'] or 'part'}': couldn't find the item '{r['target']}'.")
                elif target and (target["id"] == item_id or (existing and (target["id"] == existing or
                                                               target["id"] in item_descendant_ids(existing)))):
                    errors.append(f"'{r['name']}': can't fit a part into the donor or into itself.")
            if oc == "spare" and r["location"]:
                existing_location_id(r["location"], errors,
                                     f"'{r['name'] or 'part'}': the place you chose to store it")
            plan.append((existing, oc, r, target))

        if not errors:
            if f.get("mark_broken") == "1" or donor["status"] == "broken":
                ex("UPDATE items SET status = 'broken', broken_date = COALESCE(broken_date, ?), updated_at = ? "
                   "WHERE id = ?", (date, now(), item_id))
            if clean(f.get("notes")):
                ex("UPDATE items SET notes = COALESCE(notes || char(10), '') || ? WHERE id = ?",
                   (f"[{date}] {f.get('notes').strip()}", item_id))
            for existing, oc, r, target in plan:
                apply_part(item_id, donor_loc, existing, oc, r, target, date)
            commit()
            flash(f"Recorded {len(plan)} part(s) from {donor['ref']}", "ok")
            return redirect(url_for("item_view", item_id=item_id))
        for e in errors:
            flash(e, "error")
    else:
        for c in children:
            rows.append({"existing_id": str(c["id"]), "name": c["name"], "outcome": "leave",
                         "value": "", "location": "",
                         "target": "", "dest": "", "desc": "", "ref": c["ref"]})
        rows.append({"existing_id": "", "name": "", "outcome": "spare", "value": "", "location": "",
                     "target": "", "dest": "", "desc": ""})
    child_refs = {str(c["id"]): c["ref"] for c in children}
    for r in rows:
        r.setdefault("ref", child_refs.get(r.get("existing_id", ""), ""))
    parts, part_totals = breakdown_summary(item_id)
    return render_template("breakdown.html", donor=donor, rows=rows, locations=location_tree(),
                           donor_loc=donor_loc, today=today(), parts=parts, part_totals=part_totals)


@app.route("/items/<int:item_id>/parts", methods=["POST"])
def part_add(item_id):
    """One part from the inline row on a donor's page: a new part, or one already recorded inside it."""
    donor = get_item(item_id)
    back = redirect(url_for("item_view", item_id=item_id) + "#parts")
    if donor["status"] not in ("active", "broken"):
        flash("Parts can only be taken off items still in the collection or being broken for spares.", "error")
        return back
    f = request.form
    text, oc = (f.get("part") or "").strip(), f.get("outcome")
    r = {"name": text, "value": f.get("value", ""), "location": f.get("location", ""),
         "target": f.get("target", ""), "dest": f.get("dest", ""), "desc": ""}
    errors = []
    if not text:
        errors.append("Enter the part's name.")
    if oc not in PART_OUTCOMES or oc == "leave":
        errors.append("Choose what happened to the part.")
    inside = resolve_item(text) if text else None
    existing = inside["id"] if inside and inside["parent_id"] == item_id else None
    if existing:
        r["name"] = inside["name"]
    target = None
    if oc == "repair" or (oc == "spare" and r["target"].strip()):
        target = resolve_item(r["target"])
        if target and target["status"] != "active":
            errors.append(f"{target['ref']} {target['name']} isn't in the collection any more.")
            target = None
        elif not target:
            errors.append(f"Couldn't find the item '{r['target']}'.")
        elif target["id"] == item_id or (existing and (target["id"] == existing or
                                                       target["id"] in item_descendant_ids(existing))):
            errors.append("A part can't be fitted into the donor or into itself.")
    if oc == "spare" and r["location"]:
        existing_location_id(r["location"], errors, "The place you chose to store it")
    if errors:
        for e in errors:
            flash(e, "error")
        return back
    date = parse_date(f.get("date")) or today()
    part_id = apply_part(item_id, effective_location_id(donor), existing, oc, r, target, date)
    ex("UPDATE items SET updated_at = ? WHERE id = ?", (now(), item_id))
    commit()
    ref = q("SELECT ref FROM items WHERE id = ?", (part_id,), one=True)["ref"]
    flash(f"Recorded {r['name']} ({ref})", "ok")
    return back


@app.route("/parts/<int:part_id>/edit", methods=["POST"])
def part_edit(part_id):
    """Correct a 'parts taken off' row: the date, the amount and who it went to."""
    part = get_item(part_id)
    if not part["harvested_from_id"]:
        abort(404)
    oc, _, _, _ = part_flow(part)
    f = request.form
    date = parse_date(f.get("date")) or part["taken_off_date"] or today()
    amount = parse_money(f.get("value"))
    ex("UPDATE items SET taken_off_date = ?, updated_at = ? WHERE id = ?", (date, now(), part_id))
    if part["salvage_value"] is not None or (oc == "spare" and amount is not None):
        # the cost moved onto the part; its own purchase price follows unless that cost has since moved on
        follows = part["purchase_price"] is None and part["salvage_value"] is None or \
            (part["purchase_price"] is not None and part["purchase_price"] == part["salvage_value"])
        ex("UPDATE items SET salvage_value = ? WHERE id = ?", (amount, part_id))
        if follows:
            ex("UPDATE items SET purchase_price = ? WHERE id = ?", (amount, part_id))
    elif oc == "repair":
        t = part_transfers(part_id)[-1]
        ex("UPDATE work_log SET date = ?, cost = ? WHERE id = ?", (date, amount, t["id"]))
        refresh_value(t["item_id"])
    elif oc == "sold":
        ex("UPDATE items SET disposal_price = ? WHERE id = ?", (amount or 0.0, part_id))
    if oc in ("sold", "given_away", "scrapped"):
        ex("UPDATE items SET disposal_date = ?, disposal_destination = ? WHERE id = ?",
           (date, clean(f.get("dest")), part_id))
    commit()
    flash(f"Updated {part['name']}", "ok")
    return redirect(url_for("item_view", item_id=part["harvested_from_id"]) + "#parts")


@app.route("/parts/<int:part_id>/remove", methods=["POST"])
def part_remove(part_id):
    """Undo taking a part off: delete it if taking it off created it, otherwise put it back inside."""
    part = get_item(part_id)
    donor_id = part["harvested_from_id"]
    if not donor_id:
        abort(404)
    for t in part_transfers(part_id):
        ex("DELETE FROM work_log WHERE id = ?", (t["id"],))
        refresh_value(t["item_id"])
    if part["taken_off_new"]:
        loc = effective_location_id(part)
        ex("UPDATE items SET parent_id = ?, location_id = CASE WHEN ? IS NULL THEN ? ELSE NULL END WHERE parent_id = ?",
           (part["parent_id"], part["parent_id"], loc, part_id))
        ex("UPDATE items SET harvested_from_id = NULL WHERE harvested_from_id = ?", (part_id,))
        for ph in q("SELECT filename FROM photos WHERE item_id = ?", (part_id,)):
            remove_photo_files(ph["filename"])
        ex("DELETE FROM items WHERE id = ?", (part_id,))
        msg = f"Removed {part['name']} ({part['ref']}) and deleted it"
    else:
        moved_cost = part["salvage_value"] is not None and part["purchase_price"] == part["salvage_value"]
        ex("""UPDATE items SET status = 'active', parent_id = ?, location_id = NULL, harvested_from_id = NULL,
              salvage_value = NULL, taken_off_date = NULL, disposal_date = NULL, disposal_price = NULL,
              disposal_destination = NULL, former_location_id = NULL, former_parent_id = NULL,
              purchase_price = CASE WHEN ? THEN NULL ELSE purchase_price END, updated_at = ? WHERE id = ?""",
           (donor_id, 1 if moved_cost else 0, now(), part_id))
        msg = f"Put {part['name']} ({part['ref']}) back inside"
    ex("UPDATE items SET updated_at = ? WHERE id = ?", (now(), donor_id))
    commit()
    flash(msg, "ok")
    return redirect(url_for("item_view", item_id=donor_id) + "#parts")


# --- photos -----------------------------------------------------------------
def remove_photo_files(filename):
    for d in (PHOTO_DIR, THUMB_DIR):
        try:
            os.remove(os.path.join(d, filename))
        except OSError:
            pass


def save_photo(item_id, fs):
    if not fs or not fs.filename:
        return None
    ext = os.path.splitext(fs.filename)[1].lower()
    base = uuid.uuid4().hex
    filename = None
    if Image is not None:
        try:
            img = Image.open(fs.stream)
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((2400, 2400))
            filename = base + ".jpg"
            img.save(os.path.join(PHOTO_DIR, filename), "JPEG", quality=88, optimize=True)
            th = img.copy()
            th.thumbnail((480, 480))
            th.save(os.path.join(THUMB_DIR, filename), "JPEG", quality=80)
        except Exception:
            filename = None
            fs.stream.seek(0)
    if filename is None:
        if ext not in IMAGE_EXT:
            flash(f"Skipped {fs.filename}: not a recognised image", "error")
            return None
        filename = base + ext
        fs.save(os.path.join(PHOTO_DIR, filename))
    nxt = q("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM photos WHERE item_id = ?", (item_id,), one=True)["n"]
    ex("INSERT INTO photos(item_id, filename, sort_order) VALUES (?, ?, ?)", (item_id, filename, nxt))
    return filename


@app.route("/items/<int:item_id>/photos", methods=["POST"])
def photo_upload(item_id):
    get_item(item_id)
    n = 0
    for fs in request.files.getlist("photos"):
        if save_photo(item_id, fs):
            n += 1
    ex("UPDATE items SET updated_at = ? WHERE id = ?", (now(), item_id))
    commit()
    flash(f"Added {n} photo(s)", "ok")
    return redirect(url_for("item_view", item_id=item_id) + "#photos")


@app.route("/photos/<int:photo_id>/delete", methods=["POST"])
def photo_delete(photo_id):
    p = q("SELECT * FROM photos WHERE id = ?", (photo_id,), one=True) or abort(404)
    remove_photo_files(p["filename"])
    ex("DELETE FROM photos WHERE id = ?", (photo_id,))
    commit()
    return redirect(url_for("item_view", item_id=p["item_id"]) + "#photos")


@app.route("/photos/<int:photo_id>/primary", methods=["POST"])
def photo_primary(photo_id):
    p = q("SELECT * FROM photos WHERE id = ?", (photo_id,), one=True) or abort(404)
    ex("UPDATE photos SET sort_order = sort_order + 1 WHERE item_id = ?", (p["item_id"],))
    ex("UPDATE photos SET sort_order = 0 WHERE id = ?", (photo_id,))
    commit()
    return redirect(url_for("item_view", item_id=p["item_id"]) + "#photos")


@app.route("/media/photo/<path:filename>")
def media_photo(filename):
    return send_from_directory(PHOTO_DIR, filename, max_age=86400 * 30)


@app.route("/media/thumb/<path:filename>")
def media_thumb(filename):
    if os.path.exists(os.path.join(THUMB_DIR, filename)):
        return send_from_directory(THUMB_DIR, filename, max_age=86400 * 30)
    return send_from_directory(PHOTO_DIR, filename, max_age=86400 * 30)


# --- barcodes, labels, scanning --------------------------------------------
@app.route("/barcode/<path:text>.svg")
def barcode_svg(text):
    try:
        svg = code128.svg(text)
    except ValueError:
        abort(400)
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.route("/qr.svg")
def qr_svg():
    try:
        svg = qr.svg(request.args.get("d", ""))
    except ValueError:
        abort(400)
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.route("/qr.json")
def qr_json():
    """QR modules for drawing a label on a canvas (Bluetooth label printers)."""
    try:
        m = qr.matrix(request.args.get("d", ""))
    except ValueError:
        abort(400)
    return jsonify(["".join("1" if v else "0" for v in row) for row in m])


def base_url():
    return (setting("public_url", "") or request.host_url).rstrip("/")


def item_qr_url(ref):
    """What goes inside an item's QR label: a link that opens the item from any phone camera.

    Written in capitals where possible (HTTPS://HOST/A/REF): web addresses ignore case, and QR codes
    hold capitals, digits and - . / : far more compactly, so the code is a size smaller."""
    base = base_url()
    parts = urlsplit(base)
    upper = f"{parts.scheme}://{parts.netloc}/A/{ref}".upper()
    if parts.path in ("", "/") and not parts.query and qr._is_alnum(upper):
        return upper
    return f"{base}/a/{quote(ref, safe='-_.')}"


def label_qr_content(ref):
    """QR content for Bluetooth labels: the link, or just the reference (smaller; RelicBox scanner only)."""
    return ref if setting("nb_qr", "link") == "ref" else item_qr_url(ref)


@app.route("/A/<path:ref>")
@app.route("/a/<path:ref>")
def item_by_ref(ref):
    row = q("SELECT id FROM items WHERE ref = ? COLLATE NOCASE", (ref,), one=True)
    if not row:
        return render_template("scan.html", not_found=ref), 404
    return redirect(url_for("item_view", item_id=row["id"]))


# Old Homebox QR labels encode http://<homebox>/item/<uuid> and /location/<uuid>.
@app.route("/item/<legacy_id>")
def item_by_legacy(legacy_id):
    row = q("SELECT id FROM items WHERE legacy_id = ?", (legacy_id,), one=True)
    if not row:
        return render_template("scan.html", not_found=legacy_id), 404
    return redirect(url_for("item_view", item_id=row["id"]))


@app.route("/location/<legacy_id>")
def location_by_legacy(legacy_id):
    row = q("SELECT id FROM locations WHERE legacy_id = ?", (legacy_id,), one=True)
    if not row:
        return render_template("scan.html", not_found=legacy_id), 404
    return redirect(url_for("location_view", loc_id=row["id"]))


@app.route("/items/<int:item_id>/label")
def item_label_page(item_id):
    item = dict(get_item(item_id))
    item["descriptors"] = [d for d in item_descriptors(item_id) if d["value"]]
    return render_template("labels.html", items=[item], back=url_for("item_view", item_id=item_id))


@app.route("/labels")
def labels():
    rows = search_items(request.args.get("q", ""), request.args.get("status", "active"),
                        int_or_none(request.args.get("location")))
    for r in rows:
        r["descriptors"] = [d for d in item_descriptors(r["id"]) if d["value"]]
    return render_template("labels.html", items=rows, back=url_for("items_list", **request.args))


@app.route("/scan")
def scan():
    return render_template("scan.html")


@app.route("/lookup")
def lookup():
    code = (request.args.get("code") or "").strip()
    if not code:
        return redirect(url_for("scan"))
    # A scanned QR may hold a URL: our own /a/<ref> or /items/<id>, or an old Homebox /item/<uuid> label.
    if "://" in code or code.startswith("/"):
        path = urlsplit(code).path
        for pattern, table, endpoint, key in (
                (r"/item/([0-9A-Fa-f-]{36})$", "items", "item_view", "item_id"),
                (r"/location/([0-9A-Fa-f-]{36})$", "locations", "location_view", "loc_id")):
            m = re.search(pattern, path)
            if m:
                row = q(f"SELECT id FROM {table} WHERE legacy_id = ? COLLATE NOCASE", (m.group(1),), one=True)
                if row:
                    return redirect(url_for(endpoint, **{key: row["id"]}))
        m = re.search(r"/items/(\d+)$", path)
        if m and q("SELECT 1 FROM items WHERE id = ?", (int(m.group(1)),), one=True):
            return redirect(url_for("item_view", item_id=int(m.group(1))))
        m = re.search(r"/a/([^/]+)$", path, re.I)
        if m:
            code = unquote(m.group(1))
    row = q("SELECT * FROM items WHERE ref = ? COLLATE NOCASE", (code,), one=True)
    if row:
        return redirect(url_for("item_view", item_id=row["id"]))
    rows = q("SELECT * FROM items WHERE barcode = ? ORDER BY status = 'active' DESC, name", (code,))
    if len(rows) == 1:
        return redirect(url_for("item_view", item_id=rows[0]["id"]))
    if rows:
        return render_template("items.html", rows=decorate([dict(r) for r in rows]), qtext=code, status="all",
                               loc=None, locations=location_tree(),
                               totals=list_totals(rows))
    return render_template("scan.html", not_found=code)


@app.route("/api/items")
def api_items():
    text = request.args.get("q", "")
    status = "active" if request.args.get("pick") else request.args.get("status", "all")
    rows = search_items(text, status, limit=25) if text.strip() else []
    return jsonify([{"id": r["id"], "ref": r["ref"], "name": r["name"], "label": item_label(r),
                     "status": r["status"]} for r in rows])


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
def location_stats():
    """Inclusive (count, pieces, value) for each location, from active items."""
    loc_by_id, _ = load_locations()
    items = {r["id"]: dict(r) for r in q("SELECT * FROM items")}
    stats = defaultdict(lambda: {"count": 0, "pieces": 0, "cost": 0.0, "value": 0.0, "direct": 0})
    for it in items.values():
        if it["status"] != "active":
            continue
        lid = effective_location_id(it, items)
        c, v = cost_basis(it), it["value"] or 0
        if lid not in loc_by_id:
            lid = None
        if lid is None:
            s = stats[None]
            s["count"] += 1
            s["pieces"] += it["quantity"]
            s["cost"] += c
            s["value"] += v
            s["direct"] += 1
            continue
        stats[lid]["direct"] += 1
        for loc in location_path(lid, loc_by_id):
            s = stats[loc["id"]]
            s["count"] += 1
            s["pieces"] += it["quantity"]
            s["cost"] += c
            s["value"] += v
    return stats


def gone_stats():
    """Totals for the built-in "Sold" entry: everything sold, given away or scrapped."""
    r = q(f"""SELECT COUNT(*) AS count, COALESCE(SUM(CASE WHEN status = 'sold' THEN disposal_price END), 0) AS income
              FROM items WHERE status IN ({','.join('?' * len(DISPOSED))})""", DISPOSED, one=True)
    return dict(r)


@app.route("/locations")
def locations():
    return render_template("locations.html", tree=location_tree(), stats=location_stats(), gone=gone_stats())


def location_form_data(form, existing=None):
    name = (form.get("name") or "").strip()
    errors = []
    parent_id = existing_location_id(form.get("parent_id"), errors, "The parent location you chose")
    if not name:
        errors.append("Name is required.")
    if existing and parent_id:
        _, children = load_locations()
        if parent_id in location_descendants(existing["id"], children):
            errors.append("A location can't be inside itself.")
            parent_id = existing["parent_id"]
    return {"name": name, "parent_id": parent_id, "description": clean(form.get("description"))}, errors


@app.route("/locations/new", methods=["GET", "POST"])
def location_new():
    if request.method == "POST":
        data, errors = location_form_data(request.form)
        if not errors:
            lid = ex("INSERT INTO locations(name, parent_id, description) VALUES (?, ?, ?)",
                     (data["name"], data["parent_id"], data["description"]))
            commit()
            flash(f"Added location {data['name']}", "ok")
            return redirect(url_for("location_view", loc_id=lid))
        for e in errors:
            flash(e, "error")
        return render_template("location_form.html", loc=None, f=data, tree=location_tree())
    return render_template("location_form.html", loc=None,
                           f={"parent_id": int_or_none(request.args.get("parent_id"))}, tree=location_tree())


@app.route("/locations/<int:loc_id>")
def location_view(loc_id):
    loc_by_id, children = load_locations()
    loc = loc_by_id.get(loc_id) or abort(404)
    status = request.args.get("status", "active")
    direct = decorate([dict(r) for r in q(
        "SELECT * FROM items WHERE location_id = ? AND parent_id IS NULL" +
        ("" if status == "all" else " AND status = ?") + " ORDER BY name COLLATE NOCASE",
        (loc_id,) if status == "all" else (loc_id, status))])
    attach_contents(direct, status)
    stats = location_stats()
    return render_template("location_view.html", loc=loc, path=location_path(loc_id, loc_by_id),
                           sublocs=children.get(loc_id, []), items=direct, stats=stats, status=status,
                           has_nested=any(r["children"] for r in direct))


@app.route("/locations/<int:loc_id>/edit", methods=["GET", "POST"])
def location_edit(loc_id):
    loc = q("SELECT * FROM locations WHERE id = ?", (loc_id,), one=True) or abort(404)
    if request.method == "POST":
        data, errors = location_form_data(request.form, existing=loc)
        if not errors:
            ex("UPDATE locations SET name = ?, parent_id = ?, description = ? WHERE id = ?",
               (data["name"], data["parent_id"], data["description"], loc_id))
            commit()
            flash("Saved", "ok")
            return redirect(url_for("location_view", loc_id=loc_id))
        for e in errors:
            flash(e, "error")
    return render_template("location_form.html", loc=loc, f=dict(loc), tree=location_tree(exclude_id=loc_id))


@app.route("/locations/<int:loc_id>/delete", methods=["POST"])
def location_delete(loc_id):
    loc = q("SELECT * FROM locations WHERE id = ?", (loc_id,), one=True) or abort(404)
    # Move contents up a level rather than losing them.
    ex("UPDATE locations SET parent_id = ? WHERE parent_id = ?", (loc["parent_id"], loc_id))
    ex("UPDATE items SET location_id = ? WHERE location_id = ?", (loc["parent_id"], loc_id))
    ex("DELETE FROM locations WHERE id = ?", (loc_id,))
    commit()
    flash(f"Deleted {loc['name']}; its contents moved up a level", "ok")
    if loc["parent_id"]:
        return redirect(url_for("location_view", loc_id=loc["parent_id"]))
    return redirect(url_for("locations"))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def in_range(d, start, end):
    if not d:
        return not start and not end
    return (not start or d >= start) and (not end or d <= end)


@app.route("/reports")
def reports():
    start = parse_date(request.args.get("from"))
    end = parse_date(request.args.get("to"))
    year = request.args.get("year")
    if year and year.isdigit():
        start, end = f"{year}-01-01", f"{year}-12-31"
    group_by = int_or_none(request.args.get("group"))

    items = {r["id"]: dict(r) for r in q("SELECT * FROM items")}
    loc_by_id, _ = load_locations()


    active = [it for it in items.values() if it["status"] == "active"]
    collection = {
        "count": len(active),
        "pieces": sum(it["quantity"] for it in active),
        "value": sum(it["value"] or 0 for it in active),
        "valued": sum(1 for it in active if it["value"] is not None),
        "cost": sum(cost_basis(it) for it in active),
        "work": sum(it["work_cost"] or 0 for it in active),
        "top_level": sum(1 for it in active if not it["parent_id"]),
        "spares": sum(1 for it in active if it["harvested_from_id"] and not it["parent_id"]),
    }

    # Group by descriptor
    grouped = []
    if group_by:
        dvals = {r["item_id"]: r["value"] for r in
                 q("SELECT item_id, value FROM item_descriptors WHERE descriptor_type_id = ?", (group_by,))}
        g = defaultdict(lambda: {"count": 0, "cost": 0.0, "value": 0.0})
        for it in active:
            key = dvals.get(it["id"], "(not set)")
            g[key]["count"] += 1
            g[key]["cost"] += cost_basis(it)
            g[key]["value"] += it["value"] or 0
        grouped = sorted(g.items(), key=lambda kv: (-kv[1]["count"], kv[0].lower()))

    # Disposals
    sold = sorted([it for it in items.values() if it["status"] == "sold" and in_range(it["disposal_date"], start, end)],
                  key=lambda it: it["disposal_date"] or "", reverse=True)
    given = sorted([it for it in items.values()
                    if it["status"] == "given_away" and in_range(it["disposal_date"], start, end)],
                   key=lambda it: it["disposal_date"] or "", reverse=True)
    for it in sold + given:
        it["donor"] = items.get(it["harvested_from_id"])
    sales = {
        "count": len(sold),
        "income": sum(it["disposal_price"] or 0 for it in sold),
        "cost": sum(cost_basis(it) for it in sold),
        "whole_count": sum(1 for it in sold if not it["harvested_from_id"]),
        "whole_income": sum(it["disposal_price"] or 0 for it in sold if not it["harvested_from_id"]),
        "parts_count": sum(1 for it in sold if it["harvested_from_id"]),
        "parts_income": sum(it["disposal_price"] or 0 for it in sold if it["harvested_from_id"]),
    }
    # Profit on whole items; shells of broken-down items are accounted for under breakdowns instead.
    sales["profit"] = sum((it["disposal_price"] or 0) - cost_basis(it) for it in sold
                          if (not it["harvested_from_id"] or it["salvage_value"] is not None) and not it["broken_date"])

    broken = sorted([it for it in items.values() if it["broken_date"] and in_range(it["broken_date"], start, end)],
                    key=lambda it: it["broken_date"] or "", reverse=True)
    bt = defaultdict(float)
    for b in broken:
        rows, totals = breakdown_summary(b["id"])
        b["nparts"] = len(rows)
        b["totals"] = totals
        b["cost"] = (b["purchase_price"] or 0) + (b["work_cost"] or 0)
        b["moved"] = b["moved_out"] or 0
        b["net"] = totals["income"] - cost_basis(b)
        for k in ("sold", "shell", "income", "moved"):
            bt[k] += totals[k]
        bt["cost"] += b["cost"]
        bt["parts"] += len(rows)
    bt["net"] = bt["income"] - (bt["cost"] - bt["moved"])

    # Purchases in range
    bought = [it for it in items.values() if it["purchase_date"] and not it["harvested_from_id"]
              and in_range(it["purchase_date"], start, end)]
    purchases = {"count": len(bought), "spent": sum(it["purchase_price"] or 0 for it in bought)}
    # Work done in range (by the date of each log entry)
    wrows = [r for r in q("SELECT date, cost FROM work_log") if in_range(r["date"], start, end)]
    purchases["work_count"] = len(wrows)
    purchases["work_spent"] = sum(r["cost"] or 0 for r in wrows)

    years = sorted({d[:4] for it in items.values()
                    for d in (it["purchase_date"], it["disposal_date"], it["broken_date"]) if d}, reverse=True)
    return render_template("reports.html", collection=collection, tree=location_tree(), stats=location_stats(),
                           sold=sold, given=given, sales=sales, broken=broken, bt=bt, purchases=purchases,
                           start=start, end=end, year=year, years=years, dts=descriptor_types(),
                           group_by=group_by, grouped=grouped,
                           unassigned=location_stats().get(None))


@app.route("/export/items.csv")
def export_csv():
    dts = descriptor_types()
    items = {r["id"]: dict(r) for r in q("SELECT * FROM items ORDER BY ref")}
    loc_by_id, _ = load_locations()
    desc = defaultdict(dict)
    for r in q("SELECT * FROM item_descriptors"):
        desc[r["item_id"]][r["descriptor_type_id"]] = r["value"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ref", "name", "status", "barcode", "quantity", "location", "inside", "harvested_from", "value",
                "work_cost", "purchase_date", "purchase_price", "seller", "platform", "disposal_date", "disposal_price",
                "destination", "broken_date", "description", "notes"] + [dt["name"] for dt in dts])
    for it in items.values():
        loc = " / ".join(l["name"] for l in location_path(effective_location_id(it, items), loc_by_id))
        parent = items.get(it["parent_id"])
        donor = items.get(it["harvested_from_id"])
        w.writerow([it["ref"], it["name"], it["status"], it["barcode"], it["quantity"], loc,
                    parent["ref"] if parent else "", donor["ref"] if donor else "", it["value"],
                    it["work_cost"] or "", it["purchase_date"], it["purchase_price"], it["purchase_seller"], it["purchase_platform"],
                    it["disposal_date"], it["disposal_price"], it["disposal_destination"], it["broken_date"],
                    it["description"], it["notes"]] + [desc[it["id"]].get(dt["id"], "") for dt in dts])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=inventory-{today()}.csv"})


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "general":
            set_setting("site_name", (request.form.get("site_name") or "RelicBox").strip())
            set_setting("currency", request.form.get("currency", "£").strip())
            set_setting("ref_prefix", request.form.get("ref_prefix", "").strip())
            set_setting("label_format", "code128" if request.form.get("label_format") == "code128" else "qr")
            pub = (request.form.get("public_url") or "").strip().rstrip("/")
            if pub and not re.match(r"https?://", pub):
                pub = "https://" + pub
            set_setting("public_url", pub)
            set_setting("nb_size", request.form.get("nb_size") if request.form.get("nb_size") in NB_SIZES else "22x12")
            set_setting("nb_density", max(1, min(int_or_none(request.form.get("nb_density")) or 2, 5)))
            set_setting("nb_qr", "ref" if request.form.get("nb_qr") == "ref" else "link")
            set_setting("nb_name", "1" if request.form.get("nb_name") == "1" else "0")
            digits = int_or_none(request.form.get("ref_digits")) or 5
            set_setting("ref_digits", max(1, min(digits, 10)))
            nxt = int_or_none(request.form.get("ref_next"))
            if nxt and nxt > 0:
                set_setting("ref_next", nxt)
            flash("Settings saved", "ok")
        elif action == "add_descriptor":
            name = (request.form.get("name") or "").strip()
            if name:
                if q("SELECT 1 FROM descriptor_types WHERE name = ?", (name,), one=True):
                    flash(f"'{name}' already exists", "error")
                else:
                    n = q("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM descriptor_types", one=True)["n"]
                    ex("INSERT INTO descriptor_types(name, sort_order) VALUES (?, ?)", (name, n))
                    flash(f"Added descriptor '{name}'", "ok")
        elif action == "rename_descriptor":
            did, name = int_or_none(request.form.get("id")), (request.form.get("name") or "").strip()
            if did and name:
                clash = q("SELECT id FROM descriptor_types WHERE name = ?", (name,), one=True)
                if clash and clash["id"] != did:
                    flash(f"'{name}' already exists", "error")
                else:
                    ex("UPDATE descriptor_types SET name = ? WHERE id = ?", (name, did))
        elif action in ("up", "down"):
            dts = [dict(d) for d in descriptor_types()]
            did = int_or_none(request.form.get("id"))
            idx = next((i for i, d in enumerate(dts) if d["id"] == did), None)
            if idx is not None:
                j = idx - 1 if action == "up" else idx + 1
                if 0 <= j < len(dts):
                    dts[idx], dts[j] = dts[j], dts[idx]
                for i, d in enumerate(dts):
                    ex("UPDATE descriptor_types SET sort_order = ? WHERE id = ?", (i, d["id"]))
        elif action == "delete_descriptor":
            did = int_or_none(request.form.get("id"))
            ex("DELETE FROM item_descriptors WHERE descriptor_type_id = ?", (did,))
            ex("DELETE FROM descriptor_types WHERE id = ?", (did,))
            flash("Descriptor deleted", "ok")
        commit()
        return redirect(url_for("settings_page"))
    usage = {r["descriptor_type_id"]: r["n"] for r in
             q("SELECT descriptor_type_id, COUNT(*) AS n FROM item_descriptors GROUP BY descriptor_type_id")}
    s = {k: setting(k, "") for k in ("site_name", "currency", "ref_prefix", "ref_digits", "ref_next",
                                      "label_format", "public_url", "nb_size", "nb_density", "nb_qr", "nb_name")}
    return render_template("settings.html", s=s, dts=descriptor_types(), usage=usage)


@app.route("/import/homebox", methods=["GET", "POST"])
def import_homebox():
    report, error = None, None
    form = {"sold": "Sold", "broken": "Breaking for Parts", "ignore": "Various"}
    if request.method == "POST":
        form = {k: request.form.get(k, "") for k in form}
        fs = request.files.get("csv")
        if not fs or not fs.filename:
            error = "Choose the CSV file exported from Homebox."
        else:
            raw = fs.read()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("cp1252")
            split = lambda s: [x for x in s.split(",") if x.strip()]  # noqa: E731
            try:
                report = homebox_import.run(text, sold_locations=split(form["sold"]),
                                            broken_locations=split(form["broken"]),
                                            ignore_locations=split(form["ignore"]),
                                            allocate_ref=allocate_ref, set_setting=set_setting, now=now)
                commit()
            except ValueError as e:
                dbm.get_db().rollback()
                error = str(e)
    return render_template("import.html", report=report, error=error, form=form)


@app.route("/backup/inventory.db")
def backup_db():
    """Consistent snapshot of the SQLite database."""
    tmp = os.path.join(DATA_DIR, f".backup-{uuid.uuid4().hex}.db")
    src = dbm.connect(app.config["DB_PATH"])
    dst = dbm.connect(tmp)
    src.backup(dst)
    dst.close()
    src.close()
    with open(tmp, "rb") as fh:
        data = fh.read()
    os.remove(tmp)
    return Response(data, mimetype="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename=inventory-{today()}.db"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "3200")), debug=bool(os.environ.get("DEBUG")))
