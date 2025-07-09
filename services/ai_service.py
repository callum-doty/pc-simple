"""
AI service - handles document analysis using LLM APIs
Consolidates OCR, text extraction, and AI analysis into a single service
Now integrated with PromptManager for sophisticated analysis
"""

import asyncio
import logging
from typing import Dict, Any, List, Optional, Tuple
import json
import base64
from pathlib import Path
import io
from PIL import Image
import pytesseract
import fitz  # PyMuPDF
import anthropic
import openai
import google.generativeai as genai

from config import get_settings
from services.storage_service import StorageService
from services.taxonomy_service import TaxonomyService
from services.prompt_manager import PromptManager

logger = logging.getLogger(__name__)
settings = get_settings()


class AIService:
    """Unified AI service for document analysis with PromptManager integration"""

    def __init__(self):
        self.storage_service = StorageService()
        self.taxonomy_service = TaxonomyService()
        self.prompt_manager = PromptManager()

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
                # Try different initialization approaches for OpenAI
                try:
                    self.openai_client = openai.OpenAI(api_key=settings.openai_api_key)
                except TypeError as te:
                    # If there's a TypeError, try with minimal parameters
                    logger.info(
                        f"Trying alternative OpenAI initialization due to: {te}"
                    )
                    self.openai_client = openai.Client(api_key=settings.openai_api_key)
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
            # Get the unified analysis prompt
            prompt_data = self.prompt_manager.get_unified_analysis_prompt(filename)

            # Prepare image data if it's an image or PDF
            image_data = None
            if file_type in ["image", "pdf"]:
                image_data = self._prepare_image_data(file_content, file_type)

            # Add extracted text to the prompt
            enhanced_prompt = self._enhance_prompt_with_text(
                prompt_data["user"], extracted_text
            )

            # Call the AI service
            if self.anthropic_client:
                return await self._call_anthropic_api_with_system(
                    prompt_data["system"], enhanced_prompt, image_data
                )
            elif self.ai_provider == "none":
                return self._get_fallback_analysis(filename, file_type)
            else:
                raise ValueError(
                    f"Anthropic client not available for unified analysis."
                )

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
                prompt_data = self.prompt_manager.get_taxonomy_keyword_prompt(filename)
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

    async def _extract_text_from_pdf(self, file_content: bytes) -> str:
        """Extract text from PDF using PyMuPDF"""
        try:
            doc = fitz.open(stream=file_content, filetype="pdf")
            text_parts = []

            for page_num in range(doc.page_count):
                page = doc[page_num]
                text = page.get_text()
                if text.strip():
                    text_parts.append(f"--- Page {page_num + 1} ---\n{text}")
                else:
                    # If no text found, try OCR on page image
                    pix = page.get_pixmap()
                    img_data = pix.tobytes("png")
                    ocr_text = await self._extract_text_from_image(img_data)
                    if ocr_text.strip():
                        text_parts.append(
                            f"--- Page {page_num + 1} (OCR) ---\n{ocr_text}"
                        )

            doc.close()
            return "\n\n".join(text_parts)

        except Exception as e:
            logger.error(f"Error extracting text from PDF: {str(e)}")
            return ""

    async def _extract_text_from_image(self, file_content: bytes) -> str:
        """Extract text from image using OCR"""
        try:
            # Convert bytes to PIL Image
            image = Image.open(io.BytesIO(file_content))

            # Perform OCR
            text = pytesseract.image_to_string(image)
            return text.strip()

        except Exception as e:
            logger.error(f"Error extracting text from image: {str(e)}")
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
                model="claude-3-sonnet-20240229",
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
                model="gpt-4-vision-preview" if image_data else "gpt-4",
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

    def _extract_keywords_from_analysis(
        self, ai_analysis: Dict[str, Any]
    ) -> Tuple[List[str], List[str]]:
        """Extract keywords and categories from AI analysis"""
        keywords = []
        categories = []

        if isinstance(ai_analysis, dict):
            # Handle unified analysis format
            if "document_analysis" in ai_analysis:
                doc_analysis = ai_analysis["document_analysis"]
                if isinstance(doc_analysis, dict):
                    if "document_type" in doc_analysis:
                        categories.append(doc_analysis["document_type"])
                    if "campaign_type" in doc_analysis:
                        categories.append(doc_analysis["campaign_type"])
                    if "document_tone" in doc_analysis:
                        categories.append(doc_analysis["document_tone"])

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

            # Handle taxonomy keywords format
            if "keyword_mappings" in ai_analysis:
                mappings = ai_analysis["keyword_mappings"]
                if isinstance(mappings, list):
                    for mapping in mappings:
                        if isinstance(mapping, dict):
                            if "verbatim_term" in mapping and mapping["verbatim_term"]:
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
                                categories.append(mapping["mapped_primary_category"])

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

    async def generate_embeddings(self, text: str) -> Optional[List[float]]:
        """Generate embeddings for text (simplified version)"""
        try:
            if self.ai_provider == "openai" and self.openai_client:
                response = self.openai_client.embeddings.create(
                    model="text-embedding-ada-002",
                    input=text[:8000],  # Limit text length
                )
                return response.data[0].embedding
            else:
                # For now, return None if not using OpenAI
                # In a full implementation, you could use other embedding services
                logger.warning("Embeddings only supported with OpenAI provider")
                return None

        except Exception as e:
            logger.error(f"Error generating embeddings: {str(e)}")
            return None

    def get_ai_info(self) -> Dict[str, Any]:
        """Get information about AI configuration"""
        return {
            "ai_provider": self.ai_provider,
            "anthropic_available": self.anthropic_client is not None,
            "openai_available": self.openai_client is not None,
            "gemini_available": self.gemini_client is not None,
            "supports_vision": True,
            "supports_embeddings": self.openai_client is not None,
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
