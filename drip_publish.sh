#!/bin/bash
cd /c/Users/poonx/GrovePop_Listing_Tool
BATCH=15
for round in $(seq 1 12); do
  LIVE=$(PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -c "import sqlite3;print(sqlite3.connect('ebay_collection.db').execute('SELECT COUNT(*) FROM collected_products WHERE status=\"PUBLISHED\" AND listing_id IS NOT NULL AND listing_id!=\"\"').fetchone()[0])")
  if [ "$LIVE" -ge 250 ]; then echo "reached 250 live — done"; break; fi
  READY=$(PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -c "
import sqlite3, json
c=sqlite3.connect('ebay_collection.db'); skus=json.load(open('fill_skus.json'))
q='SELECT sku FROM collected_products WHERE status=\"READY\" AND (listing_id IS NULL OR listing_id=\"\") AND sku IN (%s) LIMIT $BATCH'%','.join('?'*len(skus))
print(','.join(x[0] for x in c.execute(q,skus).fetchall()))
")
  if [ -z "$READY" ]; then echo "no more READY — done (live=$LIVE)"; break; fi
  echo "=== round $round: live=$LIVE, publishing next $BATCH ==="
  PYTHONIOENCODING=utf-8 PYTHONPATH=. ./.venv/Scripts/python.exe batch_publish.py --sku-list "$READY" 2>&1 | grep -iE "Success:|Listing published|zero supplier" | tail -3
  echo "=== round $round done, cooling down 40min for EPS ==="
  sleep 2400
done
echo "DRIP COMPLETE"
