"""Sold area, return to collection, and inline work/valuation/part rows."""
import os, re, sqlite3, sys, tempfile
tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = tmp
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from app import app  # noqa
import db as dbm  # noqa

c = app.test_client()
DB = os.path.join(tmp, "inventory.db")
def row(i):
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM items WHERE id = ?", (i,)).fetchone(); con.close(); return r
def post(url, data=None):
    r = c.post(url, data=data or {}, follow_redirects=True); assert r.status_code == 200, (url, r.status_code); return r
def new_item(**data):
    r = post("/items/new", data); return int(re.search(r"/items/(\d+)", r.request.path).group(1))

shelf = int(re.search(r"/locations/(\d+)", post("/locations/new", {"name": "Shelf"}).request.path).group(1))
box = int(re.search(r"/locations/(\d+)", post("/locations/new", {"name": "Box"}).request.path).group(1))

# 1. selling moves an item out of its location into "Sold"; returning puts it back
a500 = new_item(name="Amiga 500", location_id=shelf, purchase_price="100")
post(f"/items/{a500}/dispose", {"kind": "sold", "price": "150", "date": "2026-11-01", "destination": "Bob"})
r = row(a500); assert r["location_id"] is None and r["former_location_id"] == shelf
assert b"Amiga 500" not in c.get(f"/locations/{shelf}?status=all").data
page = c.get("/locations").data.decode(); assert "Sold" in page and "£150.00" in page
assert b"Amiga 500" in c.get("/items?status=gone").data
assert b"Was in" in c.get(f"/items/{a500}").data
# editing a sold item can't put it in a location
post(f"/items/{a500}/edit", {"name": "Amiga 500", "status": "sold", "location_id": box, "disposal_price": "150"})
assert row(a500)["location_id"] is None
post(f"/items/{a500}/reinstate")
r = row(a500); assert r["status"] == "active" and r["location_id"] == shelf and r["former_location_id"] is None

# 2. a part sold out of a machine leaves it, and returns into it
host = new_item(name="A4000", location_id=shelf)
card = new_item(name="SCSI card", parent=f"{row(host)['ref']}")
post(f"/items/{card}/dispose", {"kind": "sold", "price": "30"})
assert row(card)["parent_id"] is None and row(card)["former_parent_id"] == host
post(f"/items/{card}/reinstate"); assert row(card)["parent_id"] == host

# 3. contents that went with it stay inside; contents that didn't stay in the collection where it was
post(f"/items/{host}/dispose", {"kind": "sold", "price": "500", "with_contents": "1"})
assert row(card)["status"] == "sold" and row(card)["parent_id"] == host
post(f"/items/{host}/reinstate"); assert row(card)["status"] == "active" and row(host)["location_id"] == shelf
post(f"/items/{host}/dispose", {"kind": "sold", "price": "500"})
assert row(card)["status"] == "active" and row(card)["parent_id"] is None and row(card)["location_id"] == shelf

# 4. existing databases: sold items with a location are moved out at startup
con = sqlite3.connect(DB); con.execute("UPDATE items SET location_id = ?, former_location_id = NULL WHERE id = ?", (box, host)); con.commit(); con.close()
dbm.init_db(DB)
assert row(host)["location_id"] is None and row(host)["former_location_id"] == box

# 5. work log & valuations: empty tables hidden, inline add/edit
item = new_item(name="Amiga 1200", location_id=shelf)
html = c.get(f"/items/{item}").data.decode()
assert "+ Add work" in html and "+ Add valuation" in html
assert re.search(r'<div class="tablewrap" hidden>\s*<table class="data ledger">', html)
post(f"/items/{item}/work", {"date": "2026-11-02", "description": "Recapped", "cost": "12.50"})
post(f"/items/{item}/valuations", {"date": "2026-11-02", "value": "250", "source": "eBay"})
con = sqlite3.connect(DB); vid = con.execute("SELECT id FROM valuations WHERE item_id = ?", (item,)).fetchone()[0]; con.close()
post(f"/valuations/{vid}/edit", {"date": "2026-11-03", "value": "275", "source": "Dealer"})
html = c.get(f"/items/{item}").data.decode()
assert "Recapped" in html and "£275.00" in html and "Dealer" in html and row(item)["value"] == 275

# 6. donor: no work/valuation sections when empty; parts added inline
donor = new_item(name="Dead CD32", location_id=box, purchase_price="60")
inside = new_item(name="Akiko chip", parent=row(donor)["ref"])
post(f"/items/{donor}/breakdown", {"date": "2026-11-04", "mark_broken": "1"})
html = c.get(f"/items/{donor}").data.decode()
assert "+ Add work" not in html and "+ Add valuation" not in html and "+ Add part" in html
assert "Record more parts" not in html
post(f"/items/{donor}/parts", {"date": "2026-11-05", "part": "PSU", "outcome": "sold", "value": "20", "dest": "Dave"})
post(f"/items/{donor}/parts", {"part": f"{row(inside)['ref']} — Akiko chip", "outcome": "spare", "location": shelf})
assert row(inside)["harvested_from_id"] == donor and row(inside)["location_id"] == shelf and row(inside)["parent_id"] is None
post(f"/items/{donor}/parts", {"part": "Floppy drive", "outcome": "repair", "value": "15", "target": row(item)["ref"]})
r = post(f"/items/{donor}/parts", {"part": "Lid", "outcome": "repair", "target": "nonsense"})
assert b"Couldn" in r.data
html = c.get(f"/items/{donor}").data.decode()
assert "PSU" in html and "Floppy drive" in html and "£35.00 income" in html
con = sqlite3.connect(DB); sold_psu = con.execute("SELECT location_id, former_location_id FROM items WHERE name = 'PSU'").fetchone(); con.close()
assert sold_psu == (None, box), sold_psu
assert b"Fitted Floppy drive" in c.get(f"/items/{item}").data
r = post(f"/items/{item}/parts", {"part": "X", "outcome": "spare"}); assert b"only be added while" in r.data
print("ALL OK")

# 7. location page: contents drop down under their container, any depth; no valuations in lists
loc = int(re.search(r"/locations/(\d+)", post("/locations/new", {"name": "Rack"}).request.path).group(1))
pc = new_item(name="Big Box PC", location_id=loc)
card2 = new_item(name="Zorro card", parent=row(pc)["ref"])
chip = new_item(name="CPU chip", parent=row(card2)["ref"])
lone = new_item(name="Lonely mouse", location_id=loc)
post(f"/items/{pc}/valuations", {"value": "999", "date": "2026-11-07"})
html = c.get(f"/locations/{loc}").data.decode()
assert "Show all subitems" in html and html.count('class="toggle"') == 2   # PC and card, not the chip or mouse
assert html.index("Big Box PC") < html.index("Zorro card") < html.index("CPU chip") < html.index("Lonely mouse") or \
       html.index("Big Box PC") < html.index("Zorro card") < html.index("CPU chip")
assert re.search(r'<ul class="cards tree sub" hidden>', html)
for page in (f"/locations/{loc}", "/locations", "/items", "/"):
    assert "£999.00" not in c.get(page).data.decode(), page
assert "£999.00" in c.get(f"/items/{pc}").data.decode()
print("ALL OK (tree)")
