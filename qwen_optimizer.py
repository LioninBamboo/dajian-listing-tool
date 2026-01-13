"""
Qwen AI 优化器 - eBay 产品标题和描述优化

改进点：
1. 简化 footer，节省字数
2. 强制包含尺寸信息 (L x W x H)
3. 尽可能多填 Item Specifics
4. 使用 eBay 标准值
"""

from openai import OpenAI
import os
import json
import traceback
import re

class QwenOptimizer:
    def __init__(self, api_key=None):
        self.api_key = api_key or os.getenv("QWEN_API_KEY")
        if not self.api_key:
            print("❌ Warning: QWEN_API_KEY not found in environment variables")
            
        self.client = OpenAI(
            api_key=self.api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        self.model = "qwen-plus"

    def extract_dimensions(self, attributes: dict, specs: dict, description: str) -> dict:
        """从各种来源提取尺寸信息"""
        dimensions = {
            "length": None,
            "width": None, 
            "height": None,
            "weight": None,
            "raw_dimensions": None
        }
        
        # 合并所有数据源
        all_data = {}
        if attributes:
            all_data.update(attributes)
        if specs:
            all_data.update(specs)
        
        # 查找尺寸相关字段
        dim_keys = ['dimensions', 'dimension', 'size', 'product dimensions', 'overall dimensions']
        weight_keys = ['weight', 'item weight', 'product weight', 'net weight']
        
        for key, value in all_data.items():
            key_lower = key.lower()
            
            # 查找尺寸
            if any(dk in key_lower for dk in dim_keys):
                dimensions["raw_dimensions"] = str(value)
                # 尝试解析 L x W x H 格式
                match = re.search(r'(\d+\.?\d*)\s*[xX×]\s*(\d+\.?\d*)\s*[xX×]\s*(\d+\.?\d*)', str(value))
                if match:
                    dimensions["length"] = match.group(1)
                    dimensions["width"] = match.group(2)
                    dimensions["height"] = match.group(3)
            
            # 单独的长宽高
            if 'length' in key_lower and dimensions["length"] is None:
                match = re.search(r'(\d+\.?\d*)', str(value))
                if match:
                    dimensions["length"] = match.group(1)
            if 'width' in key_lower and dimensions["width"] is None:
                match = re.search(r'(\d+\.?\d*)', str(value))
                if match:
                    dimensions["width"] = match.group(1)
            if 'height' in key_lower and dimensions["height"] is None:
                match = re.search(r'(\d+\.?\d*)', str(value))
                if match:
                    dimensions["height"] = match.group(1)
            
            # 查找重量
            if any(wk in key_lower for wk in weight_keys):
                match = re.search(r'(\d+\.?\d*)\s*(lbs?|kg|pounds?)?', str(value), re.IGNORECASE)
                if match:
                    dimensions["weight"] = match.group(1)
                    if match.group(2):
                        dimensions["weight"] += " " + match.group(2)
        
        return dimensions

    def optimize_product_full(self, original_title, original_description, attributes=None, images=None, specs=None):
        """
        优化产品标题和描述
        """
        print(f"🚀 Starting Qwen optimization for: {original_title[:50]}...")
        
        # 提取尺寸信息
        dimensions = self.extract_dimensions(attributes or {}, specs or {}, original_description or "")
        
        # 构建尺寸字符串
        dim_str = ""
        if dimensions["length"] and dimensions["width"] and dimensions["height"]:
            dim_str = f"{dimensions['length']} x {dimensions['width']} x {dimensions['height']} inches"
        elif dimensions["raw_dimensions"]:
            dim_str = dimensions["raw_dimensions"]
        
        weight_str = dimensions["weight"] if dimensions["weight"] else ""
        
        prompt = f"""You are an expert eBay SEO copywriter for AquaVerve store.

**Product Details:**
- **Original Title:** {original_title}
- **Attributes:** {json.dumps(attributes) if attributes else "N/A"}
- **Specs:** {json.dumps(specs) if specs else "N/A"}
- **Extracted Dimensions:** {dim_str if dim_str else "Extract from description below"}
- **Extracted Weight:** {weight_str if weight_str else "Extract from description below"}
- **Description Preview:** {original_description[:1500] if original_description else "N/A"}

**Instructions:**

1. **Title (CRITICAL):** 
   - Create a keyword-rich title (EXACTLY 75-80 characters)
   - Put most important keywords FIRST for SEO
   - Include key dimensions if space allows (e.g., "71 inch")
   - NEVER include "AquaVerve" in title
   - Use Title Case, no special characters

2. **Description (HTML, MAX 3800 characters):**
   Create a COMPREHENSIVE, visually appealing product description. Use this enhanced template:
   
   ```html
   <div style="max-width:900px;margin:0 auto;font-family:Arial,sans-serif;color:#1a1a1a;line-height:1.7">
   
   <!-- Header -->
   <div style="text-align:center;padding:30px 15px;background:linear-gradient(135deg,#0d1b2a 0%,#1a365d 100%)">
     <h1 style="margin:0;font-size:28px;font-weight:300;letter-spacing:6px;color:#d4af37">AQUAVERVE</h1>
     <p style="margin:8px 0 0;font-size:12px;color:#a0a0a0;letter-spacing:2px">PREMIUM HOME FURNISHINGS</p>
   </div>
   
   <!-- Product Title -->
   <div style="background:#f8f9fa;padding:25px;text-align:center;border-bottom:2px solid #d4af37">
     <h2 style="margin:0;font-size:20px;color:#2d3436;font-weight:500">[FULL PRODUCT TITLE]</h2>
   </div>
   
   <!-- Key Features Section -->
   <div style="padding:25px">
     <h3 style="margin:0 0 15px;font-size:16px;color:#0d1b2a;border-left:4px solid #d4af37;padding-left:12px">KEY FEATURES</h3>
     <ul style="margin:0;padding-left:20px;color:#4a4a4a">
       <li style="margin-bottom:10px"><strong>[Feature 1]:</strong> [Detailed benefit explanation]</li>
       <li style="margin-bottom:10px"><strong>[Feature 2]:</strong> [Detailed benefit explanation]</li>
       <li style="margin-bottom:10px"><strong>[Feature 3]:</strong> [Detailed benefit explanation]</li>
       <li style="margin-bottom:10px"><strong>[Feature 4]:</strong> [Detailed benefit explanation]</li>
       <li style="margin-bottom:10px"><strong>[Feature 5]:</strong> [Detailed benefit explanation]</li>
       <li style="margin-bottom:10px"><strong>[Feature 6]:</strong> [Detailed benefit explanation]</li>
     </ul>
   </div>
   
   <!-- Perfect For Section -->
   <div style="padding:20px 25px;background:#f0f4f8">
     <h3 style="margin:0 0 12px;font-size:14px;color:#0d1b2a">PERFECT FOR</h3>
     <p style="margin:0;color:#636e72">[Room types, use cases, who it's ideal for - 2-3 sentences]</p>
   </div>
   
   <!-- Specifications Table -->
   <div style="padding:25px;background:#fff">
     <h3 style="margin:0 0 15px;font-size:16px;color:#0d1b2a;border-left:4px solid #d4af37;padding-left:12px">SPECIFICATIONS</h3>
     <table style="width:100%;border-collapse:collapse">
       <tr><td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#636e72;width:40%">Overall Dimensions (L×W×H)</td><td style="padding:10px;border-bottom:1px solid #e0e0e0;font-weight:500">[XX × XX × XX inches]</td></tr>
       <tr style="background:#fafafa"><td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#636e72">Weight</td><td style="padding:10px;border-bottom:1px solid #e0e0e0;font-weight:500">[XX lbs]</td></tr>
       <tr><td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#636e72">Material</td><td style="padding:10px;border-bottom:1px solid #e0e0e0">[Materials list]</td></tr>
       <tr style="background:#fafafa"><td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#636e72">Color/Finish</td><td style="padding:10px;border-bottom:1px solid #e0e0e0">[Color details]</td></tr>
       <tr><td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#636e72">Assembly</td><td style="padding:10px;border-bottom:1px solid #e0e0e0">[Required/Not Required, time estimate]</td></tr>
       <tr style="background:#fafafa"><td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#636e72">Load Capacity</td><td style="padding:10px;border-bottom:1px solid #e0e0e0">[XX lbs if applicable]</td></tr>
     </table>
   </div>
   
   <!-- What's Included -->
   <div style="padding:20px 25px;background:#f8f9fa;border-top:1px solid #e0e0e0">
     <h3 style="margin:0 0 10px;font-size:14px;color:#0d1b2a">PACKAGE INCLUDES</h3>
     <p style="margin:0;color:#636e72">[1× Main product, hardware kit, assembly instructions, etc.]</p>
   </div>
   
   <!-- Footer -->
   <div style="text-align:center;padding:20px;background:linear-gradient(135deg,#0d1b2a 0%,#1a365d 100%)">
     <p style="margin:0;font-size:12px;color:#d4af37;letter-spacing:1px">✦ Ships from California, USA ✦</p>
     <p style="margin:8px 0 0;font-size:11px;color:#808080">Quality Guaranteed • Fast Shipping • Dedicated Support</p>
   </div>
   
   </div>
   ```
   
   IMPORTANT: Fill ALL sections with meaningful content. Be descriptive and detailed.

3. **Aspects (Item Specifics) - FILL AS MANY AS POSSIBLE:**
   
   CRITICAL RULES:
   - Use eBay's standard values when possible
   - Values must be LISTS of strings
   - MUST include dimensions as separate fields
   
   Required aspects:
   - "Brand": ["AquaVerve"]
   - "Type": [standard eBay value like "Coffee Table", "Dog Crate", "Office Chair", "TV Stand"]
   - "Material": ["Wood", "Metal", "MDF", "Fabric", "Leather", "Plastic"]
   - "Color": ["White", "Black", "Brown", "Gray", "Walnut", "Natural", "Espresso"]
   - "Item Length": ["XX in"] - MUST extract from product info
   - "Item Width": ["XX in"] - MUST extract from product info
   - "Item Height": ["XX in"] - MUST extract from product info
   - "Item Weight": ["XX lbs"] - MUST extract from product info
   
   Optional aspects (include ALL that apply):
   - "Style": ["Modern", "Contemporary", "Traditional", "Industrial", "Farmhouse", "Mid-Century Modern"]
   - "Room": ["Living Room", "Bedroom", "Office", "Kitchen", "Bathroom", "Outdoor", "Dining Room"]
   - "Features": ["Adjustable", "Foldable", "With Storage", "Waterproof", "Ergonomic", "Wheels"]
   - "Assembly Required": ["Yes"] or ["No"]
   - "Number of Items in Set": ["1"]
   - "Shape": ["Rectangular", "Round", "Square", "L-Shaped", "Oval"]
   - "Finish": ["Matte", "Glossy", "Natural", "Painted", "Lacquered"]
   - "Indoor/Outdoor": ["Indoor"] or ["Outdoor"] or ["Indoor/Outdoor"]
   - "Age Group": ["Adult"]
   - "Country/Region of Manufacture": ["China"]
   - "MPN": ["Does Not Apply"]
   - "Number of Shelves": ["1", "2", "3", etc.]
   - "Number of Drawers": ["1", "2", "3", etc.]
   - "Mounting": ["Floor Standing", "Wall Mounted", "Freestanding"]
   - "Load Capacity": ["XX lbs"]
   - "Seating Capacity": ["1", "2", "3", etc.]
   - "Adjustable Height": ["Yes"] or ["No"]

4. **Category:** Suggest eBay Category ID (number only) if known, else null.

**Output Format (JSON Only, keep description under 3500 chars):**
{{
    "title": "75-80 char optimized title",
    "description": "<div style=...>Compact HTML...</div>",
    "aspects": {{ 
        "Brand": ["AquaVerve"],
        "Type": ["..."],
        "Material": ["..."],
        "Color": ["..."],
        "Item Length": ["XX in"],
        "Item Width": ["XX in"],
        "Item Height": ["XX in"],
        "Item Weight": ["XX lbs"],
        "Style": ["..."],
        "Room": ["..."],
        "Features": ["...", "..."],
        "Assembly Required": ["Yes"],
        "MPN": ["Does Not Apply"],
        "Country/Region of Manufacture": ["China"],
        ...more aspects...
    }},
    "categoryId": "123456"
}}"""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that outputs only valid JSON. Keep description HTML under 3500 characters. Extract and include ALL dimension information. Fill as many Item Specifics as possible using eBay standard values."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.7,
            )
            
            content = response.choices[0].message.content
            print(f"✨ Qwen Response received ({len(content)} chars)")
            
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                print("⚠️ Failed to parse JSON from Qwen response, attempting repair...")
                clean_content = content.replace("```json", "").replace("```", "").strip()
                try:
                    data = json.loads(clean_content)
                except:
                    print("❌ JSON repair failed.")
                    return self._fallback_result(original_title, original_description, dimensions)
            
            # POST-PROCESSING
            if "aspects" not in data:
                data["aspects"] = {}
            
            # Force Brand = AquaVerve
            data["aspects"]["Brand"] = ["AquaVerve"]
            
            # Ensure dimensions are in aspects (from our extraction)
            if dimensions["length"] and "Item Length" not in data["aspects"]:
                data["aspects"]["Item Length"] = [f"{dimensions['length']} in"]
            if dimensions["width"] and "Item Width" not in data["aspects"]:
                data["aspects"]["Item Width"] = [f"{dimensions['width']} in"]
            if dimensions["height"] and "Item Height" not in data["aspects"]:
                data["aspects"]["Item Height"] = [f"{dimensions['height']} in"]
            if dimensions["weight"] and "Item Weight" not in data["aspects"]:
                data["aspects"]["Item Weight"] = [dimensions["weight"]]
            
            # Ensure all aspect values are lists
            for key, value in data["aspects"].items():
                if isinstance(value, str):
                    data["aspects"][key] = [value]
            
            # CRITICAL: eBay requires single-value fields to have only ONE value
            # These fields cannot have multiple values
            single_value_fields = [
                'Type', 'Brand', 'Material', 'Item Length', 'Item Width', 'Item Height', 
                'Item Weight', 'Shape', 'Indoor/Outdoor', 'Age Group', 
                'Country/Region of Manufacture', 'MPN', 'Number of Drawers', 
                'Number of Items in Set', 'Assembly Required', 'Load Capacity', 
                'Seating Capacity', 'Number of Shelves', 'Adjustable Height'
            ]
            for field in single_value_fields:
                if field in data["aspects"] and len(data["aspects"][field]) > 1:
                    # Keep only the first (most relevant) value
                    data["aspects"][field] = data["aspects"][field][:1]
            
            # Add standard aspects if missing
            if "MPN" not in data["aspects"]:
                data["aspects"]["MPN"] = ["Does Not Apply"]
            if "Country/Region of Manufacture" not in data["aspects"]:
                data["aspects"]["Country/Region of Manufacture"] = ["China"]
            
            # Truncate description if too long (eBay limit: 4000 chars)
            description = data.get("description", "")
            if len(description) > 3900:
                print(f"[WARN] Description is {len(description)} chars, truncating to 3900...")
                # Find the last complete section before 3900 chars
                description = description[:3900]
                # Try to close at a natural break point
                last_div_close = description.rfind("</div>")
                if last_div_close > 3000:
                    description = description[:last_div_close + 6]
                # Ensure proper closing
                if description.count("<div") > description.count("</div>"):
                    description += "</div>" * (description.count("<div") - description.count("</div>"))
                data["description"] = description
            
            # Use fallback category based on title keywords
            title_lower = data.get("title", "").lower()
            category_fallback = None
            
            if "dog" in title_lower and ("crate" in title_lower or "kennel" in title_lower):
                category_fallback = "177800"  # Cages & Crates
            elif "cat" in title_lower and "litter" in title_lower:
                category_fallback = "116363"  # Litter Boxes
            elif "cat" in title_lower and "tree" in title_lower:
                category_fallback = "20744"  # Cat Trees
            elif "vanity" in title_lower or "makeup" in title_lower:
                category_fallback = "32878"  # Vanities
            elif "tv stand" in title_lower:
                category_fallback = "20488"  # TV Stands
            elif "desk" in title_lower:
                category_fallback = "88057"  # Desks
            elif "office chair" in title_lower:
                category_fallback = "54235"  # Office Chairs
            elif "bookshelf" in title_lower or "bookcase" in title_lower:
                category_fallback = "3199"  # Bookcases
            
            # Validate or override categoryId
            if data.get("categoryId"):
                try:
                    cat_id = int(data["categoryId"])
                    # Valid eBay category IDs are typically 6 digits (100000+)
                    if cat_id < 100000:
                        data["categoryId"] = category_fallback
                except:
                    data["categoryId"] = category_fallback
            else:
                data["categoryId"] = category_fallback
            
            print(f"✅ Optimization complete. Title: {data.get('title', '')[:50]}...")
            print(f"   Aspects count: {len(data.get('aspects', {}))}")
            print(f"   Description length: {len(data.get('description', ''))}")
            print(f"   CategoryId: {data.get('categoryId')}")
            
            return data

        except Exception as e:
            print(f"❌ Qwen Optimization Failed: {e}")
            traceback.print_exc()
            return self._fallback_result(original_title, original_description, dimensions)
    
    def _fallback_result(self, original_title, original_description, dimensions):
        """返回降级结果"""
        aspects = {
            "Brand": ["AquaVerve"],
            "MPN": ["Does Not Apply"],
            "Country/Region of Manufacture": ["China"]
        }
        if dimensions.get("length"):
            aspects["Item Length"] = [f"{dimensions['length']} in"]
        if dimensions.get("width"):
            aspects["Item Width"] = [f"{dimensions['width']} in"]
        if dimensions.get("height"):
            aspects["Item Height"] = [f"{dimensions['height']} in"]
        if dimensions.get("weight"):
            aspects["Item Weight"] = [dimensions["weight"]]
        
        return {
            "title": original_title[:80],
            "description": original_description[:3500] if original_description else "",
            "aspects": aspects,
            "error": "Fallback result used"
        }


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    
    qwen = QwenOptimizer()
    res = qwen.optimize_product_full(
        original_title="71'' Furniture Style Double Dog Crate with 3 Fluted Drawers",
        original_description="High quality dog crate with waterproof tabletop...",
        attributes={"Material": "MDF", "Color": "White/Walnut"},
        specs={"Dimensions": "71 x 23 x 34 inches", "Weight": "85 lbs"}
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))
