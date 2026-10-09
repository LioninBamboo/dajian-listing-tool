# 智能HTML描述截断系统

## 问题
用户反馈："不希望简单粗暴地截断描述到50,000字符，这样会导致内容和结构不通畅"

## 解决方案
实现了智能HTML截断器 ([src/utils/html_truncator.py](../src/utils/html_truncator.py))

### 核心特性

#### 1. **HTML结构完整性** ✅
- 使用 `HTMLParser` 解析HTML结构
- 跟踪所有打开的标签（`tag_stack`）
- 自动闭合所有未闭合的标签
- 预留空间确保标签能正确闭合

#### 2. **智能截断点选择** ✅
- 优先在段落结束（`</p>`, `</div>`, `</li>`）处截断
- 尝试在句子结束（句号、问号、感叹号）处截断
- 如果无标点，在空格处截断
- 避免在单词中间或标签内截断

#### 3. **空间预留机制** ✅
```python
reserved_space = sum(len(f'</{tag}>') for tag in self.tag_stack)
available = max_length - current_length - reserved_space - 500
```
- 计算闭合所有打开标签所需空间
- 额外预留500字符用于截断提示
- 确保不会因空间不足而无法闭合标签

#### 4. **截断提示** ✅
```html
<div style="margin-top: 20px; padding: 15px; background-color: #f8f9fa;">
    <p style="margin: 0;">
        <strong>📋 Complete Details:</strong> This listing contains extensive 
        product specifications. For full details, please contact us.
    </p>
</div>
```

### 与刊登质量门的关系

`smart_truncate_html()` 只负责在 eBay 字符限制内保持 HTML 结构完整，不负责判断业务数据是否正确。刊登链接的数据正确性由 [src/utils/listing_quality_gate.py](../src/utils/listing_quality_gate.py) 处理：

- 尺寸、重量、类目和 item specifics 先经过质量门标准化。
- description 中的尺寸说明必须与 item specifics 使用同一份标准化测量结果。
- 对复杂多边形或缺少可靠尺寸图的产品，质量门应改成提示客户参考产品尺寸图，而不是生成占位数值。
- 图片数量、非家具 specifics、乱码文本等发布 blocker 也在质量门阶段处理。

发布链路中的顺序应是：先运行质量门，得到可信 description；如果 description 超过 eBay 限制，再调用 HTML 截断器保留结构。

### 使用位置

已更新所有发布流程：
1. ✅ [batch_publish.py](../batch_publish.py) - 批量发布脚本
2. ✅ [app.py](../app.py) - Streamlit UI发布
3. ✅ [real_ebay_client.py](../src/clients/real_ebay_client.py) - inventory / offer 侧发布能力

### 测试结果

**标签平衡测试**：
- ✅ 所有HTML标签正确闭合
- ✅ 复杂嵌套结构正确处理
- ✅ 60KB → 50KB 截断，保持完整性

**实际产品测试**：
- 当前所有产品描述 < 5KB
- 无需截断，系统待命

### API使用

```python
from src.utils.html_truncator import smart_truncate_html

# 基本使用
truncated = smart_truncate_html(
    html_content, 
    max_length=50000,   # 最大长度
    min_length=45000    # 最小阈值（提前检查）
)

# 返回值
truncated_html: str  # 截断后的有效HTML
```

### 技术优势

| 对比项 | 简单截断 [:50000] | 智能截断器 |
|--------|------------------|-----------|
| HTML有效性 | ❌ 可能破损 | ✅ 保证有效 |
| 标签闭合 | ❌ 未处理 | ✅ 自动闭合 |
| 截断位置 | ❌ 任意位置 | ✅ 自然边界 |
| 用户体验 | ❌ 内容断裂 | ✅ 流畅完整 |
| 截断提示 | ❌ 无 | ✅ 友好提示 |

## 维护

如需调整截断逻辑，编辑 [src/utils/html_truncator.py](../src/utils/html_truncator.py)：
- `handle_data()` - 文本截断策略
- `_finalize_truncation()` - 标签闭合逻辑
- `truncation_notice` - 提示文案

---
📅 实现日期：2026-02-11  
✅ 状态：生产就绪
