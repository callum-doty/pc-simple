#!/usr/bin/env python3
"""
Export AI analysis data from the database to a CSV file.
"""
import csv
import sys
from database import get_db
from models.document import Document
from sqlalchemy import and_

def export_ai_analysis():
    """Export AI analysis data to CSV."""
    output_file = 'ai_analysis.csv'
    
    try:
        # Get database session
        db = next(get_db())
        
        # Query documents with completed AI analysis
        documents = db.query(
            Document.id,
            Document.filename,
            Document.ai_analysis
        ).filter(
            and_(
                Document.status == 'COMPLETED',
                Document.ai_analysis.isnot(None)
            )
        ).all()
        
        # Write to CSV
        with open(output_file, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            
            # Write header
            writer.writerow(['id', 'filename', 'ai_analysis'])
            
            # Write data
            count = 0
            for doc in documents:
                writer.writerow([doc.id, doc.filename, doc.ai_analysis])
                count += 1
        
        print(f"Successfully exported {count} records to {output_file}")
        return 0
        
    except Exception as e:
        print(f"Error exporting AI analysis: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    finally:
        db.close()

if __name__ == '__main__':
    sys.exit(export_ai_analysis())
