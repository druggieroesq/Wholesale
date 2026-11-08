python3 -c "
import base64
code = '''import os, requests, pandas as pd

OUTPUT_PARQUET = '/app/data/catalogs/wholesale_catalogs/normalized/marlo_beauty_catalog.parquet'
APP_ID = 'U1CLN0B7V3'
API_KEY = '9c7aa3a768c5a210f5bff9d7f817b228'
INDEX_NAME = 'production_products'
URL = f'https://{APP_ID.lower()}-dsn.algolia.net/1/indexes/{INDEX_NAME}/query'

HEADERS = {
    'X-Algolia-Application-Id': APP_ID,
    'X-Algolia-API-Key': API_KEY,
    'Content-Type': 'application/json',
    'Origin': 'https://www.marlobeauty.com',
    'Referer': 'https://www.marlobeauty.com/',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
}

BRANDS = ['Wella', 'Joico', 'OPI', 'Clairol Professional', 'GiGi', 'Satin Smooth', 'Pravana', 'StyleTek', 'Matrix', 'Redken']

all_items = []
os.makedirs(os.path.dirname(OUTPUT_PARQUET), exist_ok=True)

for brand in BRANDS:
    page = 0
    print(f'Fetching {brand}...')
    while True:
        payload = {
            'query': '',
            'facetFilters': [[f'manufacturer.name:{brand}']],
            'hitsPerPage': 100,
            'page': page
        }
        try:
            r = requests.post(URL, headers=HEADERS, json=payload, timeout=15)
            if r.status_code != 200:
                break
            data = r.json()
            hits = data.get('hits', [])
            if not hits:
                break
            for h in hits:
                title = h.get('products_name', '').strip()
                price = float(h.get('products_price') or 0.0)
                model = str(h.get('products_model') or '').strip()
                upc = str(h.get('upc') or h.get('barcode') or h.get('ean') or '').strip()
                item_id = str(h.get('objectID') or '').strip()

                if price > 0 and len(title) > 3:
                    all_items.append({
                        'vendor': 'Marlo Beauty',
                        'sku': model if model else item_id,
                        'mpn': model,
                        'upc': upc,
                        'brand': brand,
                        'title': title,
                        'wholesale_cost': price,
                        'category': 'Salon Consumables',
                        'free_freight_threshold': 99.00
                    })
            if page >= data.get('nbPages', 1) - 1:
                break
            page += 1
        except Exception as e:
            print(f'  Error: {e}')
            break

if all_items:
    df = pd.DataFrame(all_items).drop_duplicates(subset=['sku', 'title'])
    df.to_parquet(OUTPUT_PARQUET, index=False)
    print(f'\\n[SUCCESS] Saved {len(df)} enriched products with MPN/UPC to {OUTPUT_PARQUET}')
'''
with open('/app/tools/scrape_marlo_beauty.py', 'w') as f:
    f.write(code)
"
