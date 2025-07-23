"""
AI service - handles document analysis using LLM APIs
Consolidates OCR, text extraction, and AI analysis into a single service
Now integrated with PromptManager for sophisticated analysis
"""

import asyncio
import logging
from typing import Dict, Any, List, Optional, Tuple, AsyncGenerator, Generator
import json
import base64
from pathlib import Path
import io
from PIL import Image
import fitz  # PyMuPDF
import anthropic
import openai
import google.generativeai as genai
from sqlalchemy.orm import Session

from config import get_settings
from services.storage_service import StorageService
from services.taxonomy_service import TaxonomyService
from services.prompt_manager import PromptManager

logger = logging.getLogger(__name__)
settings = get_settings()


class AIService:
    """Unified AI service for document analysis with PromptManager integration"""

    def __init__(self, db: Session):
        self.db = db
        self.storage_service = StorageService()
        self.taxonomy_service = TaxonomyService(db=self.db)
        self.prompt_manager = PromptManager(taxonomy_service=self.taxonomy_service)

        # Initialize AI clients with explicit parameter handling
        self.anthropic_client = None
        self.openai_client = None
        self.gemini_client = None

        if settings.gemini_api_key:
            try:
                genai.configure(api_key=settings.gemini_api_key)
                self.gemini_client = genai.GenerativeModel("gemini-pro-vision")
            except Exception as e:
                logger.warning(f"Failed to initialize Gemini client: {str(e)}")
                self.gemini_client = None

        if settings.anthropic_api_key:
            try:
                self.anthropic_client = anthropic.Anthropic(
                    api_key=settings.anthropic_api_key
                )
            except Exception as e:
                logger.warning(f"Failed to initialize Anthropic client: {str(e)}")
                self.anthropic_client = None

        if settings.openai_api_key:
            try:
                self.openai_client = openai.OpenAI(api_key=settings.openai_api_key)
            except Exception as e:
                logger.warning(f"Failed to initialize OpenAI client: {str(e)}")
                self.openai_client = None

        # Determine which AI provider to use
        self.ai_provider = self._determine_ai_provider()

    def _determine_ai_provider(self) -> str:
        """Determine which AI provider to use"""
        if settings.default_ai_provider == "gemini" and self.gemini_client:
            return "gemini"
        elif settings.default_ai_provider == "anthropic" and self.anthropic_client:
            return "anthropic"
        elif settings.default_ai_provider == "openai" and self.openai_client:
            return "openai"
        elif self.gemini_client:
            return "gemini"
        elif self.anthropic_client:
            return "anthropic"
        elif self.openai_client:
            return "openai"
        else:
            logger.warning(
                "No AI provider configured. AI analysis will be disabled. Please set ANTHROPIC_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY"
            )
            return "none"

    async def analyze_document(
        self, file_path: str, filename: str, analysis_type: str = "unified"
    ) -> Dict[str, Any]:
        """
        Complete document analysis pipeline with multiple analysis options

        Args:
            file_path: Path to the document file
            filename: Name of the document
            analysis_type: Type of analysis to perform
                - "unified": Single comprehensive analysis (default)
                - "modular": Multiple specialized analyses
                - "metadata": Core metadata only
                - "classification": Classification only
                - "entities": Entity extraction only
                - "text": Text extraction only
                - "design": Design elements only
                - "keywords": Taxonomy keywords only
                - "communication": Communication focus only

        Returns:
            Consolidated analysis results
        """
        try:
            logger.info(f"Starting {analysis_type} analysis for document: {filename}")

            # Step 1: Get file content
            file_content = await self.storage_service.get_file(file_path)
            if not file_content:
                raise ValueError(f"Could not retrieve file content for {filename}")

            # Step 2: Determine file type and extract text
            file_type = self._get_file_type(filename)
            extracted_text = await self._extract_text(file_content, file_type, filename)

            # Step 3: Perform AI analysis based on type
            if analysis_type == "unified":
                ai_analysis = await self._perform_unified_analysis(
                    extracted_text, file_content, file_type, filename
                )
            elif analysis_type == "modular":
                ai_analysis = await self._perform_modular_analysis(
                    extracted_text, file_content, file_type, filename
                )
            else:
                ai_analysis = await self._perform_specific_analysis(
                    analysis_type, extracted_text, file_content, file_type, filename
                )

            # Step 4: Extract keywords and categories
            keywords, categories = self._extract_keywords_from_analysis(ai_analysis)

            # Step 5: Consolidate results
            result = {
                "extracted_text": extracted_text,
                "ai_analysis": ai_analysis,
                "keywords": keywords,
                "categories": categories,
                "file_type": file_type,
                "analysis_provider": self.ai_provider,
                "analysis_type": analysis_type,
            }

            logger.info(f"Completed {analysis_type} analysis for document: {filename}")
            return result

        except Exception as e:
            logger.error(f"Error analyzing document {filename}: {str(e)}")
            raise

    async def _perform_unified_analysis(
        self, extracted_text: str, file_content: bytes, file_type: str, filename: str
    ) -> Dict[str, Any]:
        """Perform unified analysis using the comprehensive prompt"""
        try:
            # If Anthropic is not available, fall back to modular analysis
            if not self.anthropic_client:
                logger.warning(
                    "Anthropic client not available for unified analysis. Falling back to modular analysis."
                )
                return await self._perform_modular_analysis(
                    extracted_text, file_content, file_type, filename
                )

            # Get the unified analysis prompt
            prompt_data = await self.prompt_manager.get_unified_analysis_prompt(
                filename
            )

            # Prepare image data if it's an image or PDF
            image_data = None
            if file_type in ["image", "pdf"]:
                image_data = self._prepare_image_data(file_content, file_type)

            # Add extracted text to the prompt
            enhanced_prompt = self._enhance_prompt_with_text(
                prompt_data["user"], extracted_text
            )

            # Call the AI service
            analysis_result = await self._call_anthropic_api_with_system(
                prompt_data["system"], enhanced_prompt, image_data
            )

            # Ensure the final output has a consistent structure
            if "document_analysis" not in analysis_result:
                analysis_result["document_analysis"] = {
                    "summary": analysis_result.get("summary", "No summary available"),
                    "document_type": analysis_result.get("document_type", "unknown"),
                    "campaign_type": analysis_result.get("campaign_type", "unknown"),
                    "document_tone": analysis_result.get("document_tone", "neutral"),
                }

            return analysis_result

        except Exception as e:
            logger.error(f"Error in unified analysis: {str(e)}")
            return {"error": str(e)}

    async def _perform_modular_analysis(
        self, extracted_text: str, file_content: bytes, file_type: str, filename: str
    ) -> Dict[str, Any]:
        """Perform modular analysis using multiple specialized prompts"""
        try:
            results = {}

            # Prepare image data once
            image_data = None
            if file_type in ["image", "pdf"]:
                image_data = self._prepare_image_data(file_content, file_type)

            # Step 1: Core metadata
            metadata_result = await self._run_analysis_prompt(
                self.prompt_manager.get_core_metadata_prompt(filename),
                extracted_text,
                image_data,
            )
            results["metadata"] = metadata_result

            # Step 2: Classification (using metadata context)
            classification_result = await self._run_analysis_prompt(
                self.prompt_manager.get_classification_prompt(
                    filename, metadata_result
                ),
                extracted_text,
                image_data,
            )
            results["classification"] = classification_result

            # Step 3: Entity extraction
            entity_result = await self._run_analysis_prompt(
                self.prompt_manager.get_entity_prompt(filename, metadata_result),
                extracted_text,
                image_data,
            )
            results["entities"] = entity_result

            # Step 4: Text extraction
            text_result = await self._run_analysis_prompt(
                self.prompt_manager.get_text_extraction_prompt(
                    filename, metadata_result
                ),
                extracted_text,
                image_data,
            )
            results["text_extraction"] = text_result

            # Step 5: Design elements (only for visual documents)
            if file_type in ["image", "pdf"]:
                design_result = await self._run_analysis_prompt(
                    self.prompt_manager.get_design_elements_prompt(
                        filename, metadata_result
                    ),
                    extracted_text,
                    image_data,
                )
                results["design_elements"] = design_result

            # Step 6: Taxonomy keywords
            keyword_result = await self._run_analysis_prompt(
                self.prompt_manager.get_taxonomy_keyword_prompt(
                    filename, metadata_result
                ),
                extracted_text,
                image_data,
            )
            results["taxonomy_keywords"] = keyword_result

            # Step 7: Communication focus
            communication_result = await self._run_analysis_prompt(
                self.prompt_manager.get_communication_focus_prompt(
                    filename, metadata_result
                ),
                extracted_text,
                image_data,
            )
            results["communication_focus"] = communication_result

            # Consolidate into a unified document_analysis structure
            if "metadata" in results and "classification" in results:
                # Extract summary and other details from the 'document_analysis' block within the 'metadata' result
                doc_analysis_data = results.get("metadata", {}).get(
                    "document_analysis", {}
                )
                summary_text = doc_analysis_data.get("summary", "No summary available")
                document_type = doc_analysis_data.get("document_type", "unknown")
                campaign_type = doc_analysis_data.get("campaign_type", "unknown")
                document_tone = doc_analysis_data.get("document_tone", "neutral")

                results["document_analysis"] = {
                    "summary": summary_text,
                    "document_type": document_type,
                    "campaign_type": campaign_type,
                    "document_tone": document_tone,
                }

            return results

        except Exception as e:
            logger.error(f"Error in modular analysis: {str(e)}")
            return {"error": str(e)}

    async def _perform_specific_analysis(
        self,
        analysis_type: str,
        extracted_text: str,
        file_content: bytes,
        file_type: str,
        filename: str,
    ) -> Dict[str, Any]:
        """Perform a specific type of analysis"""
        try:
            # Prepare image data
            image_data = None
            if file_type in ["image", "pdf"]:
                image_data = self._prepare_image_data(file_content, file_type)

            # Get the appropriate prompt
            if analysis_type == "metadata":
                prompt_data = self.prompt_manager.get_core_metadata_prompt(filename)
            elif analysis_type == "classification":
                prompt_data = self.prompt_manager.get_classification_prompt(filename)
            elif analysis_type == "entities":
                prompt_data = self.prompt_manager.get_entity_prompt(filename)
            elif analysis_type == "text":
                prompt_data = self.prompt_manager.get_text_extraction_prompt(filename)
            elif analysis_type == "design":
                prompt_data = self.prompt_manager.get_design_elements_prompt(filename)
            elif analysis_type == "keywords":
                prompt_data = await self.prompt_manager.get_taxonomy_keyword_prompt(
                    filename
                )
            elif analysis_type == "communication":
                prompt_data = self.prompt_manager.get_communication_focus_prompt(
                    filename
                )
            else:
                raise ValueError(f"Unsupported analysis type: {analysis_type}")

            # Run the analysis
            return await self._run_analysis_prompt(
                prompt_data, extracted_text, image_data
            )

        except Exception as e:
            logger.error(f"Error in {analysis_type} analysis: {str(e)}")
            return {"error": str(e)}

    async def _run_analysis_prompt(
        self,
        prompt_data: Dict[str, str],
        extracted_text: str,
        image_data: Optional[str],
    ) -> Dict[str, Any]:
        """Run a single analysis prompt"""
        try:
            # Enhance prompt with extracted text
            enhanced_prompt = self._enhance_prompt_with_text(
                prompt_data["user"], extracted_text
            )

            # Call the AI service
            if self.ai_provider == "gemini":
                return await self._call_gemini_api_with_system(
                    prompt_data["system"], enhanced_prompt, image_data
                )
            elif self.ai_provider == "anthropic":
                return await self._call_anthropic_api_with_system(
                    prompt_data["system"], enhanced_prompt, image_data
                )
            elif self.ai_provider == "openai":
                return await self._call_openai_api_with_system(
                    prompt_data["system"], enhanced_prompt, image_data
                )
            elif self.ai_provider == "none":
                return {"error": "No AI provider configured"}
            else:
                raise ValueError(f"Unsupported AI provider: {self.ai_provider}")

        except Exception as e:
            logger.error(f"Error running analysis prompt: {str(e)}")
            return {"error": str(e)}

    def _enhance_prompt_with_text(self, prompt: str, extracted_text: str) -> str:
        """Enhance prompt with extracted text"""
        if extracted_text.strip():
            # Insert extracted text into the prompt
            text_section = (
                f"\n\nExtracted Text from Document:\n{extracted_text[:4000]}\n"
            )
            # Insert after the first line of the prompt
            lines = prompt.split("\n")
            if len(lines) > 1:
                lines.insert(1, text_section)
                return "\n".join(lines)
            else:
                return prompt + text_section
        return prompt

    def _get_fallback_analysis(self, filename: str, file_type: str) -> Dict[str, Any]:
        """Return a basic analysis when no AI provider is configured"""
        return {
            "document_analysis": {
                "summary": f"Document: {filename}",
                "document_type": "brochure",
                "campaign_type": "general",
                "election_year": None,
                "document_tone": "neutral",
            },
            "classification": {
                "category": "informational",
                "subcategory": None,
                "rationale": "AI analysis not available - no API keys configured",
            },
            "entities": {
                "client_name": None,
                "opponent_name": None,
                "creation_date": None,
            },
            "analysis_provider": "none",
            "file_type": file_type,
        }

    def _get_file_type(self, filename: str) -> str:
        """Determine file type from filename"""
        extension = Path(filename).suffix.lower()

        if extension == ".pdf":
            return "pdf"
        elif extension in [".jpg", ".jpeg", ".png", ".tiff", ".bmp"]:
            return "image"
        elif extension in [".txt", ".md"]:
            return "text"
        elif extension in [".doc", ".docx"]:
            return "document"
        else:
            return "unknown"

    async def _extract_text(
        self, file_content: bytes, file_type: str, filename: str
    ) -> str:
        """Extract text from file based on type"""
        try:
            if file_type == "pdf":
                return await self._extract_text_from_pdf(file_content)
            elif file_type == "image":
                return await self._extract_text_from_image(file_content)
            elif file_type == "text":
                return file_content.decode("utf-8", errors="ignore")
            elif file_type == "document":
                return await self._extract_text_from_document(file_content)
            else:
                logger.warning(
                    f"Unsupported file type for text extraction: {file_type}"
                )
                return ""

        except Exception as e:
            logger.error(f"Error extracting text from {filename}: {str(e)}")
            return ""

    async def _extract_text_from_pdf_generator(
        self, file_content: bytes
    ) -> AsyncGenerator[Tuple[int, str], None]:
        """
        Extract text from PDF page by page using AI-based OCR.
        Yields a tuple of (page_number, extracted_text).
        """
        try:
            doc = fitz.open(stream=file_content, filetype="pdf")
            if doc.page_count == 0:
                logger.warning("PDF has no pages.")
                return

            for page_num in range(len(doc)):
                try:
                    page = doc.load_page(page_num)
                    logger.info(f"Performing AI-based OCR on page {page_num + 1}.")
                    pix = page.get_pixmap(dpi=200)  # Lower DPI to save memory
                    img_data = pix.tobytes("png")
                    ocr_text = await self._extract_text_from_image(img_data)
                    if ocr_text.strip():
                        yield (page_num + 1, ocr_text)
                except Exception as page_error:
                    logger.error(
                        f"Error processing page {page_num + 1}: {str(page_error)}"
                    )
                    continue
            doc.close()
        except Exception as e:
            logger.error(f"Error extracting text from PDF with AI OCR: {str(e)}")

    async def _extract_text_from_pdf(self, file_content: bytes) -> str:
        """
        Extract text from PDF using the configured AI provider for OCR.
        This ensures that even image-based PDFs are processed correctly.
        """
        all_text = []
        async for page_num, ocr_text in self._extract_text_from_pdf_generator(
            file_content
        ):
            all_text.append(f"--- Page {page_num} ---\n{ocr_text}")
        return "\n\n".join(all_text)

    async def _extract_text_from_image(self, file_content: bytes) -> str:
        """Extract text from image using the configured AI provider."""
        try:
            if self.ai_provider == "none":
                logger.warning("No AI provider configured for OCR.")
                return ""

            image_data = base64.b64encode(file_content).decode("utf-8")

            system_prompt = "You are an expert OCR engine. Your task is to extract any and all text from the given image, accurately. Preserve the original formatting as much as possible. Only return the extracted text, with no additional comments, introductions, or summaries."
            user_prompt = "Please extract all text from this image."

            return await self._call_ai_for_raw_text(
                system_prompt, user_prompt, image_data
            )

        except Exception as e:
            logger.error(f"Error extracting text from image with AI: {str(e)}")
            return ""

    async def _extract_text_from_document(self, file_content: bytes) -> str:
        """Extract text from Word documents"""
        try:
            # For now, return empty string
            # In a full implementation, you'd use python-docx or similar
            logger.warning("Document text extraction not implemented yet")
            return ""

        except Exception as e:
            logger.error(f"Error extracting text from document: {str(e)}")
            return ""

    def _prepare_image_data(self, file_content: bytes, file_type: str) -> Optional[str]:
        """Prepare image data for AI analysis"""
        try:
            if file_type == "image":
                # Encode image as base64
                return base64.b64encode(file_content).decode("utf-8")
            elif file_type == "pdf":
                # Convert first page of PDF to image
                doc = fitz.open(stream=file_content, filetype="pdf")
                if doc.page_count > 0:
                    page = doc[0]
                    pix = page.get_pixmap()
                    img_data = pix.tobytes("png")
                    doc.close()
                    return base64.b64encode(img_data).decode("utf-8")
            return None

        except Exception as e:
            logger.error(f"Error preparing image data: {str(e)}")
            return None

    async def _call_ai_for_raw_text(
        self, system_prompt: str, user_prompt: str, image_data: Optional[str] = None
    ) -> str:
        """Call the configured AI provider and return the raw text response."""
        try:
            model_map = {
                "anthropic": "claude-3-opus-20240229",
                "openai": "gpt-4-turbo",
                "gemini": "gemini-pro-vision",
            }
            model = model_map.get(self.ai_provider)

            if not model:
                logger.warning(
                    f"OCR not supported for the '{self.ai_provider}' provider."
                )
                return ""

            if self.ai_provider == "anthropic" and self.anthropic_client:
                messages = [
                    {
                        "role": "user",
                        "content": (
                            [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": image_data,
                                    },
                                },
                                {"type": "text", "text": user_prompt},
                            ]
                            if image_data
                            else user_prompt
                        ),
                    }
                ]
                response = self.anthropic_client.messages.create(
                    model=model,
                    max_tokens=4000,
                    system=system_prompt,
                    messages=messages,
                )
                return response.content[0].text.strip()

            elif self.ai_provider == "openai" and self.openai_client:
                content = [{"type": "text", "text": user_prompt}]
                if image_data:
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_data}"},
                        }
                    )
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content},
                ]
                response = self.openai_client.chat.completions.create(
                    model=model, messages=messages, max_tokens=4000
                )
                return response.choices[0].message.content.strip()

            elif self.ai_provider == "gemini" and self.gemini_client:
                prompt_parts = []
                if image_data:
                    image_bytes = base64.b64decode(image_data)
                    img = Image.open(io.BytesIO(image_bytes))
                    prompt_parts.append(img)

                # Gemini doesn't have a system prompt, so prepend it.
                full_prompt = (
                    f"System Prompt: {system_prompt}\n\nUser Prompt: {user_prompt}"
                )
                prompt_parts.append(full_prompt)

                response = self.gemini_client.generate_content(prompt_parts)
                return response.text.strip()

            else:
                return ""

        except Exception as e:
            logger.error(f"Error calling AI for raw text OCR: {str(e)}")
            return ""

    async def _call_anthropic_api_with_system(
        self, system_prompt: str, user_prompt: str, image_data: Optional[str] = None
    ) -> Dict[str, Any]:
        """Call Anthropic Claude API with system and user prompts"""
        try:
            messages = []

            if image_data:
                # Include image in the message
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": image_data,
                                },
                            },
                            {"type": "text", "text": user_prompt},
                        ],
                    }
                )
            else:
                messages.append({"role": "user", "content": user_prompt})

            response = self.anthropic_client.messages.create(
                model="claude-3-opus-20240229",
                max_tokens=3000,  # Increased for more detailed responses
                system=system_prompt,
                messages=messages,
            )

            # Parse JSON response
            response_text = response.content[0].text
            try:
                return json.loads(response_text)
            except json.JSONDecodeError:
                # If JSON parsing fails, try to extract JSON from response
                return self._extract_json_from_response(response_text)

        except Exception as e:
            logger.error(f"Error calling Anthropic API: {str(e)}")
            return {"error": str(e)}

    async def _call_gemini_api_with_system(
        self, system_prompt: str, user_prompt: str, image_data: Optional[str] = None
    ) -> Dict[str, Any]:
        """Call Gemini API with system and user prompts"""
        try:
            prompt_parts = [user_prompt]
            if image_data:
                image_bytes = base64.b64decode(image_data)
                img = Image.open(io.BytesIO(image_bytes))
                prompt_parts.insert(0, img)

            response = self.gemini_client.generate_content(prompt_parts)
            response_text = response.text

            try:
                return json.loads(response_text)
            except json.JSONDecodeError:
                return self._extract_json_from_response(response_text)

        except Exception as e:
            logger.error(f"Error calling Gemini API: {str(e)}")
            return {"error": str(e)}

    async def _call_openai_api_with_system(
        self, system_prompt: str, user_prompt: str, image_data: Optional[str] = None
    ) -> Dict[str, Any]:
        """Call OpenAI API with system and user prompts"""
        try:
            messages = [{"role": "system", "content": system_prompt}]

            if image_data:
                # Include image in the message
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{image_data}"
                                },
                            },
                        ],
                    }
                )
            else:
                messages.append({"role": "user", "content": user_prompt})

            response = self.openai_client.chat.completions.create(
                model="gpt-4-turbo" if image_data else "gpt-4",
                messages=messages,
                max_tokens=3000,  # Increased for more detailed responses
            )

            # Parse JSON response
            response_text = response.choices[0].message.content
            try:
                return json.loads(response_text)
            except json.JSONDecodeError:
                # If JSON parsing fails, try to extract JSON from response
                return self._extract_json_from_response(response_text)

        except Exception as e:
            logger.error(f"Error calling OpenAI API: {str(e)}")
            return {"error": str(e)}

    def _extract_json_from_response(self, response_text: str) -> Dict[str, Any]:
        """Try to extract JSON from a response that may contain additional text"""
        try:
            # Look for JSON blocks in the response
            import re

            json_pattern = r"```json\s*(.*?)\s*```"
            matches = re.findall(json_pattern, response_text, re.DOTALL)

            if matches:
                return json.loads(matches[0])

            # Try to find JSON-like content
            start_idx = response_text.find("{")
            end_idx = response_text.rfind("}")

            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                json_str = response_text[start_idx : end_idx + 1]
                return json.loads(json_str)

            # If all else fails, return raw response
            return {"raw_response": response_text}

        except Exception as e:
            logger.error(f"Error extracting JSON from response: {str(e)}")
            return {"raw_response": response_text, "extraction_error": str(e)}

    def _extract_mappings_from_analysis(
        self, ai_analysis: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Extracts keyword mappings from the analysis result."""
        if not isinstance(ai_analysis, dict):
            return []

        all_mappings = []

        # Look in the top level
        if "keyword_mappings" in ai_analysis and isinstance(
            ai_analysis["keyword_mappings"], list
        ):
            all_mappings.extend(ai_analysis["keyword_mappings"])

        # Look in the nested taxonomy_keywords block (for modular analysis)
        if "taxonomy_keywords" in ai_analysis and isinstance(
            ai_analysis["taxonomy_keywords"], dict
        ):
            nested_mappings = ai_analysis["taxonomy_keywords"].get(
                "keyword_mappings", []
            )
            if isinstance(nested_mappings, list):
                all_mappings.extend(nested_mappings)

        return all_mappings

    def _extract_keywords_from_analysis(
        self, ai_analysis: Dict[str, Any]
    ) -> Tuple[List[str], List[str]]:
        """Extract keywords and categories from AI analysis with taxonomy validation"""
        keywords = []
        categories = []
        if isinstance(ai_analysis, dict):
            # Handle keyword mappings with taxonomy validation
            if "keyword_mappings" in ai_analysis:
                mappings = ai_analysis["keyword_mappings"]
                if isinstance(mappings, list):
                    for mapping in mappings:
                        if isinstance(mapping, dict):
                            verbatim_term = mapping.get("verbatim_term")
                            canonical_term = mapping.get("mapped_canonical_term")
                            primary_category = mapping.get("mapped_primary_category")
                            if verbatim_term:
                                keywords.append(verbatim_term)
                            if canonical_term:
                                keywords.append(canonical_term)
                            if primary_category:
                                categories.append(primary_category)
            # Handle modular analysis format
            if "taxonomy_keywords" in ai_analysis:
                taxonomy_data = ai_analysis["taxonomy_keywords"]
                if (
                    isinstance(taxonomy_data, dict)
                    and "keyword_mappings" in taxonomy_data
                ):
                    mappings = taxonomy_data["keyword_mappings"]
                    if isinstance(mappings, list):
                        for mapping in mappings:
                            if isinstance(mapping, dict):
                                verbatim_term = mapping.get("verbatim_term")
                                canonical_term = mapping.get("mapped_canonical_term")
                                primary_category = mapping.get(
                                    "mapped_primary_category"
                                )

                                if verbatim_term:
                                    keywords.append(verbatim_term)
                                if canonical_term:
                                    keywords.append(canonical_term)
                                if primary_category:
                                    categories.append(primary_category)

            # Handle classification format
            if "classification" in ai_analysis:
                classification = ai_analysis["classification"]
                if isinstance(classification, dict):
                    if "category" in classification:
                        categories.append(classification["category"])
                    if (
                        "subcategory" in classification
                        and classification["subcategory"]
                    ):
                        keywords.append(classification["subcategory"])

            # Handle entities format
            if "entities" in ai_analysis:
                entities = ai_analysis["entities"]
                if isinstance(entities, dict):
                    if "client_name" in entities and entities["client_name"]:
                        keywords.append(entities["client_name"])
                    if "opponent_name" in entities and entities["opponent_name"]:
                        keywords.append(entities["opponent_name"])

            # Handle modular analysis format
            if "taxonomy_keywords" in ai_analysis:
                taxonomy_data = ai_analysis["taxonomy_keywords"]
                if (
                    isinstance(taxonomy_data, dict)
                    and "keyword_mappings" in taxonomy_data
                ):
                    mappings = taxonomy_data["keyword_mappings"]
                    if isinstance(mappings, list):
                        for mapping in mappings:
                            if isinstance(mapping, dict):
                                if (
                                    "verbatim_term" in mapping
                                    and mapping["verbatim_term"]
                                ):
                                    keywords.append(mapping["verbatim_term"])
                                if (
                                    "mapped_canonical_term" in mapping
                                    and mapping["mapped_canonical_term"]
                                ):
                                    keywords.append(mapping["mapped_canonical_term"])
                                if (
                                    "mapped_primary_category" in mapping
                                    and mapping["mapped_primary_category"]
                                ):
                                    categories.append(
                                        mapping["mapped_primary_category"]
                                    )

            # Handle communication focus
            if "communication_focus" in ai_analysis:
                comm_focus = ai_analysis["communication_focus"]
                if isinstance(comm_focus, dict):
                    if "primary_issue" in comm_focus and comm_focus["primary_issue"]:
                        keywords.append(comm_focus["primary_issue"])
                    if "messaging_strategy" in comm_focus:
                        categories.append(comm_focus["messaging_strategy"])

            # Legacy format support
            if "keywords" in ai_analysis and isinstance(ai_analysis["keywords"], list):
                keywords.extend(ai_analysis["keywords"])
            elif "key_topics" in ai_analysis and isinstance(
                ai_analysis["key_topics"], list
            ):
                keywords.extend(ai_analysis["key_topics"])

            if "categories" in ai_analysis and isinstance(
                ai_analysis["categories"], list
            ):
                categories.extend(ai_analysis["categories"])

        # Remove duplicates and None values
        keywords = list(set([k for k in keywords if k and isinstance(k, str)]))
        categories = list(set([c for c in categories if c and isinstance(c, str)]))

        return keywords, categories

    async def _validate_keyword_mappings(
        self, keyword_mappings: List[Dict[str, str]]
    ) -> List[Dict[str, str]]:
        """Validate and enrich keyword mappings against canonical taxonomy"""
        validated_mappings = []
        for mapping in keyword_mappings:
            canonical_term = mapping.get("mapped_canonical_term")
            if not canonical_term:
                continue
            # Get the full hierarchy for the canonical term
            hierarchy = await self.taxonomy_service.get_term_hierarchy(canonical_term)
            if hierarchy:
                enriched_mapping = {
                    "verbatim_term": mapping.get("verbatim_term"),
                    "mapped_canonical_term": hierarchy["term"],
                    "mapped_primary_category": hierarchy["primary_category"],
                    "mapped_subcategory": hierarchy["subcategory"],
                }
                validated_mappings.append(enriched_mapping)
            else:
                logger.warning(
                    f"Skipped invalid taxonomy mapping for term: {canonical_term}"
                )
        return validated_mappings

    async def generate_embeddings(self, text: str) -> Optional[List[float]]:
        """Generate embeddings for text using the configured AI provider."""
        try:
            if self.ai_provider == "openai" and self.openai_client:
                response = self.openai_client.embeddings.create(
                    model="text-embedding-3-large",
                    input=text,
                )
                return response.data[0].embedding
            elif self.ai_provider == "gemini" and self.gemini_client:
                # Use Gemini for embeddings
                response = genai.embed_content(
                    model="models/embedding-001",
                    content=text,
                    task_type="retrieval_document",
                )
                return response["embedding"]
            else:
                logger.warning(
                    f"Embeddings not supported for the '{self.ai_provider}' provider."
                )
                return None
        except Exception as e:
            logger.error(
                f"Error generating embeddings with {self.ai_provider}: {str(e)}"
            )
            return None

    def get_ai_info(self) -> Dict[str, Any]:
        """Get information about AI configuration"""
        return {
            "ai_provider": self.ai_provider,
            "anthropic_available": self.anthropic_client is not None,
            "openai_available": self.openai_client is not None,
            "gemini_available": self.gemini_client is not None,
            "supports_vision": True,
            "supports_embeddings": self.openai_client is not None
            or self.gemini_client is not None,
            "prompt_manager_enabled": True,
            "available_analysis_types": [
                "unified",
                "modular",
                "metadata",
                "classification",
                "entities",
                "text",
                "design",
                "keywords",
                "communication",
            ],
        }

    def get_available_analysis_types(self) -> List[str]:
        """Get list of available analysis types"""
        return [
            "unified",  # Single comprehensive analysis
            "modular",  # Multiple specialized analyses
            "metadata",  # Core metadata only
            "classification",  # Classification only
            "entities",  # Entity extraction only
            "text",  # Text extraction only
            "design",  # Design elements only
            "keywords",  # Taxonomy keywords only
            "communication",  # Communication focus only
        ]

    async def analyze_text_chunk(
        self, text_chunk: str, filename: str, analysis_type: str = "unified"
    ) -> Dict[str, Any]:
        """
        Run AI analysis on a chunk of text.
        This is a lightweight version of the main analysis pipeline.
        """
        try:
            if analysis_type == "unified":
                prompt_data = await self.prompt_manager.get_unified_analysis_prompt(
                    filename
                )
                enhanced_prompt = self._enhance_prompt_with_text(
                    prompt_data["user"], text_chunk
                )
                analysis_result = await self._call_anthropic_api_with_system(
                    prompt_data["system"], enhanced_prompt
                )
            else:
                # For simplicity, this example only implements the 'unified' chunk analysis
                logger.warning(
                    f"Analysis type '{analysis_type}' not fully supported for chunked processing. Using fallback."
                )
                analysis_result = {"summary": text_chunk[:100]}  # Basic fallback

            return analysis_result
        except Exception as e:
            logger.error(f"Error analyzing text chunk for {filename}: {str(e)}")
            return {"error": str(e)}

    def analyze_document_sync(
        self, file_path: str, filename: str, analysis_type: str = "unified"
    ) -> Dict[str, Any]:
        """Synchronous version of analyze_document"""
        return asyncio.run(self.analyze_document(file_path, filename, analysis_type))

    def generate_embeddings_sync(self, text: str) -> Optional[List[float]]:
        """Synchronous version of generate_embeddings"""
        return asyncio.run(self.generate_embeddings(text))

    def extract_text_from_pdf_sync_generator(
        self, file_content: bytes
    ) -> Generator[Tuple[int, str], None, None]:
        """Synchronous generator for extracting text from PDF pages."""

        async def get_all_pages():
            return [
                page
                async for page in self._extract_text_from_pdf_generator(file_content)
            ]

        try:
            pages = asyncio.run(get_all_pages())
            for page in pages:
                yield page
        except Exception as e:
            logger.error(f"Error in sync PDF generator: {e}")
            # Yield nothing if there's an error
            return

    def analyze_text_chunk_sync(
        self, text_chunk: str, filename: str, analysis_type: str = "unified"
    ) -> Dict[str, Any]:
        """Synchronous version of analyze_text_chunk."""
        return asyncio.run(self.analyze_text_chunk(text_chunk, filename, analysis_type))
