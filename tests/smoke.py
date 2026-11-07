import io, os, re, shutil, sys, tempfile
tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = tmp
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from app import app  # noqa
from PIL import Image

c = app.test_client()

def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.data[:2000])
    return r

def post(url, data, follow=True, **kw):
    return ok(c.post(url, data=data, follow_redirects=follow, **kw))

def item_id_from(r):
    return int(re.search(r"/items/(\d+)", r.request.path).group(1))

# locations
post("/locations/new", {"name": "House", "description": "Home"})
post("/locations/new", {"name": "Loft", "parent_id": "1"})
post("/locations/new", {"name": "Shelf 3", "parent_id": "2"})
post("/locations/new", {"name": "Garage", "parent_id": "1"})
# cycle prevention
r = post("/locations/1/edit", {"name": "House", "parent_id": "3"})
assert b"inside itself" in r.data

# items
img = io.BytesIO(); Image.new("RGB", (800, 600), (200, 100, 50)).save(img, "JPEG"); img.seek(0)
r = post("/items/new", {"name": "Amiga 500", "location_id": "3", "desc_1": "Commodore", "desc_2": "A500",
                         "desc_3": "SN12345", "purchase_date": "2025-03-01", "purchase_price": "£120",
                         "purchase_seller": "Bob", "purchase_platform": "eBay", "barcode": "5012345678900",
                         "photos": (img, "a500.jpg")}, content_type="multipart/form-data")
a500 = item_id_from(r); assert b"RC-00001" in r.data
r = post("/items/new", {"name": "A501 RAM expansion", "parent": "RC-00001"})
a501 = item_id_from(r); assert b"Amiga 500" in r.data
r = post("/items/new", {"name": "Amiga 1200 (dead)", "location_id": "4", "purchase_price": "80",
                         "purchase_date": "2025-06-01", "desc_1": "Commodore"})
a1200 = item_id_from(r)
r = post("/items/new", {"name": "Gayle chip", "parent": f"RC-00003 — Amiga 1200 (dead)"})
gayle = item_id_from(r)
r = post("/items/new", {"name": "Amiga 1200 (working)", "location_id": "3", "purchase_price": "150"})
a1200w = item_id_from(r)
# nested location shown
r = ok(c.get(f"/items/{a501}")); assert b"Shelf 3" in r.data and b"Loft" in r.data
# cycle prevention on items
r = post(f"/items/{a500}/edit", {"name": "Amiga 500", "parent": "RC-00002"})
assert b"inside itself" in r.data

# breakdown form renders
r = ok(c.get(f"/items/{a1200}/breakdown")); assert b"Gayle chip" in r.data
# breakdown: existing gayle used in repair of a1200w, new PSU sold, new keyboard spare, new case scrapped
post(f"/items/{a1200}/breakdown", {
    "date": "2025-07-01", "mark_broken": "1", "donor_value": "5", "notes": "dead CPU",
    "row_existing_id": [str(gayle), "", "", "", ""],
    "row_name": ["Gayle chip", "PSU", "Keyboard", "Case top", ""],
    "row_outcome": ["repair", "sold", "spare", "scrapped", "spare"],
    "row_value": ["15", "40", "", "", ""],
    "row_location": ["", "", "2", "", ""],
    "row_target": ["RC-00005 — Amiga 1200 (working)", "", "", "", ""],
    "row_dest": ["", "eBay buyer", "", "", ""],
    "row_desc": ["", "", "", "cracked", ""],
})
r = ok(c.get(f"/items/{a1200}"))
html = r.data.decode()
assert "Breaking for spares" in html and "Added to work log of" in html and "Sold / charged to someone" in html
# donor income = gayle internal price 15 + PSU sold 40; the keyboard spare earns nothing until used
assert "£55.00" in html and "Loss so far" in html and "£25.00" in html   # 55 - 80
assert "Kept as their own items, at the value given (1)" in html
r = ok(c.get(f"/items/{a1200w}")); html = r.data.decode()
assert "Gayle chip" in html and "Fitted Gayle chip (RC-00004, from RC-00003)" in html  # in work log
assert "£165.00" in html                                                              # 150 + 15 internal price
# the spare keyboard is fitted later into the A500 at £20: donor income rises to 75
kb = q_id = None
from app import app as _app0
from db import q as _q0
with _app0.app_context():
    kb = _q0("SELECT id FROM items WHERE name = 'Keyboard'", one=True)["id"]
