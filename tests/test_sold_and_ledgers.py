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

def spend(i): r = row(i); return (r["purchase_price"] or 0) + (r["work_cost"] or 0) - (r["moved_out"] or 0)

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
assert "PSU" in html and "Floppy drive" in html and "£15.00 moved" in html and "£20.00 sold" in html
con = sqlite3.connect(DB); sold_psu = con.execute("SELECT location_id, former_location_id FROM items WHERE name = 'PSU'").fetchone(); con.close()
assert sold_psu == (None, box), sold_psu
assert b"Fitted Floppy drive" in c.get(f"/items/{item}").data
# parts can come off a working item without breaking it: its spend drops by the value moved
before = spend(item)
post(f"/items/{item}/parts", {"part": "Spare mouse", "outcome": "spare", "value": "10", "location": shelf})
assert row(item)["status"] == "active" and abs(spend(item) - (before - 10)) < 0.001
r = post(f"/items/{a500}/dispose", {"kind": "sold", "price": "1"})
r = post(f"/items/{a500}/parts", {"part": "X", "outcome": "spare"}); assert b"only be taken off items still" in r.data
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

# 8. parts kept as their own items: value, date and notes; work-log option; your monitor example
mon_a = new_item(name="Monitor A", location_id=shelf, purchase_price="75")
mon_b = new_item(name="Monitor B", location_id=shelf, purchase_price="39.50", purchase_date="2023-09-29")
post(f"/items/{mon_b}/breakdown", {"date": "2026-11-07", "mark_broken": "1"})
r = post(f"/items/{mon_b}/parts", {"date": "2026-11-07", "part": "Tilt/swivel stand", "outcome": "spare",
                                    "value": "15", "target": row(mon_a)["ref"]})
con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
stand = con.execute("SELECT * FROM items WHERE name = 'Tilt/swivel stand'").fetchone(); con.close()
assert stand["parent_id"] == mon_a and stand["purchase_price"] == 15 and stand["salvage_value"] == 15
assert stand["purchase_date"] == "2023-09-29"                       # copied from the donor
assert stand["notes"].startswith(f"Salvaged from {row(mon_b)['ref']} Monitor B on 2026-11-07")
post(f"/items/{mon_b}/dispose", {"kind": "sold", "price": "30"})
html = c.get(f"/items/{mon_b}").data.decode()
assert "Profit" in html and "£5.50" in html                         # 15 + 30 - 39.50
assert "Kept as its own item" in html and "Monitor A" in html
# whole picture = real cash: 114.50 out, 30 back
net = (15 + 30 - 39.50) - spend(mon_a) - spend(stand["id"])
assert abs(net - (30 - 114.50)) < 0.001, net
# selling the stand later is the stand's own profit; the donor keeps its 15
post(f"/items/{stand['id']}/dispose", {"kind": "sold", "price": "22"})
assert "£5.50" in c.get(f"/items/{mon_b}").data.decode()
assert b"Profit" in c.get(f"/items/{stand['id']}").data and b"\xc2\xa37.00" in c.get(f"/items/{stand['id']}").data
# "add to work log of another item": date mirrored onto the log, cost on that item
d2 = new_item(name="Donor 2", location_id=shelf, purchase_price="10", purchase_date="2024-01-01")
post(f"/items/{d2}/breakdown", {"date": "2026-11-07", "mark_broken": "1"})
post(f"/items/{d2}/parts", {"date": "2026-10-01", "part": "Fuse", "outcome": "repair", "value": "2", "target": row(mon_a)["ref"]})
con = sqlite3.connect(DB)
assert con.execute("SELECT date, cost FROM work_log WHERE description LIKE 'Fitted Fuse%'").fetchone() == ("2026-10-01", 2.0)
fuse = con.execute("SELECT purchase_date, purchase_price FROM items WHERE name = 'Fuse'").fetchone(); con.close()
assert fuse == ("2024-01-01", None)                                # date copied, cost lives in the work log
# harvested parts aren't counted as purchases in reports
assert "Harvested from item" not in c.get(f"/items/{stand['id']}/edit").data.decode()
post(f"/items/{stand['id']}/edit", {"name": "Tilt/swivel stand", "status": "sold", "disposal_price": "22"})
assert row(stand["id"])["harvested_from_id"] == mon_b               # link kept after editing
print("ALL OK (parts)")

# 9. picker and containers: sold / broken items can't be chosen
gone_box = new_item(name="Sold Big Box", location_id=shelf)
post(f"/items/{gone_box}/dispose", {"kind": "sold", "price": "5"})
assert not [x for x in c.get("/api/items?pick=1&q=Big+Box").json if x["id"] == gone_box]
assert [x for x in c.get("/api/items?q=Big+Box").json if x["id"] == gone_box]     # normal search still finds it
loose = new_item(name="Loose card", location_id=shelf)
r = post(f"/items/{loose}/edit", {"name": "Loose card", "parent": row(gone_box)["ref"]})
assert b"Choose an item that" in r.data and row(loose)["parent_id"] is None
r = post("/items/new", {"name": "Into a donor", "parent": row(mon_b)["ref"]})
assert b"Choose an item that" in r.data

