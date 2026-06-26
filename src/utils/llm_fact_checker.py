import os
import json
from typing import List, Dict, Any
from openai import OpenAI

def llm_fact_check(
    source_title: str,
    source_description: str,
    source_specs: dict,
    generated_title: str,
    generated_description: str
) -> List[Dict[str, Any]]:
    """
    LLM Fact-Checker Layer 3.
    Returns a list of violations, e.g., [{"quote": "...", "reason": "...", "severity": "HIGH"}]
    """
    api_key = os.getenv("QWEN_API_KEY")
    if not api_key:
        return []

    try:
        client = OpenAI(
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            timeout=15.0,
            max_retries=1,
        )
    except Exception as e:
        print(f"[LLM FactChecker] Failed to initialize client: {e}")
        return []

    # Format source facts
    source_text = f"SOURCE TITLE:\n{source_title}\n\n"
    source_text += f"SOURCE DESCRIPTION:\n{source_description}\n\n"
    source_text += f"SOURCE SPECS:\n{json.dumps(source_specs, ensure_ascii=False, indent=2)}\n"

    # Format generated content
    generated_text = f"GENERATED TITLE:\n{generated_title}\n\n"
    generated_text += f"GENERATED DESCRIPTION:\n{generated_description}\n"

    prompt = f"""You are a strict, emotionless Fact-Checker. 
Your task is to compare the GENERATED COPY against the SOURCE FACTS.
Identify any claims in the GENERATED COPY that are NOT supported by the SOURCE FACTS.

Focus exclusively on subjective over-extrapolations, medical/health claims, fake certifications, fake celebrity endorsements, or extreme exaggerations.
Do not complain about stylistic choices, formatting, or missing specs unless they constitute a false claim.

CRITICAL RULES:
1. IGNORE any physical dimensions (e.g., length, width, height, weight, inches, cm). These may have been manually extracted from product images and are NOT hallucinations even if missing from SOURCE FACTS.
2. IGNORE any claims about "supplier dimension images" or "product size images".
3. ONLY flag severe subjective claims (e.g. cures pain, NASA certified).

SOURCE FACTS:
{source_text}

GENERATED COPY:
{generated_text}

Analyze the GENERATED COPY line by line. If a claim is unsupported, extract the exact quote, explain the reason, and assign a severity (HIGH or MEDIUM).
- HIGH: Medical claims (e.g. cures pain), fake certifications (e.g. NASA, FDA, Michelin), or severe functional fabrications (e.g. fireproof when it's just wood).
- MEDIUM: Mild exaggerations (e.g. "world's best", "indestructible").

Return ONLY a JSON array of objects. Example format:
[
  {{
    "quote": "Ergonomic design cures neck pain",
    "reason": "Source facts do not mention curing any medical conditions.",
    "severity": "HIGH"
  }}
]
If there are no violations, return an empty array []. Do NOT wrap the JSON in Markdown code blocks like ```json.
"""

    try:
        response = client.chat.completions.create(
            model="qwen-plus",
            messages=[
                {"role": "system", "content": "You are a strict JSON-outputting Fact-Checker."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=1500
        )
        
        content = response.choices[0].message.content.strip()
        
        # Cleanup potential markdown wrap just in case
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        if not content:
            return []

        violations = json.loads(content)
        if isinstance(violations, list):
            # Python-level Post-filtering for dimension false positives
            filtered = []
            for v in violations:
                quote = v.get("quote", "").lower()
                reason = v.get("reason", "").lower()
                # If the quote or reason complains about dimensions, skip it
                filter_keywords = [
                    "dimension", "inch", " cm", "length", "width", "height", "depth", 
                    "尺寸", "英寸", "厘米", "长", "宽", "高", "深", "size image", "seat-depth"
                ]
                if any(kw in quote or kw in reason for kw in filter_keywords):
                    continue
                filtered.append(v)
            return filtered
        return []
    except Exception as e:
        print(f"[LLM FactChecker] API error or JSON parse error: {e}")
        return []
