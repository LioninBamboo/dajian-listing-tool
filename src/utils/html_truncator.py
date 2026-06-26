"""
智能HTML描述截断器
避免简单粗暴截断导致HTML结构破损和内容不通畅
"""
from html.parser import HTMLParser
from typing import List, Tuple


class SmartHTMLTruncator(HTMLParser):
    """智能HTML截断解析器"""
    
    def __init__(self, max_length: int = 50000):
        super().__init__()
        self.max_length = max_length
        self.current_length = 0
        self.result = []
        self.tag_stack: List[str] = []  # 跟踪打开的标签
        self.truncated = False
        self.section_boundaries = []  # 记录段落/列表等边界位置
        self.in_important_section = False  # 是否在重要部分（如h1, ul等）
        self.last_clean_result = []
        self.last_clean_stack = []
        
    def handle_starttag(self, tag, attrs):
        """处理开始标签"""
        if self.truncated:
            return
            
        # 检查是否是重要内容标签
        if tag in ['h1', 'h2', 'h3', 'ul', 'ol', 'table']:
            self.in_important_section = True
            
        # 构建标签HTML
        attrs_str = ''.join([f' {k}="{v}"' for k, v in attrs])
        tag_html = f'<{tag}{attrs_str}>'
        
        if self.current_length + len(tag_html) > self.max_length:
            self._finalize_truncation()
            return
            
        self.result.append(tag_html)
        self.current_length += len(tag_html)
        
        # 只有非自闭合标签才入栈
        if tag not in ['br', 'hr', 'img', 'meta', 'link']:
            self.tag_stack.append(tag)
    
    def handle_endtag(self, tag):
        """处理结束标签"""
        if self.truncated:
            return
            
        end_tag = f'</{tag}>'
        
        if self.current_length + len(end_tag) > self.max_length:
            self._finalize_truncation()
            return
        
        self.result.append(end_tag)
        self.current_length += len(end_tag)
        
        # 从栈中移除（倒序查找，因为可能有嵌套的同名标签）
        if tag in self.tag_stack:
            # 找到最后一个匹配的标签并移除
            for i in range(len(self.tag_stack) - 1, -1, -1):
                if self.tag_stack[i] == tag:
                    self.tag_stack.pop(i)
                    break
        
        # 段落/列表结束是好的截断点
        if tag in ['p', 'li', 'div', 'tr', 'h1', 'h2', 'h3', 'ul', 'ol', 'table']:
            self.section_boundaries.append(self.current_length)
            self.last_clean_result = list(self.result)
            self.last_clean_stack = list(self.tag_stack)
            if tag in ['h1', 'h2', 'h3', 'ul', 'ol', 'table']:
                self.in_important_section = False
    
    def handle_data(self, data):
        """处理文本内容"""
        if self.truncated:
            return
        
        # 计算需要预留的空间来闭合当前所有打开的标签
        reserved_space = sum(len(f'</{tag}>') for tag in self.tag_stack)
        notice_margin = 120 if self.max_length < 10000 else 500
        available = self.max_length - self.current_length - reserved_space - notice_margin
        
        if available <= 0:
            self._finalize_truncation()
            return
        
        # 智能截断文本：尽量在句子结束或空格处
        if len(data) > available:
            # 尝试在句号、问号、感叹号后截断
            for punct in ['. ', '! ', '? ', '。', '！', '？']:
                last_punct = data.rfind(punct, 0, available)
                if last_punct > available * 0.7:  # 至少用70%的可用空间
                    data = data[:last_punct + len(punct)]
                    break
            else:
                # 如果没有标点，在空格处截断
                last_space = data.rfind(' ', 0, available)
                if last_space > available * 0.7:
                    data = data[:last_space]
                else:
                    data = data[:available]
            
            self.result.append(data)
            self.current_length += len(data)
            self._finalize_truncation()
            return
        
        self.result.append(data)
        self.current_length += len(data)
    
    def _finalize_truncation(self):
        """完成截断：闭合所有打开的标签并添加提示"""
        if self.truncated:
            return
        
        self.truncated = True
        
        # 回滚到上一个完整的 HTML 边界以防截断脏字或破损表格行
        if self.last_clean_result:
            self.result = list(self.last_clean_result)
            self.tag_stack = list(self.last_clean_stack)
        
        # 闭合所有打开的标签（逆序）- 不再检查长度，因为我们已经预留了空间
        tags_to_close = list(reversed(self.tag_stack))
        for tag in tags_to_close:
            end_tag = f'</{tag}>'
            self.result.append(end_tag)
            self.current_length += len(end_tag)
        
        # 清空栈
        self.tag_stack.clear()
        
        # 添加截断提示（在所有标签闭合后）
        if self.max_length < 10000:
            truncation_notice = '<div style="margin-top:15px;padding:10px;background:#f8f9fa;border-left:4px solid #007bff;font-size:12px;color:#495057;"><strong>📋 Complete Details:</strong> Specs available upon request.</div>'
        else:
            truncation_notice = '''
<div style="margin-top: 20px; padding: 15px; background-color: #f8f9fa; border-left: 4px solid #007bff;">
    <p style="margin: 0; color: #495057; font-size: 14px;">
        <strong>📋 Complete Details:</strong> This listing contains extensive product specifications. 
        For full details including dimensions, materials, and care instructions, please contact us.
    </p>
</div>
'''
        
        # 如果有空间，添加提示
        if self.current_length + len(truncation_notice) < self.max_length:
            self.result.append(truncation_notice)
            self.current_length += len(truncation_notice)
    
    def get_result(self) -> Tuple[str, bool]:
        """
        返回截断后的HTML和是否发生了截断
        Returns:
            (truncated_html, was_truncated)
        """
        # 如果没有被截断但还有未闭合的标签，闭合它们
        if not self.truncated:
            while self.tag_stack:
                tag = self.tag_stack.pop()
                self.result.append(f'</{tag}>')
        
        return ''.join(self.result), self.truncated


