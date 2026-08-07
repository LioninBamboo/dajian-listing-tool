"""
Qwen AI 优化器 - eBay 产品标题和描述优化

改进点：
1. 简化 footer，节省字数
2. 强制包含尺寸信息 (L x W x H)
3. 尽可能多填 Item Specifics
4. 使用 eBay 标准值
5. 集成 Terapeak 市场数据驱动标题和 Item Specifics
"""

from openai import OpenAI
import builtins
import os
import json
import traceback
import re
import requests
import sys
import multiprocessing
import sys
import threading
import queue as queue_module
from src.utils.dimension_helpers import (
    extract_all_dimensions,
    extract_product_weight_from_text,
    extract_product_dimensions_from_text,
    find_weight,
    find_dimension,
    # Called at the description-normalization step below but never imported, so
    # every optimization silently logged "name ... is not defined" and skipped
    # measurement normalization — descriptions could keep numbers that disagree
    # with the forced Item Length/Width/Height aspects.
    replace_description_measurements,
)
from src.utils.claim_diff_engine import build_source_constraints, FEATURE_CLAIM_PATTERNS
from src.utils.store_profile import get_store_profile


def _safe_print(*args, **kwargs):
    sep = kwargs.pop("sep", " ")
    end = kwargs.pop("end", "\n")
    file = kwargs.get("file", sys.stdout)
    encoding = getattr(file, "encoding", None) or "utf-8"
    message = sep.join(str(arg) for arg in args)
    safe_message = message.encode(encoding, errors="backslashreplace").decode(encoding, errors="ignore")
    builtins.print(safe_message, end=end, **kwargs)


print = _safe_print


class QwenOptimizationTimeoutError(TimeoutError):
    """Raised when a Qwen optimization subprocess exceeds the wall-clock limit."""


def _qwen_optimize_worker(result_queue, api_key, kwargs):
    try:
        optimizer = QwenOptimizer(api_key=api_key)
        result_queue.put({
            "ok": True,
            "result": optimizer.optimize_product_full(**kwargs),
        })
    except Exception as exc:
        result_queue.put({
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        })


def _optimize_product_full_in_process(api_key=None, **kwargs):
    print("[WARN] Falling back to in-process Qwen optimization")
    optimizer = QwenOptimizer(api_key=api_key)
    return optimizer.optimize_product_full(**kwargs)


def optimize_product_full_with_timeout(api_key=None, timeout_seconds=None, **kwargs):
    """Run Qwen optimization in a killable subprocess so one hung request cannot block the queue."""
    if timeout_seconds is None:
        timeout_seconds = float(os.getenv("QWEN_OPTIMIZE_TIMEOUT_SECONDS", "240"))

    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(
        target=_qwen_optimize_worker,
        args=(result_queue, api_key, kwargs),
        daemon=True,
    )
    process.start()
    process.join(timeout_seconds)

    if process.is_alive():
        process.terminate()
        process.join(5)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(5)
        raise QwenOptimizationTimeoutError(
            f"Qwen optimization timed out after {timeout_seconds:.0f}s"
        )

    try:
        payload = result_queue.get_nowait()
    except queue_module.Empty:
        if process.exitcode and process.exitcode != 0:
            print(f"[WARN] Qwen optimization subprocess exited with code {process.exitcode}")
            return _optimize_product_full_in_process(api_key=api_key, **kwargs)
        print("[WARN] Qwen optimization subprocess returned no result")
        return _optimize_product_full_in_process(api_key=api_key, **kwargs)

    if not payload.get("ok"):
        raise RuntimeError(
            f"Qwen optimization failed: {payload.get('error')}\n{payload.get('traceback', '')}"
        )

    return payload["result"]


