"""数据库模型定义 - 采集系统专用"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Float, DateTime, JSON
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


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
    
    # 日志
    logs = Column(JSON)  # ["Received from extension", "AI optimization complete", ...]
    
    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
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
