"""数据库模型定义 - 采集系统专用"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, Integer, String, Text, Float, DateTime, JSON, event
from sqlalchemy.orm import declarative_base
from sqlalchemy.types import TypeDecorator

from src.utils.logs_codec import dump_logs, parse_logs

Base = declarative_base()
logger = logging.getLogger(__name__)


class CorruptLogs(list):
    """List subclass returned when collected_products.logs is not valid JSON."""

    parse_status: str
    trailing: str
    error: str | None
    leading_entries: list[Any]

    def __init__(
        self,
        items: list[Any],
        *,
        parse_status: str,
        trailing: str = "",
        error: str | None = None,
        leading_entries: list[Any] | None = None,
    ) -> None:
        super().__init__(items)
        self.parse_status = parse_status
        self.trailing = trailing
        self.error = error
        self.leading_entries = list(leading_entries if leading_entries is not None else items)


class TolerantJSONList(TypeDecorator):
    """TEXT-backed JSON list that never raises on a corrupt collected_products.logs cell."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, list):
            raise TypeError(
                f"CollectedProduct.logs must be a list or None, got {type(value).__name__}"
            )
        return dump_logs(list(value))

    def process_result_value(self, value: Any, dialect: Any) -> list[Any] | None:
        parsed = parse_logs(value)
        if parsed.status in {"ok", "empty", "non_list"}:
            return parsed.entries
        return CorruptLogs(
            parsed.entries_with_trailing(),
            parse_status=parsed.status,
            trailing=parsed.trailing,
            error=parsed.error,
            leading_entries=parsed.entries,
        )


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class CategoryValidityCache(Base):
    """Taxonomy leaf checks. 7-day TTL; rows die when categoryTreeVersion changes."""
    __tablename__ = "category_validity_cache"

    tree_id = Column(String(16), primary_key=True)
    category_id = Column(String(32), primary_key=True)
    valid = Column(Integer, nullable=False)
    leaf = Column(Integer, nullable=False)
    name = Column(String(255))
    tree_version = Column(String(64))
    checked_at = Column(DateTime, nullable=False)


class CollectedProduct(Base):
    """采集产品表 - 用于浏览器扩展采集的产品"""
    __tablename__ = 'collected_products'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    sku = Column(String(100), unique=True, nullable=False, index=True)
    
    # 基础信息
    title = Column(String(500), nullable=False)
    price = Column(Float, nullable=False)  # 大建价格
    shipping = Column(Float, default=0.0)  # 运费
    stock = Column(Integer, default=99)
    url = Column(Text)  # 大建产品页URL
    
    # 媒体资源 (JSON数组)
    images = Column(JSON)  # ["url1", "url2", ...]
    videos = Column(JSON)  # ["url1", "url2", ...]
    
    # 产品详情
    description = Column(Text)  # HTML描述(包含Features和Specs)
    attributes = Column(JSON)  # {"Variant": "Gray", "Material": "MDF"}
    specs = Column(JSON)  # {"Weight": "85 lbs", "Dimensions": "71x23x34"}
    
    # 成本和定价 (JSON对象)
    cost_breakdown = Column(JSON)  # {"base": 101, "shipping": 45.73, "insurance": 2.93, ...}
    suggested_price = Column(Float)  # AI建议售价
    
    # AI优化结果 (JSON对象)
    optimization = Column(JSON)  # {"title": "...", "description": "...", "aspects": {...}}
    
    # 状态管理
    status = Column(String(20), default='PENDING', index=True)  # PENDING, READY, PUBLISHED, ERROR
    listing_id = Column(String(50))  # eBay listing ID
    
    # 日志 — tolerant reader; still TEXT in SQLite, no schema migration
    logs = Column(TolerantJSONList)
    
    # 时间戳
    created_at = Column(DateTime, default=_utcnow_naive)
    updated_at = Column(DateTime, default=_utcnow_naive, onupdate=_utcnow_naive)
    published_at = Column(DateTime)  # 上架时间
    
    def to_dict(self) -> dict:
        """序列化为字典"""
        return {
            'id': self.id,
            'sku': self.sku,
            'title': self.title,
            'price': self.price,
            'shipping': self.shipping,
            'stock': self.stock,
            'url': self.url,
            'images': self.images or [],
            'videos': self.videos or [],
            'description': self.description or '',
            'attributes': self.attributes or {},
            'specs': self.specs or {},
            'cost_breakdown': self.cost_breakdown or {},
            'suggested_price': self.suggested_price,
            'optimization': self.optimization or {},
            'status': self.status,
            'listing_id': self.listing_id,
            'logs': self.logs or [],
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'published_at': self.published_at.isoformat() if self.published_at else None
        }


@event.listens_for(CollectedProduct, "load")
def _warn_corrupt_collected_logs(target: CollectedProduct, _context: Any) -> None:
    logs = target.logs
    if not isinstance(logs, CorruptLogs):
        return
    logger.warning(
        "corrupt logs sku=%s status=%s leading_items=%s trailing_len=%s",
        target.sku or "?",
        logs.parse_status,
        len(logs.leading_entries),
        len(logs.trailing or ""),
    )
