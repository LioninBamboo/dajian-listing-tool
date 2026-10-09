"""S101 — VoC 评论主题建模 (纯 Python).

输入: reviews=[str]; 输出: topics=[{keywords, doc_count, sample_indices}].
策略: 分词→停用词→TF-IDF→共现聚类 (简化为 top-N 关键词分桶).
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List

STOPWORDS = {
    'the', 'a', 'an', 'is', 'it', 'this', 'that', 'and', 'or', 'but',
    'i', 'you', 'we', 'they', 'he', 'she', 'my', 'your', 'our', 'their',
    'in', 'on', 'at', 'to', 'of', 'for', 'with', 'by', 'as', 'so',
    'be', 'been', 'was', 'were', 'are', 'am', 'do', 'did', 'does',
    'have', 'has', 'had', 'will', 'would', 'can', 'could', 'should',
    'not', 'no', 'yes', 'too', 'very', 'just', 'all', 'any', 'some',
    '的', '了', '是', '在', '我', '你', '他', '她', '它', '们', '和',
    '都', '也', '就', '又', '还', '一', '个', '这', '那', '有', '没',
}

TOKEN_RE = re.compile(r'[A-Za-z]{3,}|[\u4e00-\u9fa5]+')


def tokenize(text: str) -> List[str]:
    if not text:
        return []
    tokens = TOKEN_RE.findall(text.lower())
    return [t for t in tokens if t not in STOPWORDS]


def compute_tfidf(docs: List[List[str]]) -> List[Dict[str, float]]:
    n = len(docs)
    if n == 0:
        return []
    df: Dict[str, int] = defaultdict(int)
    for d in docs:
        for w in set(d):
            df[w] += 1
    out = []
    for d in docs:
        tf = Counter(d)
        scores: Dict[str, float] = {}
        for w, c in tf.items():
            idf = math.log((n + 1) / (df[w] + 1)) + 1
            scores[w] = c * idf
        out.append(scores)
    return out


def extract_topics(reviews: Iterable[str],
                   *,
                   top_k_words: int = 30,
                   min_topic_size: int = 2,
                   max_topics: int = 8,
                   ) -> Dict[str, Any]:
    docs = [tokenize(r) for r in reviews]
    docs = [d for d in docs if d]
    if not docs:
        return {'topics': [], 'doc_count': 0}

    tfidf = compute_tfidf(docs)

    # 全局 TF-IDF 累计 → 取 top_k_words 作为关键词种子
    global_score: Dict[str, float] = defaultdict(float)
    for s in tfidf:
        for w, v in s.items():
            global_score[w] += v
    seed_words = [w for w, _ in sorted(
        global_score.items(), key=lambda x: -x[1])[:top_k_words]]

    # 桶: 一个 doc 加入它含有的每个种子词的桶 (multi-membership)
    buckets: Dict[str, List[int]] = defaultdict(list)
    for idx, d in enumerate(docs):
        for w in set(d):
            if w in seed_words:
                buckets[w].append(idx)

    topics = []
    for word, doc_idxs in sorted(buckets.items(),
                                 key=lambda x: -len(x[1]))[:max_topics]:
        if len(doc_idxs) < min_topic_size:
            continue
        # 关键词扩展: 这些 doc 里高频且非种子的词
        co_words: Counter = Counter()
        for i in doc_idxs:
            for w in docs[i]:
                if w != word:
                    co_words[w] += 1
        keywords = [word] + [w for w, _ in co_words.most_common(4)]
        topics.append({
            'primary_keyword': word,
            'keywords': keywords,
            'doc_count': len(doc_idxs),
            'sample_indices': doc_idxs[:5],
        })

    return {'topics': topics, 'doc_count': len(docs)}


def summarise_topics(result: Dict[str, Any]) -> str:
    topics = result.get('topics', [])
    if not topics:
        return '无主题 (评论为空或全是停用词)'
    parts = [f"{t['primary_keyword']}({t['doc_count']})" for t in topics]
    return ' / '.join(parts)
