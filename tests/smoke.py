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
r = post("/items/new", {"name": "A501 RAM expansion", "parent": "RC-00001", "value": "25"})
a501 = item_id_from(r); assert b"Amiga 500" in r.data
r = post("/items/new", {"name": "Amiga 1200 (dead)", "location_id": "4", "purchase_price": "80",
                         "purchase_date": "2025-06-01", "desc_1": "Commodore"})
a1200 = item_id_from(r)
r = post("/items/new", {"name": "Gayle chip", "parent": f"RC-00003 — Amiga 1200 (dead)", "value": "15"})
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
    "row_value": ["15", "40", "30", "", ""],
    "row_location": ["", "", "2", "", ""],
    "row_target": ["RC-00005 — Amiga 1200 (working)", "", "", "", ""],
    "row_dest": ["", "eBay buyer", "", "", ""],
    "row_desc": ["", "", "", "cracked", ""],
})
r = ok(c.get(f"/items/{a1200}"))
html = r.data.decode()
assert "Broken for spares" in html and "Used to repair another item" in html and "Sold separately" in html
assert "£85.00" in html, "recovered should be 15+40+30"
# net = 85 + 5 - 80 = 10
assert "£10.00" in html
r = ok(c.get(f"/items/{a1200w}")); assert b"Gayle chip" in r.data  # now inside repaired machine
# bad target error
r = post(f"/items/{a1200}/breakdown", {"row_existing_id": [""], "row_name": ["X"], "row_outcome": ["repair"],
        "row_value": [""], "row_location": [""], "row_target": ["nope"], "row_dest": [""], "row_desc": [""]})
assert b"find the item it repaired" in r.data

# sell the A500 whole
post(f"/items/{a500}/dispose", {"kind": "sold", "date": "2025-08-01", "price": "200", "destination": "Facebook", "with_contents": "1"})
r = ok(c.get(f"/items/{a501}")); assert b"With RC-00001" in r.data
r = ok(c.get(f"/items/{a500}")); assert b"profit" in r.data and b"\xc2\xa380.00" in r.data

# search
r = ok(c.get("/items?q=commodore&status=all")); assert b"Amiga 500" in r.data and b"Amiga 1200 (dead)" in r.data
r = ok(c.get("/items?q=manufacturer:commodore+a500&status=all")); assert b"Amiga 500" in r.data and b"1200" not in r.data
r = ok(c.get("/items?q=keyboard&status=active")); assert b"Keyboard" in r.data
r = ok(c.get("/items?status=active&location=2")); assert b"Keyboard" in r.data and b"A501" not in r.data  # A501 went with sold A500? still inside
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
print("CSV:\n" + c.get("/export/items.csv").data.decode()[:600])
print("ALL OK", tmp)
