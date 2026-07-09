# Dewmix Hardware Website — Source Code

## File Structure

```
dewmix_source/
├── index.html      ← MAIN SITE — edit this in your code editor (4 MB, ~2,900 lines)
├── products.html   ← Quote-link landing page opened from WhatsApp (139 KB)
├── images.js       ← Category product images, 1,160 products (~25 MB, don't edit by hand)
├── wadfow.js       ← Featured-carousel images, 62 Wadfow products (~2.5 MB)
├── logos.js        ← Brand partner logos for footer marquee (~500 KB)
├── imgs/           ← Same 1,160 product photos as real .jpg files (~8.6 MB total)
└── README.md       ← This file
```

## How To Use

### Open in a code editor
Open the `dewmix_source/` folder in VS Code (or any editor) and edit `index.html` directly.
It loads `images.js`, `wadfow.js`, and `logos.js` at runtime via `<script>` tags.

### Run locally
Browsers block loading local `.js` files over `file://`, so use a local server:

**VS Code — Live Server extension:**
Right-click `index.html` → "Open with Live Server"

**Python:**
```bash
cd dewmix_source
python3 -m http.server 8080
# open http://localhost:8080
```

**Node.js:**
```bash
cd dewmix_source
npx serve .
```

### Deploy to Hostinger (or any host)
Upload these files to the same folder on your server:
- `index.html`
- `products.html`
- `images.js`
- `wadfow.js`
- `logos.js`
- the `imgs/` folder (all 1,160 `.jpg` files)

All files must sit in the same directory — the relative paths assume that.

## What Each File Does

| File | Purpose | Edit? |
|---|---|---|
| `index.html` | All HTML structure, CSS, and site JavaScript | Yes |
| `products.html` | Landing page opened when a customer taps a WhatsApp quote link | Yes |
| `images.js` | Product photos for every category page (Taps, Locks, Bathroom, etc.) | No |
| `wadfow.js` | Wadfow tool images for the homepage Featured Products carousel | Only to swap photos |
| `logos.js` | Partner brand logos for the footer scroll strip | Only to swap logos |
| `imgs/*.jpg` | Same product photos as plain image files, named `<id>.jpg` | Only to swap photos |

## Key Sections in index.html

| What | Search for |
|---|---|
| CSS styles | `<style>` near the top |
| Navigation bar | `class="hnav"` |
| Hero slider | `<!-- HERO SLIDER -->` |
| Featured carousel | `<!-- FEATURED CAROUSEL -->` |
| Footer & brand logos | `<footer>` / `blogo-strip` |
| Google Maps links | `maps.app.goo.gl` |
| Product catalog data | `const CATALOG=` |
| WhatsApp quote logic | `function waHrefSingle` / `function sendBulkWA` |
| Hash-based product deep links | `function checkHash` |
| Favicon | `rel="icon"` |

## WhatsApp Quote Flow

1. Customer taps **Request Quote** on a product, or **Send Bulk Quote** from the basket.
2. A WhatsApp message opens with the product name, SKU, category, and a link to `products.html?ids=...`.
3. `products.html` shows the product list and offers **"View Products with Images"**, which opens `index.html#product-<id>` (single) or `index.html?products=<id1>,<id2>,...` (multiple).
4. `index.html` opens the product lightbox using the existing `IMAGES`/`CATALOG` data.

To change the WhatsApp number, search both `index.html` and `products.html` for `254787151516`.

## Changing Business Details

- Business name: search `Dewmix`
- Address: search `Kenol`
- Google Maps link: search `maps.app.goo.gl`
- Phone numbers: search `254787151516` and `254743448862`

## Favicon

Embedded inline in `index.html` as base64 — search `rel="icon"` to find and replace it.
