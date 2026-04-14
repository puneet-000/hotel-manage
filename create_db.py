import sqlite3

conn = sqlite3.connect("database.db")
c = conn.cursor()

c.execute("""
CREATE TABLE menu(
id INTEGER PRIMARY KEY AUTOINCREMENT,
food TEXT,
price INTEGER,
food_type TEXT DEFAULT 'veg'
)
""")

c.execute("""
CREATE TABLE tables(
id INTEGER PRIMARY KEY AUTOINCREMENT
)
""")

c.execute("""
CREATE TABLE orders(
id INTEGER PRIMARY KEY AUTOINCREMENT,
table_id INTEGER,
food TEXT,
status TEXT,
time TEXT,
accepted_at INTEGER,
ready_at INTEGER
)
""")

for i in range(5):
    c.execute("INSERT INTO tables DEFAULT VALUES")

conn.commit()
conn.close()

print("Database Created Successfully")