ok(c.get(f"/items/{kb}/fit"))
post(f"/items/{kb}/fit", {"target": "RC-00001", "price": "20", "date": "2025-07-15"})
html = ok(c.get(f"/items/{a1200}")).data.decode()
assert "£75.00" in html and "Loss so far" in html and "£5.00" in html
# bad target error
r = post(f"/items/{a1200}/breakdown", {"row_existing_id": [""], "row_name": ["X"], "row_outcome": ["repair"],
        "row_value": [""], "row_location": [""], "row_target": ["nope"], "row_dest": [""], "row_desc": [""]})
assert b"find the item" in r.data

# sell the A500 whole
post(f"/items/{a500}/dispose", {"kind": "sold", "date": "2025-08-01", "price": "200", "destination": "Facebook", "with_contents": "1"})
r = ok(c.get(f"/items/{a501}")); assert b"With RC-00001" in r.data
r = ok(c.get(f"/items/{a500}")); assert b"Profit" in r.data and b"\xc2\xa360.00" in r.data  # 200 - (120 + 20 keyboard)

# search
r = ok(c.get("/items?q=commodore&status=all")); assert b"Amiga 500" in r.data and b"Amiga 1200 (dead)" in r.data
r = ok(c.get("/items?q=manufacturer:commodore+a500&status=all")); assert b"Amiga 500" in r.data and b"1200" not in r.data
r = ok(c.get("/items?q=keyboard&status=all")); assert b"Keyboard" in r.data
r = ok(c.get("/items?status=active&location=2")); assert b"A501" not in r.data
r = ok(c.get("/api/items?q=gay")); assert r.json[0]["ref"] == "RC-00004"

# scan lookup
r = ok(c.get("/lookup?code=rc-00005"), 302)
r = ok(c.get("/lookup?code=5012345678900"), 302)
r = ok(c.get("/lookup?code=zzz")); assert b"Create a new item" in r.data

# reports
r = ok(c.get("/reports")); html = r.data.decode()
assert "£200.00" in html and "£40.00" in html  # whole sale + part sale
r = ok(c.get("/reports?year=2025&group=1")); assert b"(not set)" in r.data
r = ok(c.get("/reports?from=2026-01-01")); assert r.status_code == 200

# labels / barcode
ok(c.get(f"/items/{a500}/label")); ok(c.get("/labels?status=all"))
r = ok(c.get("/barcode/RC-00001.svg")); assert r.mimetype == "image/svg+xml"

# photos
r = ok(c.get(f"/items/{a500}")); fn = re.search(rb"/media/thumb/([0-9a-f]+\.jpg)", r.data).group(1).decode()
ok(c.get(f"/media/thumb/{fn}")); ok(c.get(f"/media/photo/{fn}"))
assert Image.open(os.path.join(tmp, "photos", "thumbs", fn)).size[0] <= 480

# all pages render
for u in ["/", "/items", "/items/new", f"/items/{a500}/edit", f"/items/{a500}/dispose", "/locations",
          "/locations/1", "/locations/1/edit", "/locations/new", "/scan", "/settings", "/export/items.csv",
          "/backup/inventory.db", f"/items/new?copy={a500}", f"/items/new?parent_id={a500}"]:
    ok(c.get(u))

# settings
post("/settings", {"action": "add_descriptor", "name": "Chipset"})
post("/settings", {"action": "general", "site_name": "Retro Lab", "currency": "£", "ref_prefix": "AM", "ref_digits": "4", "ref_next": "50"})
r = post("/items/new", {"name": "Floppy drive"}); assert b"AM0050" in r.data
r = ok(c.get("/settings")); assert b"Chipset" in r.data and b"Retro Lab" in r.data

# delete location moves items up
post("/locations/3/delete", {})
r = ok(c.get(f"/items/{a1200w}")); assert b"Loft" in r.data
# delete item moves children up
post(f"/items/{a1200w}/delete", {})
r = ok(c.get(f"/items/{gayle}")); assert b"Loft" in r.data

# reinstate
post(f"/items/{a500}/reinstate", {})

# --- new item form: no status/value fields; status forced active ---------------
html = ok(c.get("/items/new")).data.decode()
assert 'name="value"' not in html and 'name="disposal_date"' not in html
r = post("/items/new", {"name": "Sneaky", "status": "sold", "disposal_price": "9"})
sneaky = item_id_from(r)
from app import app as _app
from db import q as _q
with _app.app_context():
    row = _q("SELECT status, disposal_price FROM items WHERE id = ?", (sneaky,), one=True)
    assert row["status"] == "active" and row["disposal_price"] is None

# --- breakdowns no longer create valuations -------------------------------------
with _app.app_context():
    assert _q("SELECT COUNT(*) AS n FROM valuations", one=True)["n"] == 0
