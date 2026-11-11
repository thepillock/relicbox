# RelicBox

A self-hosted inventory for collections, especially collections where things get bought, repaired and broken down for parts. It was built for retro computers but works for any collection.

Python/Flask + SQLite, one container, no external services. Everything (database + photos) lives in `/data`.

## Features

- **Nested locations** with descriptions (House › Loft › Shelf 3 › Box A).
- **Nested items**: a chip inside a card inside a computer inside a box. An item inside another item takes its location automatically, and lists show every container ("In Zorro card, Amiga 3000 – House › Office"). Only items still in the collection can be chosen as a container.
- **Reference numbers**: auto-generated with a configurable prefix (`RC-00001`) or typed by hand. Each item can also store a manufacturer barcode.
- **Barcode labels**: Code 128 labels for single items or a whole search result (62×29, 50×25, 89×36 mm or A4 3×8).
- **Scanning**: the Scan page uses the phone camera to read item labels or manufacturer barcodes and jumps to the item. Unknown codes offer "create item with this barcode".
- **Photos**: multiple per item, auto-rotated, resized and thumbnailed, with a choice of main photo.
- **User-defined descriptors** (Manufacturer, Model, Serial number, Version…). Set them up in Settings.
- **Purchase info**: date, price, seller and platform (autocompletes from earlier entries).
- Work log, valuations and parts taken off are tables on the item page. They're hidden when empty, and you add or edit rows in place.
- **Work log**: a dated entry for each repair or job, with what was done and what it cost. The item page shows the spend: purchase + work = total spent, and the sale price and profit once sold.
- **Valuations**: a separate, dated record of what each item is worth (e.g. for insurance), each with a source. They don't affect spend or profit; lists and reports show them as their own total.
- **Sold / given away / scrapped**: date, price and destination. Optionally the contents go with the item. The item leaves its location and is listed under **Sold** on the Locations page. Sold isn't a real location, so only recording a sale puts things there. **Back in collection** returns it to where it was.
- **Taking parts off**: parts can come off any item: a working one (**Take a part off**) or one marked **Breaking for spares**, which leaves the collection. List the parts as you take them off. Each part becomes its own item (with a reference number and a link back to the donor) and has one of these outcomes:
  - *Create new item or subitem*: becomes its own item, in a location or inside another item. An optional value moves that much cost from the item it came off onto the new item, so if you sell it later the profit or loss is the part's own.
  - *Add to work log of another item*: gets nested inside that item and logged in its work log, with an optional internal price. That price moves from the item it came off into the other item's work cost.
  - *Sold / charged to someone*: records the price and who to.
  - *Given away / scrapped*.
  
  Parts already recorded inside the donor can be processed the same way. Later parts are added straight into the **Parts taken off** table on the donor's page. Each row can be edited (date, amount, who it went to). **Remove** deletes a part that taking it off created; a part that was already recorded is **Put back** inside instead. A spare on the shelf has **Fit into another item** and **Sold / given away** buttons, and its income still goes to the donor even after the donor is finished. When nothing more will come off the donor, use **Finished: dispose of shell** (selling the shell adds to its income).
- **Search**: keywords across name, ref, barcode, descriptors, notes, seller etc. Use `descriptor:value` to filter on one descriptor, e.g. `manufacturer:commodore model:a1200`.
- **Reports**:
  - Collection: item count, spend and valuations, per location (sub-locations included) and by any descriptor.
  - Activity, by year or date range: purchases, work spend, sales income (whole items vs parts), profit on whole items, given away, and a table per donor (cost, income from parts, net).
- CSV export and a one-click database backup.
- Optional single password (`APP_PASSWORD`).
- Mobile-friendly with dark mode.

## Run it

```bash
docker compose up -d --build
# then open http://<server>:3200
```

Or without compose:

```bash
docker build -t relicbox .
docker run -d --name relicbox -p 3200:3200 -v /path/to/data:/data relicbox
```

### Unraid

Images are built by GitHub Actions and published to `ghcr.io/thepillock/relicbox`. There are two tracks:

| Track | Image tag | Updated when |
|---|---|---|
| Stable | `:latest` (also `:1.2.3`, `:1.2`) | a release tag like `v1.2.3` is pushed |
| Test | `:test` | anything is pushed to `main` |

Test builds show a red **TEST** badge in the top bar. The running version is shown at the bottom of Settings.

1. Copy `unraid/relicbox.xml` and `unraid/relicbox-test.xml` to `/boot/config/plugins/dockerMan/templates-user/` on the server.
2. Docker tab → **Add Container** → pick `relicbox` from the template list → Apply. Do the same for `relicbox-test`.
   - Stable uses port 3200 and `/mnt/user/appdata/relicbox`. Test uses port 3201 and `/mnt/user/appdata/relicbox-test`. **Never point the test container at the stable data folder**: a test build may change the database.
