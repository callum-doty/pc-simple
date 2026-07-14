# simplified_app/services/prompt_manager.py
import os
import json

from services.taxonomy_service import TaxonomyService


class PromptManager:
    """Manager for document analysis prompts with dynamic taxonomy injection"""

    # Bump this whenever the wording of any prompt template in this class
    # changes in a way that could plausibly change extracted data (not just
    # cosmetic edits) — e.g. changing the taxonomy instructions, the output
    # schema, or the analysis instructions. This is deliberately coarse (one
    # version for the whole class, not per prompt method): the "unified"
    # prompt is the only one exercised by the normal document pipeline, and
    # the modular/specific prompts are reachable only via manual admin
    # reprocessing, so a single counter is enough to flag "something in the
    # prompt set changed" for downstream data analysis without the overhead
    # of tracking each of the ~8 prompt methods independently.
    #
    # Changelog:
    #   1 — initial versioned baseline (2026-07-14). No prompt wording changed
    #       when this was introduced; this just establishes the starting point.
    PROMPT_VERSION = 1

    def __init__(self, taxonomy_service=None):
        self.model_capabilities = self._get_model_capabilities()
        self.base_system_prompt = """You are an expert document analyzer specializing in political and campaign materials. 
Provide accurate, objective analysis in the exact JSON format requested."""
        self.taxonomy_service = taxonomy_service

    def _get_model_capabilities(self):
        """Get capabilities based on configured model"""
        model = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

        if "claude-sonnet-4" in model:
            return {"vision": True, "structure": "high", "detail": "high"}
        elif "claude-3-sonnet" in model:
            return {"vision": True, "structure": "medium", "detail": "medium"}
        else:
            return {"vision": True, "structure": "basic", "detail": "basic"}

    async def _get_canonical_taxonomy(self):
        """Get canonical taxonomy structure from database"""
        if not self.taxonomy_service:
            # Fallback to empty structure if no taxonomy service
            return {}

        try:
            # Get the hierarchical taxonomy structure
            hierarchy = await self.taxonomy_service.get_taxonomy_hierarchy()

            # Convert to the format needed for prompts
            canonical_structure = {}
            for primary_category, subcategories in hierarchy.items():
                canonical_structure[primary_category] = {}
                for subcategory, terms in subcategories.items():
                    # Extract just the term names for the prompt
                    term_names = (
                        [term["term"] for term in terms]
                        if isinstance(terms, list)
                        else []
                    )
                    canonical_structure[primary_category][subcategory] = term_names

            return canonical_structure
        except Exception as e:
            print(f"Error getting taxonomy: {e}")
            return {}

    async def get_unified_analysis_prompt(self, filename):
        """
        A single, robust prompt that uses chain-of-thought to improve accuracy
        and combines metadata, classification, and keyword extraction.
        """
        # Get the canonical taxonomy dynamically
        canonical_taxonomy_structure = await self._get_canonical_taxonomy()
        taxonomy_for_prompt = json.dumps(canonical_taxonomy_structure, indent=2)

        return {
            "system": self.base_system_prompt,
            "user": f"""
Analyze the document '{filename}'. Think through the following internally before writing your response — do NOT output your reasoning, only the final JSON.

Internal checklist (do not output):
- Summary: what is the document's core message?
- Document type: what visual/text clues indicate the type?
- Election year: is there a date or phrase like "Vote on November 5th"?
- Tone: what words establish the tone?
- Category: what is the main goal?
- Verbatim keyphrases: identify 10-15 important phrases used in the document.
- Map each keyphrase to the single most relevant canonical term from the taxonomy below.

**Official Canonical Taxonomy:**
```json
{taxonomy_for_prompt}
```

Output ONLY the following JSON — no explanation, no markdown, no text before or after it:

{{
  "document_analysis": {{
    "summary": "Clear 1-2 sentence overview of the document's purpose.",
    "document_type": "Choose ONLY from: ['mailer', 'digital ad', 'handout', 'poster', 'letter', 'brochure']",
    "campaign_type": "Choose ONLY from: ['primary', 'general', 'special', 'runoff']",
    "election_year": "The four-digit election year (e.g., 2024). MUST be null if not found.",
    "document_tone": "Choose ONLY from: ['positive', 'negative', 'neutral', 'informational', 'contrast']"
  }},
  "classification": {{
    "category": "Choose ONLY from: ['GOTV', 'attack', 'comparison', 'endorsement', 'issue', 'biographical']",
    "subcategory": "A specific, one-to-three word description of the narrower topic (e.g., 'Taxes', 'Healthcare Policy'). MUST be null if not applicable.",
    "rationale": "One sentence justifying the category choice."
  }},
  "entities": {{
    "client_name": "Full name of the client/candidate. MUST be null if not mentioned.",
    "opponent_name": "Full name of any opponent mentioned. MUST be null if none.",
    "creation_date": "Creation or print date (YYYY-MM-DD). MUST be null if not visible."
  }},
  "keyword_mappings": [
    {{
      "verbatim_term": "The exact phrase from the document, e.g., 'universal background checks'",
      "mapped_primary_category": "The primary category from the official taxonomy, e.g., 'Policy Issues & Topics'",
      "mapped_subcategory": "The subcategory from the official taxonomy, e.g., 'Public Safety & Justice'",
      "mapped_canonical_term": "The canonical term from the official taxonomy, e.g., 'Guns/Gun Control'"
    }}
  ]
}}

Rules: if you cannot find evidence for a field its value MUST be null. For enum fields use only the listed options. Output raw JSON only — no code fences, no extra text.
""",
        }

    async def get_taxonomy_keyword_prompt(self, filename, metadata=None):
        """Generate a prompt for hierarchical taxonomy keyword extraction with dynamic taxonomy injection"""
        context = ""
        if metadata:
            context = f"""Based on prior analysis, this is a {metadata.get('document_type', '')} 
from {metadata.get('election_year', '')} that appears to be {metadata.get('document_tone', '')}.
"""

        # Get the canonical taxonomy dynamically
        canonical_taxonomy_structure = await self._get_canonical_taxonomy()
        taxonomy_for_prompt = json.dumps(canonical_taxonomy_structure, indent=2)

        return {
            "system": self.base_system_prompt,
            "user": f"""
{context}Analyze the document '{filename}' and perform the following three steps:

**Step 1: Extract Verbatim Keyphrases**
Identify 10-15 of the most important and specific keywords or keyphrases mentioned in the document. These should be the exact phrases used.

**Step 2: Map to Canonical Taxonomy**
For each verbatim keyphrase you extracted, map it to the single most relevant canonical term from the official taxonomy provided below.

**Official Canonical Taxonomy:**
```json
{taxonomy_for_prompt}
```

**Step 3: Generate JSON Output**
Provide your response ONLY in the following JSON format. For each verbatim term, provide its mapping to a primary category, subcategory, and the specific canonical term.

```json
{{
  "keyword_mappings": [
    {{
      "verbatim_term": "The exact phrase from the document, e.g., 'universal background checks'",
      "mapped_primary_category": "The primary category from the official taxonomy, e.g., 'Policy Issues & Topics'",
      "mapped_subcategory": "The subcategory from the official taxonomy, e.g., 'Public Safety & Justice'",
      "mapped_canonical_term": "The canonical term from the official taxonomy, e.g., 'Guns/Gun Control'"
    }},
    {{
      "verbatim_term": "The exact phrase from the document, e.g., 'a 15% flat tax'",
      "mapped_primary_category": "Policy Issues & Topics",
      "mapped_subcategory": "Economy & Taxes", 
      "mapped_canonical_term": "Taxes"
    }}
  ]
}}
```

**CRITICAL REQUIREMENTS:**
- You MUST extract 10-15 verbatim terms from the document
- You MUST only use categories and terms that exist in the provided taxonomy
- If you cannot find a good match in the taxonomy, use the closest available term
- Every verbatim_term MUST be an exact phrase from the document

Your response MUST be valid JSON formatted exactly as requested above.
""",
        }

    def get_core_metadata_prompt(self, filename):
        """Generate a prompt for core metadata extraction"""
        return {
            "system": self.base_system_prompt,
            "user": f"""
Analyze the document '{filename}' and extract only the core metadata.

Return ONLY the following JSON. If a value is not found, it MUST be null.

{{
  "document_analysis": {{
    "summary": "Clear 1-2 sentence overview of the document's purpose.",
    "document_type": "Choose ONLY from: ['mailer', 'digital ad', 'handout', 'poster', 'letter', 'brochure']",
    "campaign_type": "Choose ONLY from: ['primary', 'general', 'special', 'runoff']",
    "election_year": "The four-digit election year (e.g., 2024). MUST be null if not found.",
    "document_tone": "Choose ONLY from: ['positive', 'negative', 'neutral', 'informational', 'contrast']"
  }}
}}

Your response MUST be valid JSON formatted exactly as requested above.
""",
        }

    def get_classification_prompt(self, filename, metadata=None):
        """Generate a prompt for document classification"""
        context = ""
        if metadata:
            context = f"""Based on prior analysis, this is a {metadata.get('document_type', '')} 
from {metadata.get('election_year', '')} that appears to be {metadata.get('document_tone', '')}.
"""

        return {
            "system": self.base_system_prompt,
            "user": f"""{context}Analyze the document '{filename}' and classify it.

Return ONLY the following JSON. If a value is not found, it MUST be null.

{{
  "classification": {{
    "category": "Choose ONLY from: ['GOTV', 'attack', 'comparison', 'endorsement', 'issue', 'biographical']",
    "subcategory": "A specific, one-to-three word description of the narrower topic (e.g., 'Taxes', 'Healthcare Policy'). MUST be null if not applicable.",
    "rationale": "Briefly justify the category choice."
  }}
}}

Your response MUST be valid JSON formatted exactly as requested above.
""",
        }

    def get_entity_prompt(self, filename, metadata=None):
        """Generate a prompt for entity extraction"""
        context = ""
        if metadata:
            context = f"""Based on prior analysis, this is a {metadata.get('document_type', '')} 
from {metadata.get('election_year', '')} that appears to be {metadata.get('document_tone', '')}.
"""

        return {
            "system": self.base_system_prompt,
            "user": f"""{context}Analyze the document '{filename}' and extract entity information.

Return ONLY the following JSON. If a value is not found, it MUST be null.

{{
  "entities": {{
    "client_name": "Full name of the client/candidate. If not mentioned, this value MUST be null.",
    "opponent_name": "Full name of any opponent mentioned. If no opponent is mentioned, this value MUST be null.",
    "creation_date": "The creation or print date shown on the document (YYYY-MM-DD format). If no date is visible, this value MUST be null.",
    "survey_question": "Any survey questions shown. If not applicable, this value MUST be null.",
    "file_identifier": "Any naming convention or identifier visible in the document. If not applicable, this value MUST be null."
  }}
}}

Your response MUST be valid JSON formatted exactly as requested above.
""",
        }

    def get_text_extraction_prompt(self, filename, metadata=None):
        """Generate a prompt for text extraction"""
        context = ""
        if metadata:
            context = f"""Based on prior analysis, this is a {metadata.get('document_type', '')} 
from {metadata.get('election_year', '')}.
"""

        return {
            "system": self.base_system_prompt,
            "user": f"""{context}Analyze the document '{filename}' and extract the text content.

Return ONLY the following JSON. If a value is not found, it MUST be null.

{{
  "extracted_text": {{
    "main_message": "Primary headline/slogan as a single string. MUST be null if not found.",
    "supporting_text": "Secondary messages as a single string. MUST be null if not found.",
    "call_to_action": "Specific voter instruction if present (e.g., 'Vote on Nov 8'). MUST be null if not found."
  }}
}}

Your response MUST be valid JSON formatted exactly as requested above.
""",
        }

    def get_design_elements_prompt(self, filename, metadata=None):
        """Generate a prompt for design element analysis"""
        context = ""
        if metadata:
            context = f"""Based on prior analysis, this is a {metadata.get('document_type', '')} 
from {metadata.get('election_year', '')}.
"""

        return {
            "system": self.base_system_prompt,
            "user": f"""{context}Analyze the visual design elements in the document '{filename}'.

Return ONLY the following JSON. If a value is not found, it MUST be null.

{{
  "design_elements": {{
    "color_scheme": "List of up to three primary colors (e.g., ['#FF0000', '#0000FF', '#FFFFFF']). MUST be null if not applicable.",
    "theme": "Choose ONLY from: ['patriotic', 'conservative', 'progressive', 'modern', 'traditional', 'corporate']. MUST be null if not applicable.",
    "mail_piece_type": "Choose ONLY from: ['postcard', 'letter', 'brochure', 'door hanger', 'digital ad', 'poster']. MUST be null if not applicable.",
    "geographic_location": "City, State or State only. MUST be null if not found.",
    "target_audience": "Specific demographic focus (e.g., 'republicans', 'democrats', 'veterans'). MUST be null if not applicable.",
    "campaign_name": "Candidate and position sought (e.g., 'Smith for Senate'). MUST be null if not applicable.",
    "visual_elements": "List of key visual elements (e.g., ['flag', 'candidate photo', 'family']). MUST be null if not applicable."
  }}
}}

Your response MUST be valid JSON formatted exactly as requested above.
""",
        }

    def get_communication_focus_prompt(self, filename, metadata=None):
        """Generate a prompt for communication focus analysis"""
        context = ""
        if metadata:
            context = f"""Based on prior analysis, this is a {metadata.get('document_type', '')} 
from {metadata.get('election_year', '')} that appears to be {metadata.get('document_tone', '')}.
"""

        return {
            "system": self.base_system_prompt,
            "user": f"""{context}Analyze the document '{filename}' and determine its primary communication focus and strategy.

Return ONLY the following JSON. If a value is not found, it MUST be null.

{{
  "communication_focus": {{
    "primary_issue": "The main policy issue or focus of the communication. MUST be null if not applicable.",
    "secondary_issues": "List of other issues mentioned. MUST be null if not applicable.",
    "messaging_strategy": "Choose ONLY from: ['attack', 'positive', 'comparison', 'biographical', 'endorsement', 'GOTV', 'informational']",
    "audience_persuasion": "Describe how the document attempts to persuade its audience. MUST be null if not applicable."
  }}
}}

Your response MUST be valid JSON formatted exactly as requested above.
""",
        }