# donor finished: shell sold for 10 adds to income; a spare used afterwards still counts
post(f"/items/{a1200}/dispose", {"kind": "sold", "price": "10", "date": "2025-09-01"})
html = ok(c.get(f"/items/{a1200}")).data.decode()
# (the repaired A1200 was deleted above, taking the Gayle's £15 fit entry with it)
assert "Shell sold for" in html and "so far" not in html and "£60.00" in html and "Loss" in html  # 40+20+10 - 80

# --- work log and valuations -----------------------------------------------------
r = post("/items/new", {"name": "Amiga 600", "purchase_price": "100", "purchase_date": "2025-01-01"})
a600 = item_id_from(r)
def val600():
    with _app.app_context():
        return dict(_q("SELECT value, work_cost FROM items WHERE id = ?", (a600,), one=True))
post(f"/items/{a600}/work", {"date": "2025-02-01", "description": "Recapped", "cost": "20"})
post(f"/items/{a600}/work", {"date": "2025-03-01", "description": "Cleaned keyboard", "cost": ""})
assert val600() == {"value": None, "work_cost": 20}
html = ok(c.get(f"/items/{a600}")).data.decode()
assert "Recapped" in html and "Total spent" in html and "£120.00" in html and "none recorded" in html
post(f"/items/{a600}/valuations", {"date": "2025-04-01", "value": "150", "source": "eBay sold"})
post(f"/items/{a600}/valuations", {"date": "2025-03-15", "value": "130", "source": "older"})
assert val600()["value"] == 150                               # latest by date wins
post(f"/items/{a600}/work", {"date": "2025-05-01", "description": "New RF shield", "cost": "12.50"})
assert val600() == {"value": 150, "work_cost": 32.5}         # work never changes the valuation
with _app.app_context():
    wid = _q("SELECT id FROM work_log WHERE description = 'New RF shield'", one=True)["id"]
    vid = _q("SELECT id FROM valuations WHERE value = 150", one=True)["id"]
post(f"/work/{wid}/edit", {"date": "2025-05-01", "description": "New RF shield", "cost": "5"})
assert val600() == {"value": 150, "work_cost": 25}
post(f"/valuations/{vid}/delete", {})
assert val600()["value"] == 130
post(f"/work/{wid}/delete", {})
assert val600() == {"value": 130, "work_cost": 20}
post(f"/items/{a600}/work", {"date": "2025-05-02", "description": "Fan", "cost": "5"})
html = ok(c.get(f"/items/{a600}")).data.decode()
assert "Work log" in html and "Valuations" in html and "£125.00" in html
# items list shows spend; valuations stay on the item page and in reports
html = ok(c.get("/items?q=Amiga+600")).data.decode()
assert "£125.00 spent" in html and "valued at" not in html and "£130.00" not in html
# profit = sale - (purchase + work), not affected by valuation
post(f"/items/{a600}/dispose", {"kind": "sold", "price": "200", "date": "2025-06-01"})
html = ok(c.get(f"/items/{a600}")).data.decode()
assert "Profit" in html and "£75.00" in html                  # 200 - (100 + 25)
ok(c.get("/reports")); ok(c.get("/reports?year=2025")); ok(c.get("/")); ok(c.get("/locations")); ok(c.get("/items"))

# --- migration of an old database with plain values --------------------------------
import sqlite3, db as _db
old = os.path.join(tempfile.mkdtemp(), "old.db")
conn = sqlite3.connect(old)
conn.executescript(_db.SCHEMA.split("-- Valuation history")[0].replace("    work_cost            REAL NOT NULL DEFAULT 0,\n", ""))
conn.execute("INSERT INTO items(ref, name, value, updated_at) VALUES ('X-1', 'Old', 42, '2024-05-06 10:00:00')")
conn.commit(); conn.close()
_db.init_db(old); _db.init_db(old)                             # twice: must be idempotent
conn = sqlite3.connect(old)
assert conn.execute("SELECT date, value FROM valuations").fetchall() == [("2024-05-06", 42.0)]
assert conn.execute("SELECT value, work_cost FROM items").fetchone() == (42.0, 0)
conn.close()

print("CSV:\n" + c.get("/export/items.csv").data.decode()[:600])
# a location deleted while a form was open gives a message, not a server error
gone = int(re.search(r"/locations/(\d+)", post("/locations/new", {"name": "Doomed"}).request.path).group(1))
post(f"/locations/{gone}/delete", {})
for url, data in [("/items/new", {"name": "Orphan", "location_id": str(gone)}),
                  ("/locations/new", {"name": "Orphan loc", "parent_id": str(gone)})]:
    r = ok(c.post(url, data=data))
    assert b"no longer exists" in r.data, url

print("ALL OK", tmp)