class QwenOptimizer:
    def __init__(self, api_key=None):
        self.api_key = api_key or os.getenv("QWEN_API_KEY")
        if not self.api_key:
            print("❌ Warning: QWEN_API_KEY not found in environment variables")
            
        self.client = OpenAI(
            api_key=self.api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            timeout=120.0,
            max_retries=2,
        )
        self.model = "qwen-plus-latest"

    def fetch_market_intelligence(self, product_title: str, category_id: str = None) -> dict:
        """
        从 eBay Browse API 获取市场数据：
        - 竞品畅销标题（提取高频关键词）
        - 竞品 Item Specifics（找出必填和常用的 aspects）
        - 价格区间
        
        Returns:
            {
                'top_keywords': ['keyword1', 'keyword2', ...],
                'competitor_titles': ['title1', 'title2', ...],
                'common_aspects': {'Type': ['value1', 'value2'], ...},
                'price_stats': {'avg': float, 'min': float, 'max': float},
                'total_listings': int
            }
        """
        result = {
            'top_keywords': [],
            'competitor_titles': [],
            'common_aspects': {},
            'price_stats': {},
            'total_listings': 0
        }
        
        try:
            from src.services.ebay_auth import EbayOAuthService
            oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
            token = oauth.get_valid_token()
            
            if not token:
                print("   [WARN] No eBay token for market research, skipping")
                return result
            
            headers = {
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
                'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US'
            }
            
            # Extract search keywords from title (first 3-4 meaningful words)
            search_words = self._extract_search_keywords(product_title)
            if not search_words:
                return result
            
            print(f"   [SEARCH] Market research keywords: {search_words}")
            
            # Search eBay Browse API for competitor listings
            url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
            params = {
                'q': search_words,
                'limit': 30,
                'sort': 'bestMatch',
                'filter': 'buyingOptions:{FIXED_PRICE},conditions:{NEW}'
            }
            if category_id:
                params['category_ids'] = category_id
            
            resp = requests.get(url, headers=headers, params=params, timeout=20, verify=False)
            if resp.status_code != 200:
                print(f"   [WARN] Browse API returned {resp.status_code}")
                return result
            
            data = resp.json()
            items = data.get('itemSummaries', [])
            result['total_listings'] = data.get('total', 0)
            
            if not items:
                return result
            
            # Collect competitor titles
            titles = [item.get('title', '') for item in items[:20]]
            result['competitor_titles'] = titles
            
            # Price stats
            prices = [float(item.get('price', {}).get('value', 0)) 
                      for item in items if item.get('price')]
            prices = [p for p in prices if p > 10]  # filter noise
            if prices:
                result['price_stats'] = {
                    'avg': round(sum(prices) / len(prices), 2),
                    'min': round(min(prices), 2),
                    'max': round(max(prices), 2),
                    'median': round(sorted(prices)[len(prices)//2], 2)
                }
            
            # Extract top keywords from competitor titles
            keyword_freq = self._extract_keyword_frequencies(titles)
            result['top_keywords'] = keyword_freq[:20]
            
            # Fetch detailed item specifics from top 5 items
            aspect_counts = {}
            for item in items[:5]:
                item_id = item.get('itemId')
                if not item_id:
                    continue
                try:
                    detail_url = f"https://api.ebay.com/buy/browse/v1/item/{item_id}"
                    detail_resp = requests.get(detail_url, headers=headers, timeout=10, verify=False)
                    if detail_resp.status_code == 200:
                        detail = detail_resp.json()
                        local_aspects = detail.get('localizedAspects', [])
                        for asp in local_aspects:
                            name = asp.get('name', '')
                            value = asp.get('value', '')
                            if name and value:
                                if name not in aspect_counts:
                                    aspect_counts[name] = []
                                if value not in aspect_counts[name]:
                                    aspect_counts[name].append(value)
                except Exception:
                    continue
            
            result['common_aspects'] = aspect_counts
            
            print(f"   [OK] Market intel: {len(titles)} titles, {len(aspect_counts)} aspect types, "
                  f"avg price ${result['price_stats'].get('avg', 0)}")
            
        except Exception as e:
            print(f"   [WARN] Market research failed: {e}")
        
        return result
    
    def _extract_search_keywords(self, title: str) -> str:
        """从标题提取搜索关键词（去掉尺寸、颜色等修饰词）"""
        if not title:
            return ""
        
        # 去掉常见修饰词/噪音
        noise_words = {
            'with', 'and', 'for', 'the', 'set', 'of', 'in', 'to', 'a', 'an',
            'new', 'brand', 'hot', 'sale', 'free', 'shipping', 'fast',
            'black', 'white', 'gray', 'grey', 'brown', 'beige', 'blue', 'red',
            'green', 'pink', 'gold', 'silver', 'natural', 'walnut', 'oak',
            'small', 'medium', 'large', 'xl', 'xxl',
        }
        
        words = re.findall(r'[a-zA-Z]+', title)
        keywords = [w for w in words if w.lower() not in noise_words and len(w) > 2]
        
        # Take first 4-5 meaningful words
        return ' '.join(keywords[:5])
    
    def _extract_keyword_frequencies(self, titles: list) -> list:
        """从竞品标题提取高频关键词"""
        word_count = {}
        stop_words = {
            'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
            'of', 'with', 'by', 'from', 'as', 'is', 'was', 'are', 'be', 'been',
            'has', 'have', 'had', 'not', 'this', 'that', 'it', 'its', 'new',
            'set', 'x', '-', '&', '/', 'w', '|'
        }
        
        for title in titles:
            words = re.findall(r'[a-zA-Z]+', title.lower())
            seen = set()  # per-title dedup
            for w in words:
                if w not in stop_words and len(w) > 2 and w not in seen:
                    seen.add(w)
                    word_count[w] = word_count.get(w, 0) + 1
        
        # Sort by frequency, return top keywords
        sorted_kw = sorted(word_count.items(), key=lambda x: x[1], reverse=True)
        return [kw for kw, count in sorted_kw if count >= 2]

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
        
        # IMPROVED: More comprehensive key matching for dimensions
        # Priority: Assembled > Product > Overall > General
        length_keys = ['assembled length', 'product length', 'overall length', 'length', 'item length']
        width_keys = ['assembled width', 'product width', 'overall width', 'width', 'item width']
        height_keys = ['assembled height', 'product height', 'overall height', 'height', 'item height']
        weight_keys = ['product weight', 'item weight', 'net weight', 'overall product weight', 'weight']
        dim_keys = ['dimensions', 'dimension', 'size', 'product dimensions', 'overall dimensions']
        
        for key, value in all_data.items():
            key_lower = key.lower().strip()
            value_str = str(value).strip()
            shipping_indicators = ['package', 'shipping', 'gross', 'carton', 'box']
            is_shipping_measurement = any(si in key_lower for si in shipping_indicators)
            
            # Skip empty values
            if not value_str or value_str in ['N/A', 'n/a', '-', '']:
                continue
            
            # 1. First try to match specific dimension fields (highest priority)
            # Assembled Length (in.): 76.00 format
            if not is_shipping_measurement and any(lk in key_lower for lk in length_keys) and dimensions["length"] is None:
                match = re.search(r'(\d+\.?\d*)', value_str)
                if match:
                    dimensions["length"] = match.group(1)
                    print(f"   📏 Length extracted from '{key}': {dimensions['length']}")
            
            if not is_shipping_measurement and any(wk in key_lower for wk in width_keys) and dimensions["width"] is None:
                match = re.search(r'(\d+\.?\d*)', value_str)
                if match:
                    dimensions["width"] = match.group(1)
                    print(f"   📏 Width extracted from '{key}': {dimensions['width']}")
            
            if not is_shipping_measurement and any(hk in key_lower for hk in height_keys) and dimensions["height"] is None:
                match = re.search(r'(\d+\.?\d*)', value_str)
                if match:
                    dimensions["height"] = match.group(1)
                    print(f"   📏 Height extracted from '{key}': {dimensions['height']}")
            
            # 2. Try to parse combined dimension format: L x W x H
            if not is_shipping_measurement and any(dk in key_lower for dk in dim_keys):
                dimensions["raw_dimensions"] = value_str
                match = re.search(r'(\d+\.?\d*)\s*[xX×]\s*(\d+\.?\d*)\s*[xX×]\s*(\d+\.?\d*)', value_str)
                if match and dimensions["length"] is None:
                    dimensions["length"] = match.group(1)
                    dimensions["width"] = match.group(2)
                    dimensions["height"] = match.group(3)
                    print(f"   📏 Dimensions parsed from '{key}': {dimensions['length']} x {dimensions['width']} x {dimensions['height']}")
            
            # 3. Weight extraction (EXCLUDE capacity/load fields to avoid confusion)
            capacity_indicators = ['capacity', 'load', 'support', 'hold', 'max']
            if any(wk in key_lower for wk in weight_keys) and dimensions["weight"] is None:
                if not any(ci in key_lower for ci in capacity_indicators) and not any(si in key_lower for si in shipping_indicators):
                    match = re.search(r'(\d+\.?\d*)\s*(lbs?|kg|pounds?)?', value_str, re.IGNORECASE)
                    if match:
                        weight_val = match.group(1)
                        weight_unit = match.group(2) if match.group(2) else "lbs"
                        dimensions["weight"] = f"{weight_val} {weight_unit}"
                        print(f"   ⚖️ Weight extracted from '{key}': {dimensions['weight']}")
                else:
                    print(f"   ⚠️ Skipped capacity field '{key}': {value_str} (not actual weight)")

        # Prefer explicit overall/net item weight fields over generic "Product Weight"
        # because some source payloads reuse product-weight slots for package weight.
        preferred_weight_keys = [
            'weight of overrall product',
            'weight of overall product',
            'overall product weight',
            'overall weight',
            'net  weight',
            'net weight',
            'item weight',
            'product weight',
        ]
        for preferred in preferred_weight_keys:
            for key, value in all_data.items():
                key_lower = key.lower().strip()
                value_str = str(value).strip()
                if preferred not in key_lower:
                    continue
                if any(ci in key_lower for ci in capacity_indicators) or any(si in key_lower for si in shipping_indicators):
                    continue
                match = re.search(r'(\d+\.?\d*)\s*(lbs?|kg|pounds?)?', value_str, re.IGNORECASE)
                if match:
                    weight_val = match.group(1)
                    weight_unit = match.group(2) if match.group(2) else "lbs"
                    dimensions["weight"] = f"{weight_val} {weight_unit}"
                    break
            if dimensions["weight"]:
                break
        
        # Try to extract product dimensions/weight from description when structured fields are missing.
        if description:
            if dimensions["length"] is None or dimensions["width"] is None or dimensions["height"] is None:
                desc_dims = extract_product_dimensions_from_text(description)
                if desc_dims["length"] and desc_dims["width"] and desc_dims["height"]:
                    dimensions["length"] = dimensions["length"] or str(desc_dims["length"])
                    dimensions["width"] = dimensions["width"] or str(desc_dims["width"])
                    dimensions["height"] = dimensions["height"] or str(desc_dims["height"])
                    dimensions["raw_dimensions"] = f'{desc_dims["length"]} x {desc_dims["width"]} x {desc_dims["height"]} inches'
                    print(f"   📏 Dimensions from description: {dimensions['length']} x {dimensions['width']} x {dimensions['height']}")

            if dimensions["weight"] is None:
                desc_weight = extract_product_weight_from_text(description)
                if desc_weight:
                    dimensions["weight"] = f"{desc_weight} lbs"
                    print(f"   ⚖️ Weight extracted from description: {dimensions['weight']}")
        
        # Bounds validation: reject unreasonable values
        for dim_key in ["length", "width", "height"]:
            if dimensions[dim_key]:
                try:
                    val = float(dimensions[dim_key])
                    if val < 0.5 or val > 999:
                        print(f"   ⚠️ {dim_key}={val} out of bounds (0.5-999 in), discarded")
                        dimensions[dim_key] = None
                except (ValueError, TypeError):
                    dimensions[dim_key] = None
        
        if dimensions["weight"]:
            try:
                weight_num = float(re.search(r'(\d+\.?\d*)', dimensions["weight"]).group(1))
                if weight_num < 0.1 or weight_num > 9999:
                    print(f"   ⚠️ weight={weight_num} out of bounds (0.1-9999), discarded")
                    dimensions["weight"] = None
            except (ValueError, TypeError, AttributeError):
                dimensions["weight"] = None
        
        return dimensions

    def optimize_with_pain_points(self, original_title: str, original_description: str,
                                  pain_points: list = None, attributes: dict = None,
                                  specs: dict = None, images: list = None) -> dict:
        """
        针对竞品痛点进行差异化优化
        
        Args:
            original_title: 原始标题
            original_description: 原始描述
            pain_points: 竞品痛点列表 [{"issue": "腿不稳", "our_advantage": "加固实木腿", "suggested_emphasis": "..."}]
            attributes: 产品属性
            specs: 产品规格
            images: 产品图片
        
        Returns:
            优化后的产品数据，包含针对痛点的差异化描述
        """
        print(f"🎯 Starting pain-point-aware optimization...")
        
        # 构建痛点强调提示
        pain_point_prompt = ""
        if pain_points:
            pain_point_prompt = "\n\n**COMPETITIVE DIFFERENTIATION (VERY IMPORTANT):**\n"
            pain_point_prompt += "Competitors have these common complaints. EMPHASIZE our advantages:\n"
            for i, pp in enumerate(pain_points, 1):
                issue = pp.get('issue', '')
                advantage = pp.get('our_advantage', '')
                emphasis = pp.get('suggested_emphasis', '')
                pain_point_prompt += f"{i}. Competitor Issue: '{issue}' → OUR ADVANTAGE: {advantage}\n"
                pain_point_prompt += f"   Suggested emphasis: \"{emphasis}\"\n"
            pain_point_prompt += "\nIncorporate these advantages prominently in the description's KEY FEATURES section!"
        
        # 提取尺寸
        dimensions = self.extract_dimensions(attributes or {}, specs or {}, original_description or "")
        
        dim_str = ""
        if dimensions["length"] and dimensions["width"] and dimensions["height"]:
            dim_str = f"{dimensions['length']} x {dimensions['width']} x {dimensions['height']} inches"
        
        weight_str = dimensions["weight"] if dimensions["weight"] else ""
        
        _brand = get_store_profile().brand_name
        prompt = f"""You are an expert eBay SEO copywriter for {_brand} store, focused on COMPETITIVE DIFFERENTIATION.

**Product Details:**
- **Original Title:** {original_title}
- **Attributes:** {json.dumps(attributes) if attributes else "N/A"}
- **Specs:** {json.dumps(specs) if specs else "N/A"}
- **Extracted Dimensions:** {dim_str if dim_str else "Extract from description"}
- **Extracted Weight:** {weight_str if weight_str else "Extract from description"}
- **Description Preview:** {original_description[:1200] if original_description else "N/A"}
{pain_point_prompt}

{self._format_source_constraints(build_source_constraints(attributes, specs, original_description or "", original_title))}

**Instructions:**

1. **Title (75-80 chars):** Create high-converting, readable title with key search terms at beginning.

2. **Description (HTML, MAX 3500 chars):**
   - Start KEY FEATURES with our competitive advantages (from pain points above)
   - Make advantages prominent and specific (not generic)
   - Use confident language: "reinforced", "premium", "engineered for"
   - Include all standard sections: Features, Perfect For, Specs, Package

3. **Aspects:** Fill all relevant Item Specifics with accurate values.

**Output Format (JSON):**
{{
    "title": "75-80 char title",
    "description": "<div>HTML with competitive advantages emphasized</div>",
    "aspects": {{...}},
    "competitive_advantages": ["advantage 1", "advantage 2", "advantage 3"]
}}"""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a faithful product listing writer that outputs only valid JSON. You must ONLY describe features, materials, and capabilities that are explicitly stated in the source data provided.\n\nABSOLUTE RULES:\n1. NEVER add features, materials, or capabilities not present in the source data\n2. NEVER upgrade materials (e.g., glass→tempered glass, PU leather→genuine leather, MDF→solid wood)\n3. NEVER add specific counts/positions not in source data (e.g., '5-position', '3-tier')\n4. NEVER claim certifications not in source (TSA, UL, ASTM, etc.)\n5. If source says a generic term, keep it generic (e.g., 'wood' stays 'wood', not 'oak')\n6. For Features aspect: ONLY include features with explicit source evidence\n\nYou MAY rephrase for marketing appeal, but you must NOT add factual claims."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.4,
            )
            
            content = response.choices[0].message.content
            print(f"✨ Pain-point optimization complete ({len(content)} chars)")
            
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                clean_content = content.replace("```json", "").replace("```", "").strip()
                data = json.loads(clean_content)
            
            # Post-processing (same as optimize_product_full)
            if "aspects" not in data:
                data["aspects"] = {}

            if get_store_profile().force_house_brand:
                data["aspects"]["Brand"] = [get_store_profile().brand_name]

            # Force our extracted dimensions
            if dimensions["length"]:
                data["aspects"]["Item Length"] = [f"{dimensions['length']} in"]
            if dimensions["width"]:
                data["aspects"]["Item Width"] = [f"{dimensions['width']} in"]
            if dimensions["height"]:
                data["aspects"]["Item Height"] = [f"{dimensions['height']} in"]
            if dimensions["weight"]:
                weight_str = dimensions["weight"]
                if not any(u in weight_str.lower() for u in ['lb', 'kg', 'oz']):
                    weight_str = f"{weight_str} lbs"
                data["aspects"]["Item Weight"] = [weight_str]
            
            # Add standard aspects
            if "MPN" not in data["aspects"]:
                data["aspects"]["MPN"] = ["Does Not Apply"]
            if "Country/Region of Manufacture" not in data["aspects"]:
                data["aspects"]["Country/Region of Manufacture"] = ["China"]
            
            # Record which pain points were addressed
            data["pain_points_addressed"] = [pp.get('issue') for pp in (pain_points or [])]
            
            return data
            
        except Exception as e:
            print(f"❌ Pain-point optimization error: {e}")
            traceback.print_exc()
            return self._fallback_result(original_title, original_description, dimensions)
    def _format_source_constraints(self, constraints: dict) -> str:
        """将 source constraints 格式化为 prompt 中的 SOURCE TRUTH BOUNDARY 段。"""
        lines = ["\n\n**SOURCE TRUTH BOUNDARY (NEVER violate):**"]
        materials = constraints.get("materials", [])
        if materials:
            lines.append(f"- Allowed materials: {', '.join(materials)}")
        supported = constraints.get("supported_features", set())
        for feature_name in sorted(FEATURE_CLAIM_PATTERNS.keys()):
            status = "✅ Supported" if feature_name in supported else "❌ NOT supported — do NOT mention"
            lines.append(f"- {feature_name}: {status}")
        counts = constraints.get("counts", {})
        for name, count in counts.items():
            lines.append(f"- {name} count: {count}")
        return "\n".join(lines)

    def optimize_arttoy_listing(self, original_title, original_description, attributes=None, specs=None, market_intel=None):
        """Generate an art-toy / blind-box listing (template_style=arttoy_hype).

        Parallel to optimize_product_full but with the Hypebeast prompt, Chinese
        translation fields, and a hard banned-terms backstop. Retries once with
        feedback if the model leaks a banned term; returns furniture-compatible
        keys ({title, description, aspects, categoryId}) plus titleCN/descriptionCN.
        """
        from src.services.arttoy_prompt import (
            build_arttoy_system_prompt,
            build_arttoy_user_prompt,
            finalize_arttoy_listing,
        )
        from src.utils.banned_terms_guard import BannedTermError

        profile = get_store_profile()
        print(f"🎨 Starting art-toy optimization for: {str(original_title)[:50]}...")

        system_prompt = build_arttoy_system_prompt(profile)
        user_prompt = build_arttoy_user_prompt(
            title=original_title,
            description=original_description,
            attributes=attributes,
            specs=specs,
            market_intel=market_intel,
        )

        last_error = None
        for attempt in range(2):
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            if last_error:
                messages.append({
                    "role": "user",
                    "content": (
                        f"Your previous attempt was REJECTED: it contained prohibited "
                        f"term(s) {last_error}. Regenerate WITHOUT any prohibited term "
                        f"anywhere in title, description, or item specifics."
                    ),
                })
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=0.4,
                )
                content = response.choices[0].message.content
                try:
                    data = json.loads(content)
                except json.JSONDecodeError:
                    data = json.loads(content.replace("```json", "").replace("```", "").strip())

                # Smart-truncate the body BEFORE finalize appends the footer, so
                # the footer always survives and eBay's 4000-char description limit
                # is respected without cutting mid-tag (broken-listing guard).
                body = str(data.get("description") or "")
                if len(body) > 3300:
                    data["description"] = self._smart_truncate_html(body, 3300)

                result = finalize_arttoy_listing(data, profile)

                # Keep the shared category matcher so publish-time taxonomy agrees.
                try:
                    from src.services.ebay_category_matcher import create_category_matcher
                    matcher = create_category_matcher(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
                    matched_id, matched_name, matched_aspects = matcher.get_category_and_aspects(
                        original_title or result.get("title", ""),
                        result.get("aspects", {}),
                        result.get("description", ""),
                    )
                    if matched_id:
                        result["categoryId"] = matched_id
                        result["categoryName"] = matched_name
                        result["aspects"] = matched_aspects or result.get("aspects", {})
                except Exception as cat_err:
                    print(f"   [WARN] category matcher unavailable: {cat_err}")

                print(f"✅ Art-toy optimization complete. Title: {result.get('title','')[:50]}...")
                return result

            except BannedTermError as e:
                last_error = ", ".join(sorted({h.term for h in e.hits}))
                print(f"   [RETRY] art-toy output had banned terms: {last_error}")
                continue
            except Exception as e:
                print(f"❌ Art-toy optimization failed: {e}")
                traceback.print_exc()
                break

        # Fall back rather than publish something unvalidated.
        print("   [FALLBACK] art-toy generation could not produce a clean listing")
        return {
            "title": str(original_title or "")[:80],
            "titleCN": "",
            "description": str(original_description or ""),
            "descriptionCN": "",
            "aspects": {},
            "features": [],
            "error": f"banned terms unresolved: {last_error}" if last_error else "generation failed",
        }

    def optimize_auto_technical_listing(self, original_title, original_description, attributes=None, specs=None, market_intel=None, compatibility=None):
        """Generate an auto-parts / tools listing (template_style=auto_technical).

        Parallel to optimize_product_full but with the technical automotive
        prompt. Auto-detects fitment (vehicle part) vs tool mode from the source
        text and swaps the prompt accordingly. The exact Year-Make-Model fitment
        is NOT written into the description — it goes to eBay's structured
        ItemCompatibilityList at publish time (Trading channel), so the copy
        never fabricates a fitment table. Returns furniture-compatible keys
        ({title, description, aspects, categoryId}) plus the detected ``mode``.
        """
        from src.services.auto_technical_prompt import (
            build_auto_technical_system_prompt,
            build_auto_technical_user_prompt,
            detect_auto_mode,
            finalize_auto_technical_listing,
        )

        profile = get_store_profile()
        mode = detect_auto_mode(original_title, original_description, attributes, compatibility)
        print(f"🔧 Starting auto-technical optimization ({mode}) for: {str(original_title)[:50]}...")

        system_prompt = build_auto_technical_system_prompt(profile, mode)
        user_prompt = build_auto_technical_user_prompt(
            title=original_title,
            description=original_description,
            mode=mode,
            attributes=attributes,
            specs=specs,
            market_intel=market_intel,
        )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.4,
            )
            content = response.choices[0].message.content
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                data = json.loads(content.replace("```json", "").replace("```", "").strip())

            # Same HTML hygiene as the furniture path, then truncate BEFORE the
            # footer is appended so the footer always survives eBay's char limit.
            body = self._validate_and_fix_html(self._clean_placeholder_text(str(data.get("description") or "")))
            if len(body) > 3300:
                body = self._smart_truncate_html(body, 3300)
            data["description"] = body

            result = finalize_auto_technical_listing(data, profile, mode)

            # Share the category matcher so generation-time taxonomy agrees with publish.
            try:
                from src.services.ebay_category_matcher import create_category_matcher
                matcher = create_category_matcher(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
                matched_id, matched_name, matched_aspects = matcher.get_category_and_aspects(
                    original_title or result.get("title", ""),
                    result.get("aspects", {}),
                    result.get("description", ""),
                )
                if matched_id:
                    result["categoryId"] = matched_id
                    result["categoryName"] = matched_name
                    result["aspects"] = matched_aspects or result.get("aspects", {})
            except Exception as cat_err:
                print(f"   [WARN] category matcher unavailable: {cat_err}")

            print(f"✅ Auto-technical optimization complete ({mode}). Title: {result.get('title','')[:50]}...")
            return result

        except Exception as e:
            print(f"❌ Auto-technical optimization failed: {e}")
            traceback.print_exc()
            # Fall back rather than publish something unvalidated.
            return {
                "title": str(original_title or "")[:80],
                "description": str(original_description or ""),
                "aspects": {},
                "features": [],
                "mode": mode,
                "error": "generation failed",
            }

    def optimize_garden_lifestyle_listing(self, original_title, original_description, attributes=None, specs=None, market_intel=None, previous_errors=None):
        """Generate an outdoor/garden/pet lifestyle listing (template_style=garden_lifestyle).

        Same deterministic-chrome architecture as the auto template: the LLM
        writes only the intro + KEY FEATURES + PERFECT FOR prose; finalize renders
        the brand banner, hero stat band, spec table and footer from aspects. When
        the shared QC retry loop passes ``previous_errors`` (e.g. FactSheet
        rejections), they are fed back so the model self-corrects instead of
        relying on a lucky re-roll.
        """
        from src.services.garden_lifestyle_prompt import (
            build_garden_lifestyle_system_prompt,
            build_garden_lifestyle_user_prompt,
            finalize_garden_lifestyle_listing,
        )

        profile = get_store_profile()
        print(f"🌿 Starting garden-lifestyle optimization for: {str(original_title)[:50]}...")
        system_prompt = build_garden_lifestyle_system_prompt(profile)
        user_prompt = build_garden_lifestyle_user_prompt(
            title=original_title,
            description=original_description,
            attributes=attributes,
            specs=specs,
            market_intel=market_intel,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        if previous_errors:
            messages.append({
                "role": "user",
                "content": (
                    "Your previous attempt was REJECTED by the fact guard for these claims: "
                    + "; ".join(str(e) for e in previous_errors[:8])
                    + ". Remove or rewrite EXACTLY those claims using only source-supported wording; "
                    "do not add any adjective the source does not state."
                ),
            })

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.4,
            )
            content = response.choices[0].message.content
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                data = json.loads(content.replace("```json", "").replace("```", "").strip())

            body = self._validate_and_fix_html(self._clean_placeholder_text(str(data.get("description") or "")))
            if len(body) > 3300:
                body = self._smart_truncate_html(body, 3300)
            data["description"] = body

            result = finalize_garden_lifestyle_listing(data, profile)

            try:
                from src.services.ebay_category_matcher import create_category_matcher
                matcher = create_category_matcher(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
                matched_id, matched_name, matched_aspects = matcher.get_category_and_aspects(
                    original_title or result.get("title", ""),
                    result.get("aspects", {}),
                    result.get("description", ""),
                )
                if matched_id:
                    result["categoryId"] = matched_id
                    result["categoryName"] = matched_name
                    result["aspects"] = matched_aspects or result.get("aspects", {})
            except Exception as cat_err:
                print(f"   [WARN] category matcher unavailable: {cat_err}")

            print(f"✅ Garden-lifestyle optimization complete. Title: {result.get('title','')[:50]}...")
            return result

        except Exception as e:
            print(f"❌ Garden-lifestyle optimization failed: {e}")
            traceback.print_exc()
            return {
                "title": str(original_title or "")[:80],
                "description": str(original_description or ""),
                "aspects": {},
                "features": [],
                "error": "generation failed",
            }

    def optimize_product_full(self, original_title, original_description, attributes=None, images=None, specs=None, video_url=None, market_intel=None, previous_errors=None):
        """
        优化产品标题和描述

        Args:
            video_url: Optional video URL to embed in description
            market_intel: Optional market intelligence from Terapeak/Browse API
                         {'top_keywords': [...], 'competitor_titles': [...],
                          'common_aspects': {...}, 'price_stats': {...}}
            previous_errors: List of error strings from previous failed attempts
        """
        # Sub-store instances route to their own template path. Furniture (the
        # main store, template_style == "furniture_classic") falls through to the
        # existing logic below, completely unchanged.
        _style = get_store_profile().template_style
        if _style == "arttoy_hype":
            return self.optimize_arttoy_listing(
                original_title,
                original_description,
                attributes=attributes,
                specs=specs,
                market_intel=market_intel,
            )
        if _style == "auto_technical":
            return self.optimize_auto_technical_listing(
                original_title,
                original_description,
                attributes=attributes,
                specs=specs,
                market_intel=market_intel,
            )
        if _style == "garden_lifestyle":
            return self.optimize_garden_lifestyle_listing(
                original_title,
                original_description,
                attributes=attributes,
                specs=specs,
                market_intel=market_intel,
                previous_errors=previous_errors,
            )

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
        
        # 构建市场情报段落
        market_section = ""
        if market_intel:
            top_kw = market_intel.get('top_keywords', [])
            comp_titles = market_intel.get('competitor_titles', [])
            common_asp = market_intel.get('common_aspects', {})
            price_stats = market_intel.get('price_stats', {})
            
            if top_kw or comp_titles:
                market_section = "\n\n**MARKET INTELLIGENCE (from eBay Terapeak data — USE THIS):**\n"
                
                if top_kw:
                    market_section += f"- **Top-selling keywords** (include as many as possible in title): {', '.join(top_kw[:15])}\n"
                
                if comp_titles:
                    market_section += f"- **Best-selling competitor titles** (study their keyword patterns):\n"
                    for t in comp_titles[:8]:
                        market_section += f"  • {t}\n"
                
                if price_stats:
                    market_section += f"- **Price range:** ${price_stats.get('min',0)}-${price_stats.get('max',0)}, avg ${price_stats.get('avg',0)}\n"
                
                if common_asp:
                    market_section += f"- **Common Item Specifics from top sellers** (MUST include all of these):\n"
                    for asp_name, asp_vals in list(common_asp.items())[:25]:
                        market_section += f"  • {asp_name}: {', '.join(asp_vals[:3])}\n"
        
        # 构建 source 约束并注入 prompt
        source_constraints = build_source_constraints(
            attrs=attributes,
            specs=specs,
            source_description=original_description or "",
            source_title=original_title,
        )
        constraint_section = self._format_source_constraints(source_constraints)

        error_feedback_section = ""
        if previous_errors:
            error_feedback_section = f"\n**CRITICAL FEEDBACK FROM PREVIOUS ATTEMPT:**\nYour previous attempt failed the quality gate for the following reasons:\n"
            for err in previous_errors:
                error_feedback_section += f"- {err}\n"
            error_feedback_section += "You MUST correct these errors and strictly follow the source facts. Do NOT hallucinate these claims again!\n"

        _profile = get_store_profile()
        _brand = _profile.brand_name
        prompt = f"""You are an expert eBay SEO copywriter for {_brand} store.

**Product Details:**
- **Original Title:** {original_title}
- **Attributes:** {json.dumps(attributes) if attributes else "N/A"}
- **Specs:** {json.dumps(specs) if specs else "N/A"}
- **Extracted Dimensions:** {dim_str if dim_str else "NOT AVAILABLE - do not guess dimensions"}
- **Extracted Weight:** {weight_str if weight_str else "NOT AVAILABLE - do not guess weight"}
- **Description Preview:** {original_description[:1500] if original_description else "N/A"}
{market_section}
{constraint_section}
{error_feedback_section}
**Instructions:**

1. **Title (CRITICAL):** 
   - Create a high-converting, READABLE title (Aim for 75-80 characters, max 80).
   - Structure: [Main Product Name] + [Key Features] + [Dimensions] + [Color/Material].
   - Ensure the title reads naturally (avoid random keyword salad).
   - Put the most critical search terms at the beginning.
   - Include key dimensions if space allows (e.g., "71 inch").
   - NEVER include "{_brand}" in title.
   - Use Title Case, no special characters.
   - **IMPORTANT: Incorporate high-frequency keywords from the market intelligence above** to match what buyers actually search for. Study the competitor titles for keyword patterns.

2. **Description (HTML, MAX 3800 characters):**
   Create a COMPREHENSIVE, visually appealing product description. Use this enhanced template:
   
   ```html
   <div style="max-width:900px;margin:0 auto;font-family:Arial,sans-serif;color:#1a1a1a;line-height:1.7">
   
   <!-- Header -->
   <div style="text-align:center;padding:30px 15px;background:linear-gradient(135deg,#0d1b2a 0%,#1a365d 100%)">
     <h1 style="margin:0;font-size:28px;font-weight:300;letter-spacing:6px;color:#d4af37">{_brand.upper()}</h1>
     <p style="margin:8px 0 0;font-size:12px;color:#a0a0a0;letter-spacing:2px">{_profile.brand_tagline}</p>
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
     <p style="margin:0;font-size:12px;color:#d4af37;letter-spacing:1px">{_profile.description_footer_line1}</p>
     <p style="margin:8px 0 0;font-size:11px;color:#808080">{_profile.description_footer_line2}</p>
   </div>
   
   </div>
   ```
   
   IMPORTANT: Fill ALL sections with meaningful content. Be descriptive and detailed.

3. **Aspects (Item Specifics) - MAXIMIZE FOR VISIBILITY:**
   
   CRITICAL RULES:
   - Use eBay's standard values when possible
   - Values must be LISTS of strings
   - MUST include dimensions as separate fields
   - **IMPORTANT: If the MARKET INTELLIGENCE section above includes "Common Item Specifics from top sellers", you MUST include ALL of those aspects using the most common values. This is how top sellers get maximum search visibility.**
   - The MORE item specifics you fill in, the MORE search filters the listing matches = MORE exposure
   - Aim for at least 15-20 item specifics per listing
   
   Required aspects (ALWAYS include):
   - "Brand": ["{_brand}"]
   - "Type": [standard eBay value like "Coffee Table", "Dog Crate", "Office Chair", "TV Stand"]
   - "Material": ["Wood", "Metal", "MDF", "Fabric", "Leather", "Plastic"]
   - "Color": ["White"] - SINGLE VALUE ONLY, pick most dominant color
   - "Item Length": ["XX in"] - ONLY if dimensions are clearly stated in specs/attributes
   - "Item Width": ["XX in"] - ONLY if dimensions are clearly stated in specs/attributes
   - "Item Height": ["XX in"] - ONLY if dimensions are clearly stated in specs/attributes
   - "Item Weight": ["XX lbs"] - ONLY actual product weight, NEVER use "weight capacity", "load capacity", or "supports up to XX lbs"
   
   ⚠️ CRITICAL: If dimensions/weight are not clearly provided in specs or attributes, OMIT these fields entirely. Do NOT fabricate or estimate dimensions. "Weight capacity" and "load capacity" are NOT the same as "Item Weight".
   
   Highly recommended aspects (IMPORTANT: Room, Style, Color, Shape, Finish must be SINGLE VALUE):
   - "Style": ["Modern"] - SINGLE VALUE, pick best match from: Modern, Contemporary, Traditional, Industrial, Farmhouse, Mid-Century Modern, Rustic, Scandinavian, Bohemian, Coastal, Transitional, Art Deco, Minimalist
   - "Room": ["Living Room"] - SINGLE VALUE, pick primary room from: Living Room, Bedroom, Office, Kitchen, Bathroom, Outdoor, Dining Room, Entryway, Nursery, Garage, Patio
   - "Features": ["Adjustable", "Foldable", "With Storage", "Ergonomic", "Waterproof", "Scratch Resistant", "Easy Clean", "Reversible", "Reclining", "Stackable", "With Cushion", "With Wheels"] - CAN have multiple values, include ALL that apply
   - "Assembly Required": ["Yes"] or ["No"]
   - "Number of Items in Set": ["1"]
   - "Shape": ["Rectangular"] - SINGLE VALUE: Rectangular, Round, Square, L-Shaped, Oval, Irregular
   - "Finish": ["Matte"] - SINGLE VALUE: Matte, Glossy, Natural, Painted, Lacquered, Distressed, Brushed, Polished
   - "Indoor/Outdoor": ["Indoor"] or ["Outdoor"] or ["Indoor/Outdoor"]
   - "Age Group": ["Adult"]
   - "Country/Region of Manufacture": ["China"]
   - "MPN": ["Does Not Apply"]
   - "Mounting": ["Floor Standing", "Wall Mounted", "Freestanding"]
   
   Additional aspects (include ALL that apply — each one increases search visibility):
   - "Number of Shelves": ["1", "2", "3", etc.]
   - "Number of Drawers": ["1", "2", "3", etc.]
   - "Load Capacity": ["XX lbs"]
   - "Seating Capacity": ["1", "2", "3", etc.]
   - "Adjustable Height": ["Yes"] or ["No"]
   - "Back Style": ["Solid Back", "Open Back", "Cushion Back", "Ladder Back"]
   - "Arm Style": ["Armless", "With Arms", "Padded Arms"]
   - "Leg Style": ["Straight Legs", "Tapered Legs", "Hairpin Legs", "Pedestal"]
   - "Frame Material": ["Wood", "Metal", "Steel", "Bamboo", "Aluminum"]
   - "Upholstery Material": ["Fabric", "Faux Leather", "Velvet", "Linen", "Polyester", "PU Leather"]
   - "Pattern": ["Solid", "Striped", "Geometric", "Floral"]
   - "Theme": ["Modern", "Nature", "Sports", "Animals"]
   - "Unit of Measurement": ["in"]
   - "Item Depth": ["XX in"]
   - "Seat Height": ["XX in"]
   - "Seat Width": ["XX in"]
   - "Seat Depth": ["XX in"]
   - "Table Height": ["XX in"]
   - "Tabletop Size": ["XX x XX in"]
   - "Door Configuration": ["1 Door", "2 Doors", "Sliding Door"]
   - "Custom Bundle": ["No"]
   - "California Prop 65 Warning": ["Cancer and Reproductive Harm-www.P65Warnings.ca.gov"]
   - "Product Line": ["Does Not Apply"]
   - "Department": ["Adults", "Teens", "Kids"]
   - "Comfort Level": ["Medium", "Firm", "Plush"]
   - "Maximum Weight Capacity": ["XX lbs"]
   - "Warranty": ["1 Year", "2 Year", "Limited Lifetime"]
   - "Care Instructions": ["Wipe Clean", "Spot Clean", "Machine Washable"]

4. **Category:** Suggest eBay Category ID (number only) if known, else null.

**Output Format (JSON Only, keep description under 3500 chars):**
{{
    "title": "75-80 char optimized title",
    "description": "<div style=...>Compact HTML...</div>",
    "aspects": {{
        "Brand": ["{_brand}"],
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
                    {"role": "system", "content": "You are a faithful product listing writer that outputs only valid JSON. You must ONLY describe features, materials, and capabilities that are explicitly stated in the source data provided.\n\nABSOLUTE RULES:\n1. NEVER add features, materials, or capabilities not present in the source data\n2. NEVER upgrade materials (e.g., glass→tempered glass, PU leather→genuine leather, MDF→solid wood)\n3. NEVER add specific counts/positions not in source data (e.g., '5-position', '3-tier')\n4. NEVER claim certifications not in source (TSA, UL, ASTM, etc.)\n5. If source says a generic term, keep it generic (e.g., 'wood' stays 'wood', not 'oak')\n6. For Features aspect: ONLY include features with explicit source evidence\n\nYou MAY rephrase for marketing appeal, but you must NOT add factual claims. Keep description HTML under 3500 characters. Fill at least 15-20 item specifics using eBay standard values."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.4,
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
            
            # Force Brand = our storefront brand (generic-goods instances only;
            # art-toy instances keep the item's own IP brand).
            if get_store_profile().force_house_brand:
                data["aspects"]["Brand"] = [get_store_profile().brand_name]
            
            # CRITICAL: FORCE use our extracted dimensions, OVERRIDE AI-generated ones
            # This ensures accuracy - AI often makes up dimensions
            if dimensions["length"]:
                data["aspects"]["Item Length"] = [f"{dimensions['length']} in"]
                print(f"   ✓ Forced Item Length: {dimensions['length']} in")
            else:
                # Remove AI-hallucinated length if we have no source data
                if "Item Length" in data["aspects"]:
                    print(f"   ⚠️ Removing AI-generated Item Length (no source data): {data['aspects']['Item Length']}")
                    del data["aspects"]["Item Length"]
            
            if dimensions["width"]:
                data["aspects"]["Item Width"] = [f"{dimensions['width']} in"]
                print(f"   ✓ Forced Item Width: {dimensions['width']} in")
            else:
                if "Item Width" in data["aspects"]:
                    print(f"   ⚠️ Removing AI-generated Item Width (no source data): {data['aspects']['Item Width']}")
                    del data["aspects"]["Item Width"]
            
            if dimensions["height"]:
                data["aspects"]["Item Height"] = [f"{dimensions['height']} in"]
                print(f"   ✓ Forced Item Height: {dimensions['height']} in")
            else:
                if "Item Height" in data["aspects"]:
                    print(f"   ⚠️ Removing AI-generated Item Height (no source data): {data['aspects']['Item Height']}")
                    del data["aspects"]["Item Height"]
            
            if dimensions["weight"]:
                # Normalize weight format
                weight_str = dimensions["weight"]
                if not any(u in weight_str.lower() for u in ['lb', 'kg', 'oz']):
                    weight_str = f"{weight_str} lbs"
                data["aspects"]["Item Weight"] = [weight_str]
                print(f"   ✓ Forced Item Weight: {weight_str}")

            if "description" in data and isinstance(data["description"], str):
                try:
                    weight_num = None
                    if dimensions["weight"]:
                        weight_match = re.search(r'(\d+\.?\d*)', str(dimensions["weight"]))
                        if weight_match:
                            weight_num = float(weight_match.group(1))
                    data["description"] = replace_description_measurements(
                        data["description"],
                        length=float(dimensions["length"]) if dimensions["length"] else None,
                        width=float(dimensions["width"]) if dimensions["width"] else None,
                        height=float(dimensions["height"]) if dimensions["height"] else None,
                        weight=weight_num,
                    )
                except Exception as desc_fix_e:
                    print(f"   [WARN] Failed to normalize description measurements: {desc_fix_e}")
            
            # CLEAN AI-hallucinated dimension fields that don't match source data
            # Remove "Product Dimensions" and "Item Depth" if AI fabricated them
            for halluc_key in ["Product Dimensions", "Item Depth"]:
                if halluc_key in data["aspects"]:
                    # Only keep if we can verify against extracted dimensions
                    if dimensions["length"] and dimensions["width"] and dimensions["height"]:
                        # Rebuild correct Product Dimensions from extracted data
                        if halluc_key == "Product Dimensions":
                            data["aspects"]["Product Dimensions"] = [f'{dimensions["length"]}"L x {dimensions["width"]}"W x {dimensions["height"]}"H']
                            print(f"   ✓ Fixed Product Dimensions from extracted data")
                        elif halluc_key == "Item Depth":
                            # Item Depth = Length for eBay
                            data["aspects"]["Item Depth"] = [f"{dimensions['length']} in"]
                            print(f"   ✓ Fixed Item Depth from extracted data")
                    else:
                        print(f"   ⚠️ Removing AI-generated {halluc_key} (cannot verify): {data['aspects'][halluc_key]}")
                        del data["aspects"][halluc_key]
            
            # Remove aspects that AI might have hallucinated or are not applicable
            # These should only be present if actually extracted from source data
            
            # CRITICAL SAFETY CHECK: If specs and attributes were empty, AI likely
            # fabricated dimensions/weight. Validate and remove suspicious values.
            specs_empty = not specs or all(not v for v in (specs.values() if isinstance(specs, dict) else []))
            attrs_empty = not attributes or all(not v for v in (attributes.values() if isinstance(attributes, dict) else []))
            if specs_empty and attrs_empty and not dimensions["weight"]:
                # No reliable source for weight - check if AI produced a suspicious value
                ai_weight = data["aspects"].get("Item Weight", [""])[0] if "Item Weight" in data["aspects"] else ""
                if ai_weight:
                    try:
                        weight_num = float(re.search(r'(\d+\.?\d*)', ai_weight).group(1))
                        if weight_num > 150:
                            print(f"   ⚠️ Removing likely hallucinated Item Weight: {ai_weight} (no source data, >150 lbs)")
                            del data["aspects"]["Item Weight"]
                    except:
                        pass
            
            optional_aspects_to_validate = ['Load Capacity', 'Seating Capacity', 'Number of Shelves', 'Number of Drawers']
            for aspect in optional_aspects_to_validate:
                if aspect in data["aspects"]:
                    val = data["aspects"][aspect][0] if data["aspects"][aspect] else ""
                    # Remove if it looks like a placeholder or invalid
                    if not val or val in ['N/A', 'Does Not Apply', '0', '0 lbs', ''] or 'excluded' in val.lower():
                        del data["aspects"][aspect]
                        print(f"   ✗ Removed invalid {aspect}: {val}")
            
            # Ensure all aspect values are lists AND clean up invalid values
            cleaned_aspects = {}
            for key, value in data["aspects"].items():
                # Skip if value is None or empty
                if value is None:
                    continue
                
                # Convert to list if string
                if isinstance(value, str):
                    if value.strip():  # Only add non-empty strings
                        cleaned_aspects[key] = [value.strip()]
                elif isinstance(value, list):
                    # Filter out empty/invalid values
                    valid_values = []
                    for v in value:
                        if v is None:
                            continue
                        v_str = str(v).strip()
                        # Skip empty strings and placeholder values
                        if v_str and v_str not in ['', 'N/A', 'null', 'None', 'undefined']:
                            valid_values.append(v_str)
                    if valid_values:
                        cleaned_aspects[key] = valid_values
                elif value:  # Other types (int, float, etc.)
                    cleaned_aspects[key] = [str(value)]
            
            data["aspects"] = cleaned_aspects
            
            # CRITICAL: eBay requires single-value fields to have only ONE value
            # These fields cannot have multiple values
            single_value_fields = [
                'Type', 'Brand', 'Material', 'Item Length', 'Item Width', 'Item Height', 
                'Item Weight', 'Shape', 'Indoor/Outdoor', 'Age Group', 
                'Country/Region of Manufacture', 'MPN', 'Number of Drawers', 
                'Number of Items in Set', 'Assembly Required', 'Load Capacity', 
                'Seating Capacity', 'Number of Shelves', 'Adjustable Height',
                'Style', 'Finish', 'Mounting', 'Color', 'Room', 'Department',
                'Pattern', 'Upholstery Fabric'  # Added more single-value fields
            ]
            for field in single_value_fields:
                if field in data["aspects"] and len(data["aspects"][field]) > 1:
                    # Keep only the first (most relevant) value
                    data["aspects"][field] = data["aspects"][field][:1]
                    print(f"   [FIX] {field}: keeping single value")
            
            # Add standard aspects if missing
            if "MPN" not in data["aspects"]:
                data["aspects"]["MPN"] = ["Does Not Apply"]
            if "Country/Region of Manufacture" not in data["aspects"]:
                data["aspects"]["Country/Region of Manufacture"] = ["China"]
            
            # Add Compatible Mattress Size for bed categories ONLY (required by eBay)
            title_lower = original_title.lower() if original_title else ""
            bed_keywords = ["bed", "bunk", "daybed", "mattress", "headboard", "footboard", "bed frame", "platform bed"]
            is_bed_product = any(kw in title_lower for kw in bed_keywords)
            
            if is_bed_product:
                if "Compatible Mattress Size" not in data["aspects"]:
                    # Detect mattress size from title
                    mattress_size = None
                    if "king" in title_lower:
                        mattress_size = "King"
                    elif "queen" in title_lower:
                        mattress_size = "Queen"
                    elif "full" in title_lower:
                        mattress_size = "Full"
                    elif "twin xl" in title_lower:
                        mattress_size = "Twin XL"
                    elif "twin" in title_lower:
                        mattress_size = "Twin"
                    
                    # Add if detected
                    if mattress_size:
                        data["aspects"]["Compatible Mattress Size"] = [mattress_size]
                        print(f"   ✓ Added Compatible Mattress Size: {mattress_size}")
            else:
                # Remove AI-hallucinated Compatible Mattress Size on non-bed products
                if "Compatible Mattress Size" in data["aspects"]:
                    print(f"   ⚠️ Removing irrelevant 'Compatible Mattress Size' from non-bed product: {data['aspects']['Compatible Mattress Size']}")
                    del data["aspects"]["Compatible Mattress Size"]
                # Also remove For Gun Type if not a safe/gun product
                safe_keywords = ["safe", "gun", "security"]
                is_safe_product = any(kw in title_lower for kw in safe_keywords)
                if not is_safe_product and "For Gun Type" in data["aspects"]:
                    print(f"   ⚠️ Removing irrelevant 'For Gun Type' from non-safe product")
                    del data["aspects"]["For Gun Type"]
            
            # IMPROVED: Validate and fix description HTML
            description = data.get("description", "")
            description = self._clean_placeholder_text(description)
            description = self._validate_and_fix_html(description)
            
            # Truncate description if too long (eBay limit: 4000 chars)
            if len(description) > 3800:
                print(f"[WARN] Description is {len(description)} chars, truncating...")
                description = self._smart_truncate_html(description, 3800)
            
            data["description"] = description
            
            # FIXED: Use BOTH original title AND AI title for category matching
            # This ensures we catch keywords even if AI rewrites the title
            original_title_lower = original_title.lower() if original_title else ""
            ai_title_lower = data.get("title", "").lower()
            combined_text = f"{original_title_lower} {ai_title_lower}"
            
            category_id = self._get_category_from_keywords(combined_text)
            category_name = None

            # Prefer the shared category matcher so collected-product generation
            # uses the same taxonomy rules as publish-time auto-category.
            try:
                from src.services.ebay_category_matcher import create_category_matcher
                matcher = create_category_matcher(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
                matched_id, matched_name, matched_aspects = matcher.get_category_and_aspects(
                    original_title or data.get("title", ""),
                    data.get("aspects", {}),
                    description,
                )
                if matched_id:
                    category_id = matched_id
                    category_name = matched_name
                    data["aspects"] = matched_aspects or data.get("aspects", {})
            except Exception as cat_err:
                print(f"   [WARN] Shared category matcher unavailable, using keyword fallback: {cat_err}")
            
            # Always prefer our validated category mapping, ignore AI's raw categoryId
            data["categoryId"] = category_id
            if category_name:
                data["categoryName"] = category_name
            
            print(f"✅ Optimization complete. Title: {data.get('title', '')[:50]}...")
            print(f"   Aspects count: {len(data.get('aspects', {}))}")
            print(f"   Description length: {len(data.get('description', ''))}")
            print(f"   CategoryId: {data.get('categoryId')}")
            
            return data

        except Exception as e:
            print(f"❌ Qwen Optimization Failed: {e}")
            traceback.print_exc()
            return self._fallback_result(original_title, original_description, dimensions)
    
    def _clean_placeholder_text(self, html: str) -> str:
        """
        Remove AI-generated placeholder text like 'Not Available — Dimensions not provided by manufacturer'
        from product descriptions. These look unprofessional on eBay listings.
        """
        if not html:
            return html
        
        import re
        
        # Patterns to match and remove entire table rows containing placeholder text
        placeholder_patterns = [
            # Match <tr> rows containing "not provided by manufacturer" or "Not Available"
            r'<tr[^>]*>(?:(?!</tr>).)*(?:not\s+(?:provided|available|specified)\s+(?:by\s+)?manufacturer)(?:(?!</tr>).)*</tr>',
            r'<tr[^>]*>(?:(?!</tr>).)*Not\s+Available\s*[—–-]\s*(?:Dimensions?|Weight|Specs?)\s+not\s+provided(?:(?!</tr>).)*</tr>',
            # Match standalone text
            r'Not\s+Available\s*[—–-]\s*(?:Dimensions?|Weight|Specs?|Size)\s+not\s+provided\s*(?:by\s+manufacturer)?',
            r'(?:Dimensions?|Weight|Specs?)\s+not\s+(?:provided|available|specified)\s*(?:by\s+(?:the\s+)?manufacturer)?',
            # Match "manufacturer does not provide" variants
            r'(?:the\s+)?manufacturer\s+(?:does\s+not|didn\'t|has\s+not)\s+provide[d]?\s+(?:this\s+)?(?:information|data|specs?|dimensions?|details?)',
        ]
        
        original_len = len(html)
        for pattern in placeholder_patterns:
            html = re.sub(pattern, '', html, flags=re.IGNORECASE | re.DOTALL)
        
        # Clean up empty table rows left behind
        html = re.sub(r'<tr[^>]*>\s*<td[^>]*>\s*</td>\s*<td[^>]*>\s*</td>\s*</tr>', '', html)
        # Clean up consecutive empty lines
        html = re.sub(r'\n{3,}', '\n\n', html)
        
        if len(html) < original_len:
            print(f"   🧹 Cleaned placeholder text from description ({original_len - len(html)} chars removed)")
        
        return html

    def _validate_and_fix_html(self, html: str) -> str:
        """
        Validate HTML and fix common issues like unclosed tags.
        Ensures description is properly formatted.
        """
        if not html:
            return ""
        
        # Check for obvious truncation indicators
        truncation_indicators = [
            '<td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#636e72',  # cut mid-style
            '<tr style="background:#fafafa"><td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#636e72',
            '"><td style="padding:10px',  # cut mid-table
        ]
        
        is_truncated = False
        for indicator in truncation_indicators:
            if html.endswith(indicator) or html.rstrip().endswith(indicator.rstrip()):
                is_truncated = True
                print(f"   ⚠️ Detected truncated HTML ending with: ...{indicator[-30:]}")
                break
        
        # Also check if HTML ends with incomplete tag
        if re.search(r'<[^>]*$', html) or re.search(r'="[^"]*$', html):
            is_truncated = True
            print("   ⚠️ Detected incomplete HTML tag at end")
        
        if is_truncated:
            # Find the last complete section (</table>, </div>, </ul>, </p>)
            safe_endings = ['</table>', '</div>', '</ul>', '</p>', '</tr>', '</li>']
            best_pos = 0
            for ending in safe_endings:
                pos = html.rfind(ending)
                if pos > best_pos:
                    best_pos = pos + len(ending)
            
            if best_pos > len(html) * 0.7:  # Only truncate if we keep at least 70%
                html = html[:best_pos]
                print(f"   ✓ Truncated to last complete section at pos {best_pos}")
        
        # Count and fix unclosed tags
        tags_to_check = ['div', 'table', 'tr', 'td', 'ul', 'li', 'p', 'span', 'h1', 'h2', 'h3']
        for tag in tags_to_check:
            open_count = len(re.findall(f'<{tag}[\\s>]', html, re.IGNORECASE))
            close_count = len(re.findall(f'</{tag}>', html, re.IGNORECASE))
            if open_count > close_count:
                html += f'</{tag}>' * (open_count - close_count)
        
        return html
    
    def _smart_truncate_html(self, html: str, max_length: int) -> str:
        """
        Intelligently truncate HTML to max_length while keeping structure intact.
        """
        if len(html) <= max_length:
            return html
        
        # Find the last complete section before max_length
        truncated = html[:max_length]
        
        # Look for safe cut points (end of sections)
        safe_points = [
            truncated.rfind('</div></div>'),  # End of a section
            truncated.rfind('</table>'),       # End of table
            truncated.rfind('</ul>'),          # End of list
            truncated.rfind('</tr>'),          # End of table row
            truncated.rfind('</p>'),           # End of paragraph
        ]
        
        best_point = max([p for p in safe_points if p > max_length * 0.6] or [max_length])
        
        if best_point < max_length:
            # Add closing tag length
            if '</div></div>' in html[best_point:best_point+20]:
                truncated = html[:best_point + 12]
            elif '</table>' in html[best_point:best_point+10]:
                truncated = html[:best_point + 8]
            else:
                truncated = html[:best_point + 6]  # Assume </div> or similar
        
        # Validate and fix the truncated HTML
        return self._validate_and_fix_html(truncated)
    
    def _fallback_result(self, original_title, original_description, dimensions):
        """返回降级结果"""
        aspects = {
            "Brand": [get_store_profile().brand_name],
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
    
    def _get_category_from_keywords(self, title_lower: str) -> str:
        """
        COMPREHENSIVE keyword-based eBay category mapping for GigaCloud/大建云仓 products.
        Returns verified eBay category IDs based on product title keywords.
        This is more reliable than AI-generated category IDs.
        
        Organized by GigaCloud main categories:
        - Living Room
        - Bedroom
        - Dining Room
        - Office
        - Bathroom
        - Outdoor/Garden
        - Pet Supplies
        - Kids Furniture
        - Storage & Organization
        """
        # Priority order matters - more specific matches first
        # NOTE: Category IDs verified against eBay Taxonomy API 2026-02-05
        CATEGORY_MAP = [
            # ==================== AUTO PARTS & ACCESSORIES ====================
            # Running Boards & Nerf Bars (eBay Motors > Exterior Parts & Accessories)
            (["running", "board"], "262210"),       # Running Boards & Nerf Bars
            (["nerf", "bar"], "262210"),            # Nerf Bars
            (["side", "step"], "262210"),           # Side Steps → Running Boards
            
            # Trailer Hitches (eBay Motors > Towing & Hauling)
            (["trailer", "hitch"], "174020"),       # Trailer Hitches
            (["tow", "hitch"], "174020"),           # Tow Hitches
            (["hitch", "receiver"], "174020"),      # Hitch Receivers
            
            # Hitch Cargo Carriers (eBay Motors > Towing & Hauling)
            (["hitch", "cargo"], "174021"),         # Hitch Cargo Carriers
            (["hitch", "carrier"], "174021"),       # Hitch Carriers
            (["hitch", "basket"], "174021"),        # Hitch Baskets
            
            # Roof Racks & Cross Bars (eBay Motors > Exterior Parts)
            (["roof", "rack"], "262216"),           # Roof Racks
            (["roof", "basket"], "262216"),         # Roof Baskets
            (["roof", "carrier"], "262216"),        # Roof Carriers
            (["cargo", "basket"], "262216"),        # Cargo Baskets (roof-mounted)
            
            # Tailgate Parts (eBay Motors > Truck Parts)
            (["tailgate", "assist"], "262093"),     # Tailgate Assist
            (["tailgate", "lift"], "262093"),       # Tailgate Lift
            (["tailgate", "ramp"], "262093"),       # Tailgate Ramp
            
            # Bike Trailers (Sporting Goods > Cycling > Bicycle Accessories > Trailers)
            (["bike", "trailer"], "85040"),         # Bike Trailers
            (["bicycle", "trailer"], "85040"),      # Bicycle Trailers
            (["bike", "cargo", "trailer"], "85040"),# Bike Cargo Trailers
            
            # ==================== WELDING EQUIPMENT ====================
            # MIG Welders (124822) — Business & Industrial > Welding Equipment
            (["mig", "welder"], "124822"),          # MIG Welders
            (["mig", "welding"], "124822"),         # MIG Welding Machines
            # TIG Welders (124825) — Business & Industrial > Welding Equipment
            (["tig", "welder"], "124825"),          # TIG Welders
            (["tig", "welding"], "124825"),         # TIG Welding Machines
            (["pulse", "welder"], "124825"),        # Pulse TIG Welders
            # Multiprocess Welders (182899)
            (["welder"], "124822"),                 # General welders → MIG (most common)
            (["welding", "machine"], "124822"),     # Welding Machines → MIG
            
            # ==================== SHOWER & BATH ====================
            # Shower Doors (147147) — Home & Garden > Bath > Showers
            (["shower", "door"], "147147"),         # Shower Doors
            (["pivot", "shower"], "147147"),        # Pivot Shower Doors
            (["shower", "glass"], "147147"),        # Shower Glass Doors
            (["shower", "enclosure"], "147147"),    # Shower Enclosures
            
            # ==================== LAWN & GARDEN EQUIPMENT ====================
            # Lawn Mowers (260921) — Home & Garden > Yard, Garden > Lawn Mowers
            (["lawn", "mower"], "260921"),          # Lawn Mowers
            (["reel", "mower"], "260921"),          # Reel/Push Mowers
            (["push", "mower"], "260921"),          # Push Mowers
            # Seeders & Spreaders (118869) — Yard, Garden > Garden Hand Tools
            (["compost", "spreader"], "118869"),    # Compost Spreaders
            (["peat", "moss", "spreader"], "118869"), # Peat Moss Spreaders
            (["lawn", "spreader"], "118869"),       # Lawn Spreaders
            (["seed", "spreader"], "118869"),       # Seed Spreaders
            
            # ==================== POOL & SPA ====================
            # Pool Covers & Reels (181068) — Pools & Spas > Pool Equipment
            (["pool", "cover"], "181068"),          # Pool Covers
            (["pool", "dome"], "181068"),           # Pool Domes
            (["pool", "enclosure"], "181068"),      # Pool Enclosures
            (["pool", "fence"], "181068"),          # Pool Fences → Pool Equipment
            
            # ==================== LIGHTING ====================
            # Chandeliers & Ceiling Fixtures (117503) — Lamps, Lighting & Ceiling Fans
            (["floor", "lamp"], "112581"),         # Floor Lamps
            (["standing", "lamp"], "112581"),      # Standing Lamps
            (["crystal", "lamp"], "112581"),       # Crystal Lamps
            (["chandelier"], "117503"),             # Chandeliers
            (["starburst"], "117503"),              # Starburst Chandeliers
            (["pendant", "light"], "117503"),       # Pendant Lights
            (["ceiling", "light"], "117503"),       # Ceiling Lights
            (["ceiling", "fixture"], "117503"),     # Ceiling Fixtures
            
            # ==================== TOYS & OUTDOOR STRUCTURES ====================
            # Inflatable Bouncers (145979) — Toys & Hobbies > Outdoor Toys
            (["inflatable", "slide"], "145979"),    # Inflatable Slides
            (["inflatable", "water"], "145979"),    # Inflatable Water Parks
            (["bounce", "house"], "145979"),        # Bounce Houses
            (["bouncy", "castle"], "145979"),       # Bouncy Castles
            (["inflatable", "bounce"], "145979"),   # Inflatable Bouncers
            
            # ==================== WAGONS & CARTS ====================
            # Wheelbarrows, Carts & Wagons (75671)
            (["wagon", "cart"], "75671"),           # Wagon Carts
            (["folding", "wagon"], "75671"),        # Folding Wagons
            (["utility", "wagon"], "75671"),        # Utility Wagons
            (["double", "decker", "wagon"], "75671"), # Double Decker Wagons
            (["collapsible", "wagon"], "75671"),    # Collapsible Wagons
            
            # ==================== GOLF ====================
            # Golf Club Bags (30109) — Sporting Goods > Golf > Golf Club Equipment
            (["golf", "bag"], "30109"),             # Golf Bags
            (["golf", "organizer"], "30109"),       # Golf Bag Organizers
            (["golf", "storage"], "30109"),         # Golf Storage
            
            # ==================== SPORTS & FITNESS ====================
            (["treadmill"], "15280"),               # Treadmills (Cardio Equipment)
            (["running", "machine"], "15280"),      # Running Machines -> Treadmills
            (["exercise", "bike"], "58102"),        # Exercise Bikes
            (["stationary", "bike"], "58102"),      # Stationary Bikes
            (["elliptical"], "72602"),              # Ellipticals
            (["rowing", "machine"], "28060"),       # Rowing Machines
            (["weight", "bench"], "158916"),        # Weight Benches
            (["home", "gym"], "158916"),            # Home Gym Equipment
            (["power", "rack"], "158916"),          # Power Racks
            (["dumbbell"], "137864"),               # Dumbbells
            (["kettlebell"], "137864"),             # Kettlebells
            (["yoga", "mat"], "158927"),            # Yoga Mats
            (["trampoline"], "57275"),              # Trampolines
            (["rebounder"], "57275"),               # Trampolines (rebounder)
            (["mini", "trampoline"], "57275"),      # Mini Trampolines
            (["fitness", "trampoline"], "57275"),   # Fitness Trampolines
            
            # ==================== TRAVEL & LUGGAGE (16080 = Luggage) ====================
            (["luggage", "set"], "16080"),          # Luggage
            (["suitcase", "set"], "16080"),         # Luggage
            (["hardshell", "luggage"], "16080"),    # Luggage (Hardshell)
            (["hardside", "luggage"], "16080"),     # Luggage
            (["softside", "luggage"], "16080"),     # Luggage
            (["carry", "on"], "16080"),             # Luggage (Carry-On)
            (["cabin", "bag"], "16080"),            # Luggage
            (["spinner", "suitcase"], "16080"),     # Luggage
            (["rolling", "luggage"], "16080"),      # Luggage
            (["travel", "suitcase"], "16080"),      # Luggage
            (["luggage"], "16080"),                 # Luggage (general)
            (["suitcase"], "16080"),                # Luggage
            (["duffle", "bag"], "16080"),           # Luggage (Duffel)
            (["duffel", "bag"], "16080"),           # Luggage
            (["garment", "bag"], "16080"),          # Luggage
            (["travel", "bag"], "16080"),           # Luggage
            
            # ==================== PET SUPPLIES ====================
            # Dog Products
            (["dog", "crate"], "121851"),           # Dog Cages & Crates
            (["dog", "kennel"], "121851"),          # Dog Cages & Crates
            (["dog", "cage"], "121851"),            # Dog Cages & Crates
            (["dog", "pen"], "20748"),              # Fences & Exercise Pens
            (["dog", "bed"], "20744"),              # Beds
            (["dog", "house"], "108884"),           # Dog Houses
            (["dog", "ramp"], "116389"),            # Ramps & Stairs
            (["dog", "stairs"], "116389"),          # Ramps & Stairs
            (["dog", "feeder"], "116385"),          # Dog Bowls & Feeders
            (["pet", "gate"], "20748"),             # Fences & Exercise Pens
            (["pet", "fence"], "20748"),            # Fences & Exercise Pens
            (["pet", "carrier"], "177788"),         # Carriers & Totes
            (["pet", "playpen"], "20748"),          # Fences & Exercise Pens
            (["pet", "stroller"], "116380"),        # Strollers
            (["pet", "bed"], "20744"),              # Beds
            (["pet", "bowl"], "116385"),            # Pet Bowls
            (["pet", "feeder"], "116385"),          # Pet Feeders
            
            # Cat Products
            (["cat", "tree"], "20740"),             # Furniture & Scratchers
            (["cat", "tower"], "20740"),            # Furniture & Scratchers
            (["cat", "condo"], "20740"),            # Furniture & Scratchers
            (["cat", "house"], "20740"),            # Furniture & Scratchers
            (["cat", "litter"], "100411"),          # Litter Boxes
            (["litter", "box"], "100411"),          # Litter Boxes
            (["cat", "bed"], "66762"),              # Beds
            (["cat", "scratch"], "20740"),          # Furniture & Scratchers
            (["scratching", "post"], "20740"),      # Furniture & Scratchers
            (["cat", "enclosure"], "100411"),       # Litter Boxes
            
            # Chicken/Poultry - Use specific keywords to avoid false matches
            (["chicken", "coop"], "63108"),         # Cages, Hutches & Enclosure
            (["poultry", "coop"], "63108"),         # Cages, Hutches & Enclosure
            (["chicken", "run"], "63108"),          # Cages, Hutches & Enclosure
            
            # Small Animals
            (["rabbit", "hutch"], "63108"),         # Cages, Hutches & Enclosure
            (["rabbit", "cage"], "63108"),          # Cages, Hutches & Enclosure
            (["hamster", "cage"], "26684"),         # Small Animal Cages
            (["guinea", "pig"], "26684"),           # Small Animal Cages
            (["bird", "cage"], "14769"),            # Bird Cages
            (["aviary"], "14769"),                  # Bird Aviaries
            (["reptile", "cage"], "63112"),         # Reptile Cages
            (["terrarium"], "63112"),               # Terrariums
            (["fish", "tank"], "77639"),            # Aquarium Tanks
            (["aquarium"], "77639"),                # Aquariums
            
            # ==================== KIDS FURNITURE ====================
            (["race", "car", "bed"], "175754"),     # Kids Beds (Race Car Beds)
            (["car", "bed"], "175754"),             # Kids Car Beds
            (["kids", "bed"], "175754"),            # Kids Beds
            (["children", "bed"], "175754"),        # Children's Beds
            (["toddler", "bed"], "175754"),         # Toddler Beds
            (["bunk", "bed"], "175754"),            # Bunk Beds -> Kids Beds
            (["loft", "bed"], "175754"),            # Loft Beds
            (["twin", "bed"], "175758"),            # Twin Beds -> Bed Frames
            (["kids", "desk"], "25290"),            # Kids Desks
            (["kids", "chair"], "66758"),           # Kids Chairs
            (["kids", "table"], "66756"),           # Kids Tables
            (["toy", "storage"], "66763"),          # Kids Storage
            (["toy", "box"], "66763"),              # Toy Boxes
            (["toy", "chest"], "66763"),            # Toy Chests
            (["kids", "bookshelf"], "66761"),       # Kids Bookcases
            (["changing", "table"], "20422"),       # Changing Tables
            (["baby", "crib"], "20421"),            # Baby Cribs
            (["crib"], "20421"),                    # Cribs

            # ==================== OUTDOOR SPECIAL CASES ====================
            # Must come before indoor sofa / chair rules.
            (["outdoor", "daybed"], "138996"),      # Outdoor Daybeds
            (["patio", "daybed"], "138996"),        # Patio Daybeds
            (["sunbed"], "138996"),                 # Outdoor Sunbeds
            (["outdoor", "chair", "set"], "79682"),# Patio Chair Sets
            (["patio", "chair", "set"], "79682"),  # Patio Chair Sets
            (["rattan", "chair", "set"], "79682"), # Rattan Chair Sets
            (["outdoor", "armchair"], "79682"),    # Outdoor Armchairs
            (["patio", "armchair"], "79682"),      # Patio Armchairs
            (["armchairs", "set"], "79682"),       # Armchairs Set
            
            # ==================== MATTRESSES & BEDDING ====================
            (["air", "mattress"], "106198"),        # Inflatable Mattresses/Blow up beds
            (["inflatable", "bed"], "106198"),      # Inflatable Mattresses
            (["blow", "up", "bed"], "106198"),      # Inflatable Mattresses
            (["mattress", "topper"], "175750"),     # Mattress Pads & Toppers
            (["mattress", "protector"], "175750"),  # Mattress Pads
            (["mattress", "pad"], "175750"),        # Mattress Pads
            (["box", "spring"], "175749"),          # Box Springs
            (["foundation"], "175749"),             # Box Springs & Foundations
            (["mattress"], "131588"),               # Standard Mattresses
            
            # ==================== BEDROOM FURNITURE ====================
            # Specific Parts (Must come BEFORE "Bed Frame" to avoid misclassification)
            (["headboard"], "175756"),              # Headboards & Footboards
            (["footboard"], "175756"),              # Headboards & Footboards
            (["bed", "slat"], "175759"),            # Bed Slats & Rails
            (["bed", "rail"], "175759"),            # Bed Rails
            (["bed", "riser"], "175759"),           # Bed Risers
            (["adjustable", "base"], "175758"),     # Adjustable Beds (map to Beds)
            
            (["bed", "frame"], "175758"),           # Bed Frames
            (["platform", "bed"], "175758"),        # Platform Beds
            (["upholstered", "bed"], "175758"),     # Upholstered Beds
            (["panel", "bed"], "175758"),           # Panel Beds
            (["canopy", "bed"], "175758"),          # Canopy Beds
            (["sleigh", "bed"], "175758"),          # Sleigh Beds
            (["murphy", "bed"], "175758"),          # Murphy Beds
            (["daybed"], "175758"),                 # Daybeds
            (["sofa", "bed"], "175758"),            # Sofa Beds
            (["nightstand"], "38199"),              # Nightstands
            (["night", "stand"], "38199"),          # Nightstands
            (["bedside", "table"], "38199"),        # Bedside Tables
            (["dresser"], "114397"),                # Dressers & Chests of Drawers
            (["chest", "drawer"], "114397"),        # Chests of Drawers
            (["drawer", "chest"], "114397"),        # Chests of Drawers
            (["wardrobe"], "103430"),               # Armoires & Wardrobes
            (["armoire"], "103430"),                # Armoires & Wardrobes
            (["closet"], "103430"),                 # Armoires & Wardrobes
            (["clothes", "rack"], "175755"),        # Clothing Racks
            (["garment", "rack"], "175755"),        # Garment Racks
            (["jewelry", "armoire"], "103430"),     # Jewelry Armoires
            (["mirror"], "20580"),                  # Mirrors
            
            # ==================== LIVING ROOM ====================
            # TV & Entertainment
            (["tv", "stand"], "20488"),             # TV Stands & Entertainment Units
            (["television", "stand"], "20488"),     # TV Stands
            (["entertainment", "center"], "20488"), # Entertainment Centers
            (["media", "console"], "20488"),        # Media Consoles
            (["media", "center"], "20488"),         # Media Centers
            (["tv", "cabinet"], "20488"),           # TV Cabinets
            (["fireplace", "tv"], "20488"),         # Fireplace TV Stands
            (["electric", "fireplace"], "175759"),  # Electric Fireplaces
            
            # Outdoor Seating (MUST come before generic Sofas/Chairs to avoid mismatch)
            (["outdoor", "daybed"], "138996"),      # Outdoor Daybeds
            (["patio", "daybed"], "138996"),        # Patio Daybeds
            (["sunbed"], "138996"),                 # Outdoor Sunbeds
            (["outdoor", "chair", "set"], "79682"),# Patio Chair Sets
            (["patio", "chair", "set"], "79682"),  # Patio Chair Sets
            (["rattan", "chair", "set"], "79682"), # Rattan Chair Sets
            (["outdoor", "lounge"], "79682"),       # Outdoor/Patio Chaise Lounges
            (["outdoor", "chaise"], "79682"),       # Outdoor Chaise Lounges
            (["patio", "lounge"], "79682"),         # Patio Lounge Chairs
            (["patio", "recliner"], "79682"),       # Patio Recliners
            (["patio", "chaise"], "79682"),         # Patio Chaise Lounges
            
            # Sofas & Seating
            (["sectional", "sofa"], "38208"),       # Sectional Sofas
            (["sectional"], "38208"),               # Sectional Sofas
            (["loveseat"], "38208"),                # Loveseats
            (["futon"], "38208"),                   # Futons
            (["sleeper", "sofa"], "38208"),         # Sleeper Sofas
            (["chaise", "lounge"], "38208"),        # Chaise Lounges (indoor)
            (["recliner", "sofa"], "38208"),        # Reclining Sofas
            (["recliner"], "38208"),                # Recliners
            (["power", "recliner"], "38208"),       # Power Recliners
            (["massage", "chair"], "181270"),       # Massage Chairs
            (["gaming", "chair"], "22513"),         # Gaming Chairs
            (["accent", "chair"], "118218"),        # Accent Chairs
            (["arm", "chair"], "118218"),           # Armchairs
            (["lounge", "chair"], "118218"),        # Lounge Chairs
            (["club", "chair"], "118218"),          # Club Chairs
            (["rocking", "chair"], "20877"),        # Rocking Chairs
            (["glider"], "20877"),                  # Gliders
            (["papasan"], "118218"),                # Papasan Chairs
            (["bean", "bag"], "40086"),             # Bean Bags
            (["floor", "chair"], "118218"),         # Floor Chairs
            (["ottoman"], "20490"),                 # Ottomans (verified by eBay API)
            (["footstool"], "20490"),               # Footstools
            (["pouf"], "20490"),                    # Poufs
            
            # Tables
            (["coffee", "table"], "38204"),         # Coffee Tables
            (["cocktail", "table"], "38204"),       # Cocktail Tables
            (["sofa", "table"], "38205"),           # Sofa Tables
            (["console", "table"], "38205"),        # Console Tables
            (["entry", "table"], "38205"),          # Entry Tables
            (["foyer", "table"], "38205"),          # Foyer Tables
            (["hall", "table"], "38205"),           # Hall Tables
            (["end", "table"], "38200"),            # End Tables
            (["side", "table"], "38200"),           # Side Tables
            (["accent", "table"], "38200"),         # Accent Tables
            (["lamp", "table"], "38200"),           # Lamp Tables
            (["nesting", "table"], "38204"),        # Nesting Tables
            
            # Bookcases & Shelving
            (["bookshelf"], "3199"),                # Bookcases & Shelving
            (["bookcase"], "3199"),                 # Bookcases
            (["book", "shelf"], "3199"),            # Book Shelves
            (["cube", "storage"], "3199"),          # Cube Storage
            (["cube", "organizer"], "3199"),        # Cube Organizers
            (["shelf", "unit"], "3199"),            # Shelving Units
            (["etagere"], "3199"),                  # Etageres
            (["ladder", "shelf"], "3199"),          # Ladder Shelves
            (["wall", "shelf"], "20487"),           # Wall Shelves
            (["floating", "shelf"], "20487"),       # Floating Shelves
            (["display", "cabinet"], "20493"),      # Display Cabinets
            (["curio", "cabinet"], "20493"),        # Curio Cabinets
            
            # ==================== DINING ROOM ====================
            (["dining", "table"], "38204"),         # Dining Tables
            (["dining", "set"], "107578"),          # Dining Sets (Home & Garden > Furniture > Dining Sets)
            (["dining", "chair"], "54235"),         # Dining Chairs → Chairs (54235, leaf)
            (["counter", "chair"], "54235"),        # Counter Height Chairs → Chairs
            (["bar", "stool"], "103431"),           # Bar Stools → Bar Stools (103431, leaf)
            (["counter", "stool"], "103431"),       # Counter Stools → Bar Stools
            (["barstool"], "103431"),               # Bar Stools → 103431
            (["pub", "table"], "38204"),            # Pub Tables → Dining/Coffee Tables
            (["bar", "table"], "38204"),            # Bar Tables → Dining/Coffee Tables
            (["counter", "table"], "38204"),        # Counter Height Tables → Dining/Coffee Tables
            (["kitchen", "island"], "177000"),      # Kitchen Islands & Carts
            (["kitchen", "cart"], "177000"),        # Kitchen Carts
            (["microwave", "cart"], "177000"),      # Microwave Carts
            (["baker", "rack"], "177000"),          # Baker's Racks
            (["buffet"], "183322"),                 # Sideboards & Buffets (183322, leaf)
            (["sideboard"], "183322"),              # Sideboards → 183322
            (["credenza"], "183322"),               # Credenzas → 183322
            (["china", "cabinet"], "38217"),        # China Cabinets
            (["hutch"], "38217"),                   # Hutches
            (["wine", "rack"], "45331"),            # Wine Racks
            (["wine", "cabinet"], "45331"),         # Wine Cabinets
            (["bar", "cabinet"], "45331"),          # Bar Cabinets
            (["pantry"], "42428"),                  # Pantry Cabinets
            (["pantry", "cabinet"], "42428"),       # Pantry Cabinets
            
            # ==================== SAFES & SECURITY (MUST come before Office to avoid "home office" false match) ====================
            (["gun", "safe"], "20584"),             # Gun Safes
            (["security", "safe"], "20584"),        # Security Safes
            (["safe", "box"], "20584"),             # Safe Boxes
            (["safe"], "20584"),                    # Safes (eBay: Home > Safes)
            (["lockbox"], "20584"),                 # Lock Boxes
            
            # ==================== OFFICE FURNITURE ====================
            (["office", "chair"], "54235"),         # Office Chairs
            (["desk", "chair"], "54235"),           # Desk Chairs
            (["executive", "chair"], "54235"),      # Executive Chairs
            (["ergonomic", "chair"], "54235"),      # Ergonomic Chairs
            (["task", "chair"], "54235"),           # Task Chairs
            (["drafting", "chair"], "54235"),       # Drafting Chairs
            (["computer", "desk"], "88057"),        # Computer Desks
            (["writing", "desk"], "88057"),         # Writing Desks
            (["executive", "desk"], "88057"),       # Executive Desks
            (["l-shaped", "desk"], "88057"),        # L-Shaped Desks
            (["l", "desk"], "88057"),               # L-Desks
            (["corner", "desk"], "88057"),          # Corner Desks
            (["standing", "desk"], "88057"),        # Standing Desks
            (["adjustable", "desk"], "88057"),      # Adjustable Desks
            (["secretary", "desk"], "88057"),       # Secretary Desks
            (["home", "office"], "88057"),          # Home Office Desks
            (["desk"], "88057"),                    # General Desks
            (["file", "cabinet"], "25306"),         # Filing Cabinets
            (["filing", "cabinet"], "25306"),       # Filing Cabinets
            (["printer", "stand"], "111508"),       # Printer Stands
            (["monitor", "stand"], "111508"),       # Monitor Stands
            (["keyboard", "tray"], "111508"),       # Keyboard Trays
            
            # ==================== BATHROOM ====================
            (["bathroom", "vanity"], "32878"),      # Bathroom Vanities
            (["sink", "vanity"], "32878"),          # Sink Vanities
            (["vanity", "cabinet"], "32878"),       # Vanity Cabinets
            (["vanity"], "32878"),                  # Vanities
            (["bathroom", "cabinet"], "42428"),     # Bathroom Cabinets
            (["medicine", "cabinet"], "42428"),     # Medicine Cabinets
            (["linen", "cabinet"], "42428"),        # Linen Cabinets
            (["bathroom", "mirror"], "133696"),     # Bathroom Mirrors
            (["bathroom", "shelf"], "42428"),       # Bathroom Shelves
            (["towel", "rack"], "42427"),           # Towel Racks
            (["towel", "bar"], "42427"),            # Towel Bars
            (["toilet", "paper"], "42425"),         # Toilet Paper Holders
            (["shower", "bench"], "42429"),         # Shower Benches
            (["bath", "stool"], "42429"),           # Bath Stools
            (["laundry", "hamper"], "43527"),       # Laundry Hampers
            (["laundry", "basket"], "43527"),       # Laundry Baskets
            
            # ==================== OUTDOOR & GARDEN ====================
            (["patio", "set"], "139849"),           # Patio & Garden Furniture Sets (139849, leaf)
            (["patio", "furniture"], "139849"),     # Patio & Garden Furniture Sets
            (["outdoor", "sofa"], "139849"),        # Outdoor Sofas → Patio Sets
            (["outdoor", "sectional"], "139849"),   # Outdoor Sectionals → Patio Sets
            (["outdoor", "chair"], "79682"),        # Outdoor Chairs → Patio Chairs
            (["adirondack"], "79682"),              # Adirondack Chairs
            (["outdoor", "table"], "79686"),        # Outdoor Tables
            (["patio", "table"], "79686"),          # Patio Tables
            (["garden", "bench"], "79683"),         # Garden Benches
            (["park", "bench"], "79683"),           # Park Benches
            (["outdoor", "bench"], "79683"),        # Outdoor Benches
            (["porch", "swing"], "79694"),          # Porch Swings
            (["hammock"], "79693"),                 # Hammocks
            (["gazebo"], "180995"),                 # Gazebos
            (["pergola"], "180994"),                # Pergolas
            (["canopy"], "180994"),                 # Canopies
            (["outdoor", "storage"], "42430"),      # Outdoor Storage
            (["deck", "box"], "42430"),             # Deck Boxes
            (["patio", "umbrella"], "180998"),      # Patio Umbrellas
            (["umbrella", "base"], "180998"),       # Umbrella Bases
            (["fire", "pit"], "85916"),             # Fire Pits
            (["outdoor", "fireplace"], "85916"),    # Outdoor Fireplaces
            (["planter"], "20518"),                  # Baskets, Pots, Window Boxes & Saucers
            (["plant", "stand"], "29514"),          # Plant Stands
            (["fence", "panel"], "139946"),         # Fence Panels
            (["garden", "fence"], "139946"),        # Garden Fence Panels
            (["garden", "cart"], "75671"),          # Wheelbarrows, Carts & Wagons
            (["wheelbarrow"], "75671"),              # Wheelbarrows, Carts & Wagons
            (["greenhouse"], "139939"),             # Greenhouses
            (["potting", "bench"], "139939"),       # Potting Benches
            (["raised", "bed"], "181017"),          # Raised Garden Beds
            
            # ==================== STORAGE & ORGANIZATION ====================
            (["storage", "cabinet"], "20487"),      # Cabinets & Cupboards
            (["storage", "shelf"], "20487"),        # Storage Shelves
            (["utility", "cabinet"], "20487"),      # Utility Cabinets
            (["garage", "cabinet"], "20487"),       # Garage Cabinets
            (["storage", "bench"], "262980"),       # Storage Benches
            (["entryway", "bench"], "262980"),      # Entryway Benches
            (["shoe", "bench"], "262980"),          # Shoe Benches
            (["hall", "tree"], "261263"),           # Hall Trees
            (["shoe", "rack"], "38221"),            # Shoe Racks
            (["shoe", "cabinet"], "38221"),         # Shoe Cabinets
            (["shoe", "bench"], "38221"),           # Shoe Benches
            (["shoe", "storage"], "38221"),         # Shoe Storage
            (["coat", "rack"], "32880"),            # Coat Racks
            (["coat", "stand"], "32880"),           # Coat Stands
            (["umbrella", "stand"], "108044"),      # Umbrella Stands
            (["key", "cabinet"], "175763"),         # Key Cabinets
            (["jewelry", "cabinet"], "262017"),     # Jewelry Boxes & Organizers
            # NOTE: safe/lockbox moved to SAFES & SECURITY section (before Office) with correct category 20584
            (["trunk"], "181087"),                  # Trunks/Chests
            (["cooler"], "79691"),                  # Ice Chests & Coolers (79691, leaf)
            (["ice", "chest"], "79691"),             # Ice Chests
            (["chest"], "181087"),                  # Chests (storage)
            
            # ==================== TABLES (GENERAL) ====================
            (["folding", "table"], "98044"),        # Folding Tables
            (["card", "table"], "98044"),           # Card Tables
            (["utility", "table"], "98044"),        # Utility Tables
            (["work", "table"], "98044"),           # Work Tables
            (["craft", "table"], "98044"),          # Craft Tables
            (["sewing", "table"], "98044"),         # Sewing Tables
            
            # ==================== MISC FURNITURE ====================
            (["room", "divider"], "175764"),        # Room Dividers
            (["screen"], "175764"),                 # Screens
            (["partition"], "175764"),              # Partitions
            (["sofa"], "38208"),                    # Sofas (catch-all)
            (["couch"], "38208"),                   # Couches (catch-all)
            (["table"], "38204"),                   # Tables (catch-all)
            (["chair"], "54235"),                   # Chairs (catch-all)
            (["cabinet"], "38221"),                 # Cabinets (catch-all)
            
            # Default fallback
        ]
        
        import re
        
        # Helper function for word boundary matching
        def word_match(keyword, text):
            """Check if keyword exists as a word (not substring) in text"""
            # Use word boundary regex for accurate matching
            pattern = r'\b' + re.escape(keyword) + r'\b'
            return bool(re.search(pattern, text, re.IGNORECASE))
        
        # Check each pattern in order
        for keywords, category_id in CATEGORY_MAP:
            if all(word_match(kw, title_lower) for kw in keywords):
                print(f"   📂 Category matched: {keywords} -> {category_id}")
                return category_id
        
        # Default: Sofas/Furniture (safest default for GigaCloud products)
        print(f"   📂 No specific category match, using default: 38208 (Sofas/Furniture)")
        return "38208"  # Sofas, Armchairs & Couches (most common GigaCloud category)

    def _extract_key_features(self, description: str) -> str:
        """
        从描述中提取关键特征（材质、尺寸、颜色、特性）
        """
        if not description:
            return ""
        
        import re
        features = []
        
        # 提取尺寸信息 (数字 + 单位)
        dimensions = re.findall(r'\d+\.?\d*\s*(?:x|×|X)\s*\d+\.?\d*\s*(?:x|×|X)?\s*\d*\.?\d*\s*(?:inch|in|cm|ft|feet)', description, re.IGNORECASE)
        if dimensions:
            features.append(f"Dimensions: {dimensions[0]}")
        
        # 提取材质关键词
        materials = ['wood', 'metal', 'plastic', 'fabric', 'leather', 'glass', 'steel', 'aluminum', 'mdf', 'oak', 'pine']
        found_materials = [m for m in materials if m in description.lower()]
        if found_materials:
            features.append(f"Material: {', '.join(found_materials[:2])}")
        
        # 提取颜色
        colors = ['black', 'white', 'brown', 'gray', 'grey', 'blue', 'red', 'green', 'beige', 'walnut', 'espresso']
        found_colors = [c for c in colors if c in description.lower()]
        if found_colors:
            features.append(f"Color: {', '.join(found_colors[:2])}")
        
        # 提取重量
        weight = re.search(r'(\d+\.?\d*)\s*(lbs?|pounds?|kg)', description, re.IGNORECASE)
        if weight:
            features.append(f"Weight: {weight.group(0)}")
        
        return "; ".join(features) if features else description[:200]
    
    def _extract_core_keywords(self, title: str) -> list:
        """
        提取标题中的核心产品关键词（去除修饰词）
        """
        import re
        
        # 常见修饰词/停用词
        stopwords = {
            'new', 'brand', 'modern', 'contemporary', 'classic', 'vintage', 'premium', 'luxury',
            'best', 'top', 'high', 'quality', 'great', 'perfect', 'ideal', 'beautiful',
            'large', 'small', 'medium', 'big', 'mini', 'compact', 'portable',
            'sale', 'hot', 'free', 'shipping', 'fast', 'quick',
            'with', 'for', 'and', 'the', 'a', 'an', 'in', 'on', 'at'
        }
        
        # 清理并分词
        words = re.findall(r'\b[a-z]+\b', title.lower())
        
        # 过滤停用词和短词
        core_words = [w for w in words if w not in stopwords and len(w) > 2]
        
        # 返回前5个核心词
        return core_words[:5]
    
    def _fetch_ebay_suggestions(self, seed_keyword: str) -> list:
        """
        获取 eBay 搜索下拉框的热搜推荐词 (Real-time Trending)
        """
        if not seed_keyword:
            return []
            
        try:
            import requests
            # eBay Autosuggest API (Public)
            # sId=0 (US), _jgr=1 (JSON format)
            url = "https://autosug.ebaystatic.com/autosug"
            params = {
                "kwd": seed_keyword,
                "sId": "0",  # US Site
                "_jgr": "1"
            }
            
            response = requests.get(url, params=params, timeout=2)
            if response.status_code == 200:
                data = response.json()
                # 格式: {"res": {"sug": ["keyword1", "keyword2", ...]}}
                suggestions = data.get("res", {}).get("sug", [])
                
                # 清理和去重
                clean_sugs = []
                for sug in suggestions:
                    # 移除原词本身，只保留扩展词
                    if sug.lower() != seed_keyword.lower():
                        clean_sugs.append(sug)
                
                return clean_sugs[:6]  # 返回前6个热搜词
        except Exception as e:
            print(f"Warning: Failed to fetch trending keywords: {e}")
        
        return []

    def optimize_title(self, title: str, category: str = "", max_length: int = 80, description: str = "", market_intel: dict = None) -> str:
        """
        优化单个标题 - 增强版 (含热搜词 + Terapeak 市场数据)
        """
        # 1. 提取核心关键词 (作为种子词)
        core_keywords = self._extract_core_keywords(title)
        seed_keyword = " ".join(core_keywords[:2]) if core_keywords else title.split()[0]
        
        # 2. 获取 eBay 热搜词
        trending_suggestions = self._fetch_ebay_suggestions(seed_keyword)
        trending_str = ", ".join(trending_suggestions) if trending_suggestions else "None available"
        
        # 2b. 获取 Terapeak 市场数据（如果未提供）
        if market_intel is None:
            try:
                market_intel = self.fetch_market_intelligence(title, category or None)
            except Exception as e:
                print(f"   ⚠️ Market intelligence fetch failed: {e}")
                market_intel = None
        
        # 构建市场数据段
        market_data_str = ""
        if market_intel:
            top_kw = market_intel.get('top_keywords', [])
            comp_titles = market_intel.get('competitor_titles', [])
            if top_kw:
                market_data_str += f"\n**Top-selling keywords (from Terapeak):** {', '.join(top_kw[:12])}"
            if comp_titles:
                market_data_str += f"\n**Best-selling competitor titles (study their patterns):**"
                for t in comp_titles[:5]:
                    market_data_str += f"\n  - {t}"
        
        # 3. 提取描述特征
        desc_context = ""
        if description:
            key_features = self._extract_key_features(description)
            desc_context = f"\n**Key Product Features:** {key_features}"
        
        # 4. 构建提示词
        core_keywords_str = ", ".join(core_keywords)
        
        prompt = f"""You are an expert eBay SEO title optimizer. Your goal is to create titles that are EXACTLY 75-80 characters.

**Original Title:** {title}
**Core Product:** {seed_keyword}
**Trending Search Terms:** {trending_str} (Try to incorporate valid ones)
**Key Features:**{desc_context}
{market_data_str}

**CRITICAL REQUIREMENTS:**
1. **LENGTH: MUST be between 75-80 characters**
2. **MUST INCLUDE core keywords:** {core_keywords_str}
3. **IMPORTANT: Incorporate high-frequency keywords from Terapeak data above** — these are what real buyers search for
4. Study the competitor titles to understand winning keyword patterns
5. Put HIGH-VALUE SEARCH KEYWORDS first
6. Use Title Case
7. Remove fluff (New, Best, Sale)
8. Make every character count

**Examples:**
- "Modern L-Shaped Computer Desk 55 Inch With Storage Shelves Home Office Brown" (77)
- "Heavy Duty Metal Dog Crate 48 Inch Double Door Wire Kennel Large Breed Black" (78)

Return ONLY the optimized title."""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=200
            )
            
            optimized = response.choices[0].message.content.strip()
            
            # 清理结果
            optimized = optimized.strip('"').strip("'").strip()
            # 移除可能的字符数标注
            optimized = re.sub(r'\s*\(\d+\s*chars?\)$', '', optimized)
            
            # 验证核心关键词是否保留
            missing_keywords = [kw for kw in core_keywords if kw not in optimized.lower()]
            if missing_keywords and len(missing_keywords) <= 2:
                # 尝试添加缺失的核心词
                for kw in missing_keywords:
                    if len(optimized) + len(kw) + 1 <= max_length:
                        optimized = optimized + " " + kw.title()
                        print(f"   [INFO] Added missing core keyword: {kw}")
            
            # 如果太短，尝试添加更多关键词
            if len(optimized) < 70 and len(title) > len(optimized):
                # 从原标题提取可能遗漏的关键词
                original_words = set(title.lower().split())
                optimized_words = set(optimized.lower().split())
                missing = original_words - optimized_words
                
                # 添加有意义的缺失词
                for word in missing:
                    if len(word) > 3 and len(optimized) + len(word) + 1 <= max_length:
                        optimized = optimized + " " + word.title()
            
            # 如果超长，智能截断
            if len(optimized) > max_length:
                words = optimized.split()
                result = ""
                for word in words:
                    if len(result) + len(word) + 1 <= max_length:
                        result = (result + " " + word).strip()
                    else:
                        break
                optimized = result
            
            return optimized if optimized else title[:max_length]
            
        except Exception as e:
            print(f"Title optimization error: {e}")
            # Fallback: 简单清理
            cleaned = re.sub(r'[^\w\s\-]', '', title)
            return cleaned[:max_length].title()


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
