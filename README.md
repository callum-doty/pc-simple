# Document Catalog

An AI-powered document processing and search system built with FastAPI. Upload documents, extract text with OCR, analyze content with AI, and search through your document collection with advanced filtering and relevance scoring.

## Features

### 🤖 AI-Powered Analysis

- **Automatic Text Extraction**: OCR for images, direct extraction for PDFs and text files
- **AI Content Analysis**: Powered by Claude, GPT, or Gemini for intelligent document understanding
- **Smart Categorization**: Automatic taxonomy classification and keyword extraction
- **Content Summarization**: AI-generated summaries and insights

### 🔍 Advanced Search

- **Full-Text Search**: PostgreSQL-powered search with relevance scoring
- **Vector Search**: Semantic search capabilities with pgvector
- **Smart Filtering**: Filter by categories, subcategories, and canonical terms
- **Search Analytics**: Track popular queries and search patterns

### 📁 Document Management

- **Drag & Drop Upload**: Modern web interface with progress tracking
- **Multiple Formats**: Support for PDF, images (JPG, PNG), text files, and DOCX
- **Preview Generation**: Automatic document previews and thumbnails
- **Flexible Storage**: Local filesystem, S3-compatible storage, or Render disk

### 🎯 Intelligent Organization

- **Taxonomy System**: Hierarchical categorization with primary categories and subcategories
- **Keyword Mapping**: Automatic extraction and mapping of document keywords
- **Canonical Terms**: Standardized terminology for consistent search results
- **Document Relationships**: Smart linking between related documents

### ⚡ Performance & Scalability

- **Background Processing**: Non-blocking document processing with Celery
- **Rate Limiting**: Built-in API rate limiting and request throttling
- **Caching**: Optimized database queries and search result caching
- **Monitoring**: Performance metrics and health check endpoints

## Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   FastAPI App   │    │  AI Services    │    │   Data Layer    │
│                 │    │                 │    │                 │
│ • Web Interface │◄──►│ • Claude/GPT    │◄──►│ • PostgreSQL    │
│ • REST API      │    │ • OCR Engine    │    │ • Vector Search │
│ • Rate Limiting │    │ • Text Analysis │    │ • File Storage  │
│ • Background    │    │ • Categorization│    │ • Search Index  │
│   Tasks         │    │                 │    │                 │
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL (or SQLite for development)
- Redis (for background processing)
- AI API Key (Anthropic, OpenAI, or Google)
- Tesseract OCR (for image text extraction)

### Installation

1. **Clone and Setup**

```bash
git clone https://github.com/callum-doty/pc-simple.git
cd pc-simple
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

2. **Environment Configuration**

```bash
cp .env.example .env
# Edit .env with your settings
```

3. **Required Environment Variables**

```env
# Database
DATABASE_URL=postgresql://user:password@localhost/documents
# or for development:
DATABASE_URL=sqlite:///./documents.db

# AI Service (choose one)
ANTHROPIC_API_KEY=your-anthropic-key
OPENAI_API_KEY=your-openai-key
GEMINI_API_KEY=your-gemini-key

# Background Processing
REDIS_URL=redis://localhost:6379/0

# Storage
STORAGE_TYPE=local
STORAGE_PATH=./storage
```

4. **Initialize Database**

```bash
# Database migrations are handled automatically on startup
python main.py
```

5. **Start Background Worker** (in separate terminal)

```bash
celery -A worker worker --loglevel=info
```

6. **Access Application**

- Web Interface: http://localhost:8000
- API Documentation: http://localhost:8000/docs
- Admin Dashboard: http://localhost:8000/admin/dashboard

## Configuration

### Environment Variables

| Variable            | Description                                    | Default                    | Required |
| ------------------- | ---------------------------------------------- | -------------------------- | -------- |
| `DATABASE_URL`      | Database connection string                     | SQLite                     | No       |
| `ANTHROPIC_API_KEY` | Anthropic Claude API key                       | -                          | Yes\*    |
| `OPENAI_API_KEY`    | OpenAI API key                                 | -                          | Yes\*    |
| `GEMINI_API_KEY`    | Google Gemini API key                          | -                          | Yes\*    |
| `REDIS_URL`         | Redis connection for background tasks          | `redis://localhost:6379/0` | Yes      |
| `STORAGE_TYPE`      | Storage backend (`local`, `s3`, `render_disk`) | `local`                    | No       |
| `STORAGE_PATH`      | Local storage directory                        | `./storage`                | No       |
| `MAX_FILE_SIZE`     | Maximum file size in bytes                     | `100MB`                    | No       |
| `SECRET_KEY`        | Application secret key                         | Generated                  | Yes      |

\*At least one AI API key is required

### Storage Options

**Local Storage**

```env
STORAGE_TYPE=local
STORAGE_PATH=./storage
```

**S3-Compatible Storage**

```env
STORAGE_TYPE=s3
S3_BUCKET=your-bucket
S3_ACCESS_KEY=your-access-key
S3_SECRET_KEY=your-secret-key
S3_REGION=us-east-1
S3_ENDPOINT_URL=https://s3.amazonaws.com  # Optional for non-AWS S3
```

**Render Disk Storage**

```env
STORAGE_TYPE=render_disk
# Automatically configured on Render platform
```

## API Reference

### Document Operations

**Upload Documents**

