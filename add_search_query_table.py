import sys
import os
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

# Add project root to Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from database import Base
from models.search_query import SearchQuery
from config import get_settings


def main():
    settings = get_settings()
    engine = create_engine(settings.database_url)

    inspector = inspect(engine)
    if not inspector.has_table(SearchQuery.__tablename__):
        print(f"Creating table: {SearchQuery.__tablename__}")
        Base.metadata.create_all(bind=engine, tables=[SearchQuery.__table__])
        print("Table created successfully.")
    else:
        print(f"Table '{SearchQuery.__tablename__}' already exists.")


if __name__ == "__main__":
    main()
