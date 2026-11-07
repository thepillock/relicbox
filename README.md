# RelicBox

A self-hosted inventory for collections, especially collections where things get bought, repaired and broken down for parts. It was built for retro computers but works for any collection.

Python/Flask + SQLite, one container, no external services. Everything (database + photos) lives in `/data`.

## Features

- **Nested locations** with descriptions (House › Loft › Shelf 3 › Box A).
- **Nested items**: a chip inside a card inside a computer inside a box. An item inside another item takes its location automatically.
- **Reference numbers**: auto-generated with a configurable prefix (`RC-00001`) or typed by hand. Each item can also store a manufacturer barcode.
- **Barcode labels**: Code 128 labels for single items or a whole search result (62×29, 50×25, 89×36 mm or A4 3×8).
- **Scanning**: the Scan page uses the phone camera to read item labels or manufacturer barcodes and jumps to the item. Unknown codes offer "create item with this barcode".
- **Photos**: multiple per item, auto-rotated, resized and thumbnailed, with a choice of main photo.
- **User-defined descriptors** (Manufacturer, Model, Serial number, Version…). Set them up in Settings.
- **Purchase info**: date, price, seller and platform (autocompletes from earlier entries).
- **Sold / given away / scrapped**: date, price and destination. Optionally the contents go with the item.
- **Break for spares**: mark a donor item as broken down and list the parts removed. Each part becomes its own item (with a reference number and a link back to the donor) and has one of these outcomes:
  - *Keep as spare*: goes to a location with an attached value.
  - *Used to repair another item*: gets nested inside the repaired item, so its history shows where it came from.
  - *Sold separately*: records the sale price and buyer.
  - *Given away / scrapped*.
  
  Parts already recorded inside the donor can be processed the same way. You can add more parts later. Outcomes are worked out from where each part is *now*, so if a spare is later fitted or sold, the donor's figures update automatically.
- **Search**: keywords across name, ref, barcode, descriptors, notes, seller etc. Use `descriptor:value` to filter on one descriptor, e.g. `manufacturer:commodore model:a1200`.
- **Reports**:
  - Collection: item count, current value, cost, value per location (sub-locations included) and by any descriptor.
  - Activity, by year or date range: purchases, sales income (whole items vs parts), profit on whole items, given away, and a table per broken-down item (cost, parts sold, value used in repairs, spares kept, shell value, net).
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

1. Copy this folder to the server (e.g. `/mnt/user/appdata/relicbox-src`) and run `docker build -t relicbox .` from it.
2. Add a container in the Docker tab: repository `relicbox`, port 3200 → 3200, path `/data` → `/mnt/user/appdata/relicbox`.

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
3. The import summary lists anything to tidy up. That includes items to re-record with **Break for spares**, and values a spreadsheet has damaged.

**Existing Homebox QR labels keep working.** They encode `http://<homebox>/item/<id>`, and RelicBox stores each item's and location's Homebox ID:
- On RelicBox's **Scan** page, an old label opens the matching item or location.
- Once Homebox is switched off, you can map RelicBox to Homebox's old port too (see the commented line in `docker-compose.yml`). Then old labels open RelicBox straight from the phone's normal camera app.

## Labels

Labels are QR codes by default. They encode `<your address>/a/<ref>`, so any phone camera opens the item directly. Set **Settings → Address used in QR labels** to the address you use on your phone (e.g. your HTTPS reverse-proxy name) *before* printing. You can switch to Code 128 barcodes in Settings.

## Configuration

| Env var | Default | |
|---|---|---|
| `APP_PASSWORD` | *(none)* | If set, a login page protects the whole app |
| `SECRET_KEY` | auto, stored in `/data/secret_key` | Session signing |
| `MAX_UPLOAD_MB` | 64 | Max upload size per request |
| `PORT` | 3200 | |

Reference prefix/digits, currency symbol, site name and descriptors are set in **Settings**.

## How values are counted

- **Collection value** = each in-collection item's *current value*, or its purchase price if no value is set. Parts listed inside an item are counted separately, so give a computer a value that excludes the parts you've listed inside it.
- **Recovered from a breakdown** = parts sold (sale price) + parts used in repairs (attached value) + parts kept as spares (attached value).
- **Net for a breakdown** = recovered + remaining shell value (or what the shell sold for) − what the donor cost.

## Backups

Back up the whole `/data` volume. Settings → *Download database backup* gives a consistent snapshot of the database. Photos are in `/data/photos`.

## Development

```bash
pip install -r requirements.txt
cd app && DATA_DIR=./data python app.py   # http://localhost:3200
```
