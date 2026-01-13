"""
Gemini AI Optimization Service (Professional Refactor)
Legacy SDK: google.generativeai (Migrating logic from TypeScript reference)
"""
import json
import time
import re
import warnings
import google.generativeai as genai
from typing import Any, Dict, List, Optional

# Suppress deprecation warnings until we fully migrate to google.genai
warnings.filterwarnings('ignore', message='.*google.generativeai.*')

class GeminiOptimizer:
    """
    Professional eBay Listing Optimizer for AquaVerve.
    Implements:
    - AIDA Marketing Model
    - 3-Strategy Title Generation (Traffic, Conversion, Bargain)
    - Oversize Shipping Logic
    - Strict HTML/CSS Styling
    - AquaVerve Branding
    """

    # Policy footer removed to save characters for product content

    def __init__(self, api_key: str, model: str = "models/gemini-2.5-flash"):
        genai.configure(api_key=api_key)
        # Using models/gemini-2.5-flash (verified working model)
        self.model_name = model
        self.model = genai.GenerativeModel(self.model_name)


    def optimize_product_full(
        self,
        original_title: str,
        original_description: str,
        attributes: Dict[str, str],
        images: List[str]
    ) -> Dict[str, Any]:
        """
        Main pipeline:
        1. Parse & Detect Oversize
        2. Build Context
        3. Generate Content (Titles, Specs, HTML)
        4. Post-process & Format
        """
        
        # 1. Oversize Logic
        is_oversize = self._check_oversize_logic(attributes, original_description)
        shipping_context = self._get_shipping_context(is_oversize)

        # 2. Build Prompt
        prompt = self._build_professional_prompt(
            original_title, 
            original_description, 
            attributes, 
            shipping_context
        )

        # 3. Generate with Schema
        try:
            generated_data = self._generate_structured_content(prompt)
        except Exception as e:
            print(f"⚠️ AI Generation Error: {e}")
            return self._fallback_response(original_title, original_description, attributes)

        # 4. Post-Processing
        
        # Handle Titles
        titles = generated_data.get("titles", {})
        best_title = titles.get("conversion", original_title)[:80] # Default to conversion
        
        # Handle Item Specifics
        specs_list = generated_data.get("itemSpecificsList", [])
        final_aspects = self._process_aspects(specs_list, is_oversize)

        # Handle HTML
        raw_html = generated_data.get("htmlDescription", "")
        final_html = self._construct_final_html(raw_html, images, best_title)

        return {
            "title": best_title,
            "titles_all": titles,
            "description": final_html,
            "aspects": final_aspects,
            "recommendedCategory": generated_data.get("recommendedCategory", "Other"),
            "mobileSummary": generated_data.get("mobileSummary", ""),
            "optimization_data": generated_data # Store full raw data
        }

    def optimize_title(
        self,
        original_title: str,
        category: str = "",
        max_length: int = 80,
        search_results: list = None
    ) -> str:
        """
        Simplified title optimization for existing listings
        Compatible with optimize_existing_listings.py
        """
        try:
            # Build market research context if available
            market_context = ""
            if search_results:
                market_context = "\n\nMARKET RESEARCH (Hot Keywords):\n"
                for i, result in enumerate(search_results[:5], 1):
                    market_context += f"{i}. {result.get('title', '')}\n"
                market_context += "\nUse these trending keywords to improve SEO visibility.\n"
            
            prompt = f"""You are an eBay SEO expert. Create an optimized product title that will maximize sales and search visibility.

Original Title: "{original_title}"
Category: {category}{market_context}

CRITICAL REQUIREMENTS:
1. Length: MUST be EXACTLY 75-80 characters (count every character!)
2. Include ALL relevant keywords from the original title
3. Incorporate trending keywords from market research (if provided)
4. Natural, grammatically correct English
5. NO promotional words like "Free Shipping", "Sale", "New"

EXAMPLES (study these patterns):

Example 1:
Input: "Stainless steel water bottle | 32oz insulated"
Output: "Stainless Steel Insulated Water Bottle 32oz Double Wall Vacuum Thermos" (75 chars)

Example 2:
Input: "Wooden bookshelf | 5 tier storage rack"
Output: "Wooden 5-Tier Bookshelf Storage Rack Display Stand Organizer Home Office" (78 chars)

Example 3:
Input: "LED desk lamp | Adjustable brightness USB"
Output: "LED Desk Lamp Adjustable Brightness USB Rechargeable Reading Light Office" (77 chars)

Now optimize this title following the EXACT same pattern. Count characters to ensure 75-80!

Return ONLY the optimized title, nothing else."""

            response = self.model.generate_content(
                prompt,
                generation_config={"temperature": 0.9, "max_output_tokens": 200}
            )
            
            optimized = response.text.strip().strip('"').strip()
            
            # Ensure within limit
            if len(optimized) > max_length:
                optimized = optimized[:max_length].rsplit(' ', 1)[0]
            
            # Clean trailing punctuation
            optimized = optimized.strip(" ,.-;:")
            
            # If too short, retry with even stricter prompt
            if len(optimized) < 75:
                print(f"   [RETRY] First attempt too short ({len(optimized)} chars), retrying...")
                
                retry_prompt = f"""CRITICAL: Previous attempt was TOO SHORT ({len(optimized)} chars). You MUST create EXACTLY 75-80 characters.

Original: "{original_title}"
Previous: "{optimized}" (REJECTED - too short)

STUDY THESE EXAMPLES (75-80 chars):
1. "Wooden Multifunctional Dog House & Bookshelf Display Stand Small Pet Furniture" (79 chars)
2. "Stainless Steel Insulated Water Bottle 32oz Double Wall Vacuum Thermos Flask" (78 chars)
3. "LED Desk Lamp Adjustable Brightness USB Rechargeable Reading Light for Office" (79 chars)

Generate a NEW title matching this length. Count carefully: 75-80 characters!"""

                retry_response = self.model.generate_content(
                    retry_prompt,
                    generation_config={"temperature": 0.9, "max_output_tokens": 200}
                )
                
                optimized = retry_response.text.strip().strip('"').strip()
            
            # Ensure within limit
            if len(optimized) > max_length:
                optimized = optimized[:max_length].rsplit(' ', 1)[0]
            
            # If still too short after retry, use smart padding
            if len(optimized) < 75:
                # Extract important keywords from original
                words = original_title.replace("|", "").split()
                optimized_lower = optimized.lower()
                
                # Add missing keywords that make sense
                for word in words:
                    if word.lower() not in optimized_lower and len(word) > 2:
                        if len(optimized) + len(word) + 1 <= max_length:
                            optimized += f" {word}"
                            if len(optimized) >= 75:
                                break
                
                # If still short, add descriptive terms
                if len(optimized) < 75:
                    terms = ["Premium", "Quality", "Durable", "Home", "Pet"]
                    for term in terms:
                        if term.lower() not in optimized.lower():
                            if len(optimized) + len(term) + 1 <= max_length:
                                optimized += f" {term}"
                                if len(optimized) >= 75:
                                    break
            
            # Clean trailing punctuation
            optimized = optimized.strip(" ,.-;:")
            
            # Final cleanup
            optimized = optimized.strip()
            
            return optimized
            
        except Exception as e:
            print(f"   [WARNING] Title optimization failed: {e}, using original")
            return original_title

    def _check_oversize_logic(self, attributes: Dict, description: str) -> bool:
        """
        Detects if item is oversize based on Dimensions and Weight.
        Rule: Combined Length + Girth > 108 OR Weight > 70 lbs.
        """
        # Try to parse from attributes first
        weight = 0.0
        length = 0.0
        width = 0.0
        height = 0.0
        
        # Helper to parse float
        def parse_float(val):
            if isinstance(val, (int, float)): return float(val)
            if isinstance(val, str):
                match = re.search(r"(\d+(\.\d+)?)", val)
                if match: return float(match.group(1))
            return 0.0

        # Attempt extraction (basic heuristic)
        # In production this should be more robust or rely on pre-parsed data
        for k, v in attributes.items():
            k_lower = k.lower()
            if "weight" in k_lower:
                w = parse_float(v)
                if w > weight: weight = w
            if "dim" in k_lower or "size" in k_lower:
                # Basic parsing if dimensions are in string "L x W x H"
                nums = re.findall(r"(\d+(\.\d+)?)", v)
                if len(nums) >= 3:
                    length, width, height = sorted([float(n[0]) for n in nums[:3]], reverse=True) # Max is length
        
        # Calculation
        girth = 2 * (width + height)
        combined = length + girth
        
        is_heavy = weight > 70
        is_large = combined > 108
        
        if is_heavy or is_large:
            print(f"📦 OVERSIZE DETECTED: Weight={weight}, Combined={combined}")
            return True
        return False

    def _get_shipping_context(self, is_oversize: bool) -> str:
        if is_oversize:
            return """
            **SHIPPING WARNING**: This item is OVERSIZE/FREIGHT.
            - Combined Length + Girth > 108 inches OR Weight > 70 lbs.
            - INSTRUCTION: You MUST add Item Specific: "Shipping Profile" = "Oversize/Freight".
            """
        else:
            return """
            Shipping: Standard size.
            - INSTRUCTION: Add Item Specific: "Shipping Profile" = "Standard".
            """

    def _build_professional_prompt(self, title: str, desc: str, attrs: Dict, shipping_context: str) -> str:
        return f"""
        Role: Expert eBay Copywriter (Cassini Algorithm Specialist) & Web Designer.
        Task: Create a high-converting, VISUALLY STUNNING eBay listing description in HTML.

        Product Info:
        - Title: {title}
        - Input Attributes: {json.dumps(attrs, ensure_ascii=False)}
        - Raw Description: {desc[:3000]}
        
        {shipping_context}

        REQUIREMENTS:
        
        1. **Category**: Identify the most appropriate eBay Category path.

        2. **Titles (Max 80 Chars)**: Provide 3 strategies (Traffic, Conversion, Bargain).
           - **Traffic**: Maximize SEO keywords (Search Volume).
           - **Conversion**: Benefit-driven, emotional triggers (e.g., "Heavy Duty", "Luxury").
           - **Bargain**: Price/Deal focused (implied value).
           - **Translation**: Provide a Chinese translation for each title.
           - **CRITICAL CONSTRAINT**: NEVER use "AquaVerve" in ANY title - we are a private label store.
           - **CRITICAL CONSTRAINT**: Titles MUST be exactly 75-80 characters (use every character for SEO).
           - Use title case, no special characters, no promotional words ("Free", "Sale", "New").

        3. **Item Specifics**: 
           - Generate a comprehensive list (Brand, Type, Material, Color, Features, Dimensions, Weight, etc.).
           - **CRITICAL**: Include the "Shipping Profile" logic defined above.
           - Normalize keys (e.g., "Material" not "Main Material").
           - Extract ALL technical specs from the product description.

        4. **Mobile Summary**: A short, text-only summary (max 800 chars) for eBay mobile view parsing.

        5. **HTML Description Design (CRITICAL - PROFESSIONAL DESIGN)**:
           - **ABSOLUTE MAXIMUM**: 3800 characters total
           - **DESIGN STYLE**: Modern, clean, professional e-commerce listing
           
           - **ENHANCED TEMPLATE** (Use THIS structure):
              ```html
              <div style="max-width:900px;margin:0 auto;font-family:'Segoe UI',Arial,sans-serif;color:#333;line-height:1.7">
              
              <!-- Hero Section with gradient background -->
              <div style="background:linear-gradient(135deg,#1a365d 0%,#2c5282 100%);color:#fff;padding:25px;border-radius:12px;margin-bottom:20px;text-align:center">
                <h1 style="margin:0;font-size:24px;font-weight:600">[Product Title - Catchy Version]</h1>
                <p style="margin:10px 0 0;opacity:0.9;font-size:14px">[One-line value proposition]</p>
              </div>
              
              <!-- Key Benefits Grid -->
              <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:15px;margin-bottom:25px">
                <div style="background:#f0fff4;border-left:4px solid #38a169;padding:15px;border-radius:0 8px 8px 0">
                  <strong style="color:#276749">✓ Benefit 1 Title</strong><br>
                  <span style="font-size:13px;color:#555">Brief explanation</span>
                </div>
                <div style="background:#ebf8ff;border-left:4px solid #3182ce;padding:15px;border-radius:0 8px 8px 0">
                  <strong style="color:#2c5282">✓ Benefit 2 Title</strong><br>
                  <span style="font-size:13px;color:#555">Brief explanation</span>
                </div>
                <div style="background:#faf5ff;border-left:4px solid #805ad5;padding:15px;border-radius:0 8px 8px 0">
                  <strong style="color:#553c9a">✓ Benefit 3 Title</strong><br>
                  <span style="font-size:13px;color:#555">Brief explanation</span>
                </div>
                <div style="background:#fffaf0;border-left:4px solid #dd6b20;padding:15px;border-radius:0 8px 8px 0">
                  <strong style="color:#c05621">✓ Benefit 4 Title</strong><br>
                  <span style="font-size:13px;color:#555">Brief explanation</span>
                </div>
              </div>
              
              <!-- Product Story -->
              <div style="background:#f7fafc;padding:20px;border-radius:12px;margin-bottom:25px">
                <h2 style="margin:0 0 12px;color:#2d3748;font-size:18px;border-bottom:2px solid #e2e8f0;padding-bottom:8px">📦 Product Details</h2>
                <p style="margin:0;color:#4a5568">[2-3 sentences about the product - what it is, who it's for, why it's great]</p>
              </div>
              
              <!-- Specifications Table -->
              <table style="width:100%;border-collapse:collapse;margin-bottom:25px;border-radius:8px;overflow:hidden">
                <tr style="background:#2c5282;color:#fff">
                  <th colspan="2" style="padding:12px;text-align:left;font-size:16px">📋 Specifications</th>
                </tr>
                <tr style="background:#f7fafc"><td style="padding:10px;border-bottom:1px solid #e2e8f0;font-weight:600;width:35%">Material</td><td style="padding:10px;border-bottom:1px solid #e2e8f0">Value</td></tr>
                <tr><td style="padding:10px;border-bottom:1px solid #e2e8f0;font-weight:600">Color</td><td style="padding:10px;border-bottom:1px solid #e2e8f0">Value</td></tr>
                <tr style="background:#f7fafc"><td style="padding:10px;border-bottom:1px solid #e2e8f0;font-weight:600">Dimensions</td><td style="padding:10px;border-bottom:1px solid #e2e8f0">Value</td></tr>
                <tr><td style="padding:10px;border-bottom:1px solid #e2e8f0;font-weight:600">Weight</td><td style="padding:10px;border-bottom:1px solid #e2e8f0">Value</td></tr>
              </table>
              
              <!-- Package Contents -->
              <div style="background:linear-gradient(135deg,#38a169 0%,#48bb78 100%);color:#fff;padding:18px;border-radius:12px;text-align:center">
                <strong style="font-size:15px">📦 Package Includes:</strong> Item 1 × 1, Hardware Kit, Assembly Instructions
              </div>
              
              </div>
              ```
           
           - **DESIGN RULES**:
              * Use modern gradient backgrounds for headers
              * Color-coded benefit cards with left border accent
              * Alternating row backgrounds in specs table
              * Professional color palette (blues, greens, purples)
              * Rounded corners (border-radius: 8-12px)
              * Good whitespace and padding
              * NO emoji overload (use sparingly: 1-2 per section)
              * Mobile-responsive grid layout
           
           - **CONTENT RULES**:
              * 4 key benefits with brief explanations
              * 2-3 sentence product narrative (compelling, not generic)
              * 6-8 specification rows (most important specs)
              * Package contents at bottom
              * NO policy/shipping info (handled separately)

        6. **Description Translation**: Provide a separate summary of the description in Chinese.

        OUTPUT JSON FORMAT:
        {{
            "recommendedCategory": "Home & Garden > ...",
            "titles": {{
                "traffic": "...",
                "conversion": "...",
                "bargain": "..."
            }},
            "titlesTranslated": {{
                "traffic": "...",
                "conversion": "...",
                "bargain": "..."
            }},
            "itemSpecificsList": [
                {{"key": "Material", "value": "Solid Wood"}},
                {{"key": "Color", "value": "Walnut"}},
                {{"key": "Shipping Profile", "value": "..."}}
            ],
            "mobileSummary": "...",
            "htmlDescription": "<div style='...'>...</div>",
            "descriptionTranslation": "..."
        }}
        """


    def _generate_structured_content(self, prompt: str) -> Dict[str, Any]:
        """Call Gemini with JSON schema enforcement"""
        
        generation_config = genai.GenerationConfig(
            temperature=0.7,
            response_mime_type="application/json",
            response_schema={
                "type": "OBJECT",
                "properties": {
                    "recommendedCategory": {"type": "STRING"},
                    "titles": {
                        "type": "OBJECT",
                        "properties": {
                            "traffic": {"type": "STRING"},
                            "conversion": {"type": "STRING"},
                            "bargain": {"type": "STRING"}
                        },
                        "required": ["traffic", "conversion", "bargain"]
                    },
                    "titlesTranslated": {
                        "type": "OBJECT",
                        "properties": {
                            "traffic": {"type": "STRING"},
                            "conversion": {"type": "STRING"},
                            "bargain": {"type": "STRING"}
                        },
                        "required": ["traffic", "conversion", "bargain"]
                    },
                    "itemSpecificsList": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "key": {"type": "STRING"},
                                "value": {"type": "STRING"}
                            },
                            "required": ["key", "value"]
                        }
                    },
                    "mobileSummary": {"type": "STRING"},
                    "htmlDescription": {"type": "STRING"},
                    "descriptionTranslation": {"type": "STRING"}
                },
                "required": ["recommendedCategory", "titles", "titlesTranslated", "itemSpecificsList", "mobileSummary", "htmlDescription", "descriptionTranslation"]
            }
        )

        response = self.model.generate_content(prompt, generation_config=generation_config)
        return json.loads(response.text)

    def _process_aspects(self, specs_list: List[Dict], is_oversize: bool) -> Dict[str, List[str]]:
        aspects = {}
        found_brand = False
        
        for item in specs_list:
            key = item.get("key", "").strip()
            value = item.get("value", "").strip()
            
            if not key or not value: continue
            
            # Filter unwanted
            if key.lower() == "brand":
                found_brand = True
                continue # We force Brand later
            
            aspects[key] = [value]

        # Force Brand
        aspects["Brand"] = ["AquaVerve"]
        
        # Enforce Oversize if missed by AI (though prompt should handle it)
        if is_oversize:
            aspects["Shipping Profile"] = ["Oversize/Freight"]
            
        return aspects

    def _construct_final_html(self, ai_html: str, images: List[str], title: str) -> str:
        
        # 1. Build Image Gallery (Top 4 Images) - Modern grid style
        gallery_html = ""
        if images:
            gallery_cells = ""
            for i, img_url in enumerate(images[:4]):
                gallery_cells += f"""
                <div style="flex:1 1 220px;padding:8px">
                    <img src="{img_url}" alt="{title[:30]} - View {i+1}" style="width:100%;height:auto;border-radius:10px;box-shadow:0 4px 15px rgba(0,0,0,0.1);border:1px solid #e2e8f0">
                </div>
                """
            gallery_html = f"""
            <div style="display:flex;flex-wrap:wrap;gap:10px;margin:20px 0 30px">
                {gallery_cells}
            </div>
            """

        # 2. Wrap Content with modern branding header
        final_doc = f"""
        <div style="font-family:'Segoe UI','Helvetica Neue',Arial,sans-serif;max-width:950px;margin:0 auto;padding:20px;color:#333;line-height:1.7;background:#fff">
            
            <!-- Elegant Brand Header -->
            <div style="text-align:center;padding:25px 0;margin-bottom:25px;border-bottom:3px solid #2c5282">
                <h1 style="margin:0;font-size:32px;font-weight:300;letter-spacing:4px;color:#1a365d">AQUAVERVE</h1>
                <p style="margin:8px 0 0;font-size:12px;letter-spacing:2px;color:#718096;text-transform:uppercase">Premium Home & Lifestyle Collection</p>
            </div>

            <!-- Image Gallery -->
            {gallery_html}

            <!-- AI Generated Content -->
            {ai_html}

            <!-- Trust Footer -->
            <div style="margin-top:30px;padding:20px;background:linear-gradient(135deg,#f7fafc 0%,#edf2f7 100%);border-radius:12px;text-align:center">
                <p style="margin:0;color:#4a5568;font-size:13px">
                    <strong style="color:#2c5282">✓ Quality Guaranteed</strong> &nbsp;|&nbsp; 
                    <strong style="color:#38a169">✓ Fast Shipping from California</strong> &nbsp;|&nbsp; 
                    <strong style="color:#805ad5">✓ Dedicated Customer Support</strong>
                </p>
            </div>
            
        </div>
        """
        return final_doc

    def _fallback_response(self, title, desc, attrs) -> Dict[str, Any]:
        """Safe fallback if AI fails"""
        return {
            "title": title[:80],
            "titles_all": {
                "traffic": title[:80],
                "conversion": title[:80],
                "bargain": title[:80]
            },
            "description": f"<p>{desc}</p>" + self.BRAND_FOOTER,
            "aspects": {"Brand": ["AquaVerve"]},
            "recommendedCategory": "Home & Garden",
            "mobileSummary": title,
            "optimization_data": {}
        }
