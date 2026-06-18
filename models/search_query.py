from sqlalchemy import Column, Integer, String, DateTime, func
from database import Base
import datetime


class SearchQuery(Base):
    __tablename__ = "search_queries"

    id = Column(Integer, primary_key=True, index=True)
    query = Column(String, nullable=False)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    user_id = Column(String)

    # Filter fields — nullable so legacy rows (pre-migration) stay valid
    filter_client = Column(String, nullable=True, index=True)
    filter_state = Column(String, nullable=True, index=True)
    filter_date_year = Column(Integer, nullable=True)

    # Result quality signal — 0 means a dead-end search
    result_count = Column(Integer, nullable=True)
