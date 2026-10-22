#!/bin/bash
cd /c/Users/poonx/GrovePop_Listing_Tool
sleep 2700   # 45-min cooldown for eBay EPS to recover
READY=$(PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -c "
import sqlite3, json
c=sqlite3.connect('ebay_collection.db')
skus=json.load(open('fill_skus.json'))
q='SELECT sku FROM collected_products WHERE status=\"READY\" AND (listing_id IS NULL OR listing_id=\"\") AND sku IN (%s)'%','.join('?'*len(skus))
print(','.join(x[0] for x in c.execute(q,skus).fetchall()))
")
echo "resuming publish of: $(echo $READY | tr ',' '\n' | wc -l) READY after EPS cooldown"
PYTHONIOENCODING=utf-8 PYTHONPATH=. ./.venv/Scripts/python.exe batch_publish.py --sku-list "$READY" 2>&1 | grep -v -i "insecure\|warnings.warn"
