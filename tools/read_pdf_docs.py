"""读取大建API PDF文档 - 使用 pdfplumber"""
import pdfplumber
import os

pdf_files = [
    r'C:\Users\poonx\Downloads\Open_API_2.0开发者文档.pdf',
    r'C:\Users\poonx\Downloads\产品价格查询.pdf',
    r'C:\Users\poonx\Downloads\产品详情查询.pdf',
    r'C:\Users\poonx\Downloads\产品列表查询.pdf',
]

output = []

for pdf_path in pdf_files:
    if os.path.exists(pdf_path):
        output.append("=" * 70)
        output.append(f"文件: {os.path.basename(pdf_path)}")
        output.append("=" * 70)
        
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for i, page in enumerate(pdf.pages[:5]):
                    text = page.extract_text()
                    if text:
                        output.append(f"\n--- 第 {i + 1} 页 ---")
                        output.append(text)
                    
                    # 提取表格
                    tables = page.extract_tables()
                    for j, table in enumerate(tables):
                        output.append(f"\n[表格 {j + 1}]")
                        for row in table:
                            output.append(" | ".join([str(cell or '') for cell in row]))
        except Exception as e:
            output.append(f"读取失败: {e}")
    else:
        output.append(f"文件不存在: {pdf_path}")

# 保存
with open("api_doc_content.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(output))

print("已保存到 api_doc_content.txt")
print(f"总行数: {len(output)}")

# 输出前200行预览
for line in output[:200]:
    print(line)