def smart_truncate_html(html: str, max_length: int = 50000, min_length: int = 45000) -> str:
    """
    智能截断HTML描述
    
    Args:
        html: 原始HTML内容
        max_length: 最大长度（字符数）
        min_length: 最小长度阈值，如果接近此值则尝试在合适位置截断
    
    Returns:
        截断后的有效HTML
    """
    # 如果本身就不长，直接返回
    if len(html) <= max_length:
        return html
    
    # 使用智能截断器
    truncator = SmartHTMLTruncator(max_length=max_length)
    
    try:
        truncator.feed(html)
        result, was_truncated = truncator.get_result()
        
        if was_truncated:
            # 记录日志
            print(f"[Smart Truncator] HTML truncated from {len(html)} to {len(result)} chars")
        
        return result
    
    except Exception as e:
        # 如果解析失败，回退到简单截断（但至少保证在标签外）
        print(f"[Smart Truncator] Parse error: {e}, using fallback truncation")
        
        # 简单的回退策略：找到min_length之后的第一个完整标签结束
        truncate_at = min_length
        while truncate_at < max_length and truncate_at < len(html):
            if html[truncate_at] == '>' and html[truncate_at-1] != '/':
                # 找到一个标签结束，检查是否是结束标签
                if truncate_at > 2 and html[truncate_at-2:truncate_at+1] in ['</p>', '</div>', '</li>', '</tr>']:
                    break
            truncate_at += 1
        
        truncated = html[:truncate_at]
        
        # 尝试闭合常见的未闭合标签
        open_tags = []
        i = 0
        while i < len(truncated):
            if truncated[i] == '<':
                tag_end = truncated.find('>', i)
                if tag_end > 0:
                    tag_content = truncated[i+1:tag_end]
                    if tag_content.startswith('/'):
                        # 结束标签
                        tag_name = tag_content[1:].split()[0]
                        if tag_name in open_tags:
                            open_tags.remove(tag_name)
                    elif not tag_content.endswith('/') and tag_content.split()[0] not in ['br', 'hr', 'img']:
                        # 开始标签
                        tag_name = tag_content.split()[0]
                        open_tags.append(tag_name)
                    i = tag_end + 1
            else:
                i += 1
        
        # 闭合剩余的打开标签
        for tag in reversed(open_tags):
            truncated += f'</{tag}>'
        
        return truncated


def estimate_html_text_length(html: str) -> int:
    """
    估算HTML的实际文本长度（去除标签）
    用于判断内容是否真的过长
    """
    class TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.text_parts = []
        
        def handle_data(self, data):
            self.text_parts.append(data.strip())
        
        def get_text(self):
            return ' '.join(self.text_parts)
    
    extractor = TextExtractor()
    try:
        extractor.feed(html)
        return len(extractor.get_text())
    except:
        # 如果解析失败，返回原始长度
        return len(html)