```http
POST /api/documents/upload
Content-Type: multipart/form-data

files: [file1, file2, ...]
```

**Search Documents**

```http
GET /api/documents/search?q=query&page=1&per_page=20&primary_category=category
```

**Get Document Details**

```http
GET /api/documents/{document_id}
```

**Download Document**

```http
GET /api/documents/{document_id}/download
```

**Reprocess Document**

```http
POST /api/documents/{document_id}/reprocess
```

### Search & Taxonomy

**Search by Canonical Term**

```http
GET /api/search/canonical/{canonical_term}?q=optional_query
```

**Get Taxonomy Hierarchy**

```http
GET /api/taxonomy/hierarchy
```

**Search Taxonomy Terms**

```http
GET /api/taxonomy/search?q=term
```

### System & Analytics

**Health Check**

```http
GET /health
```

**Application Statistics**

```http
GET /api/stats
```

**Top Search Queries**

```http
GET /api/search/top-queries
```

## Deployment

### Render.com

1. **Connect Repository**

   - Link your GitHub repository to Render
   - Render will detect the `render.yaml` configuration

2. **Environment Variables**
   Set in Render dashboard:

   ```
   ANTHROPIC_API_KEY=your-key
   DATABASE_URL=postgresql://...  # Provided by Render
   REDIS_URL=redis://...          # Provided by Render
   ENVIRONMENT=production
   ```

3. **Services**
   - Web service (FastAPI app)
   - Background worker (Celery)
   - PostgreSQL database
   - Redis instance

### Docker

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
EXPOSE 8000

CMD ["python", "main.py"]
```

### Traditional Server

1. **Install Dependencies**

```bash
pip install -r requirements.txt
sudo apt-get install tesseract-ocr  # For OCR
```

2. **Setup Services**

```bash
# PostgreSQL
sudo systemctl start postgresql

# Redis
sudo systemctl start redis

# Application
python main.py

# Background Worker
celery -A worker worker --loglevel=info
```

## Project Structure

```
pc-simple/
├── main.py                 # FastAPI application entry point
├── config.py              # Configuration management
├── database.py            # Database setup and models
├── worker.py              # Celery background tasks
├── requirements.txt       # Python dependencies
├── render.yaml           # Render deployment config
├── alembic/              # Database migrations
├── api/                  # API route modules
│   ├── documents.py      # Document endpoints
│   ├── dashboard.py      # Admin dashboard API
│   └── taxonomy.py       # Taxonomy endpoints
├── models/               # Database models
│   ├── document.py       # Document model
│   ├── taxonomy.py       # Taxonomy models
│   └── search_query.py   # Search analytics
├── services/             # Business logic services
│   ├── ai_service.py     # AI analysis and OCR
│   ├── document_service.py # Document CRUD
│   ├── search_service.py # Search and filtering
│   ├── storage_service.py # File storage
│   └── taxonomy_service.py # Taxonomy management
└── templates/            # Web interface templates
    ├── base.html         # Base template
    ├── search.html       # Search interface
    └── admin/            # Admin templates
```

## Development

### Running Tests

```bash
pytest
```

### Code Formatting

```bash
black .
isort .
```

### Database Migrations

```bash
# Create migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head
```

### Adding New Features

1. **New API Endpoint**: Add to appropriate router in `api/`
2. **New Service**: Create in `services/` directory
3. **Database Changes**: Create Alembic migration
4. **UI Changes**: Update templates in `templates/`

## Performance Optimization

### Search Performance

- PostgreSQL full-text search with GIN indexes
- Vector similarity search with pgvector
- Query result caching
- Optimized database queries

### Processing Performance

- Background processing with Celery
- Concurrent document processing
- Rate limiting to prevent overload
- Efficient file storage and retrieval

### Monitoring

- Request performance tracking
- Slow query logging
- Health check endpoints
- Processing statistics

## Troubleshooting

### Common Issues

**AI Analysis Fails**

- Verify API key configuration and credits
- Check network connectivity to AI services
- Review application logs for specific errors

**File Upload Issues**

- Check file size limits and supported formats
- Verify storage path permissions
- Ensure sufficient disk space

**Search Not Working**

- Verify PostgreSQL is running and accessible
- Check database indexes are created
- Review search service logs

**Background Processing Stuck**

- Ensure Redis is running and accessible
- Check Celery worker status
- Review worker logs for errors

### Logs and Monitoring

**Application Logs**

```bash
# View logs
tail -f app.log

# Specific service logs
grep "search_service" app.log
```

**Database Performance**

```sql
-- Check slow queries
SELECT query, mean_time, calls
FROM pg_stat_statements
ORDER BY mean_time DESC;
```

**System Health**

- Health endpoint: `/health`
- Statistics: `/api/stats`
- Processing status: `/api/documents/{id}/status`

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Add tests for new functionality
5. Run tests and ensure they pass
6. Commit your changes (`git commit -m 'Add amazing feature'`)
7. Push to the branch (`git push origin feature/amazing-feature`)
8. Open a Pull Request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

For issues and questions:

1. Check the [troubleshooting section](#troubleshooting)
2. Review application logs and error messages
3. Check the API documentation at `/docs`
4. Create an issue in the [GitHub repository](https://github.com/callum-doty/pc-simple/issues)

---

**Document Catalog** - AI-Powered Document Processing and Search
