from sqlalchemy import Column, Integer, String, DateTime, func
from database import Base
import datetime


class SearchQuery(Base):
    __tablename__ = "search_queries"

    id = Column(Integer, primary_key=True, index=True)
    query = Column(String, nullable=False)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    user_id = Column(String)
