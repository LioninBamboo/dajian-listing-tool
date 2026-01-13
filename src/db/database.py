"""数据库操作封装"""
import os
import json
from datetime import datetime
from typing import Any
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from .models import Base, Product, SyncLog, Config


class Database:
    """数据库操作类"""
    
    def __init__(self, db_path: str = "data/ebay_sync.db"):
        """
        初始化数据库连接
        
        Args:
            db_path: SQLite 数据库文件路径
        """
        # 确保目录存在
        if not os.path.isabs(db_path):
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            db_path = os.path.join(base_dir, db_path)

        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
        self.db_path = db_path
        print(f"DEBUG: Database Path: {self.db_path}") 
        self.engine = create_engine(f"sqlite:///{db_path}", echo=False)
        self.SessionLocal = sessionmaker(bind=self.engine)
        
        # 创建表
        Base.metadata.create_all(self.engine)
        
        # 初始化配置
        self._init_config()
    
    def _init_config(self):
        """初始化默认配置"""
        with self.SessionLocal() as session:
            existing = session.query(Config).first()
            if not existing:
                defaults = [
                    Config(key='last_sync_time', value='2025-01-01T00:00:00Z'),
                    Config(key='sync_interval_hours', value='24'),
                    Config(key='batch_size', value='50')
                ]
                session.add_all(defaults)
                session.commit()
    
    def save_product(self, product_data: dict[str, Any]) -> int:
        """
        保存/更新产品
        
        Args:
            product_data: 产品数据字典
            
        Returns:
            产品 ID
        """
        with self.SessionLocal() as session:
            existing = session.query(Product).filter_by(sku=product_data['sku']).first()
            
            # 处理图片 URL（转为 JSON 字符串）
            if 'images' in product_data:
                product_data['image_urls'] = json.dumps(product_data['images'])
                del product_data['images']
            
            if existing:
                # 更新
                for key, value in product_data.items():
                    if hasattr(existing, key):
                        setattr(existing, key, value)
                existing.updated_at = datetime.utcnow()
                session.commit()
                return existing.id
            else:
                # 创建
                product = Product(**product_data)
                session.add(product)
                session.commit()
                return product.id
    
    def get_product_by_sku(self, sku: str) -> dict | None:
        """
        根据 SKU 查询产品
        
        Args:
            sku: 产品 SKU
            
        Returns:
            产品字典或 None
        """
        with self.SessionLocal() as session:
            product = session.query(Product).filter_by(sku=sku).first()
            if product:
                data = product.to_dict()
                # 解析图片 URL
                if data['image_urls']:
                    try:
                        data['images'] = json.loads(data['image_urls'])
                    except:
                        data['images'] = []
                return data
            return None
    
    def get_pending_products(self, limit: int = 50) -> list[dict]:
        """
        获取待处理产品
        
        Args:
            limit: 最大数量
            
        Returns:
            产品列表
        """
        with self.SessionLocal() as session:
            products = session.query(Product)\
                .filter_by(sync_status='pending')\
                .limit(limit)\
                .all()
            
            results = []
            for p in products:
                data = p.to_dict()
                if data['image_urls']:
                    try:
                        data['images'] = json.loads(data['image_urls'])
                    except:
                        data['images'] = []
                results.append(data)
            return results
    
    def get_products_by_status(self, status: str, limit: int = 100) -> list[dict]:
        """
        根据状态查询产品
        
        Args:
            status: 同步状态
            limit: 最大数量
            
        Returns:
            产品列表
        """
        with self.SessionLocal() as session:
            products = session.query(Product)\
                .filter_by(sync_status=status)\
                .limit(limit)\
                .all()
            
            results = []
            for p in products:
                data = p.to_dict()
                if data['image_urls']:
                    try:
                        data['images'] = json.loads(data['image_urls'])
                    except:
                        data['images'] = []
                results.append(data)
            return results
    
    def update_sync_status(
        self,
        sku: str,
        status: str,
        error: str | None = None,
        **kwargs
    ):
        """
        更新同步状态
        
        Args:
            sku: 产品 SKU
            status: 新状态
            error: 错误信息
            **kwargs: 其他更新字段（如 ebay_item_id）
        """
        with self.SessionLocal() as session:
            product = session.query(Product).filter_by(sku=sku).first()
            if product:
                product.sync_status = status
                product.last_synced_at = datetime.utcnow()
                if error:
                    product.error_message = error
                
                # 更新其他字段
                for key, value in kwargs.items():
                    if hasattr(product, key):
                        setattr(product, key, value)
                
                session.commit()
    
    def log_sync(
        self,
        sku: str,
        action: str,
        status: str,
        message: str,
        execution_time_ms: int = 0
    ):
        """
        记录同步日志
        
        Args:
            sku: 产品 SKU
            action: 操作类型
            status: 状态
            message: 消息
            execution_time_ms: 执行时间（毫秒）
        """
        with self.SessionLocal() as session:
            log = SyncLog(
                sku=sku,
                action=action,
                status=status,
                message=message,
                execution_time_ms=execution_time_ms
            )
            session.add(log)
            session.commit()
    
    def get_config(self, key: str) -> str | None:
        """获取配置值"""
        with self.SessionLocal() as session:
            config = session.query(Config).filter_by(key=key).first()
            return config.value if config else None
    
    def set_config(self, key: str, value: str):
        """设置配置值"""
        with self.SessionLocal() as session:
            config = session.query(Config).filter_by(key=key).first()
            if config:
                config.value = value
                config.updated_at = datetime.utcnow()
            else:
                config = Config(key=key, value=value)
                session.add(config)
            session.commit()
