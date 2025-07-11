import sqlite3


def add_search_vector_column():
    conn = sqlite3.connect("documents.db")
    c = conn.cursor()
    try:
        c.execute("ALTER TABLE documents ADD COLUMN search_vector TEXT")
        print("Column 'search_vector' added to 'documents' table.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            print("Column 'search_vector' already exists.")
        else:
            raise
    conn.commit()
    conn.close()


if __name__ == "__main__":
    add_search_vector_column()
