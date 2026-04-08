import sqlite3
db = sqlite3.connect('data_store/polyedge.db')
c = db.cursor()
c.execute("UPDATE trades SET status='closed' WHERE status='open'")
db.commit()
c.execute("SELECT COUNT(*) FROM trades WHERE status='open'")
count = c.fetchone()[0]
db.close()
print(f"Open count: {count}")