# 10. listings show every container: "In <item>, <parent> – <location>"
outer = new_item(name="Outer machine", location_id=box)
mid = new_item(name="Middle card", parent=row(outer)["ref"])
inner = new_item(name="Inner chip", parent=row(mid)["ref"])
html = c.get("/items?q=Inner+chip").data.decode()
assert re.search(r"In</span> <a[^>]*>Middle card</a>, <a[^>]*>Outer machine</a> <span class=\"muted\">–</span> <a[^>]*>Box</a>", html), html[html.find("Inner chip"):][:600]

# 11. parts taken off can be edited and removed
d3 = new_item(name="Donor 3", location_id=shelf, purchase_price="100")
kept = new_item(name="Recorded RAM", parent=row(d3)["ref"])          # already recorded inside
post(f"/items/{d3}/breakdown", {"date": "2026-11-01", "mark_broken": "1"})
post(f"/items/{d3}/parts", {"date": "2026-11-02", "part": "Keyboard", "outcome": "spare", "value": "20"})
post(f"/items/{d3}/parts", {"date": "2026-11-02", "part": "Drive", "outcome": "sold", "value": "30", "dest": "Al"})
post(f"/items/{d3}/parts", {"date": "2026-11-02", "part": "Fan", "outcome": "repair", "value": "5", "target": row(item)["ref"]})
post(f"/items/{d3}/parts", {"date": "2026-11-02", "part": f"{row(kept)['ref']} — Recorded RAM", "outcome": "spare", "value": "12"})
con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
ids = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM items WHERE harvested_from_id = ?", (d3,))}; con.close()
assert spend(d3) == 100 - 20 - 5 - 12
post(f"/parts/{ids['Keyboard']}/edit", {"date": "2026-10-30", "value": "25"})
k = row(ids["Keyboard"]); assert k["taken_off_date"] == "2026-10-30" and k["salvage_value"] == 25 and k["purchase_price"] == 25
post(f"/parts/{ids['Drive']}/edit", {"date": "2026-10-31", "value": "35", "dest": "Bob"})
d = row(ids["Drive"]); assert (d["disposal_price"], d["disposal_date"], d["disposal_destination"]) == (35, "2026-10-31", "Bob")
before_item_work = row(item)["work_cost"]
post(f"/parts/{ids['Fan']}/edit", {"date": "2026-10-29", "value": "7"})
assert row(item)["work_cost"] == before_item_work + 2
assert spend(d3) == 100 - 25 - 7 - 12
# remove: a part created by taking it off is deleted, and its work-log entry goes too
post(f"/parts/{ids['Fan']}/remove")
assert row(ids["Fan"]) is None and row(item)["work_cost"] == before_item_work - 5
# remove: a part that was already recorded goes back inside, with its moved cost undone
post(f"/parts/{ids['Recorded RAM']}/remove")
k = row(kept); assert k["parent_id"] == d3 and k["harvested_from_id"] is None and k["purchase_price"] is None
assert spend(d3) == 100 - 25
html = c.get(f"/items/{d3}").data.decode()
assert "Put back" not in html and "Remove" in html and "qr-preview" not in html and "item-layout" in html
print("ALL OK (edit parts, pickers, chain)")

# 12. compact QR links, label printer settings saved once
post("/settings", {"action": "general", "site_name": "RelicBox", "currency": "£", "ref_prefix": "RC-", "ref_digits": "5",
                   "ref_next": "900", "label_format": "qr", "public_url": "https://relicbox.0x37.net",
                   "nb_size": "30x12", "nb_density": "3", "nb_qr": "ref", "nb_name": "0"})
html = c.get(f"/items/{item}/label").data.decode()
assert 'data-size="30x12"' in html and 'data-density="3"' in html and 'data-name="0"' in html
assert f'data-qr="{row(item)["ref"]}"' in html                       # reference only
assert f"HTTPS://RELICBOX.0X37.NET/A/{row(item)['ref']}" in html      # paper label still links, in capitals
r = c.get(f"/A/{row(item)['ref']}"); assert r.status_code == 302 and r.headers["Location"].endswith(f"/items/{item}")
r = c.get("/lookup", query_string={"code": f"HTTPS://RELICBOX.0X37.NET/A/{row(item)['ref']}"})
assert r.headers["Location"].endswith(f"/items/{item}")
import qr as qrmod
assert len(qrmod.matrix(f"HTTPS://RELICBOX.0X37.NET/A/{row(item)['ref']}")) == 25   # one size smaller than lowercase
print("ALL OK (labels)")
