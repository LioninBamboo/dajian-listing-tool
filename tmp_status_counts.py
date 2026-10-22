import sqlite3
c = sqlite3.connect("ebay_collection.db")
print("=== STATUS COUNTS ===")
rows = c.execute("SELECT status, COUNT(*) FROM collected_products GROUP BY status ORDER BY COUNT(*) DESC").fetchall()
for s, n in rows:
    print(f"{s}: {n}")
print("TOTAL:", sum(n for _, n in rows))
print("\n=== READY SKUs ===")
for r in c.execute("SELECT sku, title, price, suggested_price, stock FROM collected_products WHERE UPPER(status)='READY' ORDER BY sku"):
    print(f"{r[0]}\tprice={r[2]}\tsugg={r[3]}\tstock={r[4]}\ttitle={(r[1] or '')[:90]}")
print("\n=== ERROR count ===", c.execute("SELECT COUNT(*) FROM collected_products WHERE UPPER(status)='ERROR'").fetchone()[0])
print("=== cro_delist_pending count (note only) ===", c.execute("SELECT COUNT(*) FROM cro_delist_pending").fetchone()[0])
print("unconfirmed:", c.execute("SELECT COUNT(*) FROM cro_delist_pending WHERE confirmed_at IS NULL").fetchone()[0])
