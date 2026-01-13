"""数据库模型定义"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DECIMAL, DateTime, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

Base = declarative_base()


class Product(Base):
    """产品主表"""
    __tablename__ = 'products'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    sku = Column(String(50), unique=True, nullable=False, index=True)
    ebay_item_id = Column(String(50))
    ebay_offer_id = Column(String(50))
    
    # 原始数据
    dajian_title = Column(Text)
    dajian_description = Column(Text)
    dajian_price = Column(DECIMAL(10, 2))
    dajian_stock = Column(Integer)
    dajian_category = Column(String(100))
    
    # 优化后数据
    optimized_title = Column(String(80))
    optimized_description = Column(Text)
    
    # 图片（JSON 字符串）
    image_urls = Column(Text)
    
    # 状态追踪
    sync_status = Column(String(20), index=True, default='pending')  # pending, optimized, listed, failed
    last_synced_at = Column(DateTime)
    error_message = Column(Text)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Traffic Optimization
    last_refreshed_at = Column(DateTime) # Last time we did a "Cycle" (End & Sell Similar)
    listing_created_at = Column(DateTime) # When the current ItemID was created on eBay
    
    def to_dict(self) -> dict:
        """序列化为字典"""
        return {
            'id': self.id,
            'sku': self.sku,
            'ebay_item_id': self.ebay_item_id,
            'ebay_offer_id': self.ebay_offer_id,
            'dajian_title': self.dajian_title,
            'dajian_description': self.dajian_description,
            'dajian_price': float(self.dajian_price) if self.dajian_price else None,
            'dajian_stock': self.dajian_stock,
            'dajian_category': self.dajian_category,
            'optimized_title': self.optimized_title,
            'optimized_description': self.optimized_description,
            'image_urls': self.image_urls,
            'sync_status': self.sync_status,
            'last_synced_at': self.last_synced_at.isoformat() if self.last_synced_at else None,
            'last_refreshed_at': self.last_refreshed_at.isoformat() if self.last_refreshed_at else None,
            'listing_created_at': self.listing_created_at.isoformat() if self.listing_created_at else None,
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class SyncLog(Base):
    """同步日志表"""
    __tablename__ = 'sync_logs'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    sku = Column(String(50))
    action = Column(String(20))  # fetch, optimize, list, update
    status = Column(String(20))  # success, failed
    message = Column(Text)
    execution_time_ms = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)


class Config(Base):
    """配置表"""
    __tablename__ = 'config'
    
    key = Column(String(50), primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