3. Unraid now shows **update available** when a new image is pushed to either tag.

To release: `git tag v1.2.3 && git push origin v1.2.3`. To roll back stable, edit the container and change `:latest` to an earlier version such as `:1.2.2`.

To build locally instead: `docker build -t relicbox .` and use repository `relicbox`.

### Camera scanning needs HTTPS

Browsers only allow camera access on `https://` (or `localhost`). Put RelicBox behind your reverse proxy (e.g. Nginx Proxy Manager) with a certificate, and scanning will work on your phone. Over plain HTTP the scan page still accepts typed codes.

Chrome on Android uses the built-in barcode detector. iPhone Safari and Firefox use ZXing, which is downloaded while the image builds.

## Moving from Homebox

1. In Homebox go to **Tools → Export inventory**. Don't open the CSV in Excel first, because it turns long serial numbers into `1.03E+20`.
2. In RelicBox go to **Settings → Import from Homebox** and upload the file.
   - Locations, nested items, purchase and sale details, manufacturer, model, serial number and custom fields all carry over.
   - Homebox asset IDs (`000-138`) become reference numbers, and new items carry on the same numbering.
   - Locations you used as a status ("Sold", "Breaking for Parts") become item statuses instead of places.
   - Photos aren't in Homebox's export, so they need adding again.
   - It's safe to run the import again: items that were already imported are skipped.
3. The import summary lists anything to tidy up. That includes items to re-record with **Breaking for spares**, and values a spreadsheet has damaged.

**Existing Homebox QR labels keep working.** They encode `http://<homebox>/item/<id>`, and RelicBox stores each item's and location's Homebox ID:
- On RelicBox's **Scan** page, an old label opens the matching item or location.
- Once Homebox is switched off, you can map RelicBox to Homebox's old port too (see the commented line in `docker-compose.yml`). Then old labels open RelicBox straight from the phone's normal camera app.

## Labels

Labels are QR codes by default. They encode `<your address>/a/<ref>`, so any phone camera opens the item directly. Set **Settings → Address used in QR labels** to the address you use on your phone (e.g. your HTTPS reverse-proxy name) *before* printing. You can switch to Code 128 barcodes in Settings.

### Bluetooth label printers (NIIMBOT)

The label page can print straight to a NIIMBOT printer (e.g. D110) over Bluetooth, using [NiimBlueLib](https://github.com/MultiMote/niimbluelib) (MIT), bundled at build time. Set it up once in **Settings → Bluetooth label printer**: label size (22×12, 30×12, 40×12 or 30×15 mm), darkness, what the QR holds (the link, which opens from any phone camera, or just the reference, which is smaller but only works with RelicBox's Scan page), and whether to show the item name. Then on any label page tap **Connect printer**, choose it in the list, and **Print**. A search result prints as one job. The QR sits on the left, clear of the label's rounded corners, with the name and the reference (split as `000-` / `094`) on the right.

QR links are written in capitals (`HTTPS://HOST/A/000-094`) when the address allows. Web addresses ignore case, and QR codes store capitals much more compactly, so the code is a size smaller.

This needs Chrome on Android (or Chrome/Edge on a computer) and an `https://` address. iPhone browsers don't support Web Bluetooth.

## Configuration

| Env var | Default | |
|---|---|---|
| `APP_PASSWORD` | *(none)* | If set, a login page protects the whole app |
| `SECRET_KEY` | auto, stored in `/data/secret_key` | Session signing |
| `MAX_UPLOAD_MB` | 64 | Max upload size per request |
| `PORT` | 3200 | |

Reference prefix/digits, currency symbol, site name and descriptors are set in **Settings**.

## How values are counted

Spend and valuations are two separate running totals:

- **Spent** = purchase price + work log costs − cost moved to parts taken off it. Profit on a sale is the sale price minus this.
- **Valued at** = the latest valuation of each item that has one. Items without a valuation add nothing. Parts listed inside an item are valued separately, so value a computer without the parts you've listed inside it.
- **Moving cost to parts**: when a part comes off an item (working or being broken for spares) and you keep it as its own item with a value, or add it to another item's work log at an internal price, that amount moves from the item to the part. A £100 item with a £25 part taken off shows £75 spent, and the part £25. The purchase price stays at what you actually paid.
- **Income** only ever means real money: parts sold, and the item's own sale price.
- **Donor net** = income − spent. It shows as "so far" until the donor is finished. The totals always match the real money in and out.
- A part with no value given has no cost of its own; if it's sold, the money counts for the item it came off.
- New parts copy the donor's purchase date and get a "Salvaged from …" note. They aren't counted as purchases in reports.

## Backups

Back up the whole `/data` volume. Settings → *Download database backup* gives a consistent snapshot of the database. Photos are in `/data/photos`.

## Development

```bash
pip install -r requirements.txt
cd app && DATA_DIR=./data python app.py   # http://localhost:3200
```
