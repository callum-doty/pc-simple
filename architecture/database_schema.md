# Database Schema - Mermaid Chart

```mermaid
erDiagram
    documents {
        int id PK
        string filename
        string file_path
        int file_size
        string status
        int processing_progress
        text processing_error
        datetime created_at
        datetime updated_at
        datetime processed_at
        text extracted_text
        jsonb ai_analysis
        jsonb keywords
        jsonb file_metadata
        text search_content
        vector search_vector
        tsvector ts_vector
        string preview_url
        string thumbnail_url
    }

    taxonomy_terms {
        int id PK
        string term
        string primary_category
        string subcategory
        text description
        datetime created_at
        int parent_id FK
    }

    taxonomy_synonyms {
        int id PK
        int taxonomy_id FK
        string synonym
        datetime created_at
    }

    document_taxonomy_map {
        int document_id PK,FK
        int taxonomy_term_id PK,FK
    }

    search_queries {
        int id PK
        string query
        datetime timestamp
        string user_id
    }

    %% Relationships
    documents ||--o{ document_taxonomy_map : "has many"
    taxonomy_terms ||--o{ document_taxonomy_map : "has many"
    taxonomy_terms ||--o{ taxonomy_synonyms : "has many"
    taxonomy_terms ||--o{ taxonomy_terms : "parent-child"

    %% Indexes and Special Features
    documents ||--|| documents : "GIN index on keywords"
    documents ||--|| documents : "GIN index on ts_vector"
    documents ||--|| documents : "HNSW index on search_vector"
    documents ||--|| documents : "Composite indexes on status+timestamps"
```

## Key Features

### Documents Table

- **Core Entity**: Central table storing all document information
- **JSON Fields**: Flexible storage for AI analysis, keywords, and metadata
- **Search Capabilities**:
  - Full-text search via `ts_vector` (PostgreSQL TSVECTOR)
  - Vector similarity search via `search_vector` (pgvector extension)
  - Keyword-based search via `keywords` JSONB field
- **Processing Pipeline**: Status tracking with progress and error handling
- **File Management**: Storage paths, sizes, and preview/thumbnail URLs

### Taxonomy System

- **Hierarchical Structure**: Self-referential taxonomy terms with parent-child relationships
- **Categorization**: Primary categories and subcategories for organization
- **Synonyms**: Support for alternative terms via `taxonomy_synonyms`
- **Many-to-Many**: Documents can have multiple taxonomy terms via junction table

### Search Analytics

- **Query Tracking**: `search_queries` table logs all search activities
- **User Analytics**: Optional user ID tracking for search patterns

### Database Technology

- **PostgreSQL**: Primary database with advanced features
- **pgvector Extension**: Vector similarity search for AI embeddings
- **JSONB**: Flexible JSON storage with indexing capabilities
- **Full-Text Search**: Built-in PostgreSQL text search with computed TSVECTOR

### Performance Optimizations

- **Specialized Indexes**: GIN indexes for JSONB and text search
- **Vector Indexes**: HNSW indexes for efficient similarity search
- **Composite Indexes**: Multi-column indexes for common query patterns
- **Connection Pooling**: Optimized database connection management
