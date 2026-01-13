from openai import OpenAI
import os
import json
import traceback

class QwenOptimizer:
    def __init__(self, api_key=None):
        self.api_key = api_key or os.getenv("QWEN_API_KEY")
        if not self.api_key:
            print("❌ Warning: QWEN_API_KEY not found in environment variables")
            
        self.client = OpenAI(
            api_key=self.api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        self.model = "qwen-plus" # Using Qwen-Plus for better quality

    def optimize_product_full(self, original_title, original_description, attributes=None, images=None):
        """
        Optimize product title and description for eBay using Qwen.
        """
        print(f"🚀 Starting Qwen optimization for: {original_title[:50]}...")
        
        prompt = f"""
        You are an expert eBay SEO copywriter and luxury web designer for AquaVerve store.
        
        **Product Details:**
        - **Original Title:** {original_title}
        - **Attributes:** {json.dumps(attributes) if attributes else "N/A"}
        - **Description Preview:** {original_description[:1000] if original_description else "N/A"}
        
        **Instructions:**
        
        1. **Title (CRITICAL):** 
           - Create a keyword-rich, compelling title (EXACTLY 75-80 characters)
           - Put most important keywords FIRST for SEO
           - NEVER include "AquaVerve" in the title (save space for keywords)
           - Use Title Case, no special characters
        
        2. **Description (LUXURY HTML DESIGN):** 
           Create a premium, high-end listing design. Use this EXACT template:
           
           ```html
           <div style="max-width:900px;margin:0 auto;font-family:'Helvetica Neue',Arial,sans-serif;color:#1a1a1a;line-height:1.8">
           
           <!-- Luxury Brand Header -->
           <div style="text-align:center;padding:35px 20px;background:linear-gradient(180deg,#0d1b2a 0%,#1b263b 100%);border-radius:0">
             <h1 style="margin:0;font-size:28px;font-weight:300;letter-spacing:6px;color:#d4af37;text-transform:uppercase">AQUAVERVE</h1>
             <div style="width:60px;height:1px;background:#d4af37;margin:12px auto"></div>
             <p style="margin:0;font-size:11px;letter-spacing:3px;color:#a0a0a0;text-transform:uppercase">Premium Home & Lifestyle</p>
           </div>
           
           <!-- Hero Product Title -->
           <div style="background:#f8f9fa;padding:30px;text-align:center;border-bottom:1px solid #e9ecef">
             <h2 style="margin:0;font-size:20px;font-weight:400;color:#2d3436;letter-spacing:1px">[PRODUCT TITLE]</h2>
             <p style="margin:12px 0 0;font-size:14px;color:#636e72">[One elegant sentence about the product]</p>
           </div>
           
           <!-- Premium Features Section -->
           <div style="padding:35px 25px;background:#fff">
             <div style="display:flex;flex-wrap:wrap;gap:20px;justify-content:center">
               <div style="flex:1;min-width:200px;max-width:220px;text-align:center;padding:25px 15px;background:#fafafa;border:1px solid #eee">
                 <div style="font-size:24px;margin-bottom:10px">🏆</div>
                 <h3 style="margin:0 0 8px;font-size:13px;font-weight:600;color:#2d3436;text-transform:uppercase;letter-spacing:1px">[Feature 1]</h3>
                 <p style="margin:0;font-size:12px;color:#636e72">[Brief description]</p>
               </div>
               <div style="flex:1;min-width:200px;max-width:220px;text-align:center;padding:25px 15px;background:#fafafa;border:1px solid #eee">
                 <div style="font-size:24px;margin-bottom:10px">✨</div>
                 <h3 style="margin:0 0 8px;font-size:13px;font-weight:600;color:#2d3436;text-transform:uppercase;letter-spacing:1px">[Feature 2]</h3>
                 <p style="margin:0;font-size:12px;color:#636e72">[Brief description]</p>
               </div>
               <div style="flex:1;min-width:200px;max-width:220px;text-align:center;padding:25px 15px;background:#fafafa;border:1px solid #eee">
                 <div style="font-size:24px;margin-bottom:10px">🛡️</div>
                 <h3 style="margin:0 0 8px;font-size:13px;font-weight:600;color:#2d3436;text-transform:uppercase;letter-spacing:1px">[Feature 3]</h3>
                 <p style="margin:0;font-size:12px;color:#636e72">[Brief description]</p>
               </div>
             </div>
           </div>
           
           <!-- Product Story -->
           <div style="padding:30px 25px;background:#f8f9fa;border-top:1px solid #e9ecef;border-bottom:1px solid #e9ecef">
             <h3 style="margin:0 0 15px;font-size:14px;font-weight:600;color:#2d3436;text-transform:uppercase;letter-spacing:2px">Product Details</h3>
             <p style="margin:0;font-size:14px;color:#4a4a4a;line-height:1.9">[2-3 sentences describing the product elegantly]</p>
           </div>
           
           <!-- Specifications -->
           <div style="padding:30px 25px;background:#fff">
             <h3 style="margin:0 0 20px;font-size:14px;font-weight:600;color:#2d3436;text-transform:uppercase;letter-spacing:2px;text-align:center">Specifications</h3>
             <table style="width:100%;max-width:600px;margin:0 auto;border-collapse:collapse">
               <tr><td style="padding:12px 15px;border-bottom:1px solid #eee;color:#636e72;width:40%">Material</td><td style="padding:12px 15px;border-bottom:1px solid #eee;color:#2d3436">[Value]</td></tr>
               <tr><td style="padding:12px 15px;border-bottom:1px solid #eee;color:#636e72">Color</td><td style="padding:12px 15px;border-bottom:1px solid #eee;color:#2d3436">[Value]</td></tr>
               <tr><td style="padding:12px 15px;border-bottom:1px solid #eee;color:#636e72">Dimensions</td><td style="padding:12px 15px;border-bottom:1px solid #eee;color:#2d3436">[Value]</td></tr>
               <tr><td style="padding:12px 15px;border-bottom:1px solid #eee;color:#636e72">Weight</td><td style="padding:12px 15px;border-bottom:1px solid #eee;color:#2d3436">[Value]</td></tr>
             </table>
           </div>
           
           <!-- Package & Footer -->
           <div style="text-align:center;padding:25px;background:linear-gradient(180deg,#1b263b 0%,#0d1b2a 100%)">
             <p style="margin:0 0 8px;font-size:12px;color:#d4af37;text-transform:uppercase;letter-spacing:2px">Package Includes</p>
             <p style="margin:0;font-size:13px;color:#e0e0e0">[Item × 1, Hardware Kit, Assembly Instructions]</p>
             <div style="margin-top:20px;padding-top:20px;border-top:1px solid #2d3e50">
               <p style="margin:0;font-size:11px;color:#808080">Ships from California • Quality Guaranteed • Dedicated Support</p>
             </div>
           </div>
           
           </div>
           ```
        
        3. **Aspects (Item Specifics):** 
           - Extract ALL specs as JSON dictionary
           - **CRITICAL: Always include "Brand": ["AquaVerve"]**
           - Values must be LISTS of strings
           - Include: Type, Material, Color, Features, Weight, Dimensions, etc.
        
        4. **Category:** Suggest numeric eBay Category ID if known, else null.
        
        **DESIGN PHILOSOPHY:**
        - Minimalist luxury aesthetic (like Apple/Aesop)
        - Navy/Gold color scheme for brand consistency
        - Generous whitespace
        - Elegant typography with letter-spacing
        - No cluttered or busy designs
        - Subtle, refined borders instead of bold colors
        
        **Output Format (JSON Only):**
        {{
            "title": "Optimized 75-80 char Title Without AquaVerve",
            "description": "<div style=...>Full HTML using the EXACT luxury template above...</div>",
            "aspects": {{ 
                "Brand": ["AquaVerve"], 
                "Type": ["Product Type"],
                "Material": ["Material"],
                "Color": ["Color"] 
            }},
            "categoryId": "123456"
        }}
        """

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that outputs only valid JSON. Follow the HTML template EXACTLY."},
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
                    return {
                        "title": original_title[:80],
                        "description": original_description,
                        "aspects": {"Brand": ["AquaVerve"]},
                        "error": "JSON Parsing Failed"
                    }
            
            # POST-PROCESSING: Ensure Brand is always AquaVerve
            if "aspects" not in data:
                data["aspects"] = {}
            
            # Force Brand = AquaVerve (override any AI-generated brand)
            data["aspects"]["Brand"] = ["AquaVerve"]
            
            # Ensure all aspect values are lists
            for key, value in data["aspects"].items():
                if isinstance(value, str):
                    data["aspects"][key] = [value]
            
            print(f"✅ Optimization complete. Title: {data.get('title', '')[:50]}...")
            return data

        except Exception as e:
            print(f"❌ Qwen Optimization Failed: {e}")
            traceback.print_exc()
            return {
                "title": original_title[:80],
                "description": original_description,
                "aspects": {"Brand": ["AquaVerve"]},
                "error": str(e)
            }

if __name__ == "__main__":
    # Test block
    from dotenv import load_dotenv
    load_dotenv()
    
    qwen = QwenOptimizer()
    res = qwen.optimize_product_full(
        "Test Sofa", 
        "A red comfortable sofa", 
        {"Color": "Red"}
    )
    print(json.dumps(res, indent=2))
