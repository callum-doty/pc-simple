"""
Simplified Document Catalog Application
FastAPI-based rebuild with streamlined architecture
"""

from fastapi import (
    FastAPI,
    BackgroundTasks,
    HTTPException,
    Depends,
    UploadFile,
    File,
    Request,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import os
from typing import List, Optional
import logging
from sqlalchemy.orm import Session

from config import get_settings
from database import init_db, get_db
from services.document_service import DocumentService
from services.ai_service import AIService
from services.search_service import SearchService
from services.storage_service import StorageService
from services.taxonomy_service import TaxonomyService
from worker import process_document_task
from celery.result import AsyncResult
from models.search_query import SearchQuery

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Set sqlalchemy engine logger to WARNING to reduce verbosity
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    # Startup
    logger.info("Starting up Document Catalog application...")
    await init_db()

    # Initialize taxonomy from CSV if it exists
    taxonomy_csv_path = "taxonomy.csv"
    if os.path.exists(taxonomy_csv_path):
        logger.info("Initializing taxonomy from CSV...")
        # Create a temporary service instance for initialization
        db_session = next(get_db())
        taxonomy_service = TaxonomyService(db_session)
        success, message = await taxonomy_service.initialize_from_csv(taxonomy_csv_path)
        db_session.close()
        if success:
            logger.info(f"Taxonomy initialization: {message}")
        else:
            logger.warning(f"Taxonomy initialization failed: {message}")

    logger.info("Application startup complete")
    yield

    # Shutdown
    logger.info("Shutting down application...")


# Create FastAPI app
app = FastAPI(
    title="Document Catalog",
    description="AI-powered document processing and search system",
    version="2.0.0",
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Mount files directory for serving uploaded documents
if settings.storage_type == "local":

    @app.get("/files/{filename}")
    async def serve_file(filename: str):
        """Serve uploaded files for local storage"""
        file_path = os.path.join(settings.storage_path, filename)
        if os.path.exists(file_path):
            return FileResponse(file_path)
        raise HTTPException(status_code=404, detail="File not found")

    @app.get("/previews/{filename}")
    async def serve_preview(filename: str):
        """Serve preview images for local storage"""
        preview_path = os.path.join(settings.storage_path, "previews", filename)
        if os.path.exists(preview_path):
            return FileResponse(preview_path)
        raise HTTPException(status_code=404, detail="Preview not found")


# Templates
templates = Jinja2Templates(directory="templates")


# Dependency to get services
def get_document_service(db: Session = Depends(get_db)) -> DocumentService:
    return DocumentService(db)


def get_ai_service() -> AIService:
    return AIService()


def get_search_service(db: Session = Depends(get_db)) -> SearchService:
    return SearchService(db)


def get_storage_service() -> StorageService:
    return StorageService()


def get_taxonomy_service(db: Session = Depends(get_db)) -> TaxonomyService:
    return TaxonomyService(db)


# Routes


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Home page - redirect to search"""
    return templates.TemplateResponse("search.html", {"request": request})


@app.get("/health")
async def health_check():
    """Health check endpoint for Render"""
    return {"status": "healthy", "version": "2.0.0"}


# Document Upload
@app.post("/api/documents/upload")
async def upload_documents(
    files: List[UploadFile] = File(...),
    document_service: DocumentService = Depends(get_document_service),
    storage_service: StorageService = Depends(get_storage_service),
    background_tasks: BackgroundTasks = None,
):
    """Upload one or more documents"""
    try:
        tasks = []

        for file in files:
            if not file.filename:
                continue

            # Save file to storage
            file_path = await storage_service.save_file(file)

            # Create document record
            document = await document_service.create_document(
                filename=file.filename, file_path=file_path, file_size=file.size or 0
            )

            # Use background task to introduce a delay before Celery task
            if background_tasks:
                background_tasks.add_task(
                    process_document_task.apply_async,
                    args=[document.id],
                    countdown=2,  # 2-second delay
                )
            else:
                # Fallback for environments without BackgroundTasks
                process_document_task.apply_async(
                    args=[document.id],
                    countdown=2,  # 2-second delay
                )

            tasks.append(
                {
                    "document_id": document.id,
                    "filename": document.filename,
                }
            )

        return {
            "success": True,
            "message": f"Queued {len(tasks)} documents for processing",
            "tasks": tasks,
        }

    except Exception as e:
        logger.error(f"Upload error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# Document Search
@app.get("/api/documents/search")
async def search_documents(
    q: str = "",
    page: int = 1,
    per_page: int = 20,
    primary_category: Optional[str] = None,
    subcategory: Optional[str] = None,
    canonical_term: Optional[str] = None,
    sort_by: str = "created_at",
    sort_direction: str = "desc",
    search_service: SearchService = Depends(get_search_service),
):
    """Search documents"""
    try:
        results = await search_service.search(
            query=q,
            page=page,
            per_page=per_page,
            primary_category=primary_category,
            subcategory=subcategory,
            canonical_term=canonical_term,
            sort_by=sort_by,
            sort_direction=sort_direction,
        )

        return {
            "success": True,
            "documents": results["documents"],
            "pagination": results["pagination"],
            "facets": results["facets"],
            "total_count": results["total_count"],
        }

    except Exception as e:
        logger.error(f"Search error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# Get Document
@app.get("/api/documents/{document_id}")
async def get_document(
    document_id: int, document_service: DocumentService = Depends(get_document_service)
):
    """Get document by ID"""
    try:
        document = await document_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        return {"success": True, "document": document.to_dict()}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get document error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# Document Preview
@app.get("/api/documents/{document_id}/preview")
async def get_document_preview(
    document_id: int,
    document_service: DocumentService = Depends(get_document_service),
    storage_service: StorageService = Depends(get_storage_service),
):
    """Get document preview"""
    try:
        document = await document_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        preview_url = await storage_service.get_preview_url(document.file_path)

        return {
            "success": True,
            "preview_url": preview_url,
            "filename": document.filename,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Preview error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# Processing Status
@app.get("/api/documents/{document_id}/status")
async def get_processing_status(
    document_id: int, document_service: DocumentService = Depends(get_document_service)
):
    """Get document processing status"""
    try:
        document = await document_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        return {
            "success": True,
            "status": document.status,
            "progress": document.processing_progress or 0,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Status error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# Reprocess Document
@app.post("/api/documents/{document_id}/reprocess")
async def reprocess_document(
    document_id: int,
    analysis_type: str = "unified",
    document_service: DocumentService = Depends(get_document_service),
):
    """Reprocess a document with optional analysis type"""
    try:
        document = await document_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        # Reset status
        await document_service.update_document_status(document_id, "PENDING")

        # Queue for reprocessing with Celery
        task = process_document_task.delay(document.id, analysis_type)

        return {
            "success": True,
            "message": f"Document {document.filename} queued for reprocessing with {analysis_type} analysis",
            "task_id": task.id,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Reprocess error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# Search Interface
@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    """Search page"""
    return templates.TemplateResponse("search.html", {"request": request})


# Upload Interface
@app.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    """Upload page"""
    return templates.TemplateResponse("upload.html", {"request": request})


# Admin Interface
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Admin dashboard"""
    return templates.TemplateResponse("admin.html", {"request": request})


# Taxonomy API Endpoints
@app.get("/api/taxonomy/categories")
async def get_taxonomy_categories(
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get all primary categories from taxonomy"""
    try:
        categories = await taxonomy_service.get_primary_categories()
        return {"success": True, "categories": categories}

    except Exception as e:
        logger.error(f"Taxonomy categories error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/taxonomy/categories/{primary_category}/subcategories")
async def get_taxonomy_subcategories(
    primary_category: str,
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get subcategories for a primary category"""
    try:
        subcategories = await taxonomy_service.get_subcategories(primary_category)
        return {"success": True, "subcategories": subcategories}

    except Exception as e:
        logger.error(f"Taxonomy subcategories error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/taxonomy/hierarchy")
async def get_taxonomy_hierarchy(
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get complete taxonomy hierarchy"""
    try:
        hierarchy = await taxonomy_service.get_taxonomy_hierarchy()
        return {"success": True, "hierarchy": hierarchy}

    except Exception as e:
        logger.error(f"Taxonomy hierarchy error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/taxonomy/filter-data")
async def get_filter_taxonomy(
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get taxonomy data structured for UI filters"""
    try:
        filter_data = await taxonomy_service.get_filter_taxonomy_data()
        return {"success": True, "data": filter_data}
    except Exception as e:
        logger.error(f"Filter taxonomy data error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/taxonomy/canonical-terms")
async def get_canonical_terms(
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get a flat list of all canonical terms"""
    try:
        terms = await taxonomy_service.get_all_canonical_terms()
        return {"success": True, "terms": terms}
    except Exception as e:
        logger.error(f"Error getting canonical terms: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.get("/api/taxonomy/search")
async def search_taxonomy_terms(
    q: str,
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Search taxonomy terms"""
    try:
        terms = await taxonomy_service.search_terms(q)
        return {"success": True, "terms": terms}

    except Exception as e:
        logger.error(f"Taxonomy search error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/taxonomy/stats")
async def get_taxonomy_statistics(
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get taxonomy statistics"""
    try:
        stats = await taxonomy_service.get_statistics()
        return {"success": True, "stats": stats}

    except Exception as e:
        logger.error(f"Taxonomy stats error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# AI Service API Endpoints
@app.get("/api/ai/info")
async def get_ai_info(
    ai_service: AIService = Depends(get_ai_service),
):
    """Get AI service configuration and capabilities"""
    try:
        info = ai_service.get_ai_info()
        return {"success": True, "ai_info": info}

    except Exception as e:
        logger.error(f"AI info error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ai/analysis-types")
async def get_analysis_types(
    ai_service: AIService = Depends(get_ai_service),
):
    """Get available analysis types"""
    try:
        types = ai_service.get_available_analysis_types()
        return {"success": True, "analysis_types": types}

    except Exception as e:
        logger.error(f"Analysis types error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/documents/{document_id}/analyze")
async def analyze_document_with_type(
    document_id: int,
    analysis_type: str = "unified",
    ai_service: AIService = Depends(get_ai_service),
    document_service: DocumentService = Depends(get_document_service),
    storage_service: StorageService = Depends(get_storage_service),
):
    """Perform immediate analysis on a document with specified type"""
    try:
        # Get document
        document = await document_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        # Perform analysis
        result = await ai_service.analyze_document(
            document.file_path, document.filename, analysis_type
        )

        return {
            "success": True,
            "document_id": document_id,
            "analysis_type": analysis_type,
            "result": result,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Document analysis error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# Statistics API
@app.get("/api/tasks/{task_id}/status")
async def get_task_status(task_id: str):
    """Get the status of a Celery task"""
    task_result = AsyncResult(task_id)
    result = {
        "task_id": task_id,
        "status": task_result.status,
        "result": task_result.result,
    }
    return {"success": True, "task": result}


@app.get("/api/stats")
async def get_statistics(
    document_service: DocumentService = Depends(get_document_service),
):
    """Get application statistics"""
    try:
        stats = await document_service.get_statistics()
        return {"success": True, "stats": stats}

    except Exception as e:
        logger.error(f"Stats error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    import multiprocessing

    # Set the start method for multiprocessing
    multiprocessing.set_start_method("fork", force=True)

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=settings.debug,
    )


@app.get("/api/search/canonical/{canonical_term}")
async def search_by_canonical_term(
    canonical_term: str,
    q: str = "",
    search_service: SearchService = Depends(get_search_service),
):
    """Search documents by canonical term with optional query"""
    try:
        results = await search_service.search_by_canonical_term(canonical_term, query=q)
        return {"success": True, "documents": results}
    except Exception as e:
        logger.error(f"Canonical term search error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/search/verbatim/{verbatim_term}")
async def search_by_verbatim_term(
    verbatim_term: str,
    q: str = "",
    search_service: SearchService = Depends(get_search_service),
):
    """Search documents by verbatim term with optional query"""
    try:
        results = await search_service.search_by_verbatim_term(verbatim_term, query=q)
        return {"success": True, "documents": results}
    except Exception as e:
        logger.error(f"Verbatim term search error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/documents/{document_id}/mappings")
async def get_document_mappings(
    document_id: int,
    document_service: DocumentService = Depends(get_document_service),
):
    """Get keyword mappings for a document"""
    try:
        document = await document_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        mappings = document.get_keyword_mappings()
        return {"success": True, "mappings": mappings}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Document mappings error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stats/mappings")
async def get_mapping_stats(
    search_service: SearchService = Depends(get_search_service),
):
    """Get statistics about keyword mappings"""
    try:
        stats = await search_service.get_mapping_statistics()
        return {"success": True, "stats": stats}
    except Exception as e:
        logger.error(f"Mapping stats error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/search/top-queries")
async def get_top_queries(
    search_service: SearchService = Depends(get_search_service),
):
    """Get top 8 search queries"""
    try:
        queries = await search_service.get_top_queries(limit=8)
        return {"success": True, "queries": queries}
    except Exception as e:
        logger.error(f"Top queries error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
